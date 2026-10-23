"""Saga-continuation callbacks for approved HITL actions.

When an operator approves a request, the HITL service calls the appropriate
downstream service to resume the deal flow.  The ``callback_payload`` stored
with the approval record is the full request body for the target service,
exactly as the caller submitted it when creating the approval.

On REJECT:
  - Hard-reject actions emit a ``deal.exception`` event so the Exception
    Resolver (or a human operator) can decide the next step.
  - Soft-reject actions (e.g. SEND_NEGOTIATION_EMAIL) call ``execute_reject_callback``
    instead, which updates the draft status without raising a deal exception.

Each approve callback is:
  async def _cb_<action>(deal_id, payload) -> dict | None
  Returns a dict summary (logged) or None if nothing to report.

Each reject callback follows the same signature.

The dispatchers ``execute_callback`` / ``execute_reject_callback`` map
ApprovalAction values to handlers.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Approve handlers
# ---------------------------------------------------------------------------

async def _cb_book_freight(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: book freight container after HITL approval."""
    from services.shield_link.service import book_freight  # type: ignore[import]
    from services.shield_link.schemas import FreightBookingRequest  # type: ignore[import]
    result = await book_freight(FreightBookingRequest(**payload))
    return {"tpo_id": result.tpo_id, "status": result.status}


