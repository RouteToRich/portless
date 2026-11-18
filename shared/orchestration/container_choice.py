"""Which containers to ship a lot in: the lowest freight per tonne without wasting space
(todo: use the freight APIs to pick the container that costs least per tonne).

Options per cargo kind (payload in tonnes, internal volume in m³ — carrier typical values,
override with ``CONTAINER_<CODE>_PAYLOAD_T`` / ``_M3``):

  dry     20DRY 25 t / 33 m³ · 40DRY 26.5 t / 67 m³ · 40HC 26.5 t / 76 m³
  reefer  20RF  21 t / 28 m³ · 40RH  26 t / 67 m³

How many tonnes of *this* product fit in one box = min(payload, volume ÷ stowage factor), the
stowage factor being the catalogue's ``m3_per_mt`` (bagged rice ≈ 1.4 — weight-limited; dried
chilli ≈ 4 — volume-limited, where a 40' HC carries far more). The price of each box is the
lane's quote for that equipment (``freight_lanes``: a forwarder or staff quote keyed
``dry`` / ``dry40`` / ``hc40`` / ``reefer`` / ``reefer40``), else the 20' rate × ``CONTAINER_40_COST_FACTOR``
(1.4) for a 40', else the regional estimate. Every mix of up to two box types is tried; the
cheapest per tonne wins, its fill and the tonnes that would fill the last box are reported.
Never raises.
"""
from __future__ import annotations

import math
import os
from typing import Any

EQUIPMENT: dict[str, list[dict[str, Any]]] = {
    "dry": [
        {"code": "20DRY", "lane_key": "dry", "payload_t": 25.0, "m3": 33.0, "size": 20},
        {"code": "40DRY", "lane_key": "dry40", "payload_t": 26.5, "m3": 67.0, "size": 40},
        {"code": "40HC", "lane_key": "hc40", "payload_t": 26.5, "m3": 76.0, "size": 40},
    ],
    "reefer": [
        {"code": "20RF", "lane_key": "reefer", "payload_t": 21.0, "m3": 28.0, "size": 20},
        {"code": "40RH", "lane_key": "reefer40", "payload_t": 26.0, "m3": 67.0, "size": 40},
    ],
}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def tonnes_per_box(option: dict[str, Any], m3_per_mt: float | None) -> float:
    payload = _f(f"CONTAINER_{option['code']}_PAYLOAD_T", option["payload_t"])
    volume = _f(f"CONTAINER_{option['code']}_M3", option["m3"])
    if m3_per_mt and m3_per_mt > 0:
        return round(min(payload, volume / m3_per_mt), 2)
    return payload


