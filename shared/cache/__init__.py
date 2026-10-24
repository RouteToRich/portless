"""Application-wide MongoDB cache (TTL) with in-memory fallback.

Use for read-only / idempotent results only — never for mutating or financial
operations. See ``store.py`` for the full contract.
"""
from __future__ import annotations

from .config import cache_settings
from .store import (
    CacheBackend,
    InMemoryCacheBackend,
    MongoCacheBackend,
    cache_clear,
    cache_get,
    cache_set,
    cached,
    close_cache,
    get_backend,
    init_cache,
    make_key,
    reset_for_tests,
)

__all__ = [
    "cache_settings",
    "CacheBackend",
    "InMemoryCacheBackend",
    "MongoCacheBackend",
    "cache_clear",
    "cache_get",
    "cache_set",
    "cached",
    "close_cache",
    "get_backend",
    "init_cache",
    "make_key",
    "reset_for_tests",
]
