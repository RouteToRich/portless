"""Per-process cache for computed views that read every deal (todo P2: computed views scale
with the number of deals).

A view (directory relationships, supplier performance, the prospect desk) is computed once and
served from memory until either its TTL passes (``VIEW_CACHE_SECONDS``, 30; 0 = off) or one of
the stores it depends on changes: every deal write (``DealRepository.save`` / ``set_fields`` /
``mark_status``) calls ``bump("deals")``, so the next read recomputes. Other instances keep their
own copy for at most the TTL. Concurrent misses share one computation. Never raises past the
compute function.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

_generation: dict[str, int] = {}
_entries: dict[str, tuple[float, tuple[int, ...], Any]] = {}
_locks: dict[str, asyncio.Lock] = {}
stats = {"hits": 0, "misses": 0}


def ttl_seconds() -> float:
    try:
        return float(os.getenv("VIEW_CACHE_SECONDS", "30"))
    except ValueError:
        return 30.0


def bump(store: str) -> None:
    """A store changed: views that depend on it recompute on their next read."""
    _generation[store] = _generation.get(store, 0) + 1


def generation(store: str) -> int:
    return _generation.get(store, 0)


async def get_or_compute(name: str, compute: Callable[[], Awaitable[Any]], *,
                         deps: tuple[str, ...] = ("deals",), ttl: float | None = None) -> Any:
    ttl = ttl_seconds() if ttl is None else ttl
    if ttl <= 0:
        return await compute()
    gens = tuple(generation(d) for d in deps)
    hit = _entries.get(name)
    now = time.monotonic()
    if hit and hit[1] == gens and now - hit[0] < ttl:
        stats["hits"] += 1
        return hit[2]
    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        hit = _entries.get(name)
        gens = tuple(generation(d) for d in deps)
        if hit and hit[1] == gens and time.monotonic() - hit[0] < ttl:
            stats["hits"] += 1
            return hit[2]
        stats["misses"] += 1
        value = await compute()
        _entries[name] = (time.monotonic(), gens, value)
        return value


def clear() -> None:
    _entries.clear()
    _locks.clear()
    _generation.clear()
    stats.update(hits=0, misses=0)
