"""Demurrage and detention warnings before the free days run out (todo: demurrage / detention alerts).

* **Demurrage** — a full container sits at the port of discharge: the clock starts at arrival
  (carrier ARRIVED / DISCHARGED event, else the ARRIVED / CLEARED milestone) and stops when it
  leaves the port (DELIVERED). Free days ``DEMURRAGE_FREE_DAYS`` (5; ``DEMURRAGE_FREE_DAYS_<CC>``
  per destination country).
* **Detention** — the box is out of the port with the buyer: from delivery until the empty is back
  at the depot (carrier ``EMPTY_RETURNED`` event or staff ``POST /shield-link/deals/{id}/empty-returned``).
  Free days ``DETENTION_FREE_DAYS`` (7; ``_<CC>``).

Under CIF / CFR the buyer pays both, but an unpaid bill stops the carrier releasing the next box
and sours the buyer, so the desk warns the buyer ``DEMURRAGE_WARN_DAYS_BEFORE`` (2) days before
the free days end (templated notice through the normal gate) and pages Telegram once per deal ×
kind × stage (``warning`` / ``running``). ``status(deal_id)`` backs ``GET /shield-link/deals/{id}/demurrage``.
Daily rates (``DEMURRAGE_USD_PER_DAY`` 75, ``DETENTION_USD_PER_DAY`` 40) give the cost so far. Never raises.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_sent: set[tuple[str, str, str]] = set()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _free_days(kind: str, country: str | None) -> float:
    base = "DEMURRAGE_FREE_DAYS" if kind == "demurrage" else "DETENTION_FREE_DAYS"
    default = 5.0 if kind == "demurrage" else 7.0
    if country:
        raw = os.getenv(f"{base}_{country.upper()}")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return _f(base, default)


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def clocks(tpo: Any) -> dict[str, datetime | None]:
    """Arrival, delivery and empty-return times of one freight order."""
    events = list(getattr(tpo, "tracking_events", None) or [])

    def first(*milestones: str) -> datetime | None:
        times = [_dt(e.get("at")) for e in events if e.get("milestone") in milestones]
        times = [t for t in times if t]
        return min(times) if times else None

    arrived = first("ARRIVED", "DISCHARGED")
    if arrived is None and getattr(tpo, "last_milestone", None) in ("ARRIVED", "CLEARED", "DELIVERED"):
        arrived = _dt(getattr(tpo, "arrived_at", None)) or (
            _dt(tpo.last_milestone_at) if tpo.last_milestone in ("ARRIVED", "CLEARED") else None)
    delivered = _dt(getattr(tpo, "delivered_at", None)) or first("DELIVERED")
    returned = _dt(getattr(tpo, "empty_returned_at", None)) or first("EMPTY_RETURNED")
    return {"arrived": arrived, "delivered": delivered, "empty_returned": returned}


def assess(tpo: Any, country: str | None, now: datetime) -> list[dict[str, Any]]:
    """Pure: one row per running clock — days used, free days, days left, cost so far, stage."""
    c = clocks(tpo)
    warn = _f("DEMURRAGE_WARN_DAYS_BEFORE", 2)
    rows = []
    spans = (
        ("demurrage", c["arrived"], c["delivered"], _f("DEMURRAGE_USD_PER_DAY", 75)),
        ("detention", c["delivered"], c["empty_returned"], _f("DETENTION_USD_PER_DAY", 40)),
    )
    boxes = max(1, len(getattr(tpo, "containers", None) or []))
    for kind, start, stop, rate in spans:
        if start is None:
            continue
        end = stop or now
        used = (end - start).total_seconds() / 86400
        free = _free_days(kind, country)
        left = free - used
        over = max(0.0, -left)
        stage = "stopped" if stop else ("running" if left < 0 else ("warning" if left <= warn else "ok"))
        rows.append({"kind": kind, "started_at": start.isoformat(), "stopped_at": stop.isoformat() if stop else None,
                     "days_used": round(used, 1), "free_days": free, "days_left": round(left, 1), "stage": stage,
                     "containers": boxes, "cost_so_far_usd": round(over * rate * boxes, 2), "usd_per_day_per_box": rate})
    return rows


async def _freight_orders(deal_id: str) -> list[Any]:
    try:
        from services.shield_link.repository import get_repository  # type: ignore[import]
        return [o for o in await get_repository().list_by_deal(deal_id) if o.order_type == "FREIGHT"]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Demurrage: orders of %s unreadable: %s", deal_id, exc)
        return []


async def status(deal_id: str, country: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    out = []
    for o in await _freight_orders(deal_id):
        for row in assess(o, country, now):
            out.append({"tpo_id": o.tpo_id, "ext_ref": o.ext_ref, **row})
    return {"deal_id": deal_id, "clocks": out,
            "attention": [r for r in out if r["stage"] in ("warning", "running")]}


async def sweep(deals: list[Any], now: datetime | None = None, *, send=None, notify=None) -> int:
    """Deal watcher: warn once per deal × kind × stage. Returns the number of new warnings."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    for deal in deals:
        st = str(getattr(deal.status, "value", deal.status))
        if st not in ("IN_TRANSIT", "CLEARED"):
            continue
        country = getattr(deal, "destination_country", None) or getattr(deal, "market", None)
        res = await status(deal.deal_id, country, now)
        for row in res["attention"]:
            key = (deal.deal_id, row["kind"], row["stage"])
            if key in _sent:
                continue
            _sent.add(key)
            sent += 1
            msg = (f"{row['kind'].upper()} {'RUNNING' if row['stage'] == 'running' else 'SOON'}: {deal.deal_id} — "
                   f"{row['days_used']:g} of {row['free_days']:g} free days used"
                   + (f", {row['cost_so_far_usd']:,.0f} USD so far" if row["cost_so_far_usd"] else "")
                   + (" (empty not returned yet)" if row["kind"] == "detention" else " (container still at the port)"))
            logger.warning("Demurrage: %s", msg)
            await _page(msg, send)
            await _tell_buyer(deal, row, notify)
    return sent


async def _page(msg: str, send=None) -> None:
    try:
        sender = send
        if sender is None:
            from shared import telegram
            sender = telegram.send_message if telegram.configured() else None
        if sender is not None:
            await sender(msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Demurrage: page not sent: %s", exc)


async def _tell_buyer(deal: Any, row: dict[str, Any], notify=None) -> None:
    """Templated notice to the buyer (through the normal notification gate)."""
    try:
        if notify is None:
            from shared.orchestration import notifications
            notify = getattr(notifications, "notify_free_days", None)
        if notify is not None:
            await notify(deal, row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Demurrage: buyer notice for %s not sent: %s", deal.deal_id, exc)


def reset_for_tests() -> None:
    _sent.clear()
