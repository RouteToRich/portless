"""Deterministic remediation playbooks for the recurring deal exceptions.

The Exception Resolver's LLM is good at the long tail; the common failures
have one right answer and should not wait for a model (or for a human to read
a plan). ``services/exception_resolver/agent.py`` runs these before the LLM:

  QC_FAILED            → re-inspection (operator-approved, ``QC_REINSPECTION_MAX``
                         attempts): DISPUTE → SUPPLIER_DISPATCH_READY, the
                         escrow dispute flag is cleared, a fresh QC booking is
                         made and the supplier gets a rework notice. Past the
                         limit the operator decides refund / cancel.
  PAYMENT_FAILED       → funding shortfall: reminder to the buyer with the
                         missing amount (auto). Unfunded past
                         ``ESCROW_FUNDING_GRACE_DAYS`` → operator-approved
                         cancellation that refunds any partial funds.
  NEGOTIATION_FAILED   → next buyer candidate from the outreach funnel (auto);
                         exhausted funnel → operator.

Every money-touching or deal-closing step stays behind the RESOLVE_EXCEPTION
gate (``playbook`` in the approval's callback payload); only reminders and
"contact the next buyer" run unattended.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def reinspection_max() -> int:
    return max(0, _int("QC_REINSPECTION_MAX", 1))


def funding_grace_days() -> int:
    return max(0, _int("ESCROW_FUNDING_GRACE_DAYS", 14))


def _status(deal: Any) -> str:
    s = getattr(deal, "status", "")
    return str(getattr(s, "value", s) or "")


async def _transition(deal_id: str, target: str, reason: str) -> None:
    from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
    from services.deals.service import update_status  # type: ignore[import]
    from shared.domain.enums import DealStatus

    await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus(target), reason=reason))


# ---------------------------------------------------------------------------
# QC_FAILED → re-inspection
# ---------------------------------------------------------------------------

async def failed_inspections(deal_id: str) -> int:
    """How many QC orders of this deal ended in FAIL (= attempts used)."""
    try:
        from services.shield_link.repository import get_repository  # type: ignore[import]
        orders = await get_repository().list_by_deal(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Playbooks: QC history unavailable for %s: %s", deal_id, exc)
        return 0
    return sum(1 for o in orders if o.order_type == "QC" and o.qc_result == "FAIL")


async def reinspection_allowed(deal_id: str) -> tuple[bool, int]:
    """(allowed, failed_so_far) — a re-inspection is allowed while the number
    of failed inspections is within ``QC_REINSPECTION_MAX``."""
    failed = await failed_inspections(deal_id)
    return failed <= reinspection_max(), failed


async def book_reinspection(deal_id: str, *, reason: str = "re-inspection after QC failure") -> dict[str, Any]:
    """Approve callback for the ``reinspect`` playbook."""
    from services.deals.service import get_deal  # type: ignore[import]
    from services.shield_link.schemas import QcBookingRequest  # type: ignore[import]
    from services.shield_link.service import book_qc  # type: ignore[import]
    from shared.orchestration import notifications

    deal = await get_deal(deal_id)
    status = _status(deal)
    allowed, failed = await reinspection_allowed(deal_id)
    if not allowed:
        raise ValueError(f"deal {deal_id}: {failed} failed inspection(s) — QC_REINSPECTION_MAX={reinspection_max()} reached")
    if status == "DISPUTE":
        await _transition(deal_id, "SUPPLIER_DISPATCH_READY", f"Re-inspection #{failed}: {reason}")
    elif status != "SUPPLIER_DISPATCH_READY":
        raise ValueError(f"deal {deal_id} is {status}; re-inspection needs DISPUTE or SUPPLIER_DISPATCH_READY")

    # A buyer-raised escrow dispute is superseded by the agreed rework.
    try:
        from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
        from services.escrow_guard.service import clear_dispute  # type: ignore[import]
        hold = await _eg_repo().get_hold_by_deal(deal_id)
        if hold is not None and hold.disputed:
            await clear_dispute(hold.escrow_id, reason=f"re-inspection #{failed} agreed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Playbooks: could not clear the escrow dispute for %s: %s", deal_id, exc)

    booking = await book_qc(QcBookingRequest(
        deal_id=deal_id,
        hs_code=getattr(deal, "hs_code", None),
        quantity_mt=getattr(deal, "quantity_mt", None),
        idempotency_key=f"reinspect-{deal_id}-{failed}",
    ), reinspection=True)
    await notifications.notify_qc_rework(deal, reason, booking.ext_ref, failed)
    logger.info("Playbooks: re-inspection #%d booked for deal %s (%s)", failed, deal_id, booking.tpo_id)
    return {"action": "reinspection_booked", "attempt": failed, "tpo_id": booking.tpo_id, "ext_ref": booking.ext_ref}


# ---------------------------------------------------------------------------
# PAYMENT_FAILED → shortfall reminder / cancel unfunded
# ---------------------------------------------------------------------------

async def remind_funding_shortfall(deal_id: str) -> dict[str, Any]:
    """Auto-fix: tell the buyer exactly how much is still missing."""
    from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
    from services.escrow_guard.service import _notify_funding_shortfall  # type: ignore[import]

    hold = await _eg_repo().get_hold_by_deal(deal_id)
    if hold is None or hold.status != "PARTIALLY_FUNDED" or not hold.shortfall:
        return {"success": False, "error": "no partially funded escrow on this deal"}
    await _notify_funding_shortfall(hold)
    return {"success": True, "action": "shortfall_reminder_sent", "shortfall": hold.shortfall, "escrow_id": hold.escrow_id}


_CANCELLABLE = {"PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING"}


async def cancel_unfunded_deal(deal_id: str, *, reason: str = "unfunded past grace period") -> dict[str, Any]:
    """Approve callback for the ``cancel_unfunded`` playbook: refund partial
    funds (if any), close the deal, tell both parties."""
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.orchestration import notifications

    deal = await get_deal(deal_id)
    status = _status(deal)
    if status == "CANCELLED":
        return {"action": "already_cancelled"}
    if status not in _CANCELLABLE:
        raise ValueError(f"deal {deal_id} is {status}; the unfunded-cancel playbook covers {sorted(_CANCELLABLE)}")

    refunded: float | None = None
    currency = "USD"
    try:
        from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
        from services.escrow_guard.schemas import RefundRequest  # type: ignore[import]
        from services.escrow_guard.service import refund  # type: ignore[import]
        hold = await _eg_repo().get_hold_by_deal(deal_id)
        if hold is not None:
            currency = hold.currency
            if hold.status == "PARTIALLY_FUNDED" and (hold.funded_amount or 0) > 0:
                r = await refund(hold.escrow_id, RefundRequest(reason=f"deal cancelled: {reason}"))
                refunded = r.refunded_total
    except Exception as exc:  # noqa: BLE001
        logger.error("Playbooks: refund during cancellation of %s failed: %s — cancelling anyway", deal_id, exc)

    await _transition(deal_id, "CANCELLED", f"Cancelled: {reason}")
    deal = await get_deal(deal_id)
    sent = await notifications.notify_deal_cancelled(deal, reason, refunded, currency)
    logger.warning("Playbooks: deal %s cancelled (%s); refunded=%s; notices=%s", deal_id, reason, refunded, sent)
    return {"action": "deal_cancelled", "refunded": refunded, "currency": currency, "notices": sent}


# ---------------------------------------------------------------------------
# NEGOTIATION_FAILED → next buyer
# ---------------------------------------------------------------------------

async def next_buyer(deal_id: str) -> dict[str, Any]:
    """Auto-fix: move the outreach funnel to the next candidate."""
    from shared.orchestration import outreach

    res = await outreach.advance(deal_id)
    if res.get("advanced"):
        return {"success": True, "action": "next_buyer_contacted", **res}
    return {"success": False, "error": "no further buyer candidate" if res.get("exhausted") else "funnel idle", **res}
