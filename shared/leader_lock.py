"""One instance at a time for each background watcher.

Several backend instances (a rolling deploy, two containers, a scaled service) each start the
deal watcher, the escrow watcher, the HITL timeout watcher, the stale-negotiation watcher and
the digest. Without a lock they all act: two reminders to the same buyer, two auto-releases
racing, two digests. Each tick first takes a lease:

    if not await leader_lock.acquire("deal_watcher", ttl_seconds=interval * 3):
        continue  # another instance holds it

The lease is a document in Mongo collection ``watcher_locks`` (``_id`` = watcher name,
``holder`` = this process, ``expires_at``), taken atomically when free or expired, renewed
by the holder each tick. If the holder dies its lease runs out and another instance takes
over. Without Mongo (one process, dev / tests) every acquire succeeds. If Mongo cannot be
reached the tick is skipped (``LEADER_LOCK_FAIL_OPEN=true`` runs it anyway).
"""
from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
_COLLECTION = "watcher_locks"
_col: Any = None
_resolved = False
last_error_at: datetime | None = None
"""When the lock store last failed to answer (the heartbeat monitor alerts on it)."""


def _collection() -> Any:
    global _col, _resolved
    if _resolved:
        return _col
    _resolved = True
    try:
        from shared.config import settings
        if not settings.mongo_uri:
            return None
        from shared.mongo import get_client
        _col = get_client(settings.mongo_uri)[settings.mongo_db_name][_COLLECTION]
    except Exception as exc:  # noqa: BLE001
        logger.warning("LeaderLock: Mongo unavailable (%s) — every instance runs its watchers", exc)
        _col = None
    return _col


def set_collection_for_tests(col: Any) -> None:
    global _col, _resolved
    _col, _resolved = col, True


def _fail_open() -> bool:
    return os.getenv("LEADER_LOCK_FAIL_OPEN", "false").strip().lower() in ("1", "true", "yes", "on")


async def acquire(name: str, ttl_seconds: float, *, now: datetime | None = None, holder: str = HOLDER) -> bool:
    """Take or renew the lease for ``name``. True = this instance runs the tick."""
    col = _collection()
    if col is None:
        return True
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(seconds=max(1.0, ttl_seconds))
    try:
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError
        try:
            doc = await col.find_one_and_update(
                {"_id": name, "$or": [{"holder": holder}, {"expires_at": {"$lte": now}}]},
                {"$set": {"holder": holder, "expires_at": expires, "renewed_at": now}},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            return False  # the document exists, held by someone else and not expired
        return bool(doc) and doc.get("holder") == holder
    except Exception as exc:  # noqa: BLE001
        global last_error_at
        last_error_at = datetime.now(timezone.utc)
        logger.warning("LeaderLock: %s not acquired (%s) — %s", name, exc,
                       "running anyway (LEADER_LOCK_FAIL_OPEN)" if _fail_open() else "skipping this tick")
        return _fail_open()


async def release(name: str, *, holder: str = HOLDER) -> None:
    col = _collection()
    if col is None:
        return
    try:
        await col.delete_one({"_id": name, "holder": holder})
    except Exception as exc:  # noqa: BLE001
        logger.debug("LeaderLock: %s not released: %s", name, exc)
