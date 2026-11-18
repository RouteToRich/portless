"""An audit log that cannot be quietly edited.

Every staff decision that moves money or overrides a control is appended here: approvals
resolved by a person, the Kill Switch, compliance hold clearances, PI extensions, staff relays
of an escrow release, sanctions-review decisions, directory merges, prospects dropped.

Each entry carries a sequence number and ``hash = SHA-256(prev_hash + canonical JSON of the
entry)``, so changing, deleting or reordering any past entry breaks every hash after it —
``verify()`` (``GET /hitl-approval/audit/verify``) finds the first broken one. Entries live
in Mongo collection ``audit_log`` (insert only, never updated) or in memory without Mongo.

``record()`` never raises: an audit write that fails is logged loudly, the action itself
is not blocked (the action's own record still exists).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

GENESIS = "0" * 64
_COLLECTION = "audit_log"
_memory: list[dict[str, Any]] = []
_lock = asyncio.Lock()
_col: Any = None
_resolved = False


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
        logger.warning("AuditLog: Mongo unavailable (%s) — in-memory only", exc)
        _col = None
    return _col


def set_collection_for_tests(col: Any) -> None:
    global _col, _resolved
    _col, _resolved = col, True
    _memory.clear()


def _digest(prev_hash: str, entry: dict[str, Any]) -> str:
    body = {k: entry[k] for k in ("seq", "at", "action", "actor", "subject", "detail")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


async def _last() -> dict[str, Any] | None:
    col = _collection()
    if col is None:
        return _memory[-1] if _memory else None
    return await col.find_one({}, sort=[("seq", -1)])


async def record(action: str, actor: str | None, subject: str | None, detail: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Append one entry. Returns it (None when the write failed)."""
    try:
        async with _lock:
            last = await _last()
            entry = {
                "seq": (last["seq"] + 1) if last else 1,
                "at": datetime.now(timezone.utc).isoformat(),
                "action": action, "actor": actor or "unknown", "subject": subject or "",
                "detail": json.loads(json.dumps(detail or {}, default=str)),
                "prev_hash": last["hash"] if last else GENESIS,
            }
            entry["hash"] = _digest(entry["prev_hash"], entry)
            col = _collection()
            if col is None:
                _memory.append(entry)
            else:
                await col.insert_one({"_id": entry["seq"], **entry})
            return entry
    except Exception as exc:  # noqa: BLE001
        logger.error("AuditLog: %s by %s on %s NOT recorded: %s", action, actor, subject, exc)
        return None


async def entries(limit: int = 200, subject: str | None = None) -> list[dict[str, Any]]:
    col = _collection()
    if col is None:
        rows = [e for e in _memory if subject is None or e["subject"] == subject]
        return list(reversed(rows))[:limit]
    query = {"subject": subject} if subject else {}
    out = []
    async for doc in col.find(query).sort("seq", -1).limit(limit):
        doc.pop("_id", None)
        out.append(doc)
    return out


async def verify() -> dict[str, Any]:
    """Recompute the chain from the first entry: ``ok`` and, if not, the first broken seq."""
    col = _collection()
    if col is None:
        rows = list(_memory)
    else:
        rows = []
        async for doc in col.find({}).sort("seq", 1):
            doc.pop("_id", None)
            rows.append(doc)
    prev, expected_seq = GENESIS, 1
    for e in rows:
        if e.get("seq") != expected_seq:
            return {"ok": False, "entries": len(rows), "broken_at": expected_seq, "why": "missing or reordered entry"}
        if e.get("prev_hash") != prev or _digest(prev, e) != e.get("hash"):
            return {"ok": False, "entries": len(rows), "broken_at": e.get("seq"), "why": "entry changed"}
        prev, expected_seq = e["hash"], expected_seq + 1
    return {"ok": True, "entries": len(rows), "last_hash": prev}
