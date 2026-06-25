"""Escrow-Guard persistence layer.

Protocol + InMemoryFallback + MongoRepository following the project pattern.

Two collections:
  - escrow_holds   — one document per deal (PENDING → FUNDED → RELEASED)
  - payouts        — one document per payout disbursement
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .config import settings
from .schemas import EscrowHoldRecord, PayoutRecord

logger = logging.getLogger(__name__)

_repository: "EscrowGuardRepository | None" = None
_motor_client = None


@runtime_checkable
class EscrowGuardRepository(Protocol):
    # Escrow holds
    async def save_hold(self, hold: EscrowHoldRecord) -> None: ...
    async def get_hold(self, escrow_id: str) -> EscrowHoldRecord | None: ...
    async def get_hold_by_deal(self, deal_id: str) -> EscrowHoldRecord | None: ...
    async def get_hold_by_ext_ref(self, ext_ref: str) -> EscrowHoldRecord | None: ...
    async def get_hold_by_idempotency_key(self, key: str) -> EscrowHoldRecord | None: ...

    # Payouts
    async def save_payout(self, payout: PayoutRecord) -> None: ...
    async def get_payout(self, payout_id: str) -> PayoutRecord | None: ...
    async def get_payout_by_idempotency_key(self, key: str) -> PayoutRecord | None: ...
    async def get_payout_by_ext_ref(self, ext_ref: str) -> PayoutRecord | None: ...
    async def list_payouts_by_deal(self, deal_id: str) -> list[PayoutRecord]: ...


class InMemoryEscrowGuardRepository:
    def __init__(self) -> None:
        self._holds: dict[str, EscrowHoldRecord] = {}
        self._payouts: dict[str, PayoutRecord] = {}

    # -- holds ----------------------------------------------------------------
    async def save_hold(self, hold: EscrowHoldRecord) -> None:
        self._holds[hold.escrow_id] = hold

    async def get_hold(self, escrow_id: str) -> EscrowHoldRecord | None:
        return self._holds.get(escrow_id)

    async def get_hold_by_deal(self, deal_id: str) -> EscrowHoldRecord | None:
        for h in self._holds.values():
            if h.deal_id == deal_id:
                return h
        return None

    async def get_hold_by_ext_ref(self, ext_ref: str) -> EscrowHoldRecord | None:
        for h in self._holds.values():
            if h.ext_ref == ext_ref:
                return h
        return None

    async def get_hold_by_idempotency_key(self, key: str) -> EscrowHoldRecord | None:
        for h in self._holds.values():
            if h.idempotency_key == key:
                return h
        return None

    # -- payouts --------------------------------------------------------------
    async def save_payout(self, payout: PayoutRecord) -> None:
        self._payouts[payout.payout_id] = payout

    async def get_payout(self, payout_id: str) -> PayoutRecord | None:
        return self._payouts.get(payout_id)

    async def get_payout_by_idempotency_key(self, key: str) -> PayoutRecord | None:
        for p in self._payouts.values():
            if p.idempotency_key == key:
                return p
        return None

    async def get_payout_by_ext_ref(self, ext_ref: str) -> PayoutRecord | None:
        for p in self._payouts.values():
            if p.ext_ref == ext_ref:
                return p
        return None

    async def list_payouts_by_deal(self, deal_id: str) -> list[PayoutRecord]:
        return [p for p in self._payouts.values() if p.deal_id == deal_id]


class MongoEscrowGuardRepository:
    def __init__(self, holds_col, payouts_col) -> None:
        self._holds = holds_col
        self._payouts = payouts_col

    # -- holds ----------------------------------------------------------------
    async def save_hold(self, hold: EscrowHoldRecord) -> None:
        doc = hold.model_dump(mode="json")
        await self._holds.replace_one({"escrow_id": hold.escrow_id}, doc, upsert=True)

    async def get_hold(self, escrow_id: str) -> EscrowHoldRecord | None:
        doc = await self._holds.find_one({"escrow_id": escrow_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return EscrowHoldRecord(**doc)

    async def get_hold_by_deal(self, deal_id: str) -> EscrowHoldRecord | None:
        doc = await self._holds.find_one({"deal_id": deal_id}, sort=[("created_at", -1)])
        if not doc:
            return None
        doc.pop("_id", None)
        return EscrowHoldRecord(**doc)

    async def get_hold_by_ext_ref(self, ext_ref: str) -> EscrowHoldRecord | None:
        doc = await self._holds.find_one({"ext_ref": ext_ref})
        if not doc:
            return None
        doc.pop("_id", None)
        return EscrowHoldRecord(**doc)

    async def get_hold_by_idempotency_key(self, key: str) -> EscrowHoldRecord | None:
        doc = await self._holds.find_one({"idempotency_key": key})
        if not doc:
            return None
        doc.pop("_id", None)
        return EscrowHoldRecord(**doc)

    # -- payouts --------------------------------------------------------------
    async def save_payout(self, payout: PayoutRecord) -> None:
        doc = payout.model_dump(mode="json")
        await self._payouts.replace_one({"payout_id": payout.payout_id}, doc, upsert=True)

    async def get_payout(self, payout_id: str) -> PayoutRecord | None:
        doc = await self._payouts.find_one({"payout_id": payout_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return PayoutRecord(**doc)

    async def get_payout_by_idempotency_key(self, key: str) -> PayoutRecord | None:
        doc = await self._payouts.find_one({"idempotency_key": key})
        if not doc:
            return None
        doc.pop("_id", None)
        return PayoutRecord(**doc)

    async def get_payout_by_ext_ref(self, ext_ref: str) -> PayoutRecord | None:
        doc = await self._payouts.find_one({"ext_ref": ext_ref})
        if not doc:
            return None
        doc.pop("_id", None)
        return PayoutRecord(**doc)

    async def list_payouts_by_deal(self, deal_id: str) -> list[PayoutRecord]:
        results = []
        async for doc in self._payouts.find({"deal_id": deal_id}, sort=[("created_at", -1)]):
            doc.pop("_id", None)
            results.append(PayoutRecord(**doc))
        return results


async def _create_indexes(holds_col, payouts_col) -> None:
    from pymongo import ASCENDING, DESCENDING, IndexModel
    await holds_col.create_indexes([
        IndexModel([("escrow_id", ASCENDING)], unique=True, name="escrow_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="deal_id"),
        IndexModel([("ext_ref", ASCENDING)], sparse=True, name="ext_ref"),
        IndexModel([("idempotency_key", ASCENDING)], sparse=True, name="idempotency"),
        IndexModel([("created_at", DESCENDING)], name="created_at"),
    ])
    await payouts_col.create_indexes([
        IndexModel([("payout_id", ASCENDING)], unique=True, name="payout_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="deal_id"),
        IndexModel([("idempotency_key", ASCENDING)], unique=True, name="idempotency_unique"),
        IndexModel([("ext_ref", ASCENDING)], sparse=True, name="ext_ref"),
        IndexModel([("created_at", DESCENDING)], name="created_at"),
    ])


async def init_repository() -> None:
    global _repository, _motor_client
    uri = settings.escrow_guard_mongo_uri
    if not uri:
        logger.warning("ESCROW_GUARD_MONGO_URI not set — using in-memory fallback")
        _repository = InMemoryEscrowGuardRepository()
        return
    try:
        import motor.motor_asyncio
        client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5_000)
        await client.admin.command("ping")
        _motor_client = client
        db = client[settings.escrow_guard_mongo_db_name]
        holds_col = db[settings.escrow_guard_holds_collection]
        payouts_col = db[settings.escrow_guard_payouts_collection]
        await _create_indexes(holds_col, payouts_col)
        _repository = MongoEscrowGuardRepository(holds_col, payouts_col)
        logger.info("EscrowGuard repository: MongoDB at %s", uri.split("@")[-1])
    except Exception as exc:
        logger.error("EscrowGuard: MongoDB failed (%s) — in-memory fallback", exc)
        _repository = InMemoryEscrowGuardRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None


def get_repository() -> EscrowGuardRepository:
    if _repository is None:
        raise RuntimeError("EscrowGuard repository not initialised — call init_repository() at startup.")
    return _repository
