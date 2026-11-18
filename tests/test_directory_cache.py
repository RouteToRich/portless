"""The whole-directory read is served from a short per-process copy, dropped on every write (B-13)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mongomock_motor import AsyncMongoMockClient

from services.trade_min.directory import MongoDirectory
from services.trade_min.schemas import CounterpartyRecord

NOW = datetime.now(UTC)


def _rec(i: int, role: str = "supplier") -> CounterpartyRecord:
    return CounterpartyRecord(id=f"{role}:m{i}.in", role=role, name=f"Mill {i}", website=f"https://m{i}.in",
                              source="manual", first_seen_at=NOW, last_seen_at=NOW, hs_codes=["0904"])


class _Counting:
    """Wraps a collection and counts find() calls."""

    def __init__(self, col):
        self._col, self.finds = col, 0

    def find(self, *a, **k):
        self.finds += 1
        return self._col.find(*a, **k)

    def __getattr__(self, name):
        return getattr(self._col, name)


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("DIRECTORY_CACHE_SECONDS", "30")
    col = _Counting(AsyncMongoMockClient()["db"]["dir"])
    return MongoDirectory(col), col


async def test_whole_read_is_cached_and_writes_drop_it(store):
    s, col = store
    for i in range(3):
        await s.upsert(_rec(i))
    assert len(await s.list("supplier", limit=100_000)) == 3
    assert len(await s.list("supplier", limit=5000)) == 3       # smaller limit from the bigger read
    assert col.finds == 1
    await s.set_contact_preference("supplier:m1.in", {"do_not_contact": True})
    rows = await s.list("supplier", limit=100_000)
    assert col.finds == 2 and [r.do_not_contact for r in rows if r.id == "supplier:m1.in"] == [True]
    await s.delete("supplier:m0.in")
    assert len(await s.list("supplier", limit=100_000)) == 2 and col.finds == 3


async def test_filtered_reads_and_other_roles_are_not_served_from_it(store):
    s, col = store
    await s.upsert(_rec(1))
    await s.upsert(_rec(2, role="buyer"))
    await s.list("supplier", limit=100_000)
    await s.list("supplier", hs_code="0904")
    await s.list("buyer", limit=100_000)
    assert col.finds == 3


async def test_a_bigger_limit_than_cached_reads_again_unless_the_read_was_complete(store):
    s, col = store
    for i in range(3):
        await s.upsert(_rec(i))
    assert len(await s.list("supplier", limit=2)) == 2
    assert len(await s.list("supplier", limit=10)) == 3 and col.finds == 2   # 2 rows of a limit-2 read: maybe more
    assert len(await s.list("supplier", limit=50)) == 3 and col.finds == 2   # 3 of 10: complete


async def test_callers_get_copies(store):
    s, _ = store
    await s.upsert(_rec(1))
    first = await s.list("supplier", limit=100)
    first[0].name = "changed by a caller"
    assert (await s.list("supplier", limit=100))[0].name == "Mill 1"


async def test_zero_switches_it_off(store, monkeypatch):
    s, col = store
    monkeypatch.setenv("DIRECTORY_CACHE_SECONDS", "0")
    await s.upsert(_rec(1))
    await s.list("supplier", limit=100)
    await s.list("supplier", limit=100)
    assert col.finds == 2
