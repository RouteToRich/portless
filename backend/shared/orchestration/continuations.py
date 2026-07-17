"""Event-driven pipeline continuations.

Subscribes handlers to the in-process event bus so a deal state change itself
triggers the next pipeline step — instead of depending on a long-lived poller
task (the dev auto-flow) that can die and leave the deal parked.

  NEGOTIATION_AGREED → issue PI                     → deal PI_SENT
  PI_ACCEPTED        → open neutral escrow          → deal ESCROW_PENDING
  ESCROW_FUNDED      → create ISSUE_PO HITL gate    (operator approves → PO)
  PO_ISSUED          → book QC (buyer-opted agency) → SUPPLIER_DISPATCH_READY
  QC_PASSED          → create BOOK_FREIGHT HITL gate
  CUSTOMS_CLEARED    → create RELEASE_SUPPLIER_PAYOUT HITL gate
  DEAL_EXCEPTION     → route to the Exception Resolver agent (loop-safe:
                       resolver lifecycle echoes carry an exception_id and
                       are skipped)

Money-moving and commitment steps stay HITL-gated: the continuation creates
the approval automatically, it never approves it. Buyer actions (accepting
the PI, funding the escrow) remain buyer actions — no handler fakes them.

Design rules every handler must follow:

  • Idempotent — safe to fire twice (idempotency key + deal-state validation
    inside the target service make the second call a no-op / clean rejection;
    the same topic can legitimately arrive from both the origin service and
    the Deals status-transition emitter).
  • Never raise for business no-ops (deal already advanced, feature disabled).
  • On genuine failure: log loudly and emit ``deal.exception`` so the failure
    is visible in the UI instead of only in a console.

Enabled by default; set ``EVENT_CONTINUATION_ENABLED=false`` to fall back to
purely manual/auto-flow-driven progression.
"""
from __future__ import annotations

import logging
import os

from shared.domain.events import DomainEvent, EventTopic

logger = logging.getLogger(__name__)

_DEFAULT_QUANTITY_MT = 25.0
_DEFAULT_PAYMENT_TERMS = "30% advance + 70% on BL"


