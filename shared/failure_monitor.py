"""Failure rates that should page someone (todo: monitoring and alerting).

Two signals are counted in a sliding window (``FAILURE_WINDOW_MINUTES``, 15) per process:

* **LLM calls** per caller (gateway errors, blank replies): when at least
  ``LLM_FAILURE_MIN_CALLS`` (5) calls ran and more than ``LLM_FAILURE_RATE_PCT`` (50) % failed,
  drafts are falling back to templates and the resolver escalates everything.
* **Webhook signatures refused** per provider: ``WEBHOOK_REFUSED_ALERT`` (5) or more refusals
  mean a wrong secret (payments and QC results are being dropped) or someone probing.

The heartbeat monitor (every minute, one instance) calls ``check_and_alert``; each alert goes
to Telegram at most once per ``FAILURE_ALERT_REPEAT_MINUTES`` (60). ``GET /health/failures``.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

_events: dict[tuple[str, str], deque[tuple[float, bool]]] = {}
_last_alert: dict[str, float] = {}


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def record(kind: str, key: str, ok: bool, *, now: float | None = None) -> None:
    """Count one outcome (kind ``llm`` / ``webhook``). Never raises."""
    try:
        q = _events.setdefault((kind, key or "?"), deque(maxlen=2000))
        q.append((now if now is not None else time.monotonic(), bool(ok)))
    except Exception:  # noqa: BLE001
        pass


def status(now: float | None = None) -> dict[str, Any]:
    now = now if now is not None else time.monotonic()
    window = _num("FAILURE_WINDOW_MINUTES", 15) * 60
    rows = []
    for (kind, key), q in _events.items():
        recent = [ok for at, ok in q if now - at <= window]
        if not recent:
            continue
        failed = sum(1 for ok in recent if not ok)
        rows.append({"kind": kind, "key": key, "total": len(recent), "failed": failed,
                     "failure_pct": round(failed * 100.0 / len(recent), 1)})
    alerts = []
    for r in rows:
        if r["kind"] == "llm" and r["total"] >= _num("LLM_FAILURE_MIN_CALLS", 5) \
                and r["failure_pct"] > _num("LLM_FAILURE_RATE_PCT", 50):
            alerts.append(f"LLM CALLS FAILING: {r['key']} — {r['failed']} of {r['total']} in the last "
                          f"{int(window // 60)} min (drafts fall back to templates; check the LLM gateway)")
        if r["kind"] == "webhook" and r["failed"] >= _num("WEBHOOK_REFUSED_ALERT", 5):
            alerts.append(f"WEBHOOKS REFUSED: {r['key']} — {r['failed']} signature(s) refused in the last "
                          f"{int(window // 60)} min (wrong *_WEBHOOK_SECRET drops payments / results, or probing)")
    return {"window_minutes": int(window // 60), "rows": sorted(rows, key=lambda r: -r["failed"]), "alerts": alerts}


async def business_alerts(now: Any = None) -> list[str]:
    """Deal-level things waiting too long: a money gate pending over ``MONEY_GATE_ALERT_HOURS``
    (24) — a supplier or the buyer is waiting for money — and more than
    ``OPEN_EXCEPTIONS_ALERT`` (10) exceptions open or escalated. Never raises."""
    from datetime import datetime, timezone
    now_dt = now or datetime.now(timezone.utc)
    out: list[str] = []
    try:
        from services.hitl_approval.repository import get_repository as hitl_repo  # type: ignore[import]
        from services.hitl_approval.service import is_money_action  # type: ignore[import]
        limit_h = _num("MONEY_GATE_ALERT_HOURS", 24)
        old = []
        for g in await hitl_repo().list_by_status("PENDING"):
            created = g.created_at if g.created_at.tzinfo else g.created_at.replace(tzinfo=timezone.utc)
            hours = (now_dt - created).total_seconds() / 3600
            if is_money_action(g.action) and hours >= limit_h:
                old.append((hours, g))
        if old:
            hours, g = max(old, key=lambda x: x[0])
            out.append(f"MONEY GATES WAITING: {len(old)} money gate(s) pending over {limit_h:g} h — oldest {g.action} "
                       f"on {g.deal_id}, {hours:.0f} h (Approvals inbox)")
    except Exception as exc:  # noqa: BLE001
        logger.debug("FailureMonitor: gates unreadable: %s", exc)
    try:
        from services.exception_resolver.repository import get_repository as exc_repo  # type: ignore[import]
        rows = await exc_repo().list_all(limit=1000)
        open_rows = [r for r in rows if str(getattr(r.status, "value", r.status)) in ("OPEN", "ESCALATED", "UNRESOLVABLE")]
        if len(open_rows) > _num("OPEN_EXCEPTIONS_ALERT", 10):
            out.append(f"EXCEPTIONS PILING UP: {len(open_rows)} open or escalated (Exceptions page)")
    except Exception as exc:  # noqa: BLE001
        logger.debug("FailureMonitor: exceptions unreadable: %s", exc)
    try:  # export proceeds past the RBI deadline (realisation.py)
        from shared.orchestration import realisation
        overdue = [r for r in await realisation.open_items() if r["status"] == "overdue"]
        if overdue:
            total = sum(r["outstanding"] for r in overdue)
            out.append(f"EXPORT PROCEEDS OVERDUE: {len(overdue)} Shipping Bill(s), {total:,.0f} outstanding past the "
                       "realisation deadline (GET /deals/realisation/open — tell the AD bank)")
    except Exception as exc:  # noqa: BLE001
        logger.debug("FailureMonitor: realisation unreadable: %s", exc)
    try:  # suppliers are checked against a list nobody loaded, or an old one (denied_list.py)
        from shared.orchestration import denied_list
        st = await denied_list.status()
        if not st["loaded"] or st["stale"]:
            out.append("DGFT DENIED LIST " + ("NOT LOADED" if not st["loaded"] else f"STALE: {st['age_days']} days old")
                       + ": suppliers are not checked against the current list (PUT /deals/compliance/dgft-denied)")
    except Exception as exc:  # noqa: BLE001
        logger.debug("FailureMonitor: DGFT list status unreadable: %s", exc)
    return out


async def check_and_alert(*, now: float | None = None, send=None, business: list[str] | None = None) -> list[str]:
    now = now if now is not None else time.monotonic()
    repeat = _num("FAILURE_ALERT_REPEAT_MINUTES", 60) * 60
    fresh = []
    extra = business if business is not None else await business_alerts()
    for a in status(now)["alerts"] + extra:
        key = a.split(" — ")[0]  # e.g. "LLM CALLS FAILING: buyer_agent"
        if a.startswith(("MONEY GATES WAITING", "EXCEPTIONS PILING UP", "EXPORT PROCEEDS OVERDUE", "DGFT DENIED LIST")):
            key = a.split(":")[0]  # the count changes; one alert per kind per period
        if now - _last_alert.get(key, -1e12) < repeat:
            continue
        _last_alert[key] = now
        fresh.append(a)
    if fresh:
        sender = send
        if sender is None:
            from shared import telegram
            sender = telegram.send_message if telegram.configured() else None
        for m in fresh:
            logger.critical("FailureMonitor: %s", m)
            if sender is not None:
                try:
                    await sender(m)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("FailureMonitor: alert not sent: %s", exc)
    return fresh


def reset_for_tests() -> None:
    _events.clear()
    _last_alert.clear()