def plan_mix(quantity_mt: float, options: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure: options = [{code, tonnes, usd}] → the cheapest mix of at most two types."""
    qty = max(0.0, float(quantity_mt or 0.0))
    usable = [o for o in options if o.get("tonnes", 0) > 0 and o.get("usd", 0) > 0]
    if qty <= 0 or not usable:
        return {"boxes": {}, "usd": 0.0, "usd_per_mt": None, "capacity_mt": 0.0, "fill_pct": 0.0,
                "fill_suggestion_mt": 0.0, "options": usable}
    best: tuple[float, dict[str, int]] | None = None
    for a in usable:
        for b in usable:
            if b["code"] < a["code"]:
                continue
            for na in range(0, int(math.ceil(qty / a["tonnes"])) + 1):
                rest = max(0.0, qty - na * a["tonnes"])
                nb = int(math.ceil(rest / b["tonnes"] - 1e-9)) if a["code"] != b["code"] else 0
                if a["code"] == b["code"]:
                    if na * a["tonnes"] + 1e-9 < qty:
                        continue
                cost = na * a["usd"] + nb * b["usd"]
                boxes = {k: v for k, v in ((a["code"], na), (b["code"], nb)) if v}
                if best is None or cost < best[0] - 1e-6 or (abs(cost - best[0]) < 1e-6 and sum(boxes.values()) < sum(best[1].values())):
                    best = (cost, boxes)
    cost, boxes = best  # type: ignore[misc]
    by_code = {o["code"]: o for o in usable}
    capacity = sum(by_code[c]["tonnes"] * n for c, n in boxes.items())
    fill = round(qty / capacity * 100.0, 1) if capacity else 0.0
    spare = round(capacity - qty, 2)
    return {"boxes": boxes, "usd": round(cost, 2), "usd_per_mt": round(cost / qty, 2), "capacity_mt": round(capacity, 2),
            "fill_pct": fill, "fill_suggestion_mt": spare if fill < 75.0 and spare > 0 else 0.0,
            "usd_per_mt_if_filled": round(cost / capacity, 2) if capacity else None, "options": usable}


async def _lcl_option(market: str | None, kind: str, qty: float, m3_per_mt: float | None,
                      base20: float | None) -> dict[str, Any] | None:
    """A part-container (LCL) load for a small lot: charged per freight tonne — whichever is more
    of tonnes and cubic metres (W/M) — from a lane quote ``lcl`` (USD per W/M), else the 20' rate
    per tonne × ``LCL_PREMIUM`` (1.8); never above ``LCL_MAX_MT`` (12) and never for reefer."""
    if kind != "dry" or qty <= 0 or qty > _f("LCL_MAX_MT", 12.0):
        return None
    rate, source = None, "estimate"
    try:
        from services.trade_min import freight_lanes  # type: ignore[import]
        lane = await freight_lanes.lane(market or "", "lcl")
        if lane is not None:
            rate, source = lane.usd_per_container, lane.provider
    except Exception:  # noqa: BLE001
        pass
    if rate is None and base20:
        rate = base20 / EQUIPMENT["dry"][0]["payload_t"] * _f("LCL_PREMIUM", 1.8)
    if not rate:
        return None
    freight_tonnes = max(qty, qty * (m3_per_mt or 1.0))
    usd = max(rate * freight_tonnes, _f("LCL_MINIMUM_USD", 150.0))
    return {"usd": round(usd, 2), "usd_per_wm": round(rate, 2), "freight_tonnes": round(freight_tonnes, 2),
            "source": source}


async def choose(market: str | None, hs_code: str | None, quantity_mt: float) -> dict[str, Any]:
    """The plan for a lot on a lane, from the lane quotes and the product's stowage factor."""
    kind, m3_per_mt = "dry", None
    try:
        from shared import catalogue
        line = catalogue.line_for(hs_code or "")
        if line is not None:
            kind = (line.container_type or "dry").lower()
            m3_per_mt = getattr(line, "m3_per_mt", None)
    except Exception:  # noqa: BLE001
        pass
    kind = kind if kind in EQUIPMENT else "dry"
    base20: float | None = None
    rates: dict[str, tuple[float, str]] = {}
    try:
        from services.trade_min import freight_lanes  # type: ignore[import]
        for opt in EQUIPMENT[kind]:
            lane = await freight_lanes.lane(market or "", opt["lane_key"])
            if lane is not None:
                rates[opt["code"]] = (lane.usd_per_container, lane.provider)
        per_mt, _basis = await freight_lanes.freight_per_mt(market or "", kind)
        base20 = per_mt * EQUIPMENT[kind][0]["payload_t"] if per_mt else None
    except Exception:  # noqa: BLE001
        pass
    first = EQUIPMENT[kind][0]["code"]
    if first in rates:
        base20 = rates[first][0]
    options = []
    for opt in EQUIPMENT[kind]:
        usd, source = rates.get(opt["code"], (None, None))
        if usd is None and base20:
            usd = base20 * (_f("CONTAINER_40_COST_FACTOR", 1.4) if opt["size"] == 40 else 1.0)
            source = "estimate"
        options.append({"code": opt["code"], "tonnes": tonnes_per_box(opt, m3_per_mt), "usd": round(usd or 0.0, 2),
                        "source": source or "none"})
    plan = plan_mix(quantity_mt, options)
    lcl = await _lcl_option(market, kind, float(quantity_mt or 0), m3_per_mt, base20)
    if lcl and (plan.get("usd") in (None, 0) or lcl["usd"] < plan["usd"]):
        plan.update({"boxes": {"LCL": 1}, "usd": lcl["usd"], "usd_per_mt": round(lcl["usd"] / quantity_mt, 2),
                     "capacity_mt": float(quantity_mt), "fill_pct": 100.0, "fill_suggestion_mt": 0.0,
                     "usd_per_mt_if_filled": None, "lcl": lcl})
    elif lcl:
        plan["lcl_alternative"] = lcl
    plan.update({"kind": kind, "m3_per_mt": m3_per_mt, "market": market,
                 "quoted": sorted(c for c in rates), "estimated": sorted(o["code"] for o in options if o["source"] == "estimate")})
    return plan