def _enabled() -> bool:
    return os.getenv("EVENT_CONTINUATION_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


async def _get_deal_in_status(deal_id: str | None, required: str, stage: str):
    """Fetch the deal and return it only when it sits in *required* status.

    Returns None (logged, no error) otherwise — either the event is a
    duplicate and someone already advanced the deal, or it predates a manual
    intervention; both are idempotent no-ops.
    """
    if not deal_id:
        logger.error("Continuation[%s]: event without deal_id — skipped", stage)
        return None
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    if status != required:
        logger.info(
            "Continuation[%s]: deal %s is %s, not %s — skipped (idempotent)",
            stage, deal_id, status, required,
        )
        return None
    return deal


async def _negotiation_numbers(deal_id: str) -> tuple[float | None, float]:
    """(final supplier ask, quantity_mt) from the deal's negotiation session."""
    try:
        from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
        session = await _neg_repo().get_session_by_deal(deal_id)
        if session is not None:
            return (
                session.current_ask,
                float(session.quantity_mt) if session.quantity_mt else _DEFAULT_QUANTITY_MT,
            )
    except Exception as exc:
        logger.warning("Continuation: could not read negotiation session for %s: %s", deal_id, exc)
    return None, _DEFAULT_QUANTITY_MT


async def _create_gate(
    deal_id: str, action: str, summary: str,
    financial_impact_usd: float | None,
    callback_payload: dict, idempotency_key: str,
    autonomy_level: str = "L0",
) -> None:
    """Create a HITL approval gate (idempotent on *idempotency_key*)."""
    from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
    from services.hitl_approval.service import create_approval  # type: ignore[import]
    await create_approval(CreateApprovalRequest(
        deal_id=deal_id, action=action, summary=summary,
        financial_impact_usd=financial_impact_usd,
        autonomy_level=autonomy_level,
        callback_payload=callback_payload,
        idempotency_key=idempotency_key,
    ))


async def _emit_exception(deal_id: str | None, stage: str, exc: Exception) -> None:
    logger.exception("Continuation[%s]: FAILED for deal %s", stage, deal_id)
    try:
        from shared.bus import get_publisher
        await get_publisher().publish(DomainEvent(
            topic=EventTopic.DEAL_EXCEPTION,
            deal_id=deal_id,
            payload={
                "category": "CALLBACK_FAILED",
                "stage": stage,
                "error": str(exc),
                "action_required": "manual_retry_or_intervention",
            },
        ))
    except Exception:
        logger.exception("Continuation[%s]: could not emit deal.exception for %s", stage, deal_id)


async def on_negotiation_agreed(event: DomainEvent) -> None:
    """NEGOTIATION_AGREED → issue the Proforma Invoice automatically.

    The PI is the next commercial artifact after agreement; issuing it flips
    the deal to PI_SENT. Buyer acceptance of the PI remains a buyer action —
    this handler never auto-accepts.
    """
    if not _enabled():
        logger.info("Continuation disabled — skipping PI issuance for deal %s", event.deal_id)
        return
    deal_id = event.deal_id
    agreed_price = event.payload.get("agreed_price")
    if not deal_id or not agreed_price or float(agreed_price) <= 0:
        logger.error(
            "Continuation: cannot issue PI for deal %s — missing/invalid agreed_price %r",
            deal_id, agreed_price,
        )
        return

    deal = await _get_deal_in_status(deal_id, "NEGOTIATION_AGREED", "AGREED→PI")
    if deal is None:
        return
    _, quantity_mt = await _negotiation_numbers(deal_id)

    from services.dealmaker.schemas import IssuePiRequest  # type: ignore[import]
    from services.dealmaker.service import issue_pi  # type: ignore[import]

    try:
        pi = await issue_pi(IssuePiRequest(
            deal_id=deal_id,
            agreed_price=float(agreed_price),
            quantity_mt=quantity_mt,
            port_of_destination=f"Destination Port, {getattr(deal, 'market', '') or 'TBD'}",
            payment_terms=_DEFAULT_PAYMENT_TERMS,
            idempotency_key=f"auto-pi-{deal_id}",
        ))
        logger.info(
            "Continuation: PI %s issued for deal %s at %s × %s MT (event %s)",
            pi.pi_id, deal_id, agreed_price, quantity_mt, event.event_id,
        )
    except Exception as exc:
        await _emit_exception(deal_id, "NEGOTIATION_AGREED→PI_SENT", exc)


async def on_pi_accepted(event: DomainEvent) -> None:
    """PI_ACCEPTED → open the neutral escrow (deal → ESCROW_PENDING).

    Funding the escrow remains the buyer's action; this only opens it so the
    buyer has something to pay into. Amount comes from the accepted PI itself.
    """
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "PI_ACCEPTED", "PI_ACCEPTED→ESCROW")
    if deal is None:
        return

    try:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        pi = None
        pi_id = event.payload.get("pi_id")
        if pi_id:
            pi = await _dm_repo().get_pi(pi_id)
        if pi is None:
            pi = await _dm_repo().get_pi_by_deal(deal_id)
        if pi is None:
            raise RuntimeError(f"no PI found for deal {deal_id} — cannot size the escrow")

        # Buyer-opted QC provider drives the escrow's QC-proof tranche context.
        qc_provider = None
        try:
            from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
            session = await _neg_repo().get_session_by_deal(deal_id)
            qc_provider = getattr(session, "qc_provider", None) if session else None
        except Exception:
            pass

        from services.escrow_guard.schemas import CreateEscrowRequest  # type: ignore[import]
        from services.escrow_guard.service import create_escrow  # type: ignore[import]
        escrow = await create_escrow(CreateEscrowRequest(
            deal_id=deal_id,
            amount=round(float(pi.total_value), 2),
            qc_provider=qc_provider,
            idempotency_key=f"auto-escrow-{deal_id}",
        ))
        logger.info(
            "Continuation: escrow %s opened for deal %s (amount=%s, event %s)",
            escrow.escrow_id, deal_id, pi.total_value, event.event_id,
        )
    except Exception as exc:
        await _emit_exception(deal_id, "PI_ACCEPTED→ESCROW_PENDING", exc)


