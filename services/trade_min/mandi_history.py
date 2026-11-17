"""PortLess's own daily history of Agmarknet mandi prices — the trend behind "buy now / wait".

The daily mandi API returns today's prices only, so every mandi summary PortLess
fetches is kept as one point per commodity per day (Mongo ``mandi_price_history``,
else memory). After a week of points the radar can say whether the wholesale price is
rising or falling (90-day window) — rising: buy now; falling: the supplier price may
come down, ask for a fresh quote before committing.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_COLLECTION = "mandi_price_history"
_memory: dict[str, dict[str, Any]] = {}
WINDOW_DAYS = 90
MIN_SPAN_DAYS = 7
FLAT_PCT = 3.0


def reset_for_tests() -> None:
    _memory.clear()


async def _collection():
    try:
        from .repository import MongoOpportunityRepository, get_repository
        repo = await get_repository()
        if isinstance(repo, MongoOpportunityRepository):
            return repo._col.database[_COLLECTION]  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Mandi history: no Mongo collection: %s", exc)
    return None


async def record(summary: dict[str, Any], day: date | None = None) -> None:
    """Keep one point per commodity per day (the latest fetch of the day wins)."""
    commodity = summary.get("commodity")
    modal = summary.get("modal_inr_per_quintal")
    if not commodity or not modal:
        return
    d = (day or datetime.now(timezone.utc).date()).isoformat()
    point = {"commodity": commodity, "day": d, "modal_inr_per_quintal": float(modal),
             "markets": summary.get("markets"), "recorded_at": datetime.now(timezone.utc).isoformat()}
    key = f"{commodity}:{d}"
    _memory[key] = point
    col = await _collection()
    if col is not None:
        try:
            await col.replace_one({"_id": key}, {**point, "_id": key}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Mandi history: write failed: %s", exc)


async def points(commodity: str) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc).date() - timedelta(days=WINDOW_DAYS)).isoformat()
    col = await _collection()
    if col is not None:
        try:
            docs = await col.find({"commodity": commodity, "day": {"$gte": since}}).to_list(length=500)
            return sorted(docs, key=lambda p: p["day"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Mandi history: read failed: %s", exc)
    return sorted((p for p in _memory.values() if p["commodity"] == commodity and p["day"] >= since),
                  key=lambda p: p["day"])


def trend_of(pts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pure: first vs last week of the window → rising / falling / flat, or None when too short."""
    if len(pts) < 2:
        return None
    first, last = date.fromisoformat(pts[0]["day"]), date.fromisoformat(pts[-1]["day"])
    span = (last - first).days
    if span < MIN_SPAN_DAYS:
        return None
    head = [p["modal_inr_per_quintal"] for p in pts if (date.fromisoformat(p["day"]) - first).days <= 7]
    tail = [p["modal_inr_per_quintal"] for p in pts if (last - date.fromisoformat(p["day"])).days <= 7]
    a, b = sum(head) / len(head), sum(tail) / len(tail)
    change = round((b - a) / a * 100.0, 1) if a else 0.0
    direction = "rising" if change > FLAT_PCT else "falling" if change < -FLAT_PCT else "flat"
    advice = {"rising": "buy now — the supplier price is going up",
              "falling": "wait or ask for a fresh quote — the supplier price is coming down",
              "flat": "no clear move"}[direction]
    return {"direction": direction, "change_pct": change, "days": span, "points": len(pts), "advice": advice}


async def trend(commodity: str | None) -> dict[str, Any] | None:
    if not commodity:
        return None
    return trend_of(await points(commodity))
