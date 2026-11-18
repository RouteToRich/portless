"""LLM journeys keep every counterparty reply with the result and can replay them, so a failing
journey re-runs with the same e-mails (services/automation/journeys.py, worker --replay-run)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.automation import journeys as j


@pytest.fixture(autouse=True)
def _clean():
    j.REPLIES.clear()
    j.REPLAY.clear()
    j._CURRENT["journey"] = "UC-01"
    yield
    j.REPLIES.clear()
    j.REPLAY.clear()


async def test_reply_recorded(monkeypatch):
    class FakeLLM:
        async def chat(self, **kw):
            return SimpleNamespace(content="Dear team, we accept USD 1,180 per MT for 20 MT. Regards, Omar")

    monkeypatch.setattr(j, "LLMClient", FakeLLM)
    text = await j.persona_reply("buyer", "Gulf Foods", "offer…", "Accept at USD 1,180")
    assert "1,180" in text
    assert j.REPLIES[0]["role"] == "buyer" and j.REPLIES[0]["instruction"] == "Accept at USD 1,180" and j.REPLIES[0]["n"] == 1


async def test_replay_uses_recorded_reply_without_the_llm(monkeypatch):
    class NoLLM:
        async def chat(self, **kw):
            raise AssertionError("the LLM must not be called during a replay")

    monkeypatch.setattr(j, "LLMClient", NoLLM)
    j.REPLAY["UC-01"] = [{"n": 1, "role": "buyer", "instruction": "Counter at USD 1,150", "reply": "We can do USD 1,150. Omar"}]
    assert await j.persona_reply("buyer", "Gulf Foods", "…", "Counter at USD 1,150") == "We can do USD 1,150. Omar"
    assert j.REPLIES[0]["replayed"] is True and j.REPLAY["UC-01"] == []


async def test_divergence_falls_back_to_the_llm(monkeypatch):
    class FakeLLM:
        async def chat(self, **kw):
            return SimpleNamespace(content="Thanks, please send the proforma invoice for USD 1,200 now. Regards, Ana")

    monkeypatch.setattr(j, "LLMClient", FakeLLM)
    j.REPLAY["UC-01"] = [{"n": 1, "role": "supplier", "instruction": "something else", "reply": "x"}]
    text = await j.persona_reply("buyer", "Gulf Foods", "…", "Ask for the PI at USD 1,200")
    assert "1,200" in text and "UC-01" not in j.REPLAY and j.REPLIES[0].get("replayed") is None


async def test_worker_loads_replies_of_an_earlier_run():
    from mongomock_motor import AsyncMongoMockClient

    from services.automation.store import RESULTS, Sink
    from services.automation.worker import _replay_from
    db = AsyncMongoMockClient()["portless_automation"]
    await db[RESULTS].insert_one({"run_id": "RUN-1", "phase": "llm-journeys", "tc_id": "UC-01",
                                  "counterparty_replies": [{"n": 1, "role": "buyer", "instruction": "i", "reply": "r"}]})
    await db[RESULTS].insert_one({"run_id": "RUN-2", "phase": "llm-journeys", "tc_id": "UC-02",
                                  "counterparty_replies": [{"n": 1, "role": "buyer", "instruction": "i", "reply": "r"}]})
    out = await _replay_from(Sink(db, "RUN-3"), "RUN-1")
    assert list(out) == ["UC-01"] and out["UC-01"][0]["reply"] == "r"
    assert await _replay_from(Sink(db, "RUN-3"), "") == {}