async def on_escrow_funded(event: DomainEvent) -> None:
    """ESCROW_FUNDED → create the ISSUE_PO HITL gate (operator approves → PO)."""
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "ESCROW_FUNDED", "ESCROW_FUNDED→PO_GATE")
    if deal is None:
        return
    try:
        supplier_ask, quantity_mt = await _negotiation_numbers(deal_id)
        if not supplier_ask:
            raise RuntimeError(
                f"no supplier price available for deal {deal_id} — PO gate needs one"
            )
        from datetime import date, timedelta
        await _create_gate(
            deal_id, "ISSUE_PO",
            summary=f"Escrow funded — issue PO for deal {deal_id} at ${supplier_ask}/unit",
            financial_impact_usd=round(supplier_ask * quantity_mt, 2),
            callback_payload={
                "deal_id": deal_id, "agreed_price": supplier_ask,
                "quantity_mt": quantity_mt, "incoterm": "FOB",
                "port_of_loading": "Chennai, IN",
                "delivery_deadline": (date.today() + timedelta(days=21)).isoformat(),
                "payment_terms": _DEFAULT_PAYMENT_TERMS,
            },
            idempotency_key=f"auto-po-gate-{deal_id}",
            autonomy_level=(getattr(deal.autonomy_profile, "value", deal.autonomy_profile)
                            if getattr(deal, "autonomy_profile", None) else "L0"),
        )
        logger.info("Continuation: ISSUE_PO gate created for deal %s (event %s)",
                    deal_id, event.event_id)
    except Exception as exc:
        await _emit_exception(deal_id, "ESCROW_FUNDED→ISSUE_PO_GATE", exc)


async def on_po_issued(event: DomainEvent) -> None:
    """PO_ISSUED → book the QC inspection with the buyer-opted provider."""
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "PO_ISSUED", "PO_ISSUED→QC")
    if deal is None:
        return
    try:
        _, quantity_mt = await _negotiation_numbers(deal_id)
        from services.shield_link.schemas import QcBookingRequest  # type: ignore[import]
        from services.shield_link.service import book_qc  # type: ignore[import]
        tpo = await book_qc(QcBookingRequest(
            deal_id=deal_id,
            quantity_mt=quantity_mt,
            hs_code=getattr(deal, "hs_code", None),
            idempotency_key=f"auto-qc-{deal_id}",
        ))
        logger.info(
            "Continuation: QC booked %s (provider=%s) for deal %s (event %s)",
            tpo.tpo_id, tpo.provider, deal_id, event.event_id,
        )
    except Exception as exc:
        await _emit_exception(deal_id, "PO_ISSUED→QC_BOOKED", exc)


async def on_qc_passed(event: DomainEvent) -> None:
    """QC_PASSED → create the BOOK_FREIGHT HITL gate."""
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "QC_PASSED", "QC_PASSED→FREIGHT_GATE")
    if deal is None:
        return
    try:
        await _create_gate(
            deal_id, "BOOK_FREIGHT",
            summary=f"QC passed — book freight for deal {deal_id}",
            financial_impact_usd=None,
            callback_payload={"deal_id": deal_id, "provider": "cogoport", "incoterm": "FOB"},
            idempotency_key=f"auto-freight-gate-{deal_id}",
            autonomy_level=(getattr(deal.autonomy_profile, "value", deal.autonomy_profile)
                            if getattr(deal, "autonomy_profile", None) else "L0"),
        )
        logger.info("Continuation: BOOK_FREIGHT gate created for deal %s (event %s)",
                    deal_id, event.event_id)
    except Exception as exc:
        await _emit_exception(deal_id, "QC_PASSED→BOOK_FREIGHT_GATE", exc)


