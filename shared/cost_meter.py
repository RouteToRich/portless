"""What running PortLess costs per day, platform-wide (todo: cost dashboards — LLM, Apify, Tavily,
Atlas, hosting).

``shared.deal_costs`` books costs to a deal; scans, the radar and the directory spend outside any
deal. Every LLM call, Apify run and Tavily search is also counted here per UTC day (Mongo
``platform_costs``, one document per day, ``$inc``; memory without Mongo). Fixed monthly costs
are settings: ``COST_FIXED_MONTHLY_USD="atlas:57,hosting:25,domain:2"``, spread per day.

``report(days)`` backs ``GET /health/costs``: per day and in total — LLM calls / tokens / USD,
Apify runs / USD (with the daily run budget), Tavily searches / USD (``TAVILY_USD_PER_SEARCH``,
0.008), fixed costs, what share was booked to deals, and the cost per completed deal. Writes never
raise and never block.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

FIELDS = ("llm_calls", "llm_tokens", "llm_usd", "apify_runs", "apify_usd", "tavily_searches", "tavily_usd")
_memory: dict[str, dict[str, float]] = {}
_tasks: set[asyncio.Task] = set()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("platform_costs")
    except Exception:  # noqa: BLE001
        return None


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def add(*, now: datetime | None = None, **amounts: float) -> None:
    """Count spend for today. Never raises."""
    inc = {k: float(v) for k, v in amounts.items() if k in FIELDS and v}
    if not inc:
        return
    day = _today(now)
    row = _memory.setdefault(day, {})
    for k, v in inc.items():
        row[k] = round(row.get(k, 0.0) + v, 6)
    col = _collection()
    if col is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(
            col.update_one({"_id": day}, {"$inc": inc, "$set": {"day": day}}, upsert=True))
        _tasks.add(task)
        task.add_done_callback(lambda t: (_tasks.discard(t), t.cancelled() or t.exception()))
    except RuntimeError:
        pass


def tavily_search() -> None:
    add(tavily_searches=1, tavily_usd=_f("TAVILY_USD_PER_SEARCH", 0.008))


def fixed_monthly() -> dict[str, float]:
    out: dict[str, float] = {}
    for part in os.getenv("COST_FIXED_MONTHLY_USD", "").split(","):
        if ":" not in part:
            continue
        name, _, value = part.partition(":")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            continue
    return out


async def _days(days: int, now: datetime) -> dict[str, dict[str, float]]:
    wanted = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    out = {d: dict(_memory.get(d) or {}) for d in wanted}
    col = _collection()
    if col is not None:
        try:
            async for doc in col.find({"_id": {"$in": wanted}}):
                out[str(doc["_id"])] = {k: float(doc.get(k) or 0.0) for k in FIELDS}
        except Exception as exc:  # noqa: BLE001
            logger.debug("CostMeter: unreadable: %s", exc)
    return out


async def report(days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    days = max(1, min(int(days), 366))
    per_day = await _days(days, now)
    fixed = fixed_monthly()
    fixed_per_day = round(sum(fixed.values()) * 12 / 365, 4)
    rows = []
    for day in sorted(per_day):
        r = {k: round(per_day[day].get(k, 0.0), 4) for k in FIELDS}
        r["day"] = day
        r["fixed_usd"] = fixed_per_day
        r["total_usd"] = round(r["llm_usd"] + r["apify_usd"] + r["tavily_usd"] + fixed_per_day, 2)
        rows.append(r)
    totals = {k: round(sum(r[k] for r in rows), 4) for k in FIELDS}
    totals["fixed_usd"] = round(fixed_per_day * days, 2)
    totals["variable_usd"] = round(totals["llm_usd"] + totals["apify_usd"] + totals["tavily_usd"], 2)
    totals["total_usd"] = round(totals["variable_usd"] + totals["fixed_usd"], 2)
    booked = 0.0
    try:
        from shared import deal_costs
        booked = round(sum(r.get("tool_usd", 0.0) for r in await deal_costs.all_summaries()), 2)
    except Exception:  # noqa: BLE001
        pass
    completed = 0
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        since = now - timedelta(days=days)
        for d in await get_repository().list_active(limit=5000):
            st = str(getattr(d.status, "value", d.status))
            upd = d.updated_at if d.updated_at.tzinfo else d.updated_at.replace(tzinfo=timezone.utc)
            if st == "PAID" and upd >= since:
                completed += 1
    except Exception:  # noqa: BLE001
        pass
    apify_budget = None
    try:
        from shared.connectors import apify
        apify_budget = {"runs_today": apify.runs_today(), "max_runs_per_day": int(_f("APIFY_MAX_RUNS_PER_DAY", 0)) or None}
    except Exception:  # noqa: BLE001
        pass
    return {
        "days": days, "per_day": rows, "totals": totals, "fixed_monthly": fixed,
        "booked_to_deals_usd": booked, "completed_deals": completed,
        "cost_per_completed_deal_usd": round(totals["total_usd"] / completed, 2) if completed else None,
        "apify_budget": apify_budget,
        "note": "LLM USD is an estimate (LLM_USD_PER_1K_TOKENS); the gateway meters the real cost.",
    }


def reset_for_tests() -> None:
    _memory.clear()
