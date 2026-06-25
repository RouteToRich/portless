"""Opportunity persistence for the Trade-Min service (§10.6).

Two implementations share the same async interface:

  MongoOpportunityRepository  — production, Motor/Atlas, TTL-managed via
                                 expires_at; preferred whenever TRADE_MONGO_URI
                                 is set.

  InMemoryFallbackRepository  — in-process dict, used automatically in local
                                 dev / CI when TRADE_MONGO_URI is absent. Data
                                 is lost on process restart, but the service
                                 remains fully functional without a database.

``get_repository()`` returns whichever implementation is appropriate.

Index strategy (MongoDB only)
------------------------------
  1. status / urgency_tier / arbitrage_rank  — default list query
  2. hs_code                                 — per-commodity lookups
  3. market                                  — per-destination lookups
  4. (hs_code, market, scanned_at)           — dedup + latest-scan queries
  5. expires_at (TTL index)                  — automatic document expiry
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from .config import settings
from .schemas import EnrichedOpportunity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repository protocol — both implementations satisfy this interface
# ---------------------------------------------------------------------------

@runtime_checkable
class OpportunityRepository(Protocol):
    async def save(self, opp: EnrichedOpportunity) -> None: ...
    async def get(self, opportunity_id: str) -> EnrichedOpportunity | None: ...
    async def list_active(self, limit: int, min_rank: float) -> list[EnrichedOpportunity]: ...
    async def list_by_hs_market(self, hs_code: str, market: str) -> list[EnrichedOpportunity]: ...
    async def mark_converted(self, opportunity_id: str) -> bool: ...
    async def clear(self) -> int: ...


# ---------------------------------------------------------------------------
# In-memory fallback (local dev / CI — no MongoDB required)
# ---------------------------------------------------------------------------

class InMemoryFallbackRepository:
    """Process-local dict store.  Data lost on restart.  Zero external deps."""

    def __init__(self) -> None:
        self._store: dict[str, EnrichedOpportunity] = {}

    async def save(self, opp: EnrichedOpportunity) -> None:
        if not opp.opportunity_id:
            raise ValueError("opportunity_id is required")
        self._store[opp.opportunity_id] = opp

    async def get(self, opportunity_id: str) -> EnrichedOpportunity | None:
        return self._store.get(opportunity_id)

    async def list_active(
        self,
        limit: int = 100,
        min_rank: float = 0.0,
    ) -> list[EnrichedOpportunity]:
        now = datetime.now(timezone.utc)
        opps = [
            o for o in self._store.values()
            if o.status in ("ACTIVE", "MARGINAL")
            and o.arbitrage_rank >= min_rank
            and (o.expires_at is None or o.expires_at > now)
        ]
        opps.sort(key=lambda o: (o.urgency_tier, o.arbitrage_rank), reverse=True)
        return opps[:limit]

    async def list_by_hs_market(
        self,
        hs_code: str,
        market: str,
    ) -> list[EnrichedOpportunity]:
        return [
            o for o in self._store.values()
            if o.hs_code == hs_code and o.market == market
        ]

    async def mark_converted(self, opportunity_id: str) -> bool:
        opp = self._store.get(opportunity_id)
        if opp is None:
            return False
        self._store[opportunity_id] = opp.model_copy(update={"status": "CONVERTED"})
        return True

    async def clear(self) -> int:
        count = len(self._store)
        self._store.clear()
        return count


# ---------------------------------------------------------------------------
# MongoDB Motor implementation (production)
# ---------------------------------------------------------------------------

class MongoOpportunityRepository:
    """Async MongoDB repository backed by Motor / Atlas."""

    def __init__(self, collection: object) -> None:
        self._col = collection  # AsyncIOMotorCollection

    async def save(self, opp: EnrichedOpportunity) -> None:
        if not opp.opportunity_id:
            raise ValueError("opportunity_id is required")
        doc = opp.model_dump(mode="json")
        doc["_id"] = opp.opportunity_id
        await self._col.replace_one(  # type: ignore[union-attr]
            {"_id": opp.opportunity_id},
            doc,
            upsert=True,
        )

    async def get(self, opportunity_id: str) -> EnrichedOpportunity | None:
        doc = await self._col.find_one({"_id": opportunity_id})  # type: ignore[union-attr]
        if doc is None:
            return None
        doc.pop("_id", None)
        return EnrichedOpportunity.model_validate(doc)

    async def list_active(
        self,
        limit: int = 100,
        min_rank: float = 0.0,
    ) -> list[EnrichedOpportunity]:
        cursor = (
            self._col.find(  # type: ignore[union-attr]
                {
                    "status": {"$in": ["ACTIVE", "MARGINAL"]},
                    "arbitrage_rank": {"$gte": min_rank},
                }
            )
            .sort([("urgency_tier", -1), ("arbitrage_rank", -1)])
            .limit(limit)
        )
        results: list[EnrichedOpportunity] = []
        async for doc in cursor:
            doc.pop("_id", None)
            try:
                results.append(EnrichedOpportunity.model_validate(doc))
            except Exception as exc:
                logger.warning("Skipping malformed opportunity doc: %s", exc)
        return results

    async def list_by_hs_market(
        self,
        hs_code: str,
        market: str,
    ) -> list[EnrichedOpportunity]:
        cursor = (
            self._col.find({"hs_code": hs_code, "market": market})  # type: ignore[union-attr]
            .sort("scanned_at", -1)
            .limit(50)
        )
        results: list[EnrichedOpportunity] = []
        async for doc in cursor:
            doc.pop("_id", None)
            try:
                results.append(EnrichedOpportunity.model_validate(doc))
            except Exception as exc:
                logger.warning("Skipping malformed doc: %s", exc)
        return results

    async def mark_converted(self, opportunity_id: str) -> bool:
        result = await self._col.update_one(  # type: ignore[union-attr]
            {"_id": opportunity_id},
            {"$set": {"status": "CONVERTED"}},
        )
        return result.modified_count > 0

    async def clear(self) -> int:
        result = await self._col.delete_many({})  # type: ignore[union-attr]
        return result.deleted_count


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_repository: InMemoryFallbackRepository | MongoOpportunityRepository | None = None
_motor_client: object | None = None  # AsyncIOMotorClient


async def init_repository() -> InMemoryFallbackRepository | MongoOpportunityRepository:
    """Initialise the repository and return the process-wide instance.

    Uses MongoDB when ``TRADE_MONGO_URI`` is set, otherwise falls back to the
    in-memory implementation with a WARNING log so operators know persistence
    is non-durable.

    Safe to call multiple times — returns the cached instance.
    """
    global _repository, _motor_client

    if _repository is not None:
        return _repository

    mongo_uri = settings.trade_mongo_uri
    if not mongo_uri:
        logger.warning(
            "TRADE_MONGO_URI is not set — Trade-Min running with in-memory storage. "
            "Opportunities will be lost on process restart. "
            "Set TRADE_MONGO_URI in backend/.env for durable persistence."
        )
        _repository = InMemoryFallbackRepository()
        return _repository

    try:
        from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[import]
        _motor_client = AsyncIOMotorClient(
            mongo_uri,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
        )
        db = _motor_client[settings.trade_mongo_db_name]  # type: ignore[index]
        col = db[settings.trade_mongo_collection]

        await _ensure_indexes(col)

        _repository = MongoOpportunityRepository(col)
        logger.info(
            "Trade-Min MongoDB connected: db=%s col=%s",
            settings.trade_mongo_db_name,
            settings.trade_mongo_collection,
        )
    except Exception as exc:
        logger.error(
            "Trade-Min MongoDB connection failed: %s — falling back to in-memory storage",
            exc,
        )
        _motor_client = None
        _repository = InMemoryFallbackRepository()

    return _repository


async def _ensure_indexes(col: object) -> None:
    await col.create_index(  # type: ignore[union-attr]
        [("status", 1), ("urgency_tier", -1), ("arbitrage_rank", -1)],
        name="status_urgency_rank",
        background=True,
    )
    await col.create_index("hs_code", name="hs_code", background=True)  # type: ignore[union-attr]
    await col.create_index("market", name="market", background=True)  # type: ignore[union-attr]
    await col.create_index(  # type: ignore[union-attr]
        [("hs_code", 1), ("market", 1), ("scanned_at", -1)],
        name="hs_market_scanned",
        background=True,
    )
    await col.create_index(  # type: ignore[union-attr]
        "expires_at",
        expireAfterSeconds=0,
        name="ttl_expires_at",
        background=True,
    )


async def get_repository() -> InMemoryFallbackRepository | MongoOpportunityRepository:
    """Return the process-wide repository, initialising on first call."""
    if _repository is None:
        return await init_repository()
    return _repository


async def close_repository() -> None:
    """Close the Motor connection (no-op for in-memory repository)."""
    global _repository, _motor_client
    if _motor_client is not None:
        _motor_client.close()  # type: ignore[union-attr]
        _motor_client = None
        logger.info("Trade-Min MongoDB connection closed")
    _repository = None