async def on_customs_cleared(event: DomainEvent) -> None:
    """Deal CLEARED → create the RELEASE_SUPPLIER_PAYOUT HITL gate."""
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "CLEARED", "CLEARED→PAYOUT_GATE")
    if deal is None:
        return
    try:
        supplier_ask, quantity_mt = await _negotiation_numbers(deal_id)
        if not supplier_ask:
            raise RuntimeError(
                f"no supplier price available for deal {deal_id} — payout gate needs one"
            )
        supplier_id = getattr(deal, "winning_supplier_id", None) or "UNKNOWN"
        amount = round(supplier_ask * quantity_mt, 2)
        await _create_gate(
            deal_id, "RELEASE_SUPPLIER_PAYOUT",
            summary=f"Cargo cleared — release supplier payout ${amount} for deal {deal_id}",
            financial_impact_usd=amount,
            callback_payload={
                "deal_id": deal_id,
                "payee_id": f"SUPPLIER-{supplier_id}",
                "amount": amount,
                "idempotency_key": f"auto-payout-{deal_id}",
            },
            idempotency_key=f"auto-payout-gate-{deal_id}",
            autonomy_level=(getattr(deal.autonomy_profile, "value", deal.autonomy_profile)
                            if getattr(deal, "autonomy_profile", None) else "L0"),
        )
        logger.info("Continuation: RELEASE_SUPPLIER_PAYOUT gate created for deal %s (event %s)",
                    deal_id, event.event_id)
    except Exception as exc:
        await _emit_exception(deal_id, "CLEARED→PAYOUT_GATE", exc)


async def on_deal_exception(event: DomainEvent) -> None:
    """DEAL_EXCEPTION → route to the Exception Resolver agent.

    Completes the documented flow (\"On REJECT / callback failure →
    deal.exception emitted; Exception Resolver handles next step\") — until
    this subscriber existed, those events were emitted and never consumed.

    Loop safety: ``raise_exception()`` itself emits DEAL_EXCEPTION lifecycle
    events (OPEN, then the final status) whose payload carries the
    ``exception_id`` of the record it just persisted. Those echoes are
    notifications, not new failures — skip them, or every exception would
    re-trigger the resolver forever.

    Failure safety: this handler must NEVER call ``_emit_exception`` — a
    failure while handling deal.exception must not publish more
    deal.exception events. Log and stop; the OPEN record (when reached) is
    the crash-safe audit trace.
    """
    if not _enabled():
        return
    payload = event.payload or {}
    if payload.get("exception_id"):
        return  # resolver lifecycle echo — already being handled
    if not event.deal_id:
        logger.error("Continuation[DEAL_EXCEPTION]: event without deal_id — skipped")
        return
    try:
        from services.exception_resolver.schemas import RaiseExceptionRequest  # type: ignore[import]
        from services.exception_resolver.service import raise_exception  # type: ignore[import]

        result = await raise_exception(RaiseExceptionRequest(
            deal_id=event.deal_id,
            source_event=str(payload.get("stage") or payload.get("failed_topic") or event.topic.value),
            source_service=str(payload.get("source") or payload.get("source_service") or "event_bus"),
            detail=str(payload.get("error") or payload.get("detail") or ""),
            context=payload,
        ))
        logger.info(
            "Continuation: exception %s (%s) raised for deal %s (event %s)",
            result.exception_id, result.status.value, event.deal_id, event.event_id,
        )
    except Exception:
        logger.exception(
            "Continuation[DEAL_EXCEPTION]: resolver failed for deal %s (event %s) "
            "— manual intervention required",
            event.deal_id, event.event_id,
        )


def register_continuations(bus) -> None:
    """Subscribe every pipeline continuation on the given bus
    (InProcessEventBus or NatsJetStreamBus — same subscribe surface)."""
    bus.subscribe(EventTopic.NEGOTIATION_AGREED, on_negotiation_agreed)
    bus.subscribe(EventTopic.PI_ACCEPTED, on_pi_accepted)
    bus.subscribe(EventTopic.ESCROW_FUNDED, on_escrow_funded)
    bus.subscribe(EventTopic.PO_ISSUED, on_po_issued)
    bus.subscribe(EventTopic.QC_PASSED, on_qc_passed)
    bus.subscribe(EventTopic.CUSTOMS_CLEARED, on_customs_cleared)
    bus.subscribe(EventTopic.DEAL_EXCEPTION, on_deal_exception)
    logger.info("Pipeline continuations registered (EVENT_CONTINUATION_ENABLED=%s)", _enabled())
