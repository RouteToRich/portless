"""HITL Approval persistence layer.

Protocol + InMemoryFallback + MongoRepository following the project pattern.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .config import settings
from .schemas import ApprovalRecord

logger = logging.getLogger(__name__)

_repository: "HitlApprovalRepository | None" = None
_motor_client = None


@runtime_checkable
class HitlApprovalRepository(Protocol):
    async def save(self, approval: ApprovalRecord) -> None: ...
    async def get(self, approval_id: str) -> ApprovalRecord | None: ...
    async def get_by_idempotency_key(self, key: str) -> ApprovalRecord | None: ...
    async def list_by_status(self, status: str | None) -> list[ApprovalRecord]: ...
    async def list_by_deal(self, deal_id: str) -> list[ApprovalRecord]: ...
    async def list_pending_expired(self, now_iso: str) -> list[ApprovalRecord]: ...


class InMemoryHitlApprovalRepository:
    def __init__(self) -> None:
        self._store: dict[str, ApprovalRecord] = {}

    async def save(self, approval: ApprovalRecord) -> None:
        self._store[approval.approval_id] = approval

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._store.get(approval_id)

    async def get_by_idempotency_key(self, key: str) -> ApprovalRecord | None:
        for a in self._store.values():
            if a.idempotency_key == key:
                return a
        return None

    async def list_by_status(self, status: str | None) -> list[ApprovalRecord]:
        if status is None:
            return list(self._store.values())
        return [a for a in self._store.values() if a.status == status]

    async def list_by_deal(self, deal_id: str) -> list[ApprovalRecord]:
        return [a for a in self._store.values() if a.deal_id == deal_id]

    async def list_pending_expired(self, now_iso: str) -> list[ApprovalRecord]:
        from datetime import datetime, timezone
        now = datetime.fromisoformat(now_iso)
        results = []
        for a in self._store.values():
            if a.status == "PENDING" and a.expires_at is not None:
                exp = a.expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp <= now:
                    results.append(a)
        return results


class MongoHitlApprovalRepository:
    def __init__(self, col) -> None:
        self._col = col

    async def save(self, approval: ApprovalRecord) -> None:
        doc = approval.model_dump(mode="json")
        await self._col.replace_one({"approval_id": approval.approval_id}, doc, upsert=True)

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        doc = await self._col.find_one({"approval_id": approval_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return ApprovalRecord(**doc)

    async def get_by_idempotency_key(self, key: str) -> ApprovalRecord | None:
        doc = await self._col.find_one({"idempotency_key": key})
        if not doc:
            return None
        doc.pop("_id", None)
        return ApprovalRecord(**doc)

    async def list_by_status(self, status: str | None) -> list[ApprovalRecord]:
        query = {} if status is None else {"status": status}
        results = []
        async for doc in self._col.find(query, sort=[("created_at", -1)]):
            doc.pop("_id", None)
            results.append(ApprovalRecord(**doc))
        return results

    async def list_by_deal(self, deal_id: str) -> list[ApprovalRecord]:
        results = []
        async for doc in self._col.find({"deal_id": deal_id}, sort=[("created_at", -1)]):
            doc.pop("_id", None)
            results.append(ApprovalRecord(**doc))
        return results

    async def list_pending_expired(self, now_iso: str) -> list[ApprovalRecord]:
        results = []
        async for doc in self._col.find(
            {"status": "PENDING", "expires_at": {"$lte": now_iso}},
            sort=[("expires_at", 1)],
        ):
            doc.pop("_id", None)
            results.append(ApprovalRecord(**doc))
        return results


async def _create_indexes(col) -> None:
    from pymongo import ASCENDING, DESCENDING, IndexModel
    await col.create_indexes([
        IndexModel([("approval_id", ASCENDING)], unique=True, name="approval_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="deal_id"),
        IndexModel([("status", ASCENDING)], name="status"),
        IndexModel([("idempotency_key", ASCENDING)], sparse=True, name="idempotency"),
        IndexModel([("expires_at", ASCENDING)], sparse=True, name="expires_at"),
        IndexModel([("created_at", DESCENDING)], name="created_at"),
    ])


async def init_repository() -> None:
    global _repository, _motor_client
    uri = settings.hitl_approval_mongo_uri
    if not uri:
        logger.warning("HITL_APPROVAL_MONGO_URI not set — using in-memory fallback")
        _repository = InMemoryHitlApprovalRepository()
        return
    try:
        import motor.motor_asyncio
        client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5_000)
        await client.admin.command("ping")
        _motor_client = client
        col = client[settings.hitl_approval_mongo_db_name][settings.hitl_approval_collection]
        await _create_indexes(col)
        _repository = MongoHitlApprovalRepository(col)
        logger.info("HitlApproval repository: MongoDB at %s", uri.split("@")[-1])
    except Exception as exc:
        logger.error("HitlApproval: MongoDB failed (%s) — in-memory fallback", exc)
        _repository = InMemoryHitlApprovalRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None


def get_repository() -> HitlApprovalRepository:
    if _repository is None:
        raise RuntimeError("HitlApproval repository not initialised — call init_repository() at startup.")
    return _repository
