"""India's own export statistics (DGCI&S TradeStat via actor A.1 ``india-export-stats-ai``) — todo 17.1.

Monthly rows per 8-digit ITC-HS × destination (value USD, quantity MT, unit value) are kept in Mongo
``india_export_stats`` and used for:

1. **What a market pays for India's goods this year** — the last 12 months' unit value replaces the
   radar's ``india_to_market`` anchor when it is fresher than the Comtrade / national figure;
2. **a supplier-side check** — a supplier's FOB against India's average FOB to that destination
   (``supplier_check``: more than ``SOURCE_DISAGREE_PCT`` (25) apart is flagged);
3. **whether India already ships the product there** (``india_ships_already`` / ``india_not_shipping``);
4. **the trend** — the last 6 months' tonnes against the 6 before (``india_exports_rising`` / ``_falling``).

**Mirror data** (todo 17.1): when the market's own import figures are missing or older than two years
(UAE data was from 2023), India's export figure to it becomes the buyer-price anchor
(``price_benchmark_source=india_exports:mirror``, flag ``mirror_data``) — never an invented number.

``monthly`` (deal watcher) refreshes the catalogue's products × markets once the actor is built
(``APIFY_ACTOR_INDIA_EXPORT_STATS_BUILT``); ``INDIA_EXPORTS_ENABLED`` pins it off in tests.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_rows: dict[str, dict[str, Any]] = {}
_last: dict[str, datetime] = {}


def enabled() -> bool:
    return os.getenv("INDIA_EXPORTS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("india_export_stats")
    except Exception:  # noqa: BLE001
        return None


async def apply(rows: list[dict[str, Any]]) -> int:
    col = _collection()
    n = 0
    for r in rows:
        if not (r.get("hs_code") and r.get("country") and r.get("period")):
            continue
        key = f"{r['hs_code']}:{r['country']}:{r['period']}"
        _rows[key] = {**r, "key": key}
        n += 1
        if col is not None:
            try:
                await col.replace_one({"key": key}, dict(_rows[key]), upsert=True)
            except Exception:  # noqa: BLE001
                pass
    return n


async def load() -> None:
    col = _collection()
    if col is None:
        return
    try:
        for d in [d async for d in col.find({}, {"_id": 0})]:
            _rows[d["key"]] = d
    except Exception:  # noqa: BLE001
        pass


def latest(hs_code: str, market: str) -> dict[str, Any] | None:
    """Pure over the cache: the last 12 months to the market for the product (6-digit match)."""
    h6 = "".join(ch for ch in str(hs_code) if ch.isdigit())[:6]
    months = sorted((r for r in _rows.values() if str(r["hs_code"])[:6] == h6 and r["country"] == (market or "").upper()
                     and len(str(r["period"])) == 7), key=lambda r: r["period"])
    if not months:
        return None
    by_period: dict[str, list[dict[str, Any]]] = {}
    for r in months:
        by_period.setdefault(r["period"], []).append(r)
    periods = sorted(by_period)[-12:]
    rows = [r for p in periods for r in by_period[p]]
    value = sum(r.get("value_usd") or 0 for r in rows)
    mt = sum(r.get("quantity_mt") or 0 for r in rows)
    tonnes = [sum(r.get("quantity_mt") or 0 for r in by_period[p]) for p in periods]
    trend = None
    if len(tonnes) >= 12 and sum(tonnes[:6]) > 0:
        trend = round((sum(tonnes[6:]) - sum(tonnes[:6])) / sum(tonnes[:6]) * 100.0, 1)
    return {"period": f"{periods[0]}..{periods[-1]}", "period_to": periods[-1], "months": len(periods),
            "value_usd": round(value, 2), "quantity_mt": round(mt, 2),
            "unit_value_usd_per_mt": round(value / mt, 2) if mt else None, "trend_pct": trend,
            "ships_already": mt > 0}


def flags_for(stats: dict[str, Any] | None) -> list[str]:
    if not stats:
        return []
    out = ["india_ships_already" if stats["ships_already"] else "india_not_shipping"]
    if stats.get("trend_pct") is not None:
        if stats["trend_pct"] >= 15:
            out.append("india_exports_rising")
        elif stats["trend_pct"] <= -15:
            out.append("india_exports_falling")
    return out


def stale(period: str | None, now: datetime | None = None) -> bool:
    """Pure: a market figure missing or older than two years is stale (mirror data is used)."""
    if not period:
        return True
    year = str(period)[:4]
    return not year.isdigit() or int(year) < (now or datetime.now(timezone.utc)).year - 2


def supplier_check(hs_code: str, market: str, fob_usd_per_mt: float | None) -> dict[str, Any] | None:
    """A supplier's FOB against India's average FOB to the destination (None without both)."""
    stats = latest(hs_code, market)
    if not stats or not stats.get("unit_value_usd_per_mt") or not fob_usd_per_mt:
        return None
    ref = stats["unit_value_usd_per_mt"]
    diff = round((fob_usd_per_mt - ref) / ref * 100.0, 1)
    limit = float(os.getenv("SOURCE_DISAGREE_PCT", "25") or 25)
    return {"india_fob_usd_per_mt": ref, "supplier_fob_usd_per_mt": fob_usd_per_mt, "difference_pct": diff,
            "flag": ("supplier_far_below_india_average" if diff < -limit else
                     "supplier_far_above_india_average" if diff > limit else None), "period": stats["period"]}


def apply_to_phase1(result: Any, hs_code: str, market: str, now: datetime | None = None) -> None:
    """Fill the radar's phase-1 result from India's own figures (anchor, mirror, flags). Never raises."""
    try:
        stats = latest(hs_code, market)
        if not stats or not stats.get("unit_value_usd_per_mt"):
            return
        uv = stats["unit_value_usd_per_mt"]
        result.india_to_market_usd_per_mt = uv
        result.market_data_flags = list(dict.fromkeys([*result.market_data_flags, *flags_for(stats)]))
        if stale(result.data_period, now):
            result.price_benchmark_usd_per_mt = uv
            result.price_benchmark_source = "india_exports:mirror"
            result.data_period = stats["period_to"]
            result.market_data_flags = list(dict.fromkeys([*result.market_data_flags, "mirror_data"]))
    except Exception as exc:  # noqa: BLE001
        logger.debug("IndiaExports: phase 1 not filled for %s×%s: %s", hs_code, market, exc)


async def monthly(now: datetime | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    if _last.get("run") and now - _last["run"] < timedelta(days=30):
        return None
    _last["run"] = now
    from shared import catalogue
    from shared.connectors.registry import get_connector
    lines = catalogue.active_lines()
    res = await get_connector("apify_india_export_stats").quote({
        "hs_codes": sorted({str(getattr(line, "hs_code", "")) for line in lines}),
        "countries": sorted({m for line in lines for m in (getattr(line, "markets", None) or [])})})
    if res.get("stub"):
        return {"stub": True}
    return {"stored": await apply(res.get("rows", []))}


def reset_for_tests() -> None:
    _rows.clear()
    _last.clear()
