"""Application-wide MongoDB cache with TTL + in-memory fallback.

A single shared cache any module can use for read-only / idempotent results
(external lookups, verifications, quotes, LLM responses). Entries live in one
Mongo collection partitioned by ``namespace`` and auto-expire via a TTL index
on ``expires_at``. When Mongo is unavailable the cache degrades to an in-process
dict so callers keep working (just without cross-process sharing or durability).

NEVER cache mutating or financial operations (bookings, payments, payouts,
document issuance, webhooks). Cache only safe, repeatable reads.

Public API::

    from shared.cache import cache_get, cache_set, make_key, cached

    key = make_key("gstn", gstin)
    hit = await cache_get("regulatory:gstn", key)
    if hit is None:
        hit = await fetch()
        await cache_set("regulatory:gstn", key, hit, ttl_seconds=86_400)

    @cached("macmap", ttl_seconds=604_800)
    async def lookup_tariff(hs_code: str, market: str) -> dict: ...

All operations are best-effort: ``cache_get`` / ``cache_set`` swallow every error
so a cache outage degrades to "always call the source" rather than breaking.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol, TypeVar, runtime_checkable

from .config import cache_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key builder
# ---------------------------------------------------------------------------

def make_key(*parts: Any) -> str:
    """Deterministic cache key from arbitrary JSON-serialisable parts.

    Dicts are serialised with sorted keys so logically-equal inputs collide.
    Returns a short sha256 hex digest.
    """
    payload = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class CacheBackend(Protocol):
    async def get(self, namespace: str, key: str) -> Any | None: ...
    async def set(self, namespace: str, key: str, value: Any, ttl_seconds: int) -> None: ...
    async def delete(self, namespace: str, key: str) -> None: ...
    async def clear(self, namespace: str | None = None) -> int: ...


def _doc_id(namespace: str, key: str) -> str:
    return f"{namespace}:{key}"


# ---------------------------------------------------------------------------
# In-memory fallback
# ---------------------------------------------------------------------------

class InMemoryCacheBackend:
    """Process-local dict with manual expiry. Lost on restart."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[datetime, Any]] = {}

    async def get(self, namespace: str, key: str) -> Any | None:
        entry = self._store.get(_doc_id(namespace, key))
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= datetime.now(timezone.utc):
            self._store.pop(_doc_id(namespace, key), None)
            return None
        return value

    async def set(self, namespace: str, key: str, value: Any, ttl_seconds: int) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        self._store[_doc_id(namespace, key)] = (expires_at, value)

    async def delete(self, namespace: str, key: str) -> None:
        self._store.pop(_doc_id(namespace, key), None)

    async def clear(self, namespace: str | None = None) -> int:
        if namespace is None:
            n = len(self._store)
            self._store.clear()
            return n
        prefix = f"{namespace}:"
        keys = [k for k in self._store if k.startswith(prefix)]
        for k in keys:
            self._store.pop(k, None)
        return len(keys)


# ---------------------------------------------------------------------------
# MongoDB backend (TTL index on expires_at)
# ---------------------------------------------------------------------------

