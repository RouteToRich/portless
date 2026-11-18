"""Bulk actions and CSV export on the buyer / supplier directory (services/trade_min/directory_bulk.py)."""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.trade_min import directory as dr
from services.trade_min import directory_bulk as db
from services.trade_min import relationships as rl
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dr.reset_directory()
    monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[]))
    monkeypatch.setattr(rl, "_tracks", AsyncMock(return_value={}))
    yield
    dr.reset_directory()


async def _add(name, site, role="buyer", **kw):
    store = await dr.get_directory()
    rec = CounterpartyRecord(id=dr.record_key(role, name, f"https://{site}"), role=role, name=name,
                             website=f"https://{site}", country="AE", email=f"buy@{site}", source="apify_company_search",
                             hs_codes=["090422"], markets=["AE"], first_seen_at=NOW, last_seen_at=NOW, **kw)
    return await store.upsert(rec)


async def test_check_first_answers_its_cost_then_runs(monkeypatch):
    a, b = await _add("Gulf Foods", "gulf.ae"), await _add("Delta", "delta.ae")
    out = await db.bulk("buyer", [a.id, b.id], "check", by="ops")
    assert out["confirm_needed"] and out["runs_estimate"] == 4
    async def fake(rec):
        rec.validation = CounterpartyValidation(status="verified", sanctions="clear", email="ok")
        return rec
    monkeypatch.setattr("services.trade_min.validation.validate_record", fake)
    out = await db.bulk("buyer", [a.id, b.id], "check", by="ops", confirm=True)
    assert out["done"] == 2 and {r["detail"] for r in out["results"]} == {"verified"}
    assert (await (await dr.get_directory()).get(a.id)).validation.status == "verified"


async def test_do_not_contact_needs_a_reason_and_reports_missing():
    a = await _add("Gulf Foods", "gulf.ae")
    with pytest.raises(ValueError):
        await db.bulk("buyer", [a.id], "do_not_contact", by="ops")
    out = await db.bulk("buyer", [a.id, "buyer:nope"], "do_not_contact", by="ops", reason="asked at the fair")
    assert out["done"] == 1 and out["failed"] == 1
    rec = await (await dr.get_directory()).get(a.id)
    assert rec.do_not_contact and "bulk" in rec.dnc_reason


async def test_add_to_prospect_reports_each_refusal(monkeypatch):
    a, b = await _add("Gulf Foods", "gulf.ae"), await _add("Mute Co", "mute.ae", do_not_contact=True)
    calls = []
    async def fake_add(role, rid, deal_id):
        calls.append(rid)
        if rid == b.id:
            raise ValueError("Mute Co cannot be added: on the do-not-contact list")
        return {"next": "added"}
    monkeypatch.setattr("services.trade_min.counterparty_profile.add_to_prospect", fake_add)
    out = await db.bulk("buyer", [a.id, b.id], "add_to_prospect", by="ops", deal_id="P1")
    assert out["done"] == 1 and "do-not-contact" in out["results"][1]["detail"]
    with pytest.raises(ValueError):
        await db.bulk("buyer", [a.id], "add_to_prospect", by="ops")


async def test_limits():
    with pytest.raises(ValueError):
        await db.bulk("buyer", [], "do_not_contact", by="ops", reason="x")
    with pytest.raises(ValueError):
        await db.bulk("buyer", [f"buyer:{i}" for i in range(db.MAX_SELECTED + 1)], "do_not_contact", by="ops", reason="x")


class TestRoutes:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.trade_min.router import _staff, router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@portless.in", is_portless=True)
        return TestClient(app)

    async def test_export_csv_follows_the_filters(self):
        await _add("Gulf Foods", "gulf.ae", validation=CounterpartyValidation(status="verified", sanctions="clear", email="ok"))
        await _add("Delta", "delta.ae")
        c = self._client()
        r = c.get("/trade-min/directory/buyers/export.csv", params={"validation": "verified"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(r.text)))
        assert [row["name"] for row in rows] == ["Gulf Foods"] and rows[0]["validation"] == "verified"

    async def test_bulk_over_http(self):
        a = await _add("Gulf Foods", "gulf.ae")
        c = self._client()
        r = c.post("/trade-min/directory/buyers/bulk", json={"ids": [a.id], "action": "do_not_contact", "reason": "asked"})
        assert r.status_code == 200 and r.json()["done"] == 1
        assert c.post("/trade-min/directory/buyers/bulk", json={"ids": [a.id], "action": "do_not_contact"}).status_code == 422
