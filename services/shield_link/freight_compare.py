"""Freight quotes from more than one source (todo 11.2): book the cheapest that meets the date.

Before the ``BOOK_FREIGHT`` gate is opened, every freight provider with real credentials
(``FREIGHT_COMPARE_PROVIDERS``, default ``maersk,cogoport,freightos``; a provider whose key is
unset or a ``your_..._here`` placeholder is skipped) is asked for a rate on the lane. A quote
counts when it has a price, is not a sandbox stub, and its transit time fits: transit ≤
``FREIGHT_MAX_TRANSIT_DAYS`` (unset = any). The cheapest counting quote names the provider in the
gate's payload, with every quote and the lane-index check (freight_lanes.check_quote) beside it,
so the operator sees why. No counting quote → the gate keeps ``FREIGHT_PROVIDER`` as before.
``FREIGHT_COMPARE_QUOTES=false`` turns it off. ``GET /shield-link/deals/{id}/freight-quotes``.
Never raises.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_KEYS = {"maersk": "MAERSK_CONSUMER_KEY", "cogoport": "COGOPORT_API_KEY", "freightos": "FREIGHTOS_API_KEY"}


def enabled() -> bool:
    return os.getenv("FREIGHT_COMPARE_QUOTES", "true").strip().lower() not in ("0", "false", "no", "off")


def providers() -> list[str]:
    names = [p.strip().lower() for p in os.getenv("FREIGHT_COMPARE_PROVIDERS", "maersk,cogoport,freightos").split(",")]
    out = []
    for p in names:
        key = os.getenv(_KEYS.get(p, ""), "").strip()
        if p in _KEYS and key and not key.startswith("your_"):
            out.append(p)
    return out


def _rate(q: dict[str, Any]) -> float | None:
    for k in ("rate_usd", "total_usd", "price_usd", "amount"):
        v = q.get(k)
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    rates = q.get("rates")
    if isinstance(rates, list) and rates:
        vals = [r for r in (_rate(x) for x in rates if isinstance(x, dict)) if r]
        return min(vals) if vals else None
    return None


def _transit(q: dict[str, Any]) -> int | None:
    for k in ("transit_days", "transit_time_days", "transitTime"):
        try:
            if q.get(k) is not None:
                return int(float(q[k]))
        except (TypeError, ValueError):
            continue
    return None


def choose(quotes: list[dict[str, Any]], max_transit: int | None = None) -> dict[str, Any] | None:
    """Pure: the cheapest priced, non-stub quote whose transit fits."""
    ok = [q for q in quotes if q.get("rate_usd") and not q.get("stub") and not q.get("error")
          and (max_transit is None or q.get("transit_days") is None or q["transit_days"] <= max_transit)]
    return min(ok, key=lambda q: q["rate_usd"]) if ok else None


async def _one(provider: str, request: dict[str, Any]) -> dict[str, Any]:
    from shared.connectors.shields import get_freight_connector
    try:
        q = await asyncio.wait_for(get_freight_connector(provider).quote(request), timeout=30)
        return {"provider": provider, "rate_usd": _rate(q or {}), "transit_days": _transit(q or {}),
                "valid_until": (q or {}).get("valid_until"), "stub": bool((q or {}).get("stub")),
                "source": (q or {}).get("source")}
    except Exception as exc:  # noqa: BLE001 - one provider down never stops the others
        return {"provider": provider, "rate_usd": None, "error": str(exc)[:200]}


async def compare(*, origin: str | None, destination: str | None, market: str | None, hs_code: str | None,
                  container_type: str = "40HC", quantity_mt: float | None = None) -> dict[str, Any]:
    if not enabled():
        return {"enabled": False, "quotes": [], "chosen": None}
    names = providers()
    request = {"origin_port": origin or "", "destination_port": destination or "", "hs_code": hs_code or "",
               "container_type": container_type, "cargo_weight_mt": quantity_mt}
    quotes = list(await asyncio.gather(*(_one(p, request) for p in names))) if names else []
    try:
        max_transit = int(os.getenv("FREIGHT_MAX_TRANSIT_DAYS", "") or 0) or None
    except ValueError:
        max_transit = None
    if market:
        from services.trade_min import freight_lanes  # type: ignore[import]
        kind = "reefer" if "RF" in (container_type or "").upper() else "dry"
        for q in quotes:
            if q.get("rate_usd"):
                q["lane_check"] = await freight_lanes.check_quote(market, q["rate_usd"], kind)
    chosen = choose(quotes, max_transit)
    return {"enabled": True, "providers": names, "quotes": quotes,
            "chosen": chosen["provider"] if chosen else None,
            "chosen_rate_usd": chosen["rate_usd"] if chosen else None,
            "max_transit_days": max_transit}
