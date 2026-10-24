"""Sourcing service persistence layer.

Two concrete implementations satisfy the same ``SourcingRepository`` Protocol:

``InMemorySourcingRepository``
    Process-local dicts for RFQs and Quotes.  Used when ``SOURCING_MONGO_URI``
    is absent or the initial connection fails.

``MongoSourcingRepository``
    Motor + MongoDB Atlas.  Two collections (``rfqs`` + ``quotes``) with
    compound indexes for fast deal-scoped lookups.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .config import settings
from .schemas import QuoteRecord, RfqRecord

logger = logging.getLogger(__name__)

_repository: "SourcingRepository | None" = None
_motor_client = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class SourcingRepository(Protocol):
    async def save_rfq(self, rfq: RfqRecord) -> None: ...
    async def get_rfq(self, rfq_id: str) -> RfqRecord | None: ...
    async def list_rfqs(self, deal_id: str) -> list[RfqRecord]: ...

    async def save_quote(self, quote: QuoteRecord) -> None: ...
    async def get_quote(self, quote_id: str) -> QuoteRecord | None: ...
    async def list_quotes(
        self,
        deal_id: str | None = None,
        rfq_id: str | None = None,
    ) -> list[QuoteRecord]: ...


# ---------------------------------------------------------------------------
# In-memory fallback
# ---------------------------------------------------------------------------

class InMemorySourcingRepository:
    def __init__(self) -> None:
        self._rfqs: dict[str, RfqRecord] = {}
        self._quotes: dict[str, QuoteRecord] = {}

    async def save_rfq(self, rfq: RfqRecord) -> None:
        self._rfqs[rfq.rfq_id] = rfq

    async def get_rfq(self, rfq_id: str) -> RfqRecord | None:
        return self._rfqs.get(rfq_id)

    async def list_rfqs(self, deal_id: str) -> list[RfqRecord]:
        return [r for r in self._rfqs.values() if r.deal_id == deal_id]

    async def save_quote(self, quote: QuoteRecord) -> None:
        self._quotes[quote.quote_id] = quote

    async def get_quote(self, quote_id: str) -> QuoteRecord | None:
        return self._quotes.get(quote_id)

    async def list_quotes(
        self,
        deal_id: str | None = None,
        rfq_id: str | None = None,
    ) -> list[QuoteRecord]:
        qs = list(self._quotes.values())
        if deal_id:
            qs = [q for q in qs if q.deal_id == deal_id]
        if rfq_id:
            qs = [q for q in qs if q.rfq_id == rfq_id]
        return sorted(qs, key=lambda q: q.received_at, reverse=True)


# ---------------------------------------------------------------------------
# MongoDB (Motor) implementation
# ---------------------------------------------------------------------------

class MongoSourcingRepository:
    def __init__(self, rfq_col, quote_col) -> None:
        self._rfqs = rfq_col
        self._quotes = quote_col

    # ── RFQs ──────────────────────────────────────────────────────────────

    async def save_rfq(self, rfq: RfqRecord) -> None:
        doc = rfq.model_dump(mode="json")
        await self._rfqs.replace_one({"rfq_id": rfq.rfq_id}, doc, upsert=True)

    async def get_rfq(self, rfq_id: str) -> RfqRecord | None:
        doc = await self._rfqs.find_one({"rfq_id": rfq_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return RfqRecord(**doc)

    async def list_rfqs(self, deal_id: str) -> list[RfqRecord]:
        cursor = self._rfqs.find({"deal_id": deal_id}).sort("dispatched_at", -1)
        docs = await cursor.to_list(length=200)
        result = []
        for doc in docs:
            doc.pop("_id", None)
            try:
                result.append(RfqRecord(**doc))
            except Exception as exc:
                logger.warning("Skipping malformed RFQ document: %s", exc)
        return result

    # ── Quotes ─────────────────────────────────────────────────────────────

    async def save_quote(self, quote: QuoteRecord) -> None:
        doc = quote.model_dump(mode="json")
        await self._quotes.replace_one({"quote_id": quote.quote_id}, doc, upsert=True)

    async def get_quote(self, quote_id: str) -> QuoteRecord | None:
        doc = await self._quotes.find_one({"quote_id": quote_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return QuoteRecord(**doc)

    async def list_quotes(
        self,
        deal_id: str | None = None,
        rfq_id: str | None = None,
    ) -> list[QuoteRecord]:
        filt: dict = {}
        if deal_id:
            filt["deal_id"] = deal_id
        if rfq_id:
            filt["rfq_id"] = rfq_id
        cursor = self._quotes.find(filt).sort("received_at", -1).limit(200)
        docs = await cursor.to_list(length=200)
        result = []
        for doc in docs:
            doc.pop("_id", None)
            try:
                result.append(QuoteRecord(**doc))
            except Exception as exc:
                logger.warning("Skipping malformed Quote document: %s", exc)
        return result


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def _create_indexes(rfq_col, quote_col) -> None:
    from pymongo import ASCENDING, DESCENDING, IndexModel

    await rfq_col.create_indexes([
        IndexModel([("rfq_id", ASCENDING)], unique=True, name="rfq_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="rfq_deal_id"),
        IndexModel([("dispatched_at", DESCENDING)], name="rfq_dispatched_at"),
    ])
    await quote_col.create_indexes([
        IndexModel([("quote_id", ASCENDING)], unique=True, name="quote_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="quote_deal_id"),
        IndexModel([("rfq_id", ASCENDING)], name="quote_rfq_id"),
        IndexModel([("received_at", DESCENDING)], name="quote_received_at"),
    ])


async def init_repository() -> None:
    global _repository, _motor_client

    uri = settings.sourcing_mongo_uri
    if not uri:
        logger.warning(
            "SOURCING_MONGO_URI not set — Sourcing service falling back to "
            "in-memory store. Data will not survive a process restart."
        )
        _repository = InMemorySourcingRepository()
        return

    try:
        import motor.motor_asyncio

        client = motor.motor_asyncio.AsyncIOMotorClient(
            uri, serverSelectionTimeoutMS=5_000
        )
        await client.admin.command("ping")
        _motor_client = client

        db = client[settings.sourcing_mongo_db_name]
        rfq_col = db[settings.sourcing_rfq_collection]
        quote_col = db[settings.sourcing_quotes_collection]
        await _create_indexes(rfq_col, quote_col)

        _repository = MongoSourcingRepository(rfq_col, quote_col)
        logger.info(
            "Sourcing MongoDB repository ready (%s)",
            settings.sourcing_mongo_db_name,
        )
    except Exception as exc:
        logger.error(
            "Sourcing MongoDB connection failed — falling back to in-memory store: %s",
            exc,
        )
        _repository = InMemorySourcingRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None
        logger.info("Sourcing MongoDB client closed")


def get_repository() -> SourcingRepository:
    if _repository is None:
        raise RuntimeError(
            "Sourcing repository not initialised — call init_repository() at startup."
        )
    return _repository
