"""Persistence layer for Email Monitor — InMemory + MongoDB Motor."""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from .config import settings
from .schemas import InboundEmailRecord, InboundEmailStatus

logger = logging.getLogger(__name__)


@runtime_checkable
class EmailMonitorRepository(Protocol):
    async def save(self, record: InboundEmailRecord) -> None: ...
    async def get(self, inbound_id: str) -> InboundEmailRecord | None: ...
    async def list_all(
        self,
        status: InboundEmailStatus | None = None,
        negotiation_id: str | None = None,
        limit: int = 100,
    ) -> list[InboundEmailRecord]: ...
    async def find_by_message_id(self, message_id: str) -> InboundEmailRecord | None: ...


# ---------------------------------------------------------------------------
# In-memory fallback (dev / CI)
# ---------------------------------------------------------------------------

class InMemoryEmailMonitorRepository:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    async def save(self, record: InboundEmailRecord) -> None:
        self._store[record.inbound_id] = record.model_dump()

    async def get(self, inbound_id: str) -> InboundEmailRecord | None:
        doc = self._store.get(inbound_id)
        return InboundEmailRecord(**doc) if doc else None

    async def list_all(
        self,
        status: InboundEmailStatus | None = None,
        negotiation_id: str | None = None,
        limit: int = 100,
    ) -> list[InboundEmailRecord]:
        docs = list(self._store.values())
        if status:
            docs = [d for d in docs if d.get("status") == status.value]
        if negotiation_id:
            docs = [d for d in docs if d.get("negotiation_id") == negotiation_id]
        return [InboundEmailRecord(**d) for d in docs[:limit]]

    async def find_by_message_id(self, message_id: str) -> InboundEmailRecord | None:
        for doc in self._store.values():
            if doc.get("message_id") == message_id:
                return InboundEmailRecord(**doc)
        return None


# ---------------------------------------------------------------------------
# MongoDB Motor repository
# ---------------------------------------------------------------------------

class MongoEmailMonitorRepository:
    def __init__(self, uri: str, db_name: str, collection: str) -> None:
        import motor.motor_asyncio as motor  # type: ignore[import]
        client: Any = motor.AsyncIOMotorClient(uri)
        self._col = client[db_name][collection]

    async def _ensure_indexes(self) -> None:
        from pymongo import ASCENDING, DESCENDING  # type: ignore[import]
        await self._col.create_index([("inbound_id", ASCENDING)], unique=True, background=True)
        await self._col.create_index([("negotiation_id", ASCENDING)], background=True)
        await self._col.create_index([("message_id", ASCENDING)], background=True)
        await self._col.create_index([("status", ASCENDING)], background=True)
        await self._col.create_index([("received_at", DESCENDING)], background=True)

    async def save(self, record: InboundEmailRecord) -> None:
        doc = record.model_dump()
        await self._col.replace_one({"inbound_id": record.inbound_id}, doc, upsert=True)

    async def get(self, inbound_id: str) -> InboundEmailRecord | None:
        doc = await self._col.find_one({"inbound_id": inbound_id}, {"_id": 0})
        return InboundEmailRecord(**doc) if doc else None

    async def list_all(
        self,
        status: InboundEmailStatus | None = None,
        negotiation_id: str | None = None,
        limit: int = 100,
    ) -> list[InboundEmailRecord]:
        query: dict[str, Any] = {}
        if status:
            query["status"] = status.value
        if negotiation_id:
            query["negotiation_id"] = negotiation_id
        cursor = self._col.find(query, {"_id": 0}).sort("received_at", -1).limit(limit)
        return [InboundEmailRecord(**doc) async for doc in cursor]

    async def find_by_message_id(self, message_id: str) -> InboundEmailRecord | None:
        doc = await self._col.find_one({"message_id": message_id}, {"_id": 0})
        return InboundEmailRecord(**doc) if doc else None


# ---------------------------------------------------------------------------
# Init / teardown
# ---------------------------------------------------------------------------

_repository: EmailMonitorRepository | None = None


async def init_repository() -> None:
    global _repository
    if settings.email_monitor_mongo_uri:
        repo = MongoEmailMonitorRepository(
            settings.email_monitor_mongo_uri,
            settings.email_monitor_mongo_db_name,
            settings.email_monitor_inbound_collection,
        )
        try:
            await repo._ensure_indexes()
            logger.info("EmailMonitor: MongoDB repository ready")
        except Exception as exc:
            logger.warning(
                "EmailMonitor: MongoDB index creation failed (%s) — using in-memory fallback", exc
            )
            _repository = InMemoryEmailMonitorRepository()
            return
        _repository = repo
    else:
        logger.info("EmailMonitor: EMAIL_MONITOR_MONGO_URI not set — using in-memory repository")
        _repository = InMemoryEmailMonitorRepository()


async def close_repository() -> None:
    global _repository
    _repository = None


def get_repository() -> EmailMonitorRepository:
    if _repository is None:
        raise RuntimeError("EmailMonitor repository not initialised — call init_repository() first")
    return _repository
