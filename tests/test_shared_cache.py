"""Tests for the application-wide shared cache (shared.cache)."""
from __future__ import annotations

import pytest

from shared import cache as sc


@pytest.fixture(autouse=True)
def _fresh_cache():
    sc.reset_for_tests()
    yield
    sc.reset_for_tests()


# --------------------------------------------------------------------------- #
# Key builder
# --------------------------------------------------------------------------- #
def test_make_key_is_order_independent() -> None:
    assert sc.make_key("ns", {"a": 1, "b": 2}) == sc.make_key("ns", {"b": 2, "a": 1})


def test_make_key_differs_by_input() -> None:
    assert sc.make_key("gstn", "A") != sc.make_key("gstn", "B")


# --------------------------------------------------------------------------- #
# Backend round-trip / expiry / namespace isolation
# --------------------------------------------------------------------------- #
async def test_get_set_round_trip() -> None:
    await sc.cache_set("ns1", "k", {"v": 1}, ttl_seconds=60)
    assert await sc.cache_get("ns1", "k") == {"v": 1}


async def test_namespaces_are_isolated() -> None:
    await sc.cache_set("ns1", "k", {"v": 1}, ttl_seconds=60)
    assert await sc.cache_get("ns2", "k") is None


async def test_expired_entry_is_a_miss() -> None:
    await sc.cache_set("ns", "k", {"v": 1}, ttl_seconds=-1)
    assert await sc.cache_get("ns", "k") is None


async def test_clear_by_namespace() -> None:
    await sc.cache_set("a", "k1", {"v": 1}, ttl_seconds=60)
    await sc.cache_set("a", "k2", {"v": 2}, ttl_seconds=60)
    await sc.cache_set("b", "k3", {"v": 3}, ttl_seconds=60)
    removed = await sc.cache_clear("a")
    assert removed == 2
    assert await sc.cache_get("a", "k1") is None
    assert await sc.cache_get("b", "k3") == {"v": 3}


# --------------------------------------------------------------------------- #
# Kill switch + fail-safe
# --------------------------------------------------------------------------- #
async def test_kill_switch_disables_reads_and_writes(monkeypatch) -> None:
    await sc.cache_set("ns", "k", {"v": 1}, ttl_seconds=60)
    monkeypatch.setattr(sc.cache_settings, "enabled", False)
    assert await sc.cache_get("ns", "k") is None          # reads bypassed
    await sc.cache_set("ns", "k2", {"v": 2}, ttl_seconds=60)  # no-op
    monkeypatch.setattr(sc.cache_settings, "enabled", True)
    assert await sc.cache_get("ns", "k2") is None


async def test_get_never_raises_on_backend_error() -> None:
    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("backend down")

        async def set(self, *a, **k): ...
        async def delete(self, *a, **k): ...
        async def clear(self, *a, **k):
            return 0

    sc.reset_for_tests(_Boom())
    assert await sc.cache_get("ns", "k") is None  # swallowed → miss


# --------------------------------------------------------------------------- #
# @cached decorator
# --------------------------------------------------------------------------- #
async def test_cached_decorator_memoises() -> None:
    calls: list[tuple] = []

    @sc.cached("demo", ttl_seconds=60)
    async def lookup(a: str, b: str) -> dict:
        calls.append((a, b))
        return {"pair": f"{a}-{b}"}

    assert await lookup("x", "y") == {"pair": "x-y"}
    assert await lookup("x", "y") == {"pair": "x-y"}
    assert len(calls) == 1                      # second call cached
    await lookup("x", "z")
    assert len(calls) == 2                      # different args → fetched


async def test_cached_decorator_skips_empty_results() -> None:
    calls: list[int] = []

    @sc.cached("demo", ttl_seconds=60)
    async def lookup() -> dict:
        calls.append(1)
        return {}

    await lookup()
    await lookup()
    assert len(calls) == 2  # empty results are never cached
