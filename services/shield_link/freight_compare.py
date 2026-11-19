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


LCL_MAX_MT = 15.0


def _equipment(hs_code: str | None) -> tuple[list[dict[str, Any]], float | None]:
    """The box types to ask for (dry or reefer, per the catalogue) and the stowage factor."""
    from shared.orchestration.container_choice import EQUIPMENT
    kind, m3 = "dry", None
    try:
        from shared import catalogue
        line = catalogue.line_for(hs_code or "")
        if line is not None:
            kind = (line.container_type or "dry").lower()
            m3 = getattr(line, "m3_per_mt", None)
    except Exception:  # noqa: BLE001
        pass
    return EQUIPMENT.get(kind, EQUIPMENT["dry"]), m3


def cheapest_plan(quotes: list[dict[str, Any]], quantity_mt: float, m3_per_mt: float | None,
                  max_transit: int | None = None) -> dict[str, Any] | None:
    """Pure: per provider, the cheapest mix of the quoted box types for the lot (container_choice
    .plan_mix, payload / volume limits by box) or its LCL price; the cheapest provider plan wins.
    Cost per tonne decides — a 40' HC beats two 20' only when the cargo fills it."""
    from shared.orchestration.container_choice import EQUIPMENT, plan_mix, tonnes_per_box
    specs = {o["code"]: o for opts in EQUIPMENT.values() for o in opts}
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for q in quotes:
        if q.get("rate_usd") and not q.get("stub") and not q.get("error") and \
                (max_transit is None or q.get("transit_days") is None or q["transit_days"] <= max_transit):
            by_provider.setdefault(q["provider"], []).append(q)
    best: dict[str, Any] | None = None
    for provider, rows in by_provider.items():
        lcl = next((r for r in rows if r.get("equipment") == "LCL"), None)
        options = [{"code": r["equipment"], "tonnes": tonnes_per_box(specs[r["equipment"]], m3_per_mt),
                    "usd": r["rate_usd"]} for r in rows if r.get("equipment") in specs]
        plan = plan_mix(quantity_mt, options) if options else None
        cand = None
        if plan and plan.get("usd"):
            cand = {"provider": provider, "boxes": plan["boxes"], "usd": plan["usd"], "usd_per_mt": plan["usd_per_mt"],
                    "fill_pct": plan["fill_pct"]}
        if lcl and (cand is None or lcl["rate_usd"] < cand["usd"]):
            cand = {"provider": provider, "boxes": {"LCL": 1}, "usd": lcl["rate_usd"],
                    "usd_per_mt": round(lcl["rate_usd"] / quantity_mt, 2) if quantity_mt else None, "fill_pct": 100.0}
        if cand and (best is None or cand["usd"] < best["usd"]):
            best = cand
    return best


async def compare(*, origin: str | None, destination: str | None, market: str | None, hs_code: str | None,
                  container_type: str = "40HC", quantity_mt: float | None = None) -> dict[str, Any]:
    if not enabled():
        return {"enabled": False, "quotes": [], "chosen": None}
    names = providers()
    base = {"origin_port": origin or "", "destination_port": destination or "", "hs_code": hs_code or "",
            "cargo_weight_mt": quantity_mt}
    request = {**base, "container_type": container_type}
    jobs: list[tuple[str, str, dict[str, Any]]] = []
    equipment, m3_per_mt = _equipment(hs_code)
    if quantity_mt:  # one quote per box type (and LCL for a small lot) → cheapest per tonne
        for p in names:
            for opt in equipment:
                jobs.append((p, opt["code"], {**base, "container_type": opt["code"]}))
            if quantity_mt <= LCL_MAX_MT:
                jobs.append((p, "LCL", {**base, "container_type": "LCL", "load_type": "LCL",
                                        "volume_cbm": round(quantity_mt * (m3_per_mt or 1.5), 2)}))
    else:
        jobs = [(p, container_type, request) for p in names]
    results = await asyncio.gather(*(_one(p, req) for p, _, req in jobs)) if jobs else []
    quotes = []
    for (p, equip, _), q in zip(jobs, results):
        quotes.append({**q, "equipment": equip})
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
    plan = cheapest_plan(quotes, float(quantity_mt), m3_per_mt, max_transit) if quantity_mt else None
    chosen = None if plan else choose(quotes, max_transit)
    return {"enabled": True, "providers": names, "quotes": quotes,
            "chosen": plan["provider"] if plan else (chosen["provider"] if chosen else None),
            "chosen_rate_usd": (plan["usd"] if plan else (chosen["rate_usd"] if chosen else None)),
            "plan": plan, "max_transit_days": max_transit}
