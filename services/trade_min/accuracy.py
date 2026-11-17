"""How the radar's estimates compared with what deals really earned — and the correction.

Every deal started from an opportunity keeps the radar's numbers unchanged
(``DealRecord.radar_estimate``). Once a deal is settled, its realised margin
(``realized_margin_usd``, patched at escrow settlement) sits next to that estimate.
Per product: realised ÷ estimated margin, the median over the settled deals. With at
least ``MIN_DEALS`` deals of the product (else ``MIN_DEALS_ALL`` over every product) the
radar multiplies its margin estimate by that factor (clamped 0.5–1.2) and says so.

``GET /trade-min/profit/accuracy`` lists every compared deal and the factors.
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Any

logger = logging.getLogger(__name__)

MIN_DEALS = 3
MIN_DEALS_ALL = 5
_TTL = 600.0
_cache: tuple[float, dict[str, Any]] | None = None


def reset_for_tests() -> None:
    global _cache
    _cache = None


async def _deals() -> list[Any]:
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        return await deals_repo().list_active(limit=2000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Accuracy: deals unavailable: %s", exc)
        return []


def compare(deals: list[Any]) -> dict[str, Any]:
    """Pure: the settled deals with a radar estimate → rows + factor per HS code and overall."""
    rows: list[dict[str, Any]] = []
    for d in deals:
        est = getattr(d, "radar_estimate", None) or {}
        realised = getattr(d, "realized_margin_usd", None)
        per_mt = est.get("margin_usd_per_mt")
        qty = getattr(d, "quantity_mt", None) or est.get("lot_mt")
        if realised is None or not per_mt or not qty or per_mt <= 0:
            continue
        estimated = per_mt * qty
        rows.append({
            "deal_id": d.deal_id, "hs_code": d.hs_code, "market": d.market,
            "estimated_margin_usd": round(estimated, 2), "realised_margin_usd": round(float(realised), 2),
            "ratio": round(float(realised) / estimated, 3),
            "supplier_price_basis": est.get("supplier_price_basis"),
        })
    by_hs: dict[str, list[float]] = {}
    for r in rows:
        by_hs.setdefault(r["hs_code"], []).append(r["ratio"])
    factors = {hs: {"deals": len(v), "median_ratio": round(statistics.median(v), 3)} for hs, v in by_hs.items()}
    overall = {"deals": len(rows), "median_ratio": round(statistics.median([r["ratio"] for r in rows]), 3)} if rows else None
    return {"rows": rows, "by_hs": factors, "overall": overall}


async def summary() -> dict[str, Any]:
    global _cache
    if _cache and time.monotonic() - _cache[0] < _TTL:
        return _cache[1]
    out = compare(await _deals())
    _cache = (time.monotonic(), out)
    return out


async def factor_for(hs_code: str) -> tuple[float | None, str | None]:
    """(factor, basis) to apply to the margin estimate, or (None, None) without enough history."""
    s = await summary()
    hs = s["by_hs"].get(hs_code)
    if hs and hs["deals"] >= MIN_DEALS:
        f, basis = hs["median_ratio"], f"{hs['deals']} settled deals of HS {hs_code}"
    elif s["overall"] and s["overall"]["deals"] >= MIN_DEALS_ALL:
        f, basis = s["overall"]["median_ratio"], f"{s['overall']['deals']} settled deals (all products)"
    else:
        return None, None
    f = max(0.5, min(1.2, f))
    return round(f, 3), f"realised ÷ estimated margin ×{f:.2f} on {basis}"
