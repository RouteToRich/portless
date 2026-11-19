"""Scan planning — where to spend the Apify budget (todo 23.2, P2).

Each day the plan says, per catalogue corridor (product line × market), whether to **scan**
(never scanned / its opportunity expired), **rescan** (a listed opportunity older than
``SCAN_PLAN_RESCAN_DAYS`` (7) whose expected profit is worth the run), or **skip** (dropped for
export policy / no demand in the last scan, or the expected profit does not pay for the runs).
Corridors are ranked by expected profit per Apify dollar (the radar's expected profit ÷ the
scan cost the corridor took last time, else ``SCAN_PLAN_DEFAULT_COST_USD`` 0.6), and the plan
stops at ``APIFY_MAX_USD_PER_DAY`` minus what was spent today. Code decides; staff see the plan
(``GET /trade-min/scan/plan``) and run it with ``POST /trade-min/scan`` using ``scan_request``.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

_POLICY_DROP = ("export policy", "prohibited", "embargo")
_NO_DEMAND = ("no live demand", "no measured price gap")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def plan_rows(corridors: list[tuple[str, str]], opportunities: list[Any], last_report: Any,
              *, budget_usd: float | None, now: datetime) -> list[dict[str, Any]]:
    """Pure: one row per corridor with action, reason, expected profit, cost and value per dollar."""
    rescan_after = timedelta(days=_f("SCAN_PLAN_RESCAN_DAYS", 7))
    default_cost = _f("SCAN_PLAN_DEFAULT_COST_USD", 0.6)
    min_value = _f("SCAN_PLAN_MIN_PROFIT_PER_USD", 100.0)
    by_corridor: dict[tuple[str, str], Any] = {}
    for o in opportunities:
        key = (str(o.hs_code)[:6], str(o.market).upper())
        if key not in by_corridor or _aware(o.scanned_at) > _aware(by_corridor[key].scanned_at):
            by_corridor[key] = o
    dropped = {(str(c.hs_code)[:6], str(c.market).upper()): c.reason
               for c in (getattr(last_report, "corridors", None) or []) if c.result == "dropped"}
    rows: list[dict[str, Any]] = []
    for hs, market in corridors:
        key = (hs[:6], market.upper())
        opp = by_corridor.get(key)
        cost = default_cost
        if opp is not None and (opp.scan_cost or {}).get("usd"):
            cost = max(0.05, float(opp.scan_cost["usd"]))
        profit = None
        if opp is not None and getattr(opp, "profit", None) is not None:
            profit = getattr(opp.profit, "expected_profit_usd", None)
        reason_drop = (dropped.get(key) or "").lower()
        if any(w in reason_drop for w in _POLICY_DROP):
            action, why = "skip", f"last scan: {dropped[key]}"
        elif opp is None and any(w in reason_drop for w in _NO_DEMAND):
            action, why = "skip", f"last scan: {dropped[key]} — rescan when a national source or the directory adds demand"
        elif opp is None:
            action, why = "scan", "never scanned or no listed opportunity"
        elif now - (_aware(opp.scanned_at) or now) >= rescan_after:
            if profit is not None and profit / cost < min_value:
                action, why = "skip", f"expected profit {profit:,.0f} USD does not pay for the runs"
            else:
                action, why = "rescan", f"last scanned {(now - _aware(opp.scanned_at)).days} days ago"
        else:
            action, why = "skip", "scanned recently"
        rows.append({"hs_code": hs, "market": market.upper(), "action": action, "reason": why,
                     "expected_profit_usd": profit, "cost_usd": round(cost, 2),
                     "profit_per_usd": round(profit / cost, 1) if profit else None,
                     "opportunity_id": getattr(opp, "opportunity_id", None)})
    order = {"rescan": 0, "scan": 1, "skip": 2}
    rows.sort(key=lambda r: (order[r["action"]], -(r["profit_per_usd"] or 0)))
    spent = 0.0
    for r in rows:
        if r["action"] == "skip":
            continue
        if budget_usd is not None and spent + r["cost_usd"] > budget_usd:
            r["action"], r["reason"] = "skip", "over today's Apify budget — next run"
            continue
        spent += r["cost_usd"]
    return rows


async def build_plan(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    from shared import catalogue
    from shared.connectors import apify
    from . import scan_reports
    from .config import DEFAULT_CONFIG
    from .repository import get_repository
    lines = catalogue.active_lines()
    if lines:
        corridors = [(line.hs_code, m) for line in lines for m in (line.markets or [])]
    else:
        corridors = [(h, m) for h in DEFAULT_CONFIG.default_hs_codes for m in DEFAULT_CONFIG.default_markets]
    repo = await get_repository()
    opps = await repo.list_active(limit=1000, min_rank=0.0)
    report = await scan_reports.latest()
    cap = apify.max_usd_per_day()
    usage = await apify.usage_today()
    budget = max(0.0, cap - float(usage.get("usd_today") or 0.0)) if cap else None
    rows = plan_rows(corridors, opps, report, budget_usd=budget, now=now)
    todo = [r for r in rows if r["action"] in ("scan", "rescan")]
    return {
        "at": now.isoformat(), "budget_left_usd": budget, "planned_cost_usd": round(sum(r["cost_usd"] for r in todo), 2),
        "corridors": rows,
        "scan_request": {"hs_codes": sorted({r["hs_code"] for r in todo}),
                         "target_markets": sorted({r["market"] for r in todo})} if todo else None,
    }
