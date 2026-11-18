"""Park and drop a prospect (the desk's last two next moves, as real actions).

- **Park** — stop every reminder (outreach follow-ups, the stall check) and keep the record,
  optionally until a date; replies are still read. ``POST /deals/{id}/prospect/park``
  (reason required), ``POST /deals/{id}/prospect/unpark``. A park with a date ends by itself.
- **Drop** — close the prospect as CANCELLED with the reason (status history, outcomes via
  ``deals.update_status``); its pending prospect gates (match proposals, indicative offers,
  outreach, RFQ) close as superseded. ``POST /deals/{id}/prospect/drop`` (reason required).

Only a prospect (DRAFT / MATCHING) can be parked or dropped; a deal in negotiation or later
has its own exits.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

PROSPECT_STATUSES = ("DRAFT", "MATCHING")
_PROSPECT_GATES = frozenset({"CONFIRM_MATCH", "SEND_INDICATIVE_OFFER", "SEND_BUYER_OUTREACH", "DISPATCH_RFQ",
                             "CONFIRM_SUPPLIER_SELECTION"})


class NotAProspect(ValueError):
    """The deal is past the prospect stage."""


def _status(deal: Any) -> str:
    s = getattr(deal, "status", "")
    return str(getattr(s, "value", s) or "")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_parked(deal: Any, now: datetime | None = None) -> bool:
    """Parked and the park has not run out."""
    if not getattr(deal, "parked_at", None):
        return False
    until = _aware(getattr(deal, "parked_until", None))
    return until is None or until > (now or datetime.now(timezone.utc))


async def _save(deal_id: str, fields: dict[str, Any]) -> Any:
    from services.deals.repository import get_repository  # type: ignore[import]
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    if _status(deal) not in PROSPECT_STATUSES:
        raise NotAProspect(f"deal {deal_id} is {_status(deal)}, not a prospect")
    updated = deal.model_copy(update={**fields, "updated_at": datetime.now(timezone.utc)})
    await get_repository().save(updated)
    return updated


async def park(deal_id: str, reason: str, by: str, until: datetime | None = None) -> Any:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required")
    now = datetime.now(timezone.utc)
    if until is not None and _aware(until) <= now:
        raise ValueError("the park date must be in the future")
    deal = await _save(deal_id, {"parked_at": now, "parked_reason": f"{reason[:300]} (by {by})",
                                 "parked_until": _aware(until), "parked_by": by})
    logger.info("Prospect %s parked by %s until %s — %s", deal_id, by, until or "unparked by hand", reason)
    return deal


async def unpark(deal_id: str, by: str) -> Any:
    deal = await _save(deal_id, {"parked_at": None, "parked_reason": None, "parked_until": None, "parked_by": None})
    logger.info("Prospect %s unparked by %s", deal_id, by)
    return deal


async def drop(deal_id: str, reason: str, by: str) -> Any:
    from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
    from services.deals.service import get_deal, update_status  # type: ignore[import]
    from shared.domain.enums import DealStatus
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required")
    deal = await get_deal(deal_id)
    if _status(deal) not in PROSPECT_STATUSES:
        raise NotAProspect(f"deal {deal_id} is {_status(deal)}, not a prospect")
    text = f"prospect dropped by {by}: {reason[:300]}"
    try:
        from services.hitl_approval.service import withdraw_pending  # type: ignore[import]
        await withdraw_pending(deal_id, _PROSPECT_GATES, text)
    except Exception as exc:  # noqa: BLE001 - the drop still happens; a stale gate stays visible
        logger.warning("Prospect %s: pending gates not withdrawn: %s", deal_id, exc)
    updated = await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus.CANCELLED, reason=text))
    logger.warning("Prospect %s dropped by %s — %s", deal_id, by, reason)
    return updated
