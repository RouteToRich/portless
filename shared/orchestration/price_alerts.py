"""Price alerts from the price book: buy on a dip, hold offers on a spike.

For every active catalogue product (else every HS code of an open prospect), the median
supplier FOB of the last ``PRICE_ALERT_RECENT_DAYS`` (14) is compared with the median of the
``PRICE_ALERT_BASE_DAYS`` (60) before that. A move of at least ``PRICE_ALERT_PCT`` (8 %) in
either direction is an alert, needing ``PRICE_ALERT_MIN_POINTS`` (3) observations on each side:

- **dip**   — supplier prices fell: a good moment to ask for firm quotes / lock a PO;
- **spike** — supplier prices rose: hold new buyer offers (or price them up) until it settles.

Listed in the daily digest and at ``GET /deals/price-alerts``. Read-only, never raises.
"""
from __future__ import annotations

import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compare(observations: list[Any], now: datetime | None = None) -> dict[str, Any] | None:
    """The alert for one HS code's supplier-side observations, or None."""
    now = now or datetime.now(timezone.utc)
    recent_days, base_days = _num("PRICE_ALERT_RECENT_DAYS", 14), _num("PRICE_ALERT_BASE_DAYS", 60)
    threshold, min_points = _num("PRICE_ALERT_PCT", 8.0), int(_num("PRICE_ALERT_MIN_POINTS", 3))
    cut = now - timedelta(days=recent_days)
    start = cut - timedelta(days=base_days)
    recent = [o.price_usd_per_mt for o in observations if _aware(o.observed_at) >= cut]
    base = [o.price_usd_per_mt for o in observations if start <= _aware(o.observed_at) < cut]
    if len(recent) < min_points or len(base) < min_points:
        return None
    now_med, base_med = statistics.median(recent), statistics.median(base)
    if base_med <= 0:
        return None
    change = (now_med - base_med) / base_med * 100.0
    if abs(change) < threshold:
        return None
    dip = change < 0
    return {
        "kind": "dip" if dip else "spike", "change_pct": round(change, 1),
        "recent_median_usd_per_mt": round(now_med, 2), "base_median_usd_per_mt": round(base_med, 2),
        "recent_points": len(recent), "base_points": len(base),
        "advice": ("supplier prices fell — ask for firm quotes / lock the PO now" if dip else
                   "supplier prices rose — hold new buyer offers or price them up until it settles"),
    }


async def _hs_codes() -> list[str]:
    codes: list[str] = []
    try:
        from shared import catalogue
        for line in catalogue.active_lines():
            if line.hs_code not in codes:
                codes.append(line.hs_code)
    except Exception as exc:  # noqa: BLE001
        logger.debug("PriceAlerts: catalogue unavailable: %s", exc)
    if not codes:
        try:
            from services.deals.repository import get_repository  # type: ignore[import]
            for d in await get_repository().list_active(limit=1000):
                status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
                if status in ("DRAFT", "MATCHING", "NEGOTIATING") and d.hs_code not in codes:
                    codes.append(d.hs_code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("PriceAlerts: deals unavailable: %s", exc)
    return codes


async def alerts(now: datetime | None = None) -> list[dict[str, Any]]:
    from shared import pricebook
    out = []
    for hs in await _hs_codes():
        try:
            a = compare(await pricebook.observations(hs, "supplier"), now)
        except Exception as exc:  # noqa: BLE001
            logger.debug("PriceAlerts: HS %s skipped: %s", hs, exc)
            continue
        if a:
            out.append({"hs_code": hs, **a})
    out.sort(key=lambda a: -abs(a["change_pct"]))
    return out