class MongoCacheBackend:
    """Async MongoDB cache. One collection, partitioned by ``namespace``."""

    def __init__(self, collection: object) -> None:
        self._col = collection  # AsyncIOMotorCollection

    async def get(self, namespace: str, key: str) -> Any | None:
        doc = await self._col.find_one({"_id": _doc_id(namespace, key)})  # type: ignore[union-attr]
        if doc is None:
            return None
        # Honour expiry even if Mongo's ~60s TTL sweep hasn't run yet.
        expires_at = doc.get("expires_at")
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return None
        return doc.get("payload")

    async def set(self, namespace: str, key: str, value: Any, ttl_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        doc = {
            "_id": _doc_id(namespace, key),
            "namespace": namespace,
            "payload": value,
            "cached_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        }
        await self._col.replace_one({"_id": doc["_id"]}, doc, upsert=True)  # type: ignore[union-attr]

    async def delete(self, namespace: str, key: str) -> None:
        await self._col.delete_one({"_id": _doc_id(namespace, key)})  # type: ignore[union-attr]

    async def clear(self, namespace: str | None = None) -> int:
        query = {} if namespace is None else {"namespace": namespace}
        result = await self._col.delete_many(query)  # type: ignore[union-attr]
        return result.deleted_count


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_backend: InMemoryCacheBackend | MongoCacheBackend | None = None
_motor_client: object | None = None


async def init_cache() -> InMemoryCacheBackend | MongoCacheBackend:
    """Initialise and return the process-wide cache backend. Idempotent."""
    global _backend, _motor_client

    if _backend is not None:
        return _backend

    uri = cache_settings.resolved_uri()
    if not uri:
        logger.info("Cache: no Mongo URI resolved — using in-memory backend.")
        _backend = InMemoryCacheBackend()
        return _backend

    try:
        from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[import]
        _motor_client = AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
        )
        col = _motor_client[cache_settings.mongo_db_name][cache_settings.collection]  # type: ignore[index]
        await col.create_index(  # type: ignore[union-attr]
            "expires_at", expireAfterSeconds=0, name="ttl_expires_at", background=True
        )
        await col.create_index("namespace", name="namespace", background=True)  # type: ignore[union-attr]
        _backend = MongoCacheBackend(col)
        logger.info(
            "Cache: MongoDB connected (db=%s col=%s)",
            cache_settings.mongo_db_name, cache_settings.collection,
        )
    except Exception as exc:
        logger.warning("Cache: MongoDB connection failed (%s) — using in-memory backend.", exc)
        _motor_client = None
        _backend = InMemoryCacheBackend()

    return _backend


async def get_backend() -> InMemoryCacheBackend | MongoCacheBackend:
    if _backend is None:
        return await init_cache()
    return _backend


async def close_cache() -> None:
    """Close the Motor client and reset the singleton (no-op for in-memory)."""
    global _backend, _motor_client
    if _motor_client is not None:
        _motor_client.close()  # type: ignore[union-attr]
        _motor_client = None
        logger.info("Cache: MongoDB connection closed")
    _backend = None


def reset_for_tests(backend: InMemoryCacheBackend | MongoCacheBackend | None = None) -> None:
    """Swap in a fresh backend synchronously (test helper)."""
    global _backend, _motor_client
    _motor_client = None
    _backend = backend if backend is not None else InMemoryCacheBackend()


# ---------------------------------------------------------------------------
# Best-effort wrappers — never raise into the caller
# ---------------------------------------------------------------------------

async def cache_get(namespace: str, key: str) -> Any | None:
    """Look up a cached value. Returns None on miss, disabled cache, or error."""
    if not cache_settings.enabled:
        return None
    try:
        backend = await get_backend()
        return await backend.get(namespace, key)
    except Exception as exc:
        logger.debug("cache_get failed (%s/%s): %s", namespace, key, exc)
        return None


async def cache_set(namespace: str, key: str, value: Any, ttl_seconds: int) -> None:
    """Store a value. Silently no-ops when disabled or on any error."""
    if not cache_settings.enabled:
        return
    try:
        backend = await get_backend()
        await backend.set(namespace, key, value, ttl_seconds)
    except Exception as exc:
        logger.debug("cache_set failed (%s/%s): %s", namespace, key, exc)


async def cache_clear(namespace: str | None = None) -> int:
    try:
        backend = await get_backend()
        return await backend.clear(namespace)
    except Exception as exc:
        logger.debug("cache_clear failed (%s): %s", namespace, exc)
        return 0


# ---------------------------------------------------------------------------
# Decorator for async read-only functions
# ---------------------------------------------------------------------------

T = TypeVar("T")


def cached(
    namespace: str,
    ttl_seconds: int,
    key_func: Callable[..., str] | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Cache an async function's dict/JSON result in the shared cache.

    ``key_func`` builds the cache key from the call args; it defaults to hashing
    the positional + keyword arguments. Only non-empty results are stored, so
    failures/empties are never cached. Apply ONLY to idempotent reads.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            if key_func is not None:
                key = key_func(*args, **kwargs)
            else:
                key = make_key(func.__qualname__, args, kwargs)

            hit = await cache_get(namespace, key)
            if hit is not None:
                return hit  # type: ignore[return-value]

            result = await func(*args, **kwargs)
            if result:  # skip None / empty
                await cache_set(namespace, key, result, ttl_seconds)
            return result

        return wrapper

    return decorator
