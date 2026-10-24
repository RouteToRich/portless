"""Tests for the Trade-Min raw API response cache (quota conservation).

Covers the key builder, the shared-cache delegation, and the ``_safe_quote``
short-circuit that avoids a second live connector call. The trade_min cache is
a thin adapter over ``shared.cache`` (see test_shared_cache.py for the backend).
"""
from __future__ import annotations

import pytest

from services.trade_min import cache, service
from services.trade_min.config import DEFAULT_CONFIG
from shared import cache as shared_cache


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """Force a clean in-memory shared backend around each test.

    Also forces TRADE_MIN_MOCK_MODE off: these tests exercise _safe_quote's
    live-connector caching path directly, which mock mode short-circuits
    before the cache is ever consulted (see service.py:_safe_quote). Without
    this, every test here fails whenever TRADE_MIN_MOCK_MODE=true is set in
    .env (the recommended default for network-free local dev).
    """
    monkeypatch.setattr(service.settings, "trade_min_mock_mode", False)
    shared_cache.reset_for_tests()
    yield
    shared_cache.reset_for_tests()


# --------------------------------------------------------------------------- #
# Key builder
# --------------------------------------------------------------------------- #
def test_key_is_order_independent() -> None:
    k1 = cache.make_cache_key("comtrade", {"hs_code": "090422", "period": "2025"})
    k2 = cache.make_cache_key("comtrade", {"period": "2025", "hs_code": "090422"})
    assert k1 == k2


def test_key_differs_by_request() -> None:
    k1 = cache.make_cache_key("comtrade", {"hs_code": "090422"})
    k2 = cache.make_cache_key("comtrade", {"hs_code": "100190"})
    assert k1 != k2


# --------------------------------------------------------------------------- #
# Adapter round-trip
# --------------------------------------------------------------------------- #
async def test_set_then_get_round_trips() -> None:
    key = cache.make_cache_key("comtrade", {"hs_code": "090422"})
    await cache.cache_set(key, "comtrade", {"records": [1, 2, 3]}, ttl_seconds=60)
    assert await cache.cache_get(key) == {"records": [1, 2, 3]}


async def test_get_miss_returns_none() -> None:
    assert await cache.cache_get("nope") is None


# --------------------------------------------------------------------------- #
# _safe_quote integration — cache short-circuits the live connector
# --------------------------------------------------------------------------- #
async def test_cached_connector_skips_second_live_call(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeConn:
        async def quote(self, req: dict) -> dict:
            calls.append(req)
            return {"records": [{"fob_value_usd": 1.0}], "total_records": 1}

    monkeypatch.setattr(service, "_build_connector", lambda name: _FakeConn())

    req = {"hs_code": "090422", "period": "2025"}
    first = await service._safe_quote("comtrade", req)
    second = await service._safe_quote("comtrade", req)

    assert first == second
    assert len(calls) == 1, "second call should be served from cache"


async def test_live_connector_not_cached(monkeypatch) -> None:
    """Sources without a TTL (e.g. eu_ted) always hit the live connector."""
    assert "eu_ted" not in DEFAULT_CONFIG.cache_ttl_seconds
    calls: list[dict] = []

    class _FakeConn:
        async def quote(self, req: dict) -> dict:
            calls.append(req)
            return {"tenders": []}

    monkeypatch.setattr(service, "_build_connector", lambda name: _FakeConn())

    req = {"query": "pepper"}
    await service._safe_quote("eu_ted", req)
    await service._safe_quote("eu_ted", req)
    assert len(calls) == 2, "live signals must never be cached"


async def test_kill_switch_disables_cache(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "trade_cache_enabled", False)
    calls: list[dict] = []

    class _FakeConn:
        async def quote(self, req: dict) -> dict:
            calls.append(req)
            return {"records": [], "total_records": 0}

    monkeypatch.setattr(service, "_build_connector", lambda name: _FakeConn())

    req = {"hs_code": "090422", "period": "2025"}
    await service._safe_quote("comtrade", req)
    await service._safe_quote("comtrade", req)
    assert len(calls) == 2, "kill switch should bypass the cache"
