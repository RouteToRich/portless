"""MongoDB connection manager for the LLM Gateway.

Wraps a Motor async client with:
  - Connection pooling (Motor default: 100 connections)
  - Automatic index creation on first use (idempotent)
  - Ping / health check
  - Graceful shutdown

One MongoConnection per (uri, db_name) pair is cached at module level so
LLMClient instances share the same pool rather than opening new sockets.

Usage::

    from shared.llm_gateway.mongo_connection import get_connection

    conn = get_connection()                    # uses settings defaults
    col  = conn.get_collection("llm_requests")
    await col.insert_one({"key": "value"})
    healthy = await conn.ping()
    # at app shutdown:
    await conn.close()
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── module-level pool: one client per (uri, db) ─────────────────────────────
_pool: dict[str, "MongoConnection"] = {}
_pool_lock = asyncio.Lock()


async def get_connection(
    uri: str | None = None,
    db_name: str | None = None,
) -> "MongoConnection":
    """Return (or create) the shared MongoConnection for this (uri, db) pair.

    Safe to call concurrently — uses asyncio.Lock to prevent duplicate clients.
    Passing uri=None / db_name=None falls back to LLMGatewaySettings values.
    """
    from .config import llm_gateway_settings

    resolved_uri = uri or llm_gateway_settings.mongo_uri
    resolved_db = db_name or llm_gateway_settings.mongo_db_name
    key = f"{resolved_uri}::{resolved_db}"

    async with _pool_lock:
        if key not in _pool:
            conn = MongoConnection(uri=resolved_uri, db_name=resolved_db)
            await conn.connect()
            _pool[key] = conn
            logger.info("MongoDB connection established: db=%s", resolved_db)
        return _pool[key]


def get_connection_sync(
    uri: str | None = None,
    db_name: str | None = None,
) -> "MongoConnection":
    """Create a MongoConnection synchronously (Motor client is lazy-connect).

    Use this in __init__ methods. The actual TCP handshake happens on
    the first awaited operation. Call connect() explicitly to warm up.
    """
    from .config import llm_gateway_settings

    resolved_uri = uri or llm_gateway_settings.mongo_uri
    resolved_db = db_name or llm_gateway_settings.mongo_db_name
    key = f"{resolved_uri}::{resolved_db}"

    if key in _pool:
        return _pool[key]
    conn = MongoConnection(uri=resolved_uri, db_name=resolved_db)
    _pool[key] = conn
    return conn


async def close_all() -> None:
    """Close every pooled connection (call from app shutdown handler)."""
    async with _pool_lock:
        for conn in _pool.values():
            await conn.close()
        _pool.clear()
    logger.info("All MongoDB connections closed.")


class MongoConnection:
    """Thin wrapper around a Motor AsyncIOMotorClient.

    Responsibilities:
      - Hold the client + db reference
      - Provide get_collection() so callers never touch the client directly
      - Manage index creation (idempotent, guarded by asyncio.Lock)
      - Expose ping() for health checks
    """

    def __init__(self, uri: str, db_name: str) -> None:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError as exc:
            raise RuntimeError(
                "motor is not installed. "
                "Add 'motor>=3.3' to requirements.txt and reinstall."
            ) from exc

        self._uri = uri
        self._db_name = db_name
        self._client = AsyncIOMotorClient(
            uri,
            # Connection pool
            maxPoolSize=50,
            minPoolSize=2,
            # Timeouts (ms)
            connectTimeoutMS=5_000,
            serverSelectionTimeoutMS=5_000,
            socketTimeoutMS=30_000,
            # Write concern: wait for primary acknowledgement
            w=1,
            # TLS — Atlas requires it; Motor enables it automatically for srv URIs
        )
        self._db = self._client[db_name]
        # Per-collection index-creation guard
        self._index_locks: dict[str, asyncio.Lock] = {}
        self._indexes_done: set[str] = set()

    # ---------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        """Force the connection and verify it is reachable."""
        await self._client.admin.command("ping")

    async def close(self) -> None:
        """Release the Motor client and all its connections."""
        self._client.close()

    # ----------------------------------------------------------------- public

    def get_collection(self, name: str) -> Any:
        """Return the Motor collection for `name` in this database."""
        return self._db[name]

    def get_database(self) -> Any:
        """Return the Motor database handle (needed for e.g. GridFS buckets)."""
        return self._db

    async def ensure_indexes(
        self,
        collection_name: str,
        indexes: list[dict[str, Any]],
    ) -> None:
        """Create indexes on `collection_name` if not already created.

        Each entry in `indexes` is a dict with keys:
          - "keys": list of (field, direction) tuples  e.g. [("created_at", -1)]
          - "options": dict of create_index kwargs     e.g. {"unique": True}

        Idempotent — calling multiple times is safe (MongoDB deduplicates).
        Guarded by a per-collection asyncio.Lock to avoid duplicate creation.
        """
        if collection_name in self._indexes_done:
            return

        if collection_name not in self._index_locks:
            self._index_locks[collection_name] = asyncio.Lock()

        async with self._index_locks[collection_name]:
            if collection_name in self._indexes_done:
                return  # double-checked inside the lock

            col = self.get_collection(collection_name)
            for idx in indexes:
                try:
                    await col.create_index(idx["keys"], **idx.get("options", {}))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Index creation failed on %s.%s: %s",
                        self._db_name,
                        collection_name,
                        exc,
                    )
            self._indexes_done.add(collection_name)
            logger.debug("Indexes ensured on %s.%s", self._db_name, collection_name)

    async def ping(self) -> bool:
        """Return True if the server is reachable within the timeout."""
        try:
            await self._client.admin.command("ping")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("MongoDB ping failed: %s", exc)
            return False

    # ---------------------------------------------------------------- repr

    def __repr__(self) -> str:
        return f"MongoConnection(db={self._db_name!r})"
