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
    """Continue: release supplier payout after HITL approval."""
    from services.escrow_guard.service import request_payout  # type: ignore[import]
    from services.escrow_guard.schemas import PayoutRequest  # type: ignore[import]
    result = await request_payout(PayoutRequest(**payload))
    return {"payout_id": result.payout_id, "status": result.status}


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
}


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
