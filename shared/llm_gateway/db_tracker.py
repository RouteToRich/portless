"""MongoDB-backed request tracker for the LLM Gateway.

Replaces the append-only JSONL file tracker with a MongoDB collection so that:
  - Multiple FastAPI workers can share one tracking store (no file-lock contention)
  - Records are queryable via MongoDB aggregation (no grep on disk)
  - Historical data persists across restarts (no in-memory reset)
  - Per-deal cost attribution is easy via JOIN in the application layer

Database : gateway
Collection: llm_requests  (created automatically on first write)

Document shape::

    {
      "_id":              ObjectId,
      "request_id":       "uuid4",           # unique index
      "session_id":       "uuid4",           # groups requests from one process startup
      "caller":           "negotiation_agent",
      "model":            "qwen/qwen3-32b",
      "status":           "success",         # pending | success | fallback | error
      "prompt_tokens":    800,
      "completion_tokens": 240,
      "total_tokens":     1040,
      "latency_ms":       1230.5,
      "error_message":    "",
      "fallback_model":   "",
      "retry_count":      0,
      "estimated_cost_usd": 0.000302,
      "metadata":         {},
      "created_at":       ISODate("..."),    # UTC — set on start_request
      "completed_at":     ISODate("..."),    # UTC — set on complete_request
    }

Interface matches RequestTracker (same method signatures) except that every
method is **async** — callers must await them.  LLMClient handles both
sync and async trackers transparently via asyncio.iscoroutinefunction().

Usage::

    from shared.llm_gateway.db_tracker import MongoTracker

    tracker = MongoTracker.from_settings()
    client  = LLMClient(tracker=tracker)

    # Or in a FastAPI lifespan:
    async with asynccontextmanager as lifespan:
        tracker = await MongoTracker.create()
        yield {"tracker": tracker}
        await tracker.close()
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .tracker import RequestRecord

logger = logging.getLogger(__name__)

# ── collection-level index definition ───────────────────────────────────────

_INDEXES = [
    # Fast lookup by request_id for update operations
    {"keys": [("request_id", 1)], "options": {"unique": True, "name": "idx_request_id"}},
    # Most-recent queries
    {"keys": [("created_at", -1)], "options": {"name": "idx_created_at"}},
    # Per-caller queries and dashboards
    {"keys": [("caller", 1), ("created_at", -1)], "options": {"name": "idx_caller_ts"}},
    # Filter by status (error rate dashboards)
    {"keys": [("status", 1)], "options": {"name": "idx_status"}},
    # Filter by model (model-level analytics)
    {"keys": [("model", 1)], "options": {"name": "idx_model"}},
    # Group by session (per-startup queries)
    {"keys": [("session_id", 1)], "options": {"name": "idx_session_id"}},
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _doc_to_record(doc: dict[str, Any]) -> RequestRecord:
    return RequestRecord(
        request_id=doc.get("request_id", ""),
        caller=doc.get("caller", ""),
        model=doc.get("model", ""),
        prompt_tokens=doc.get("prompt_tokens", 0),
        completion_tokens=doc.get("completion_tokens", 0),
        total_tokens=doc.get("total_tokens", 0),
        latency_ms=doc.get("latency_ms", 0.0),
        status=doc.get("status", "unknown"),
        error_message=doc.get("error_message", ""),
        fallback_model=doc.get("fallback_model", ""),
        retry_count=doc.get("retry_count", 0),
        created_at=doc.get("created_at", _utcnow()),
        metadata=doc.get("metadata", {}),
    )


class MongoTracker:
    """Async MongoDB-backed tracker with the same interface as RequestTracker.

    All public methods are async — callers must await them.
    LLMClient dispatches via asyncio.iscoroutinefunction() so existing sync
    code and tests are not affected.

    Error isolation: MongoDB failures are caught and logged; they never
    propagate to LLM call failures.  A request whose MongoDB write failed
    is still returned to the caller — only the tracking record is lost.
    """

    def __init__(
        self,
        connection: Any | None = None,   # MongoConnection | None
        collection_name: str = "llm_requests",
        session_id: str | None = None,
    ) -> None:
        from .mongo_connection import get_connection_sync
        self._conn = connection or get_connection_sync()
        self._collection_name = collection_name
        self._session_id = session_id or str(uuid4())
        self._col = self._conn.get_collection(collection_name)
        self._indexes_ensured = False

    # ──────────────────────────────────────────────── factory

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "MongoTracker":
        """Create a MongoTracker from LLMGatewaySettings (or defaults)."""
        from .config import llm_gateway_settings
        from .mongo_connection import get_connection_sync

        s = settings or llm_gateway_settings
        conn = get_connection_sync(uri=s.mongo_uri, db_name=s.mongo_db_name)
        return cls(connection=conn, collection_name=s.mongo_collection)

    @classmethod
    async def create(
        cls,
        uri: str | None = None,
        db_name: str | None = None,
        collection_name: str = "llm_requests",
    ) -> "MongoTracker":
        """Async factory — creates the connection, verifies it, returns a warm tracker.

        Prefer this in FastAPI lifespan handlers where you want to fail fast
        if MongoDB is unreachable at startup.
        """
        from .mongo_connection import get_connection

        conn = await get_connection(uri=uri, db_name=db_name)
        tracker = cls(connection=conn, collection_name=collection_name)
        await tracker._ensure_indexes()
        return tracker

    # ──────────────────────────────────────────────── lifecycle

    async def close(self) -> None:
        """Close the underlying connection (call from app shutdown)."""
        await self._conn.close()

    # ──────────────────────────────────────────────── public interface

    async def start_request(self, caller: str = "", model: str = "", **metadata: Any) -> RequestRecord:
        """Insert a pending document and return a RequestRecord.

        The RequestRecord is created locally (UUID generated here) so the
        caller has an ID even if the MongoDB insert fails.
        """
        record = RequestRecord(caller=caller, model=model, metadata=dict(metadata))
        try:
            await self._ensure_indexes()
            await self._col.update_one(
                {"request_id": record.request_id},
                {
                    "$setOnInsert": {
                        "request_id": record.request_id,
                        "session_id": self._session_id,
                        "caller": caller,
                        "model": model,
                        "metadata": dict(metadata),
                        "created_at": record.created_at,
                    },
                    "$set": {
                        "status": "pending",
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "latency_ms": 0.0,
                        "error_message": "",
                        "fallback_model": "",
                        "retry_count": 0,
                        "estimated_cost_usd": 0.0,
                    },
                },
                upsert=True,
            )
            logger.debug(
                "MongoTracker: started request_id=%s caller=%s model=%s",
                record.request_id, caller, model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "MongoTracker: start_request insert failed (tracking lost) — %s: %s",
                type(exc).__name__, exc,
            )
        return record

    async def complete_request(
        self,
        record: RequestRecord,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status: str = "success",
        error_message: str = "",
        fallback_model: str = "",
        retry_count: int = 0,
    ) -> None:
        """Update the document with final outcome fields.

        Uses upsert so the write succeeds even if start_request's insert
        failed (e.g. transient network hiccup at request start).
        """
        # Mirror updates on the in-memory record (same as RequestTracker)
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        record.total_tokens = prompt_tokens + completion_tokens
        record.latency_ms = latency_ms
        record.status = status
        record.error_message = error_message
        record.fallback_model = fallback_model
        record.retry_count = retry_count

        if status == "error":
            logger.warning(
                "LLM request failed | id=%s model=%s retries=%d error=%s",
                record.request_id, record.model, retry_count, error_message,
            )
        else:
            logger.info(
                "LLM request ok | id=%s model=%s tokens=%d latency=%.0fms "
                "status=%s retries=%d fallback=%s",
                record.request_id, record.model, record.total_tokens,
                latency_ms, status, retry_count, fallback_model or "—",
            )

        try:
            estimated_cost = record.metadata.get("estimated_cost_usd", 0.0)
            await self._col.update_one(
                {"request_id": record.request_id},
                {
                    "$set": {
                        "status": status,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "latency_ms": round(latency_ms, 2),
                        "error_message": error_message,
                        "fallback_model": fallback_model,
                        "retry_count": retry_count,
                        "estimated_cost_usd": estimated_cost,
                        "completed_at": _utcnow(),
                    },
                    # In case start_request insert failed, create the document now
                    "$setOnInsert": {
                        "request_id": record.request_id,
                        "session_id": self._session_id,
                        "caller": record.caller,
                        "model": record.model,
                        "created_at": record.created_at,
                        "metadata": record.metadata,
                    },
                },
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "MongoTracker: complete_request update failed (tracking lost) — %s: %s",
                type(exc).__name__, exc,
            )

    async def get_records(
        self,
        limit: int = 100,
        caller: str | None = None,
        status: str | None = None,
        model: str | None = None,
        session_only: bool = False,
    ) -> list[RequestRecord]:
        """Return the most recent `limit` records, with optional filters.

        Args:
            limit: maximum number of documents to return.
            caller: filter to this caller only.
            status: filter to this status only.
            model: filter to this model only.
            session_only: restrict to documents from this process startup.
        """
        query: dict[str, Any] = {}
        if caller:
            query["caller"] = caller
        if status:
            query["status"] = status
        if model:
            query["model"] = model
        if session_only:
            query["session_id"] = self._session_id

        try:
            cursor = self._col.find(query, sort=[("created_at", -1)], limit=limit)
            docs = await cursor.to_list(length=limit)
            return [_doc_to_record(doc) for doc in docs]
        except Exception as exc:  # noqa: BLE001
            logger.error("MongoTracker: get_records failed — %s: %s", type(exc).__name__, exc)
            return []

    async def get_stats(
        self,
        caller: str | None = None,
        session_only: bool = False,
    ) -> dict[str, Any]:
        """Run an aggregation pipeline and return the same shape as RequestTracker.get_stats().

        Optional filters: by caller or this session only.
        """
        match: dict[str, Any] = {"status": {"$ne": "pending"}}
        if caller:
            match["caller"] = caller
        if session_only:
            match["session_id"] = self._session_id

        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "successes": {
                        "$sum": {"$cond": [{"$eq": ["$status", "success"]}, 1, 0]}
                    },
                    "fallbacks": {
                        "$sum": {"$cond": [{"$eq": ["$status", "fallback"]}, 1, 0]}
                    },
                    "errors": {
                        "$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}
                    },
                    "total_tokens": {"$sum": "$total_tokens"},
                    "total_retries": {"$sum": "$retry_count"},
                    "avg_latency_ms": {"$avg": "$latency_ms"},
                    "total_cost_usd": {"$sum": "$estimated_cost_usd"},
                }
            },
        ]

        try:
            results = await self._col.aggregate(pipeline).to_list(length=1)
            if not results:
                return {"total_requests": 0}

            row = results[0]
            total = row["total"]
            successes = row["successes"]
            return {
                "total_requests": total,
                "successes": successes,
                "fallbacks": row["fallbacks"],
                "errors": row["errors"],
                "success_rate": round(successes / total, 3) if total else 0.0,
                "total_tokens": row["total_tokens"],
                "avg_latency_ms": round(row.get("avg_latency_ms") or 0.0, 1),
                "total_retries": row["total_retries"],
                "total_cost_usd": round(row.get("total_cost_usd") or 0.0, 6),
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("MongoTracker: get_stats failed — %s: %s", type(exc).__name__, exc)
            return {"total_requests": 0, "error": str(exc)}

    async def get_model_breakdown(self) -> list[dict[str, Any]]:
        """Return per-model aggregated stats (for dashboards)."""
        pipeline = [
            {"$match": {"status": {"$ne": "pending"}}},
            {
                "$group": {
                    "_id": "$model",
                    "requests": {"$sum": 1},
                    "errors": {
                        "$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}
                    },
                    "total_tokens": {"$sum": "$total_tokens"},
                    "avg_latency_ms": {"$avg": "$latency_ms"},
                    "total_cost_usd": {"$sum": "$estimated_cost_usd"},
                }
            },
            {"$sort": {"requests": -1}},
        ]
        try:
            docs = await self._col.aggregate(pipeline).to_list(length=100)
            return [
                {
                    "model": d["_id"],
                    "requests": d["requests"],
                    "errors": d["errors"],
                    "error_rate": round(d["errors"] / d["requests"], 3) if d["requests"] else 0.0,
                    "total_tokens": d["total_tokens"],
                    "avg_latency_ms": round(d.get("avg_latency_ms") or 0.0, 1),
                    "total_cost_usd": round(d.get("total_cost_usd") or 0.0, 6),
                }
                for d in docs
            ]
        except Exception as exc:  # noqa: BLE001
            logger.error("MongoTracker: get_model_breakdown failed — %s: %s", type(exc).__name__, exc)
            return []

    async def get_caller_breakdown(self) -> list[dict[str, Any]]:
        """Return per-caller aggregated stats (for per-agent cost attribution)."""
        pipeline = [
            {"$match": {"status": {"$ne": "pending"}}},
            {
                "$group": {
                    "_id": "$caller",
                    "requests": {"$sum": 1},
                    "total_tokens": {"$sum": "$total_tokens"},
                    "total_cost_usd": {"$sum": "$estimated_cost_usd"},
                    "errors": {
                        "$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}
                    },
                }
            },
            {"$sort": {"total_cost_usd": -1}},
        ]
        try:
            docs = await self._col.aggregate(pipeline).to_list(length=100)
            return [
                {
                    "caller": d["_id"],
                    "requests": d["requests"],
                    "total_tokens": d["total_tokens"],
                    "total_cost_usd": round(d.get("total_cost_usd") or 0.0, 6),
                    "errors": d["errors"],
                }
                for d in docs
            ]
        except Exception as exc:  # noqa: BLE001
            logger.error("MongoTracker: get_caller_breakdown failed — %s: %s", type(exc).__name__, exc)
            return []

    # ──────────────────────────────────────────────── internals

    async def _ensure_indexes(self) -> None:
        if self._indexes_ensured:
            return
        await self._conn.ensure_indexes(self._collection_name, _INDEXES)
        self._indexes_ensured = True

    def __repr__(self) -> str:
        return (
            f"MongoTracker(db={self._conn._db_name!r}, "
            f"collection={self._collection_name!r}, "
            f"session={self._session_id[:8]}...)"
        )
