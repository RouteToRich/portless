"""What each deal costs to run: LLM calls, Apify runs and operator decisions (review board B-24).

The realised margin says what a deal earned; this says what it cost to get there, so the
founder can see which corridors and deal sizes pay for their own effort.

* **Scope.** ``deal_scope(deal_id)`` sets the deal for everything awaited inside it. The event
  bus wraps every handler in the event's deal, the HITL saga callbacks in the gate's deal —
  so an enrichment, a screening run or a negotiation draft triggered for a deal is booked to it.
* **LLM.** ``LLMClient.chat`` books each successful call (tokens, estimated USD).
* **Apify.** every finished actor run inside a deal scope books its ``usageTotalUsd``.
* **People.** each gate resolved by a person books one decision and how long it waited.

Counters per deal in memory and Mongo collection ``deal_costs`` (``$inc``, one document per
deal). Writes never raise and never block the work they count. ``GET /deals/{id}/run-costs``.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("portless_deal", default=None)
_memory: dict[str, dict[str, float]] = {}
_col: Any = None
_resolved = False
_tasks: set[asyncio.Task] = set()

FIELDS = ("llm_calls", "llm_tokens", "llm_usd", "apify_runs", "apify_usd", "decisions", "decision_wait_hours",
          "samples", "samples_usd")


def current() -> str | None:
    return _CURRENT.get()


@contextlib.contextmanager
def deal_scope(deal_id: str | None) -> Iterator[None]:
    token = _CURRENT.set(deal_id or _CURRENT.get())
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _collection() -> Any:
    global _col, _resolved
    if _resolved:
        return _col
    _resolved = True
    try:
        from shared.mongo import collection
        _col = collection("deal_costs")
    except Exception as exc:  # noqa: BLE001
        logger.debug("DealCosts: Mongo unavailable: %s", exc)
    return _col


def set_collection_for_tests(col: Any) -> None:
    global _col, _resolved
    _col, _resolved = col, True
    _memory.clear()


def add(deal_id: str | None = None, **amounts: float) -> None:
    """Add to a deal's counters (the current deal when none is named). Never raises.
    LLM spend also goes to the platform-wide day counter (shared/cost_meter.py); Apify runs
    and spend are written there by shared/connectors/apify_usage.py (the daily budget)."""
    try:
        from shared import cost_meter
        cost_meter.add(**{k: v for k, v in amounts.items() if k.startswith("llm_")})
    except Exception:  # noqa: BLE001
        pass
    deal_id = deal_id or current()
    if not deal_id:
        return
    inc = {k: float(v) for k, v in amounts.items() if k in FIELDS and v}
    if not inc:
        return
    row = _memory.setdefault(deal_id, {})
    for k, v in inc.items():
        row[k] = round(row.get(k, 0.0) + v, 6)
    col = _collection()
    if col is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(
            col.update_one({"_id": deal_id}, {"$inc": inc, "$set": {"deal_id": deal_id}}, upsert=True))
        _tasks.add(task)
        task.add_done_callback(_done)
    except RuntimeError:
        pass  # no running loop (sync context): memory only


def _done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.debug("DealCosts: write failed: %s", task.exception())


def llm_usd(tokens: int) -> float:
    """Rough USD for a gateway call (LLM_USD_PER_1K_TOKENS, default 0.002 — the gateway's
    blended rate; the gateway meters the real cost)."""
    import os
    try:
        rate = float(os.getenv("LLM_USD_PER_1K_TOKENS", "0.002"))
    except ValueError:
        rate = 0.002
    return round(tokens / 1000.0 * rate, 6)


async def summary(deal_id: str) -> dict[str, Any]:
    row: dict[str, float] = dict(_memory.get(deal_id) or {})
    col = _collection()
    if col is not None:
        try:
            doc = await col.find_one({"_id": deal_id})
            if doc:
                row = {k: float(doc.get(k) or 0.0) for k in FIELDS}
        except Exception as exc:  # noqa: BLE001
            logger.debug("DealCosts: %s unreadable: %s", deal_id, exc)
    out: dict[str, Any] = {k: round(row.get(k, 0.0), 4) for k in FIELDS}
    out["deal_id"] = deal_id
    out["tool_usd"] = round(out["llm_usd"] + out["apify_usd"], 2)
    return out


async def all_summaries(limit: int = 500) -> list[dict]:
    """Every deal with run costs, most expensive first (Money & Margin)."""
    ids: set[str] = set(_memory)
    col = _collection()
    if col is not None:
        try:
            async for d in col.find({}, {"_id": 1}).limit(limit):
                ids.add(str(d["_id"]))
        except Exception as exc:  # noqa: BLE001
            logger.debug("DealCosts: list failed: %s", exc)
    rows = [await summary(i) for i in list(ids)[:limit]]
    return sorted(rows, key=lambda r: -(r["tool_usd"] + r["decisions"]))