async def _cb_issue_po(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: issue Purchase Order after HITL approval."""
    from services.dealmaker.service import issue_po  # type: ignore[import]
    from services.dealmaker.schemas import IssuePoRequest  # type: ignore[import]
    result = await issue_po(IssuePoRequest(**payload))
    return {"po_id": result.po_id, "status": result.status}


async def _cb_release_supplier_payout(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: release supplier payout after HITL approval.

    The payload may carry ``kind`` (``supplier_payout`` / ``supplier_balance``)
    and ``total_owed`` (PO value) — Escrow-Guard caps the amount at the funds
    released from escrow and only marks the deal PAID once the supplier
    received everything they are owed."""
    from services.escrow_guard.service import request_payout  # type: ignore[import]
    from services.escrow_guard.schemas import PayoutRequest  # type: ignore[import]
    allowed = {"deal_id", "payee_id", "amount", "currency", "description", "idempotency_key",
               "kind", "total_owed"}
    result = await request_payout(PayoutRequest(**{k: v for k, v in payload.items() if k in allowed}))
    return {"payout_id": result.payout_id, "status": result.status, "kind": result.kind}


async def _cb_file_customs(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: file customs (CHA) after HITL approval."""
    from services.shield_link.service import file_cha  # type: ignore[import]
    from services.shield_link.schemas import ChaFilingRequest  # type: ignore[import]
    result = await file_cha(ChaFilingRequest(**payload))
    return {"tpo_id": result.tpo_id, "status": result.status}


async def _cb_resolve_exception(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: resolve a deal exception after HITL approval."""
    logger.info("HITL: RESOLVE_EXCEPTION approved for deal %s — payload: %s", deal_id, payload)
    return {"action": "RESOLVE_EXCEPTION", "deal_id": deal_id}


async def _cb_send_negotiation_email(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Send the operator-approved negotiation email draft.

    If the operator supplied a ``modified_body`` in the resolve request it is
    forwarded here via the payload and passed to send_approved_email so the
    final email uses the operator's version instead of the LLM draft.
    """
    from agents.negotiation_agent.service import send_approved_email  # type: ignore[import]

    email_id: str = payload["email_id"]
    modified_body: str | None = payload.get("modified_body")
    await send_approved_email(email_id, modified_body)
    status = "modified_sent" if modified_body else "sent"
    return {"email_id": email_id, "status": status}


async def _cb_confirm_negotiation_agreement(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: operator confirmed a detected agreement — finalize it.

    Moves the negotiation session to AGREED and advances the parent deal to
    NEGOTIATION_AGREED (unblocking PI issuance). Until this fires, a detected
    acceptance parks the session at HITL_PENDING — no deal transition happens
    on the LLM/guard verdict alone.
    """
    from agents.negotiation_agent.service import finalize_agreement  # type: ignore[import]

    negotiation_id: str = payload["negotiation_id"]
    await finalize_agreement(negotiation_id, payload.get("agreed_price"))
    return {"negotiation_id": negotiation_id, "status": "agreed"}


# ── Automation legs (see shared/orchestration/continuations.py) ──────────────

async def _cb_dispatch_rfq(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Deal reached MATCHING → dispatch the RFQ to the shortlisted suppliers."""
    from services.sourcing.schemas import RfqRequest  # type: ignore[import]
    from services.sourcing.service import dispatch_rfq  # type: ignore[import]

    result = await dispatch_rfq(RfqRequest(**payload))
    return {
        "rfq_id": result.rfq_id,
        "status": result.status,
        "dispatched_count": result.dispatched_count,
        "failed_count": result.failed_count,
    }


async def _cb_confirm_supplier_selection(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Operator confirmed the proposed supplier → record it on the deal and
    advance to NEGOTIATING (via MATCHING when re-matching after a failed
    negotiation). The NEGOTIATING transition emits NEGOTIATION_STARTED, which
    opens the negotiation session with this supplier's ask."""
    from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
    from services.deals.service import (  # type: ignore[import]
        get_deal, record_match_result, update_status,
    )
    from shared.domain.enums import DealStatus

    supplier_id: str = payload["supplier_id"]
    await record_match_result(
        deal_id,
        supplier_id=supplier_id,
        company_name=payload.get("company_name") or "",
        fob_price_usd=payload.get("fob_price_usd"),
        composite_score=payload.get("composite_score"),
        match_id=payload.get("match_id"),
        buyer_target_price_usd=payload.get("buyer_target_price_usd"),
        make_winner=True,
    )

    deal = await get_deal(deal_id)
    status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    reason = payload.get("reason") or f"Supplier {supplier_id} confirmed by operator"
    if status == "NEGOTIATION_FAILED":
        await update_status(deal_id, UpdateStatusRequest(
            target_status=DealStatus.MATCHING, reason=f"Re-match: {reason}",
        ))
        status = "MATCHING"
    if status == "MATCHING":
        await update_status(deal_id, UpdateStatusRequest(
            target_status=DealStatus.NEGOTIATING, reason=reason,
        ))
        return {"deal_id": deal_id, "supplier_id": supplier_id, "status": "NEGOTIATING"}
    logger.info(
        "HITL: CONFIRM_SUPPLIER_SELECTION for deal %s — deal is %s, no transition applied",
        deal_id, status,
    )
    return {"deal_id": deal_id, "supplier_id": supplier_id, "status": status}


async def _cb_confirm_pi_acceptance(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Buyer's reply read as PI acceptance and the operator (or policy) confirmed it."""
    from services.dealmaker.schemas import AcceptPiRequest  # type: ignore[import]
    from services.dealmaker.service import accept_pi  # type: ignore[import]

    pi_id: str = payload["pi_id"]
    result = await accept_pi(pi_id, AcceptPiRequest(
        accepted_by=payload.get("accepted_by"),
        notes=payload.get("notes"),
        buyer_email=payload.get("buyer_email"),
    ))
    return {"pi_id": pi_id, "status": result.status}


async def _cb_release_escrow_tranche(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Release one staged escrow tranche against its proof refs."""
    from services.escrow_guard.schemas import ReleaseTrancheRequest  # type: ignore[import]
    from services.escrow_guard.service import release_tranche  # type: ignore[import]

    escrow_id: str = payload["escrow_id"]
    result = await release_tranche(escrow_id, ReleaseTrancheRequest(
        tranche_id=payload["tranche_id"],
        proof_refs=list(payload.get("proof_refs") or []),
        idempotency_key=payload.get("idempotency_key"),
    ))
    return {
        "escrow_id": escrow_id,
        "tranche_id": payload["tranche_id"],
        "released_total": result.released_total,
        "status": result.status,
    }


async def _cb_send_deal_notification(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Send a templated counterparty notification (same transport as negotiation emails)."""
    from agents.negotiation_agent.service import send_approved_email  # type: ignore[import]

    email_id: str = payload["email_id"]
    await send_approved_email(email_id, payload.get("modified_body"))
    return {"email_id": email_id, "kind": payload.get("kind"), "status": "sent"}


async def _cb_confirm_counterparty_risk(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Operator accepted a REVIEW screening verdict → issue the Proforma Invoice
    the NEGOTIATION_AGREED leg held back."""
    from shared.orchestration.continuations import issue_pi_for_deal

    pi = await issue_pi_for_deal(
        deal_id,
        agreed_price=float(payload["agreed_price"]),
        quantity_mt=payload.get("quantity_mt"),
        market=payload.get("market"),
    )
    return {"pi_id": getattr(pi, "pi_id", None), "status": "pi_issued"}


async def _cb_confirm_buyer_acceptance(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Buyer's post-delivery e-mail confirmed as acceptance → release the final
    ACCEPTED-trigger escrow tranche (the buyer's authority, relayed by the operator)."""
    from services.escrow_guard.schemas import AcceptEscrowRequest  # type: ignore[import]
    from services.escrow_guard.service import accept_escrow  # type: ignore[import]

    result = await accept_escrow(payload["escrow_id"], AcceptEscrowRequest(
        accepted_by=payload.get("accepted_by"),
        notes=payload.get("notes"),
    ))
    return {"escrow_id": payload["escrow_id"], "released_total": result.released_total,
            "status": result.status}


async def _cb_parse_inbound_email(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Informational: the operator has read the email. Any resulting action
    (price, acceptance, reassignment) is submitted through the Email
    Processor's manual inbound endpoint, not here."""
    logger.info(
        "HITL: PARSE_INBOUND_EMAIL acknowledged for deal %s (inbound=%s)",
        deal_id, payload.get("inbound_id"),
    )
    return {"inbound_id": payload.get("inbound_id"), "status": "acknowledged"}


# ---------------------------------------------------------------------------
# Reject handlers (soft-reject actions only)
# ---------------------------------------------------------------------------

async def _rcb_send_negotiation_email(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Mark a negotiation email draft as rejected (operator declined to send)."""
    from agents.negotiation_agent.service import reject_email  # type: ignore[import]

    email_id: str = payload["email_id"]
    await reject_email(email_id)
    return {"email_id": email_id, "status": "rejected"}


async def _rcb_confirm_negotiation_agreement(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Operator declined the detected agreement — reopen the negotiation."""
    from agents.negotiation_agent.service import reject_agreement  # type: ignore[import]

    negotiation_id: str = payload["negotiation_id"]
    await reject_agreement(negotiation_id)
    return {"negotiation_id": negotiation_id, "status": "reopened"}


async def _rcb_log_only(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Soft reject for routine operator decisions (supplier proposal declined,
    RFQ not sent, PI-acceptance reading declined, inbound email dismissed):
    the deal simply stays where it is — no exception, nothing to undo."""
    logger.info("HITL: soft reject for deal %s — payload: %s", deal_id, payload)
    return {"deal_id": deal_id, "status": "declined"}


# ---------------------------------------------------------------------------
# Approve dispatcher
# ---------------------------------------------------------------------------

_CALLBACK_MAP = {
    "BOOK_FREIGHT": _cb_book_freight,
    "ISSUE_PO": _cb_issue_po,
    "RELEASE_SUPPLIER_PAYOUT": _cb_release_supplier_payout,
    "FILE_CUSTOMS": _cb_file_customs,
    "RESOLVE_EXCEPTION": _cb_resolve_exception,
    "SEND_NEGOTIATION_EMAIL": _cb_send_negotiation_email,
    "CONFIRM_NEGOTIATION_AGREEMENT": _cb_confirm_negotiation_agreement,
    "DISPATCH_RFQ": _cb_dispatch_rfq,
    "CONFIRM_SUPPLIER_SELECTION": _cb_confirm_supplier_selection,
    "CONFIRM_PI_ACCEPTANCE": _cb_confirm_pi_acceptance,
    "RELEASE_ESCROW_TRANCHE": _cb_release_escrow_tranche,
    "SEND_DEAL_NOTIFICATION": _cb_send_deal_notification,
    "PARSE_INBOUND_EMAIL": _cb_parse_inbound_email,
    "CONFIRM_COUNTERPARTY_RISK": _cb_confirm_counterparty_risk,
    "CONFIRM_BUYER_ACCEPTANCE": _cb_confirm_buyer_acceptance,
}


async def execute_callback(
    action: str,
    deal_id: str,
    callback_payload: dict[str, Any],
    approval_id: str,
) -> dict | None:
    """Execute the saga-continuation callback for an approved action.

    Errors are caught and logged rather than re-raised so that a failed
    callback does not roll back the approval record (the approval itself
    succeeded — the downstream failure should surface as a deal.exception).
    """
    handler = _CALLBACK_MAP.get(action)
    if handler is None:
        logger.warning(
            "HITL: no callback registered for action %r (approval=%s)", action, approval_id
        )
        return None

    try:
        result = await handler(deal_id, callback_payload)
        logger.info(
            "HITL: callback %s executed for deal %s approval %s → %s",
            action, deal_id, approval_id, result,
        )
        return result
    except Exception as exc:
        logger.error(
            "HITL: callback %s failed for deal %s approval %s: %s",
            action, deal_id, approval_id, exc,
        )
        try:
            from shared.bus import get_publisher
            from shared.domain.events import DomainEvent, EventTopic
            await get_publisher().publish(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION,
                deal_id=deal_id,
                payload={
                    "source": "hitl_approval.callback",
                    "approval_id": approval_id,
                    "action": action,
                    "error": str(exc),
                    "action_required": "manual_retry_or_intervention",
                },
            ))
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Reject dispatcher (soft-reject actions)
# ---------------------------------------------------------------------------

_REJECT_CALLBACK_MAP = {
    "SEND_NEGOTIATION_EMAIL": _rcb_send_negotiation_email,
    "CONFIRM_NEGOTIATION_AGREEMENT": _rcb_confirm_negotiation_agreement,
    "SEND_DEAL_NOTIFICATION": _rcb_send_negotiation_email,  # same draft lifecycle
    "DISPATCH_RFQ": _rcb_log_only,
    "CONFIRM_SUPPLIER_SELECTION": _rcb_log_only,
    "CONFIRM_PI_ACCEPTANCE": _rcb_log_only,
    "PARSE_INBOUND_EMAIL": _rcb_log_only,
    # "Not actually an acceptance" — the inspection window keeps running.
    "CONFIRM_BUYER_ACCEPTANCE": _rcb_log_only,
}

SOFT_REJECT_ACTIONS: frozenset[str] = frozenset(_REJECT_CALLBACK_MAP)
"""Actions whose REJECT is a routine operator decision, not a blocked deal
(no DEAL_EXCEPTION). Money/commitment actions (PO, freight, customs, payout,
tranche release, exception resolution) and CONFIRM_COUNTERPARTY_RISK ("do not
trade with this buyer") stay hard rejects."""


async def execute_reject_callback(
    action: str,
    deal_id: str,
    callback_payload: dict[str, Any],
    approval_id: str,
) -> dict | None:
    """Execute the reject handler for a soft-reject action.

    Unlike ``execute_callback``, failures here are logged only — no
    DEAL_EXCEPTION is emitted because the rejection itself was intentional.
    """
    handler = _REJECT_CALLBACK_MAP.get(action)
    if handler is None:
        return None

    try:
        result = await handler(deal_id, callback_payload)
        logger.info(
            "HITL: reject callback %s executed for deal %s approval %s → %s",
            action, deal_id, approval_id, result,
        )
        return result
    except Exception as exc:
        logger.error(
            "HITL: reject callback %s failed for deal %s approval %s: %s",
            action, deal_id, approval_id, exc,
        )
        return None
