"""Shield-Link persistence layer.

Protocol + InMemoryFallback + MongoRepository following the project pattern.
"""
from __future__ import annotations

import logging
from typing import Literal, Protocol, runtime_checkable

from .config import settings
from .schemas import ThirdPartyOrderRecord

logger = logging.getLogger(__name__)

_repository: "ShieldLinkRepository | None" = None
_motor_client = None


@runtime_checkable
class ShieldLinkRepository(Protocol):
    async def save(self, tpo: ThirdPartyOrderRecord) -> None: ...
    async def get(self, tpo_id: str) -> ThirdPartyOrderRecord | None: ...
    async def get_by_deal_and_type(
        self, deal_id: str, order_type: Literal["QC", "FREIGHT", "CHA"]
    ) -> ThirdPartyOrderRecord | None: ...
    async def get_by_ext_ref(self, ext_ref: str) -> ThirdPartyOrderRecord | None: ...
    async def list_by_deal(self, deal_id: str) -> list[ThirdPartyOrderRecord]: ...
    async def get_by_idempotency_key(self, key: str) -> ThirdPartyOrderRecord | None: ...


class InMemoryShieldLinkRepository:
    def __init__(self) -> None:
        self._store: dict[str, ThirdPartyOrderRecord] = {}

    async def save(self, tpo: ThirdPartyOrderRecord) -> None:
        self._store[tpo.tpo_id] = tpo

    async def get(self, tpo_id: str) -> ThirdPartyOrderRecord | None:
        return self._store.get(tpo_id)

    async def get_by_deal_and_type(
        self, deal_id: str, order_type: Literal["QC", "FREIGHT", "CHA"]
    ) -> ThirdPartyOrderRecord | None:
        for t in self._store.values():
            if t.deal_id == deal_id and t.order_type == order_type and t.status != "FAILED":
                return t
        return None

    async def get_by_ext_ref(self, ext_ref: str) -> ThirdPartyOrderRecord | None:
        for t in self._store.values():
            if t.ext_ref == ext_ref:
                return t
        return None

    async def list_by_deal(self, deal_id: str) -> list[ThirdPartyOrderRecord]:
        return [t for t in self._store.values() if t.deal_id == deal_id]

    async def get_by_idempotency_key(self, key: str) -> ThirdPartyOrderRecord | None:
        for t in self._store.values():
            if t.idempotency_key == key:
                return t
        return None


class MongoShieldLinkRepository:
    def __init__(self, col) -> None:
        self._col = col

    async def save(self, tpo: ThirdPartyOrderRecord) -> None:
        doc = tpo.model_dump(mode="json")
        await self._col.replace_one({"tpo_id": tpo.tpo_id}, doc, upsert=True)

    async def get(self, tpo_id: str) -> ThirdPartyOrderRecord | None:
        doc = await self._col.find_one({"tpo_id": tpo_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return ThirdPartyOrderRecord(**doc)

    async def get_by_deal_and_type(
        self, deal_id: str, order_type: Literal["QC", "FREIGHT", "CHA"]
    ) -> ThirdPartyOrderRecord | None:
        doc = await self._col.find_one(
            {"deal_id": deal_id, "order_type": order_type, "status": {"$ne": "FAILED"}},
            sort=[("created_at", -1)],
        )
        if not doc:
            return None
        doc.pop("_id", None)
        return ThirdPartyOrderRecord(**doc)

    async def get_by_ext_ref(self, ext_ref: str) -> ThirdPartyOrderRecord | None:
        doc = await self._col.find_one({"ext_ref": ext_ref})
        if not doc:
            return None
        doc.pop("_id", None)
        return ThirdPartyOrderRecord(**doc)

    async def list_by_deal(self, deal_id: str) -> list[ThirdPartyOrderRecord]:
        results = []
        async for doc in self._col.find({"deal_id": deal_id}, sort=[("created_at", -1)]):
            doc.pop("_id", None)
            results.append(ThirdPartyOrderRecord(**doc))
        return results

    async def get_by_idempotency_key(self, key: str) -> ThirdPartyOrderRecord | None:
        doc = await self._col.find_one({"idempotency_key": key})
        if not doc:
            return None
        doc.pop("_id", None)
        return ThirdPartyOrderRecord(**doc)


async def _create_indexes(col) -> None:
    from pymongo import ASCENDING, DESCENDING, IndexModel
    await col.create_indexes([
        IndexModel([("tpo_id", ASCENDING)], unique=True, name="tpo_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="deal_id"),
        IndexModel([("deal_id", ASCENDING), ("order_type", ASCENDING)], name="deal_type"),
        IndexModel([("ext_ref", ASCENDING)], sparse=True, name="ext_ref"),
        IndexModel([("idempotency_key", ASCENDING)], sparse=True, name="idempotency"),
        IndexModel([("created_at", DESCENDING)], name="created_at"),
    ])


async def init_repository() -> None:
    global _repository, _motor_client
    uri = settings.shield_link_mongo_uri
    if not uri:
        logger.warning("SHIELD_LINK_MONGO_URI not set — using in-memory fallback")
        _repository = InMemoryShieldLinkRepository()
        return
    try:
        import motor.motor_asyncio
        client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5_000)
        await client.admin.command("ping")
        _motor_client = client
        col = client[settings.shield_link_mongo_db_name][settings.shield_link_tpo_collection]
        await _create_indexes(col)
        _repository = MongoShieldLinkRepository(col)
        logger.info("ShieldLink repository: MongoDB at %s", uri.split("@")[-1])
    except Exception as exc:
        logger.error("ShieldLink: MongoDB failed (%s) — in-memory fallback", exc)
        _repository = InMemoryShieldLinkRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None


def get_repository() -> ShieldLinkRepository:
    if _repository is None:
        raise RuntimeError("ShieldLink repository not initialised — call init_repository() at startup.")
    return _repository
