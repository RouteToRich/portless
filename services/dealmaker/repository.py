"""DealMaker service persistence layer.

Two concrete implementations satisfy the same ``DealMakerRepository`` Protocol:

``InMemoryDealMakerRepository``
    Process-local dicts for PI and PO records.  Used automatically when
    ``DEALMAKER_MONGO_URI`` is absent or the initial connection fails.  Data
    is lost on process restart; acceptable for dev / CI.

``MongoDealMakerRepository``
    Motor + MongoDB Atlas.  Two collections (``proforma_invoices`` +
    ``purchase_orders``) with indexes for fast deal-scoped lookups and
    idempotency-key deduplication.

``init_repository()`` selects the implementation at startup.
``get_repository()`` returns the active instance.
``close_repository()`` releases the Motor client on shutdown.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .config import settings
from .schemas import PiRecord, PoRecord

logger = logging.getLogger(__name__)

_repository: "DealMakerRepository | None" = None
_motor_client = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DealMakerRepository(Protocol):
    async def save_pi(self, pi: PiRecord) -> None: ...
    async def get_pi(self, pi_id: str) -> PiRecord | None: ...
    async def get_pi_by_deal(self, deal_id: str) -> PiRecord | None: ...
    async def get_pi_by_idempotency_key(self, key: str) -> PiRecord | None: ...

    async def save_po(self, po: PoRecord) -> None: ...
    async def get_po(self, po_id: str) -> PoRecord | None: ...
    async def get_po_by_deal(self, deal_id: str) -> PoRecord | None: ...
    async def get_po_by_idempotency_key(self, key: str) -> PoRecord | None: ...


# ---------------------------------------------------------------------------
# In-memory fallback
# ---------------------------------------------------------------------------

class InMemoryDealMakerRepository:
    def __init__(self) -> None:
        self._pis: dict[str, PiRecord] = {}
        self._pos: dict[str, PoRecord] = {}

    # ── PI ────────────────────────────────────────────────────────────────

    async def save_pi(self, pi: PiRecord) -> None:
        self._pis[pi.pi_id] = pi

    async def get_pi(self, pi_id: str) -> PiRecord | None:
        return self._pis.get(pi_id)

    async def get_pi_by_deal(self, deal_id: str) -> PiRecord | None:
        for pi in self._pis.values():
            if pi.deal_id == deal_id and pi.status != "SUPERSEDED":
                return pi
        return None

    async def get_pi_by_idempotency_key(self, key: str) -> PiRecord | None:
        for pi in self._pis.values():
            if pi.idempotency_key == key:
                return pi
        return None

    # ── PO ────────────────────────────────────────────────────────────────

    async def save_po(self, po: PoRecord) -> None:
        self._pos[po.po_id] = po

    async def get_po(self, po_id: str) -> PoRecord | None:
        return self._pos.get(po_id)

    async def get_po_by_deal(self, deal_id: str) -> PoRecord | None:
        for po in self._pos.values():
            if po.deal_id == deal_id and po.status != "CANCELLED":
                return po
        return None

    async def get_po_by_idempotency_key(self, key: str) -> PoRecord | None:
        for po in self._pos.values():
            if po.idempotency_key == key:
                return po
        return None


# ---------------------------------------------------------------------------
# MongoDB (Motor) implementation
# ---------------------------------------------------------------------------

class MongoDealMakerRepository:
    def __init__(self, pi_col, po_col) -> None:
        self._pi = pi_col
        self._po = po_col

    # ── PI ────────────────────────────────────────────────────────────────

    async def save_pi(self, pi: PiRecord) -> None:
        doc = pi.model_dump(mode="json")
        await self._pi.replace_one({"pi_id": pi.pi_id}, doc, upsert=True)

    async def get_pi(self, pi_id: str) -> PiRecord | None:
        doc = await self._pi.find_one({"pi_id": pi_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return PiRecord(**doc)

    async def get_pi_by_deal(self, deal_id: str) -> PiRecord | None:
        doc = await self._pi.find_one(
            {"deal_id": deal_id, "status": {"$ne": "SUPERSEDED"}},
            sort=[("issued_at", -1)],
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return PiRecord(**doc)

    async def get_pi_by_idempotency_key(self, key: str) -> PiRecord | None:
        doc = await self._pi.find_one({"idempotency_key": key})
        if doc is None:
            return None
        doc.pop("_id", None)
        return PiRecord(**doc)

    # ── PO ────────────────────────────────────────────────────────────────

    async def save_po(self, po: PoRecord) -> None:
        doc = po.model_dump(mode="json")
        await self._po.replace_one({"po_id": po.po_id}, doc, upsert=True)

    async def get_po(self, po_id: str) -> PoRecord | None:
        doc = await self._po.find_one({"po_id": po_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return PoRecord(**doc)

    async def get_po_by_deal(self, deal_id: str) -> PoRecord | None:
        doc = await self._po.find_one(
            {"deal_id": deal_id, "status": {"$ne": "CANCELLED"}},
            sort=[("issued_at", -1)],
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return PoRecord(**doc)

    async def get_po_by_idempotency_key(self, key: str) -> PoRecord | None:
        doc = await self._po.find_one({"idempotency_key": key})
        if doc is None:
            return None
        doc.pop("_id", None)
        return PoRecord(**doc)


# ---------------------------------------------------------------------------
# Index creation
# ---------------------------------------------------------------------------

async def _create_indexes(pi_col, po_col) -> None:
    from pymongo import ASCENDING, DESCENDING, IndexModel

    await pi_col.create_indexes([
        IndexModel([("pi_id", ASCENDING)], unique=True, name="pi_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="pi_deal_id"),
        IndexModel([("idempotency_key", ASCENDING)], sparse=True, name="pi_idempotency"),
        IndexModel([("issued_at", DESCENDING)], name="pi_issued_at"),
    ])
    await po_col.create_indexes([
        IndexModel([("po_id", ASCENDING)], unique=True, name="po_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="po_deal_id"),
        IndexModel([("idempotency_key", ASCENDING)], sparse=True, name="po_idempotency"),
        IndexModel([("issued_at", DESCENDING)], name="po_issued_at"),
    ])


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def init_repository() -> None:
    global _repository, _motor_client

    uri = settings.dealmaker_mongo_uri
    if not uri:
        logger.warning(
            "DEALMAKER_MONGO_URI not set — DealMaker falling back to in-memory store. "
            "Data will not survive a process restart."
        )
        _repository = InMemoryDealMakerRepository()
        return

    try:
        import motor.motor_asyncio

        client = motor.motor_asyncio.AsyncIOMotorClient(
            uri, serverSelectionTimeoutMS=5_000
        )
        await client.admin.command("ping")
        _motor_client = client

        db = client[settings.dealmaker_mongo_db_name]
        pi_col = db[settings.dealmaker_pi_collection]
        po_col = db[settings.dealmaker_po_collection]
        await _create_indexes(pi_col, po_col)

        _repository = MongoDealMakerRepository(pi_col, po_col)
        logger.info(
            "DealMaker repository: MongoDB at %s (db=%s)",
            uri.split("@")[-1],
            settings.dealmaker_mongo_db_name,
        )
    except Exception as exc:
        logger.error(
            "DealMaker: MongoDB connection failed (%s) — falling back to in-memory store.",
            exc,
        )
        _repository = InMemoryDealMakerRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None


def get_repository() -> DealMakerRepository:
    if _repository is None:
        raise RuntimeError(
            "DealMaker repository not initialised — call init_repository() at startup."
        )
    return _repository
