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


# Main harvest (peak arrival) months per Agmarknet commodity — arrivals peak and mandi prices
# are usually lowest then; the months before the harvest are the lean season (todo 16.3).
# India's main crop calendar (kharif / rabi); one entry per commodity name the mandi connector
# uses (shared/connectors/mandi.py HS_TO_COMMODITY). Override with
# HARVEST_MONTHS_<COMMODITY_KEY>="2,3,4" (key = name upper-cased, non-letters → _).
HARVEST_MONTHS: dict[str, tuple[int, ...]] = {
    "Black pepper": (12, 1, 2),
    "Dry Chillies": (1, 2, 3, 4),
    "Cardamoms": (8, 9, 10, 11),
    "Corriander seed": (2, 3, 4),
    "Cummin Seed(Jeera)": (2, 3, 4),
    "Dry Ginger": (12, 1, 2),
    "Turmeric": (2, 3, 4),
    "Methi Seeds": (2, 3, 4),
    "Paddy(Dhan)(Common)": (10, 11, 12),
    "Rice": (10, 11, 12),
    "Wheat": (3, 4, 5),
    "Sesamum(Sesame,Gingelly,Til)": (9, 10, 11),
    "Onion": (3, 4, 5, 11, 12),
    "Potato": (1, 2, 3),
    "Garlic": (3, 4, 5),
    "Groundnut": (10, 11, 12),
    "Soyabean": (10, 11),
    "Mango": (4, 5, 6),
    "Banana": tuple(range(1, 13)),
}
LEAN_MONTHS_BEFORE = 2


def harvest_months(commodity: str | None) -> tuple[int, ...]:
    import os
    import re
    if not commodity:
        return ()
    key = re.sub(r"[^A-Z]+", "_", commodity.upper()).strip("_")
    raw = os.getenv(f"HARVEST_MONTHS_{key}", "").strip()
    if raw:
        try:
            months = tuple(int(x) for x in raw.split(",") if x.strip())
            if months and all(1 <= m <= 12 for m in months):
                return months
        except ValueError:
            pass
    return HARVEST_MONTHS.get(commodity, ())


def season_of(commodity: str | None, on: date | None = None) -> dict[str, Any] | None:
    """Pure: where the month sits in the commodity's crop year — ``harvest`` (arrivals peak,
    prices usually lowest: a good time to fix a supplier price), ``lean`` (the
    ``LEAN_MONTHS_BEFORE`` months before the harvest: prices usually highest, stocks thin) or
    ``between``. None when the calendar does not know the commodity."""
    months = harvest_months(commodity)
    if not months:
        return None
    month = (on or datetime.now(timezone.utc).date()).month
    if len(set(months)) == 12:
        phase = "year_round"
    elif month in months:
        phase = "harvest"
    elif any(((h - month) % 12) in range(1, LEAN_MONTHS_BEFORE + 1) for h in months):
        phase = "lean"
    else:
        phase = "between"
    advice = {
        "harvest": "harvest arrivals — mandi prices are usually lowest; a good time to fix the supplier price",
        "lean": "lean season before the harvest — prices are usually highest and stocks thin; "
                "a short validity or a later shipment may cost less",
        "between": "after the harvest — prices usually firm up as stocks are used",
        "year_round": "harvested all year — no strong season",
    }[phase]
    return {"phase": phase, "harvest_months": list(months), "advice": advice}


async def trend(commodity: str | None) -> dict[str, Any] | None:
    """The price trend from PortLess's own history, with the crop-calendar season. Before a
    week of history the season alone is returned (``direction: "unknown"``)."""
    if not commodity:
        return None
    pts = await points(commodity)
    out = trend_of(pts)
    season = season_of(commodity)
    if out is None and season is None:
        return None
    if out is None:
        out = {"direction": "unknown", "change_pct": None, "days": 0, "points": len(pts),
               "advice": season["advice"] if season else "no clear move"}
    if season is not None:
        out["season"] = season
    return out


# Agmarknet's historical, variety-wise daily prices on data.gov.in — the backfill source so the
# trend works from day one instead of after a week of PortLess's own daily points. Resource id
# and field names (Arrival_Date dd/mm/yyyy, Modal_Price INR / quintal) follow the published
# dataset; verify on live data (MANDI_HISTORY_RESOURCE_ID overrides the id).
HISTORY_RESOURCE_ID = "35985678-0d79-46b4-9ed6-6f13308a1d24"


def _history_days(records: list[dict[str, Any]]) -> dict[str, float]:
    """Median modal price per day (ISO date) from historical records (pure)."""
    import statistics
    per_day: dict[str, list[float]] = {}
    for r in records:
        raw = str(r.get("Arrival_Date") or r.get("arrival_date") or "").strip()
        try:
            d, m, y = (int(x) for x in raw.replace("-", "/").split("/")[:3])
            day = date(y, m, d).isoformat()
        except (ValueError, TypeError):
            continue
        try:
            modal = float(r.get("Modal_Price") or r.get("modal_price") or r.get("Modal_x0020_Price") or 0)
        except (TypeError, ValueError):
            continue
        if modal > 0:
            per_day.setdefault(day, []).append(modal)
    return {d: round(statistics.median(v), 2) for d, v in per_day.items()}


async def backfill(commodity: str, *, days: int = WINDOW_DAYS, transport: Any = None) -> dict[str, Any]:
    """Fill the last ``days`` of history for a commodity from the historical dataset (staff,
    ``POST /trade-min/mandi/{hs_code}/backfill``). Existing days are kept (a live fetch wins).
    Needs ``DATA_GOV_IN_API_KEY``. Never raises."""
    import os

    import httpx
    key = os.getenv("DATA_GOV_IN_API_KEY", "").strip()
    if not key or key.startswith("your_"):
        return {"commodity": commodity, "added": 0, "reason": "DATA_GOV_IN_API_KEY not set"}
    resource = os.getenv("MANDI_HISTORY_RESOURCE_ID", HISTORY_RESOURCE_ID)
    params = {"api-key": key, "format": "json", "limit": "10000", "filters[Commodity]": commodity}
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as client:
            resp = await client.get(f"https://api.data.gov.in/resource/{resource}", params=params)
        if resp.status_code != 200:
            return {"commodity": commodity, "added": 0, "reason": f"HTTP {resp.status_code}"}
        records = resp.json().get("records") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Mandi history: backfill failed for %s: %s", commodity, exc)
        return {"commodity": commodity, "added": 0, "reason": str(exc)}
    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    have = {p["day"] for p in await points(commodity)}
    added = 0
    for day, modal in sorted(_history_days(records).items()):
        if day in have or date.fromisoformat(day) < since:
            continue
        await record({"commodity": commodity, "modal_inr_per_quintal": modal, "markets": None},
                     day=date.fromisoformat(day))
        added += 1
    return {"commodity": commodity, "added": added, "records": len(records)}
