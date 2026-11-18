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


async def check_and_alert(*, now: float | None = None, send=None) -> list[str]:
    now = now if now is not None else time.monotonic()
    repeat = _num("FAILURE_ALERT_REPEAT_MINUTES", 60) * 60
    fresh = []
    for a in status(now)["alerts"]:
        key = a.split(" — ")[0]
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
