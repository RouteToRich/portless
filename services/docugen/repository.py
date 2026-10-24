"""DocuGen persistence layer.

Two implementations behind a Protocol:
  InMemoryDocuGenRepository  — process-local dict; used when MongoDB is absent.
  MongoDocuGenRepository     — Motor + MongoDB; selected when DOCUGEN_MONGO_URI is set.

``init_repository()`` selects the implementation at startup.
``get_repository()`` returns the active instance.
``close_repository()`` releases the Motor client on shutdown.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from shared.domain.enums import DocumentType

from .config import settings
from .schemas import DocumentRecord

logger = logging.getLogger(__name__)

_repository: "DocuGenRepository | None" = None
_motor_client = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DocuGenRepository(Protocol):
    async def save(self, doc: DocumentRecord) -> None: ...
    async def get(self, doc_id: str) -> DocumentRecord | None: ...
    async def get_by_deal_and_type(
        self, deal_id: str, doc_type: DocumentType
    ) -> DocumentRecord | None: ...
    async def list_by_deal(self, deal_id: str) -> list[DocumentRecord]: ...
    async def get_by_idempotency_key(self, key: str) -> DocumentRecord | None: ...


# ---------------------------------------------------------------------------
# In-memory fallback
# ---------------------------------------------------------------------------

class InMemoryDocuGenRepository:
    def __init__(self) -> None:
        self._docs: dict[str, DocumentRecord] = {}

    async def save(self, doc: DocumentRecord) -> None:
        self._docs[doc.doc_id] = doc

    async def get(self, doc_id: str) -> DocumentRecord | None:
        return self._docs.get(doc_id)

    async def get_by_deal_and_type(
        self, deal_id: str, doc_type: DocumentType
    ) -> DocumentRecord | None:
        for doc in self._docs.values():
            if (
                doc.deal_id == deal_id
                and doc.doc_type == doc_type
                and doc.status != "FAILED"
            ):
                return doc
        return None

    async def list_by_deal(self, deal_id: str) -> list[DocumentRecord]:
        return [d for d in self._docs.values() if d.deal_id == deal_id]

    async def get_by_idempotency_key(self, key: str) -> DocumentRecord | None:
        for doc in self._docs.values():
            if doc.idempotency_key == key:
                return doc
        return None


# ---------------------------------------------------------------------------
# MongoDB (Motor) implementation
# ---------------------------------------------------------------------------

class MongoDocuGenRepository:
    def __init__(self, col) -> None:
        self._col = col

    async def save(self, doc: DocumentRecord) -> None:
        d = doc.model_dump(mode="json")
        await self._col.replace_one({"doc_id": doc.doc_id}, d, upsert=True)

    async def get(self, doc_id: str) -> DocumentRecord | None:
        d = await self._col.find_one({"doc_id": doc_id})
        if d is None:
            return None
        d.pop("_id", None)
        return DocumentRecord(**d)

    async def get_by_deal_and_type(
        self, deal_id: str, doc_type: DocumentType
    ) -> DocumentRecord | None:
        d = await self._col.find_one(
            {"deal_id": deal_id, "doc_type": doc_type.value, "status": {"$ne": "FAILED"}},
            sort=[("created_at", -1)],
        )
        if d is None:
            return None
        d.pop("_id", None)
        return DocumentRecord(**d)

    async def list_by_deal(self, deal_id: str) -> list[DocumentRecord]:
        cursor = self._col.find({"deal_id": deal_id}, sort=[("created_at", -1)])
        results = []
        async for d in cursor:
            d.pop("_id", None)
            results.append(DocumentRecord(**d))
        return results

    async def get_by_idempotency_key(self, key: str) -> DocumentRecord | None:
        d = await self._col.find_one({"idempotency_key": key})
        if d is None:
            return None
        d.pop("_id", None)
        return DocumentRecord(**d)


# ---------------------------------------------------------------------------
# Index creation
# ---------------------------------------------------------------------------

async def _create_indexes(col) -> None:
    from pymongo import ASCENDING, DESCENDING, IndexModel

    await col.create_indexes([
        IndexModel([("doc_id", ASCENDING)], unique=True, name="doc_id_unique"),
        IndexModel([("deal_id", ASCENDING)], name="deal_id"),
        IndexModel([("deal_id", ASCENDING), ("doc_type", ASCENDING)], name="deal_doc_type"),
        IndexModel([("idempotency_key", ASCENDING)], sparse=True, name="idempotency"),
        IndexModel([("created_at", DESCENDING)], name="created_at"),
    ])


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def init_repository() -> None:
    global _repository, _motor_client

    uri = settings.docugen_mongo_uri
    if not uri:
        logger.warning(
            "DOCUGEN_MONGO_URI not set — DocuGen falling back to in-memory store. "
            "Data will not survive a process restart."
        )
        _repository = InMemoryDocuGenRepository()
        return

    try:
        import motor.motor_asyncio

        client = motor.motor_asyncio.AsyncIOMotorClient(
            uri, serverSelectionTimeoutMS=5_000
        )
        await client.admin.command("ping")
        _motor_client = client

        col = client[settings.docugen_mongo_db_name][settings.docugen_docs_collection]
        await _create_indexes(col)

        _repository = MongoDocuGenRepository(col)
        logger.info(
            "DocuGen repository: MongoDB at %s (db=%s)",
            uri.split("@")[-1],
            settings.docugen_mongo_db_name,
        )
    except Exception as exc:
        logger.error(
            "DocuGen: MongoDB connection failed (%s) — falling back to in-memory store.",
            exc,
        )
        _repository = InMemoryDocuGenRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None


def get_repository() -> DocuGenRepository:
    if _repository is None:
        raise RuntimeError(
            "DocuGen repository not initialised — call init_repository() at startup."
        )
    return _repository
