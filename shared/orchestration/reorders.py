"""Repeat orders: one click to reorder, and a reminder before the buyer's usual order date.

A buyer that received a good shipment is the cheapest next deal: no outreach, a known
supplier, a known price. ``reorder(deal_id)`` starts a new prospect from a closed (PAID)
deal — same product, market, quantity, buyer and supplier as parties, the last agreed price
as the buyer's target — and moves it to MATCHING, so the creation checks run (screening,
compliance) and the supplier gets the RFQ for a fresh quote. The negotiation then proceeds
as usual; nothing is assumed from the old price.

``due(now)`` finds the buyers whose next order is near: the median gap between their PAID
deals (one deal → ``REORDER_DEFAULT_DAYS``, 90), next date = last delivery + gap; listed in
``GET /deals/reorders/due`` and the daily digest when within ``REORDER_REMIND_DAYS`` (7) and
no reorder is open for that buyer and product.
"""
from __future__ import annotations

import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_OPEN = ("DRAFT", "MATCHING", "NEGOTIATING", "NEGOTIATION_AGREED", "PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING")


def _days(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _status(d: Any) -> str:
    return str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))


def _party(deal: Any, role: str) -> Any:
    return next((p for p in getattr(deal, "parties", None) or []
                 if str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower() == role), None)


def _buyer_key(deal: Any) -> str | None:
    b = _party(deal, "buyer")
    if b is None:
        return None
    return (getattr(b, "email", None) or getattr(b, "legal_name", None) or "").strip().lower() or None


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def reorder(deal_id: str, *, by: str, quantity_mt: float | None = None, org_id: str | None = None) -> Any:
    from services.deals.schemas import CreateDealRequest, DealStatus  # type: ignore[import]
    from services.deals.service import create_deal, get_deal, update_status  # type: ignore[import]
    from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]

    old = await get_deal(deal_id)
    if _status(old) != "PAID":
        raise ValueError(f"deal {deal_id} is {_status(old)} — only a completed (PAID) deal can be reordered")
    buyer, supplier = _party(old, "buyer"), _party(old, "supplier")
    if buyer is None:
        raise ValueError(f"deal {deal_id} has no buyer party")
    parties = [p.model_copy() for p in (buyer, supplier) if p is not None]
    new = await create_deal(CreateDealRequest(
        operator_id=by, hs_code=old.hs_code, market=old.market, margin_floor_pct=getattr(old, "margin_floor_pct", None),
        parties=parties,
    ), org_id=org_id or getattr(old, "org_id", "portless"))
    qty = quantity_mt or getattr(old, "quantity_mt", None)
    last_price = getattr(old, "buyer_target_price_usd", None)
    try:
        from services.dealmaker.repository import get_repository as dm_repo  # type: ignore[import]
        pi = await dm_repo().get_pi_by_deal(deal_id)
        last_price = float(pi.agreed_price) if pi is not None and getattr(pi, "agreed_price", None) else last_price
    except Exception as exc:  # noqa: BLE001 - the RFQ reply sets the price anyway
        logger.debug("Reorders: PI of %s unavailable: %s", deal_id, exc)
    from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
    await deals_repo().set_fields(new.deal_id, {"reorder_of": deal_id, "quantity_mt": qty,
                                                "buyer_target_price_usd": last_price})
    if _status(new) == "DRAFT":
        await update_status(new.deal_id, UpdateStatusRequest(target_status=DealStatus.MATCHING,
                                                             reason=f"reorder of {deal_id} by {by}"))
    logger.info("Reorders: %s reordered as %s by %s", deal_id, new.deal_id, by)
    return await get_deal(new.deal_id)


def due_from(deals: list[Any], now: datetime) -> list[dict[str, Any]]:
    """Pure: buyers whose next usual order falls within REORDER_REMIND_DAYS."""
    remind = timedelta(days=_days("REORDER_REMIND_DAYS", 7))
    default_gap = _days("REORDER_DEFAULT_DAYS", 90)
    by_buyer: dict[tuple[str, str], list[Any]] = {}
    open_keys: set[tuple[str, str]] = set()
    for d in deals:
        key = _buyer_key(d)
        if not key:
            continue
        k = (key, str(d.hs_code)[:6])
        if _status(d) == "PAID":
            by_buyer.setdefault(k, []).append(d)
        elif _status(d) in _OPEN:
            open_keys.add(k)
    out = []
    for k, paid in by_buyer.items():
        if k in open_keys:
            continue
        dates = sorted(x for x in (_aware(getattr(d, "updated_at", None)) for d in paid) if x)
        if not dates:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        gap = int(statistics.median(gaps)) if gaps else default_gap
        nxt = dates[-1] + timedelta(days=gap)
        if nxt - now > remind:
            continue
        last = max(paid, key=lambda d: _aware(getattr(d, "updated_at", None)) or now)
        b = _party(last, "buyer")
        out.append({"deal_id": last.deal_id, "buyer": getattr(b, "legal_name", None), "hs_code": last.hs_code,
                    "market": last.market, "orders": len(paid), "usual_gap_days": gap,
                    "next_order_due": nxt.date().isoformat(), "overdue_days": max(0, (now - nxt).days)})
    return sorted(out, key=lambda r: r["next_order_due"])


async def due(now: datetime | None = None) -> list[dict[str, Any]]:
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Reorders: deals unavailable: %s", exc)
        return []
    return due_from(deals, now or datetime.now(timezone.utc))
