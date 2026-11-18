"""Staff notes and follow-up reminders on a directory company (services/trade_min/company_notes.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.trade_min import company_notes as cn
from services.trade_min import directory as dr
from services.trade_min import relationships as rl
from services.trade_min.schemas import CounterpartyRecord

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dr.reset_directory()
    remember = MagicMock(return_value=True)
    monkeypatch.setattr("shared.orchestration.memory.remember_note", remember)
    monkeypatch.setattr(rl, "_deals", lambda: _none())
    yield remember
    dr.reset_directory()


async def _none():
    return []


async def _add(role="buyer", name="Gulf Foods", site="gulf.ae"):
    store = await dr.get_directory()
    rec = CounterpartyRecord(id=dr.record_key(role, name, f"https://{site}"), role=role, name=name,
                             website=f"https://{site}", country="AE", email=f"buy@{site}", source="manual",
                             hs_codes=["090422"], markets=["AE"], first_seen_at=NOW, last_seen_at=NOW)
    return await store.upsert(rec)


async def test_a_note_is_kept_and_sent_to_memory(_fresh):
    rec = await _add()
    saved = await cn.add(rec.id, "Met at Gulfood: wants 2 x 40ft in March", "ops@portless.in")
    (note,) = saved.notes
    assert note.text.startswith("Met at Gulfood") and note.by == "ops@portless.in" and note.remind_at is None
    kw = _fresh.call_args.kwargs
    assert kw["subject_type"] == "buyer" and kw["subject_id"] == "Gulf Foods" and kw["extra"]["counterparty_id"] == rec.id


async def test_a_due_reminder_shows_until_done():
    rec = await _add()
    await cn.add(rec.id, "Call back about the March order", "ops", remind_at=NOW - timedelta(hours=1))
    await cn.add(rec.id, "Later", "ops", remind_at=NOW + timedelta(days=5))
    due = await cn.due(NOW)
    assert [d["text"] for d in due] == ["Call back about the March order"]
    saved = await cn.done(rec.id, due[0]["note_id"], "ops")
    assert saved.notes[0].done_at and saved.notes[0].done_by == "ops"
    assert await cn.due(NOW) == []


async def test_refusals():
    rec = await _add()
    with pytest.raises(ValueError):
        await cn.add(rec.id, "  ", "ops")
    assert await cn.add("buyer:nope", "x", "ops") is None
    with pytest.raises(LookupError):
        await cn.done(rec.id, "N-NOPE", "ops")


def test_digest_lists_the_due_followups():
    from shared.orchestration import digest
    text = digest.render_digest_text({"generated_at": NOW.isoformat(), "followups": [
        {"company": "Gulf Foods", "role": "buyer", "remind_at": NOW.isoformat(), "text": "Call back about March"}]})
    assert "FOLLOW-UPS DUE (1):" in text and "Gulf Foods (buyer" in text and "Call back about March" in text


class TestRoutes:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.trade_min.router import _staff, router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@portless.in", is_portless=True)
        return TestClient(app)

    async def test_add_and_done_over_http(self):
        rec = await _add()
        c = self._client()
        r = c.post(f"/trade-min/buyers/{rec.id}/notes", json={"text": "Phoned: samples next week",
                                                              "remind_at": (NOW + timedelta(days=7)).isoformat()})
        assert r.status_code == 200 and r.json()["notes"][0]["by"] == "ops@portless.in"
        note_id = r.json()["notes"][0]["note_id"]
        assert c.post(f"/trade-min/suppliers/{rec.id}/notes", json={"text": "x"}).status_code == 404
        d = c.post(f"/trade-min/buyers/{rec.id}/notes/{note_id}/done")
        assert d.status_code == 200 and d.json()["notes"][0]["done_at"]
        assert c.post(f"/trade-min/buyers/{rec.id}/notes/N-NOPE/done").status_code == 404
