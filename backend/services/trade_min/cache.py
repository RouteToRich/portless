"""Trade-Min raw API response cache.

Thin adapter over the application-wide cache in ``shared.cache``. Slow-changing
sources (Comtrade prior-year annual trade, MacMap tariffs) return identical
payloads for the same ``(connector, request)`` for days; caching the raw response
short-circuits the live call and conserves quota (Comtrade free tier = 500/day).

Only connectors listed in ``TradeMinConfig.cache_ttl_seconds`` are cached — live
signals (Alibaba RFQ, GDELT, EU TED) are always fetched fresh. See
``service.py:_safe_quote`` for the call-site wiring.
"""
from __future__ import annotations

import logging

from shared.cache import cache_get as _shared_get
from shared.cache import cache_set as _shared_set
from shared.cache import close_cache as _shared_close
from shared.cache import init_cache as _shared_init
from shared.cache import make_key

logger = logging.getLogger(__name__)

# All trade-min connector responses share one logical partition.
_NAMESPACE = "trade_min_api"


def make_cache_key(connector: str, request: dict) -> str:
    """Deterministic key for a ``(connector, request)`` pair."""
    return make_key(connector, request)


async def cache_get(key: str) -> dict | None:
    """Look up a cached connector response. None on miss/disabled/error."""
    return await _shared_get(_NAMESPACE, key)


async def cache_set(key: str, connector: str, value: dict, ttl_seconds: int) -> None:
    """Store a connector response. Best-effort; never raises."""
    await _shared_set(_NAMESPACE, key, value, ttl_seconds)


async def init_cache() -> None:
    """Initialise the shared cache backend (called from the service lifespan)."""
    await _shared_init()


async def close_cache() -> None:
    """Close the shared cache backend (called from the service lifespan)."""
    await _shared_close()
