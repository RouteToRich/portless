"""Hash-chained audit log of staff decisions (shared/audit_log.py)."""
from __future__ import annotations

import pytest
from mongomock_motor import AsyncMongoMockClient

from shared import audit_log as al


@pytest.fixture(params=["memory", "mongo"])
def store(request):
    al.set_collection_for_tests(AsyncMongoMockClient()["t"]["audit_log"] if request.param == "mongo" else None)
    yield request.param
    al.set_collection_for_tests(None)


async def test_entries_chain_and_verify(store):
    a = await al.record("kill_switch.pause", "ops@portless.in", "platform", {"reason": "provider outage"})
    b = await al.record("approval.approve", "cfo@portless.in", "APPR-1", {"financial_impact_usd": 15000})
    assert (a["seq"], b["seq"]) == (1, 2) and b["prev_hash"] == a["hash"] and a["prev_hash"] == al.GENESIS
    assert [e["seq"] for e in await al.entries()] == [2, 1]
    assert [e["action"] for e in await al.entries(subject="APPR-1")] == ["approval.approve"]
    assert (await al.verify()) == {"ok": True, "entries": 2, "last_hash": b["hash"]}


async def test_a_changed_entry_breaks_the_chain(store):
    await al.record("pi.extended", "ops", "D1", {"reason": "buyer asked"})
    await al.record("approval.approve", "ops", "APPR-2", {"financial_impact_usd": 1000})
    await al.record("prospect.dropped", "ops", "P1", {"reason": "no margin"})
    if store == "memory":
        al._memory[1]["detail"]["financial_impact_usd"] = 999999
    else:
        await al._collection().update_one({"_id": 2}, {"$set": {"detail.financial_impact_usd": 999999}})
    out = await al.verify()
    assert out["ok"] is False and out["broken_at"] == 2 and out["why"] == "entry changed"


async def test_a_removed_entry_breaks_the_chain(store):
    for i in range(3):
        await al.record("approval.approve", "ops", f"APPR-{i}", {})
    if store == "memory":
        del al._memory[0]
    else:
        await al._collection().delete_one({"_id": 1})
    out = await al.verify()
    assert out["ok"] is False and out["broken_at"] == 1


async def test_a_failed_write_never_raises():
    class Broken:
        async def find_one(self, *a, **k):
            raise ConnectionError("Atlas down")
    al.set_collection_for_tests(Broken())
    try:
        assert await al.record("approval.approve", "ops", "APPR-9", {}) is None
    finally:
        al.set_collection_for_tests(None)


async def test_the_kill_switch_is_recorded():
    al.set_collection_for_tests(None)
    from shared.orchestration import kill_switch
    kill_switch.reset_for_tests()
    try:
        await kill_switch.pause("ops@portless.in", "provider outage")
        await kill_switch.resume("ops@portless.in", "provider back")
        actions = [(e["action"], e["actor"], e["detail"]["reason"]) for e in await al.entries()]
        assert actions == [("kill_switch.resume", "ops@portless.in", "provider back"),
                           ("kill_switch.pause", "ops@portless.in", "provider outage")]
    finally:
        kill_switch.reset_for_tests()


async def test_two_instances_racing_for_the_same_number_both_land():
    from pymongo.errors import DuplicateKeyError
    col = AsyncMongoMockClient()["t"]["audit_race"]
    al.set_collection_for_tests(col)
    real_insert = col.insert_one
    calls = {"n": 0}

    async def racing_insert(doc):
        calls["n"] += 1
        if calls["n"] == 1:  # another instance wrote seq 1 just before us
            await real_insert({"_id": 1, "seq": 1, "at": "x", "action": "other", "actor": "b", "subject": "",
                               "detail": {}, "prev_hash": al.GENESIS, "hash": al._digest(al.GENESIS, {
                                   "seq": 1, "at": "x", "action": "other", "actor": "b", "subject": "", "detail": {}})})
            raise DuplicateKeyError("E11000 duplicate key error")
        return await real_insert(doc)
    col.insert_one = racing_insert
    try:
        entry = await al.record("approval.approve", "a", "APPR-1", {})
        assert entry is not None and entry["seq"] == 2
        assert (await al.verify())["ok"]
    finally:
        al.set_collection_for_tests(None)


async def test_a_final_failure_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr("shared.telegram.configured", lambda: True)

    async def fake_send(text):
        sent.append(text)
        return True
    monkeypatch.setattr("shared.telegram.send_message", fake_send)

    class Broken:
        async def find_one(self, *a, **k):
            raise ConnectionError("Atlas down")
    al.set_collection_for_tests(Broken())
    try:
        assert await al.record("kill_switch.pause", "ops", "platform", {}) is None
        assert sent and "AUDIT LOG WRITE FAILED: kill_switch.pause" in sent[0]
    finally:
        al.set_collection_for_tests(None)
