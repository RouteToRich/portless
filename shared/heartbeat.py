"""Heartbeats of the background watchers: is each one still running?

A watcher that dies silently (an exception loop, a frozen instance, a serverless function
that was suspended) stops sending reminders, releasing lapsed windows or resolving timeouts,
and nothing says so. Every watcher tick that did its work calls ``beat(name, interval)``;
``status()`` lists each known watcher with its last beat and ``stale`` when no beat came for
``3 × interval``. Exposed at ``GET /health/watchers`` (signed in) and in the daily digest.

Beats are kept in memory and in Mongo collection ``watcher_heartbeats`` (one document per
watcher, so any instance can report on all of them). Never raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_COLLECTION = "watcher_heartbeats"
_memory: dict[str, dict[str, Any]] = {}
_col: Any = None
_resolved = False
STALE_FACTOR = 3.0


def _collection() -> Any:
    global _col, _resolved
    if _resolved:
        return _col
    _resolved = True
    try:
        from shared.config import settings
        if settings.mongo_uri:
            import motor.motor_asyncio as motor  # type: ignore[import]
            _col = motor.AsyncIOMotorClient(settings.mongo_uri)[settings.mongo_db_name][_COLLECTION]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Heartbeat: Mongo unavailable (%s) — memory only", exc)
        _col = None
    return _col


def set_collection_for_tests(col: Any) -> None:
    global _col, _resolved
    _col, _resolved = col, True
    _memory.clear()


async def beat(name: str, interval_seconds: float, *, now: datetime | None = None, note: str | None = None) -> None:
    from shared.leader_lock import HOLDER
    now = now or datetime.now(timezone.utc)
    doc = {"name": name, "at": now, "interval_seconds": float(interval_seconds), "holder": HOLDER, "note": note}
    _memory[name] = doc
    col = _collection()
    if col is None:
        return
    try:
        await col.update_one({"_id": name}, {"$set": doc}, upsert=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Heartbeat: %s not stored: %s", name, exc)


def _aware(dt: Any) -> datetime | None:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def status(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    docs: dict[str, dict[str, Any]] = dict(_memory)
    col = _collection()
    if col is not None:
        try:
            async for d in col.find({}):
                d.pop("_id", None)
                cur = docs.get(d["name"])
                if cur is None or (_aware(d.get("at")) or now) > (_aware(cur.get("at")) or now):
                    docs[d["name"]] = d
        except Exception as exc:  # noqa: BLE001
            logger.debug("Heartbeat: read failed: %s", exc)
    rows = []
    for name, d in sorted(docs.items()):
        at = _aware(d.get("at"))
        interval = float(d.get("interval_seconds") or 60)
        age = (now - at).total_seconds() if at else None
        rows.append({"name": name, "last_beat_at": at.isoformat() if at else None, "age_seconds": round(age or 0),
                     "interval_seconds": interval, "holder": d.get("holder"),
                     "stale": age is None or age > STALE_FACTOR * interval + 30})
    return {"ok": not any(r["stale"] for r in rows), "watchers": rows,
            "stale": [r["name"] for r in rows if r["stale"]]}
