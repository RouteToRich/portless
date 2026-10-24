"""Exception-Resolver persistence layer.

Two concrete backends:
  InMemoryExceptionRepository  — process-local dict; activated when EXCEPTION_RESOLVER_MONGO_URI absent.
  MongoExceptionRepository     — Motor + Atlas; activated when URI is set.

``init_repository()`` selects the backend at startup.
``get_repository()`` returns the active instance.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .config import settings
from .schemas import ExceptionRecord, ExceptionStatus

logger = logging.getLogger(__name__)

_repository: "ExceptionRepository | None" = None
_motor_client = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ExceptionRepository(Protocol):
    async def save(self, record: ExceptionRecord) -> None: ...
    async def get(self, exception_id: str) -> ExceptionRecord | None: ...
    async def list_by_deal(self, deal_id: str) -> list[ExceptionRecord]: ...
    async def list_by_status(self, status: ExceptionStatus) -> list[ExceptionRecord]: ...
    async def list_all(self, limit: int = 100) -> list[ExceptionRecord]: ...


# ---------------------------------------------------------------------------
# In-memory fallback
# ---------------------------------------------------------------------------

class InMemoryExceptionRepository:
    def __init__(self) -> None:
        self._store: dict[str, ExceptionRecord] = {}

    async def save(self, record: ExceptionRecord) -> None:
        self._store[record.exception_id] = record

    async def get(self, exception_id: str) -> ExceptionRecord | None:
        return self._store.get(exception_id)

    async def list_by_deal(self, deal_id: str) -> list[ExceptionRecord]:
        return [r for r in self._store.values() if r.deal_id == deal_id]

    async def list_by_status(self, status: ExceptionStatus) -> list[ExceptionRecord]:
        return [r for r in self._store.values() if r.status == status]

    async def list_all(self, limit: int = 100) -> list[ExceptionRecord]:
        items = sorted(self._store.values(), key=lambda r: r.raised_at, reverse=True)
        return items[:limit]


# ---------------------------------------------------------------------------
# MongoDB (Motor) implementation
# ---------------------------------------------------------------------------

class MongoExceptionRepository:
    def __init__(self, collection) -> None:
        self._col = collection

    async def save(self, record: ExceptionRecord) -> None:
        doc = record.model_dump(mode="json")
        await self._col.replace_one({"exception_id": record.exception_id}, doc, upsert=True)

    async def get(self, exception_id: str) -> ExceptionRecord | None:
        doc = await self._col.find_one({"exception_id": exception_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return ExceptionRecord(**doc)

    async def list_by_deal(self, deal_id: str) -> list[ExceptionRecord]:
        cursor = self._col.find({"deal_id": deal_id}, sort=[("raised_at", -1)])
        return [ExceptionRecord(**{**doc, "_id": None} if "_id" in doc else doc)
                async for doc in cursor
                if not doc.pop("_id", None) or True]

    async def list_by_status(self, status: ExceptionStatus) -> list[ExceptionRecord]:
        cursor = self._col.find({"status": status.value}, sort=[("raised_at", -1)])
        docs = []
        async for doc in cursor:
            doc.pop("_id", None)
            docs.append(ExceptionRecord(**doc))
        return docs

    async def list_all(self, limit: int = 100) -> list[ExceptionRecord]:
        cursor = self._col.find({}, sort=[("raised_at", -1)], limit=limit)
        docs = []
        async for doc in cursor:
            doc.pop("_id", None)
            docs.append(ExceptionRecord(**doc))
        return docs


# ---------------------------------------------------------------------------
# Index creation
# ---------------------------------------------------------------------------

async def _create_indexes(col) -> None:
    from pymongo import ASCENDING, DESCENDING, IndexModel

    await col.create_indexes([
        IndexModel([("exception_id", ASCENDING)], unique=True, name="exc_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="exc_deal_id"),
        IndexModel([("status", ASCENDING)], name="exc_status"),
        IndexModel([("raised_at", DESCENDING)], name="exc_raised_at"),
    ])


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def init_repository() -> None:
    global _repository, _motor_client

    uri = settings.exception_resolver_mongo_uri
    if not uri:
        logger.warning(
            "EXCEPTION_RESOLVER_MONGO_URI not set — falling back to in-memory store. "
            "Data will not survive a process restart."
        )
        _repository = InMemoryExceptionRepository()
        return

    try:
        import motor.motor_asyncio

        client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5_000)
        await client.admin.command("ping")
        _motor_client = client

        col = client[settings.exception_resolver_mongo_db_name][settings.exception_resolver_collection]
        await _create_indexes(col)

        _repository = MongoExceptionRepository(col)
        logger.info(
            "ExceptionResolver repository: MongoDB at %s (db=%s)",
            uri.split("@")[-1],
            settings.exception_resolver_mongo_db_name,
        )
    except Exception as exc:
        logger.error(
            "ExceptionResolver: MongoDB connection failed (%s) — falling back to in-memory store.",
            exc,
        )
        _repository = InMemoryExceptionRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None


def get_repository() -> ExceptionRepository:
    if _repository is None:
        raise RuntimeError(
            "ExceptionResolver repository not initialised — call init_repository() at startup."
        )
    return _repository
