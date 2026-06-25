"""Deals service persistence layer (LLD 4.2).

Two concrete implementations satisfy the same ``DealRepository`` Protocol:

``InMemoryFallbackRepository``
    Process-local dict — used automatically when ``DEALS_MONGO_URI`` is absent
    or when the initial MongoDB connection attempt fails.  No data survives a
    process restart; acceptable for local dev / CI.

``MongoDealRepository``
    Motor + MongoDB Atlas.  Five indexes:
      - deal_id (unique)
      - operator_id (list by operator)
      - status   (filter by lifecycle status)
      - opportunity_id (sparse — back-ref from Trade-Min)
      - created_at (descending — default sort)

``init_repository()`` must be called at startup (``main.on_startup``).
``get_repository()`` returns the active instance; raises ``RuntimeError`` if
``init_repository()`` was never called.
``close_repository()`` cleans up the Motor client on shutdown.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from .config import settings
from .schemas import DealRecord, DealStatus, StatusHistoryEntry

logger = logging.getLogger(__name__)

_repository: "DealRepository | None" = None
_motor_client = None  # motor.motor_asyncio.AsyncIOMotorClient — imported lazily


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DealRepository(Protocol):
    async def save(self, deal: DealRecord) -> None: ...
    async def get(self, deal_id: str) -> DealRecord | None: ...
    async def list_active(
        self,
        limit: int = 100,
        status: DealStatus | None = None,
        operator_id: str | None = None,
    ) -> list[DealRecord]: ...
    async def mark_status(
        self,
        deal_id: str,
        entry: StatusHistoryEntry,
    ) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory fallback
# ---------------------------------------------------------------------------

class InMemoryFallbackRepository:
    """Process-local dict.  Satisfies DealRepository without any I/O."""

    def __init__(self) -> None:
        self._store: dict[str, DealRecord] = {}

    async def save(self, deal: DealRecord) -> None:
        self._store[deal.deal_id] = deal

    async def get(self, deal_id: str) -> DealRecord | None:
        return self._store.get(deal_id)

    async def list_active(
        self,
        limit: int = 100,
        status: DealStatus | None = None,
        operator_id: str | None = None,
    ) -> list[DealRecord]:
        deals = list(self._store.values())
        if status is not None:
            deals = [d for d in deals if d.status == status]
        if operator_id is not None:
            deals = [d for d in deals if d.operator_id == operator_id]
        return sorted(deals, key=lambda d: d.created_at, reverse=True)[:limit]

    async def mark_status(
        self,
        deal_id: str,
        entry: StatusHistoryEntry,
    ) -> bool:
        deal = self._store.get(deal_id)
        if deal is None:
            return False
        self._store[deal_id] = deal.model_copy(
            update={
                "status": entry.status,
                "updated_at": entry.transitioned_at,
                "status_history": deal.status_history + [entry],
            }
        )
        return True


# ---------------------------------------------------------------------------
# MongoDB (Motor) implementation
# ---------------------------------------------------------------------------

class MongoDealRepository:
    """Motor/Atlas implementation with 5 indexes and automatic serialisation."""

    def __init__(self, collection) -> None:
        self._col = collection

    async def save(self, deal: DealRecord) -> None:
        doc = deal.model_dump(mode="json")
        await self._col.replace_one(
            {"deal_id": deal.deal_id},
            doc,
            upsert=True,
        )

    async def get(self, deal_id: str) -> DealRecord | None:
        doc = await self._col.find_one({"deal_id": deal_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return DealRecord(**doc)

    async def list_active(
        self,
        limit: int = 100,
        status: DealStatus | None = None,
        operator_id: str | None = None,
    ) -> list[DealRecord]:
        filt: dict = {}
        if status is not None:
            filt["status"] = status.value
        if operator_id is not None:
            filt["operator_id"] = operator_id
        cursor = self._col.find(filt).sort("created_at", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        result = []
        for doc in docs:
            doc.pop("_id", None)
            try:
                result.append(DealRecord(**doc))
            except Exception as exc:
                logger.warning("Skipping malformed deal document: %s", exc)
        return result

    async def mark_status(
        self,
        deal_id: str,
        entry: StatusHistoryEntry,
    ) -> bool:
        now_iso = entry.transitioned_at.isoformat()
        res = await self._col.update_one(
            {"deal_id": deal_id},
            {
                "$set": {
                    "status": entry.status.value,
                    "updated_at": now_iso,
                },
                "$push": {
                    "status_history": entry.model_dump(mode="json"),
                },
            },
        )
        return res.matched_count > 0


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def _create_indexes(collection) -> None:
    import motor.motor_asyncio  # noqa: F401 — already imported by caller
    from pymongo import ASCENDING, DESCENDING, IndexModel

    await collection.create_indexes([
        IndexModel([("deal_id", ASCENDING)], unique=True, name="deal_id_unique"),
        IndexModel([("operator_id", ASCENDING)], name="operator_id"),
        IndexModel([("status", ASCENDING)], name="status"),
        IndexModel([("opportunity_id", ASCENDING)], sparse=True, name="opportunity_id"),
        IndexModel([("created_at", DESCENDING)], name="created_at_desc"),
    ])


async def init_repository() -> None:
    """Initialise the repository.  Called once at application startup."""
    global _repository, _motor_client

    uri = settings.deals_mongo_uri
    if not uri:
        logger.warning(
            "DEALS_MONGO_URI not set — Deals service falling back to in-memory store. "
            "Data will not survive a process restart."
        )
        _repository = InMemoryFallbackRepository()
        return

    try:
        import motor.motor_asyncio

        client = motor.motor_asyncio.AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=5_000,
        )
        # ping to verify connection before proceeding
        await client.admin.command("ping")
        _motor_client = client

        col = client[settings.deals_mongo_db_name][settings.deals_mongo_collection]
        await _create_indexes(col)

        _repository = MongoDealRepository(col)
        logger.info(
            "Deals MongoDB repository ready (%s / %s)",
            settings.deals_mongo_db_name,
            settings.deals_mongo_collection,
        )
    except Exception as exc:
        logger.error(
            "Deals MongoDB connection failed — falling back to in-memory store: %s", exc
        )
        _repository = InMemoryFallbackRepository()


async def close_repository() -> None:
    """Release the Motor client.  Called at application shutdown."""
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None
        logger.info("Deals MongoDB client closed")


def get_repository() -> DealRepository:
    """Return the active repository.  Raises ``RuntimeError`` if not initialised."""
    if _repository is None:
        raise RuntimeError(
            "Deals repository not initialised — call init_repository() at startup."
        )
    return _repository
