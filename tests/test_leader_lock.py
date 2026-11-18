"""One instance at a time for each background watcher (shared/leader_lock.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient

from shared import leader_lock as ll

NOW = datetime.now(UTC)


@pytest.fixture
def col():
    c = AsyncMongoMockClient()["t"]["watcher_locks"]
    ll.set_collection_for_tests(c)
    yield c
    ll.set_collection_for_tests(None)


async def test_one_holder_at_a_time_until_the_lease_runs_out(col):
    assert await ll.acquire("deal_watcher", 60, now=NOW, holder="a")
    assert not await ll.acquire("deal_watcher", 60, now=NOW + timedelta(seconds=10), holder="b")
    assert await ll.acquire("deal_watcher", 60, now=NOW + timedelta(seconds=20), holder="a")  # renew
    assert not await ll.acquire("deal_watcher", 60, now=NOW + timedelta(seconds=70), holder="b")  # renewed at +20
    assert await ll.acquire("deal_watcher", 60, now=NOW + timedelta(seconds=81), holder="b")  # a died, b takes over
    assert not await ll.acquire("deal_watcher", 60, now=NOW + timedelta(seconds=90), holder="a")


async def test_locks_are_per_watcher_and_release(col):
    assert await ll.acquire("deal_watcher", 60, now=NOW, holder="a")
    assert await ll.acquire("escrow_watcher", 60, now=NOW, holder="b")
    await ll.release("deal_watcher", holder="a")
    assert await ll.acquire("deal_watcher", 60, now=NOW, holder="b")


async def test_without_mongo_every_instance_runs():
    ll.set_collection_for_tests(None)
    assert await ll.acquire("deal_watcher", 60, holder="a") and await ll.acquire("deal_watcher", 60, holder="b")


async def test_an_unreachable_store_skips_the_tick_unless_fail_open(monkeypatch):
    class Broken:
        async def find_one_and_update(self, *a, **k):
            raise ConnectionError("no route to Atlas")
    ll.set_collection_for_tests(Broken())
    try:
        assert await ll.acquire("deal_watcher", 60) is False
        monkeypatch.setenv("LEADER_LOCK_FAIL_OPEN", "true")
        assert await ll.acquire("deal_watcher", 60) is True
    finally:
        ll.set_collection_for_tests(None)
