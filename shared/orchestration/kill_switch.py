"""Emergency stop for the whole platform (the console's Kill Switch).

While paused nothing leaves PortLess: no approval can be given, no gate
auto-approves, no escrow release / acceptance / payout / refund, no PO, QC or
freight order, no customs filing, no e-mail, and the watchers that would do any
of these on their own wait. Inbound facts still land — a provider webhook or a
reply is recorded, and whatever it would start becomes a gate that waits.

Staff only, reason required, every pause and resume audited. The state lives in
Mongo (``platform_controls``) so every process obeys it; it is re-read every
couple of seconds. A store that has never been readable counts as paused:
money must not move on an unknown answer.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_COLLECTION = "platform_controls"
_DOC_ID = "kill_switch"
_REFRESH_SECONDS = 2.0
_HISTORY_MAX = 100


class PlatformPaused(HTTPException):
    """Refused because the platform is paused (HTTP 423 on any route)."""

    def __init__(self, what: str, state: dict[str, Any]) -> None:
        super().__init__(
            status_code=423,
            detail=f"{what} refused: the platform is paused ({state.get('reason') or 'no reason recorded'}). "
                   "Nothing leaves PortLess until it is resumed.",
        )


class KillSwitchStoreError(Exception):
    """The pause / resume could not be saved, so it did not happen."""


def _running() -> dict[str, Any]:
    return {"paused": False, "reason": None, "by": None, "at": None}


_state: dict[str, Any] = _running()
_history: list[dict[str, Any]] = []
_refreshed_at = 0.0
_ever_read = False
_client: Any = None


def _collection():
    global _client
    from shared.config import settings
    if not settings.mongo_uri:
        return None
    if _client is None:
        import motor.motor_asyncio as motor  # type: ignore[import]
        _client = motor.AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=3000)
    return _client[settings.mongo_db_name][_COLLECTION]


async def _refresh() -> None:
    global _state, _history, _refreshed_at, _ever_read
    try:
        col = _collection()
        if col is None or time.monotonic() - _refreshed_at < _REFRESH_SECONDS:
            return
        doc = await col.find_one({"_id": _DOC_ID})
    except Exception as exc:  # noqa: BLE001
        logger.error("KillSwitch: state unreadable: %s", exc)
        if not _ever_read:
            _state = {**_running(), "paused": True, "reason": "pause state unreadable"}
        return
    doc = doc or {}
    _state = {key: doc.get(key, default) for key, default in _running().items()}
    _history = list(doc.get("history") or [])
    _refreshed_at = time.monotonic()
    _ever_read = True


async def status() -> dict[str, Any]:
    await _refresh()
    return {**_state, "history": list(reversed(_history[-20:]))}


async def is_paused() -> bool:
    await _refresh()
    return bool(_state["paused"])


async def assert_running(what: str) -> None:
    """Raises PlatformPaused (423) while the platform is paused."""
    if await is_paused():
        logger.warning("KillSwitch: refused — %s", what)
        raise PlatformPaused(what, _state)


async def _set(paused: bool, by: str, reason: str) -> dict[str, Any]:
    global _state, _history, _refreshed_at
    await _refresh()
    if bool(_state["paused"]) == paused:
        return await status()
    new_state = {"paused": paused, "reason": reason, "by": by,
                 "at": datetime.now(timezone.utc).isoformat()}
    entry = {"action": "pause" if paused else "resume", "by": by, "reason": reason, "at": new_state["at"]}
    try:
        col = _collection()
        if col is not None:
            await col.update_one(
                {"_id": _DOC_ID},
                {"$set": new_state, "$push": {"history": {"$each": [entry], "$slice": -_HISTORY_MAX}}},
                upsert=True,
            )
    except Exception as exc:  # noqa: BLE001
        raise KillSwitchStoreError(f"could not save the {entry['action']}: {exc}") from exc
    _state = new_state
    _history = (_history + [entry])[-_HISTORY_MAX:]
    _refreshed_at = time.monotonic()
    logger.critical("KillSwitch: platform %s by %s — %s", "PAUSED" if paused else "RESUMED", by, reason)
    return await status()


async def pause(by: str, reason: str) -> dict[str, Any]:
    return await _set(True, by, reason)


async def resume(by: str, reason: str) -> dict[str, Any]:
    return await _set(False, by, reason)


def reset_for_tests() -> None:
    global _state, _history, _refreshed_at, _ever_read, _client
    _state, _history, _refreshed_at, _ever_read, _client = _running(), [], 0.0, False, None
