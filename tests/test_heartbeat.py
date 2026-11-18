"""Heartbeats of the background watchers (shared/heartbeat.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient

from shared import heartbeat as hb

NOW = datetime.now(UTC)


@pytest.fixture(params=["memory", "mongo"])
def store(request):
    hb.set_collection_for_tests(AsyncMongoMockClient()["t"]["hb"] if request.param == "mongo" else None)
    yield
    hb.set_collection_for_tests(None)


async def test_fresh_and_stale_watchers(store):
    await hb.beat("deal_watcher", 300, now=NOW - timedelta(seconds=200))
    await hb.beat("escrow_watcher", 60, now=NOW - timedelta(minutes=10))
    out = await hb.status(NOW)
    by = {w["name"]: w for w in out["watchers"]}
    assert not by["deal_watcher"]["stale"] and by["escrow_watcher"]["stale"]
    assert out["ok"] is False and out["stale"] == ["escrow_watcher"]


async def test_other_instances_beats_count():
    hb.set_collection_for_tests(AsyncMongoMockClient()["t"]["hb2"])
    await hb._collection().update_one({"_id": "hitl_timeout_watcher"}, {"$set": {
        "name": "hitl_timeout_watcher", "at": NOW, "interval_seconds": 30, "holder": "other:1"}}, upsert=True)
    out = await hb.status(NOW)
    hb.set_collection_for_tests(None)
    assert out["ok"] and out["watchers"][0]["holder"] == "other:1"


def test_digest_warns_about_stale_watchers():
    from shared.orchestration import digest
    text = digest.render_digest_text({"generated_at": NOW.isoformat(), "watchers": {"stale": ["escrow_watcher"]}})
    assert "WATCHERS NOT RUNNING: escrow_watcher" in text


async def test_alert_once_when_a_watcher_goes_stale_and_when_it_is_back(store):
    hb.reset_for_tests()
    sent = []

    async def send(m):
        sent.append(m)
    await hb.beat("escrow_watcher", 60, now=NOW - timedelta(minutes=10))
    await hb.check_and_alert(NOW, send=send)
    await hb.check_and_alert(NOW, send=send)  # still stale: no second message
    assert sent == ["WATCHER NOT RUNNING: escrow_watcher — reminders, auto-releases or timeouts may be waiting (GET /health/watchers)"]
    await hb.beat("escrow_watcher", 60, now=NOW)
    await hb.check_and_alert(NOW, send=send)
    assert sent[-1] == "Watcher back: escrow_watcher"
    hb.reset_for_tests()


async def test_alert_when_the_lock_store_stops_answering(store, monkeypatch):
    hb.reset_for_tests()
    from shared import leader_lock
    monkeypatch.setattr(leader_lock, "last_error_at", NOW - timedelta(minutes=1))
    sent = []

    async def send(m):
        sent.append(m)
    await hb.check_and_alert(NOW, send=send)
    await hb.check_and_alert(NOW, send=send)
    assert len([m for m in sent if "LEADER-LOCK STORE" in m]) == 1
    hb.reset_for_tests()
