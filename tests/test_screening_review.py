"""Sanctions "review" queue: staff clear or fail a counterparty whose screening could not
decide, with a reason kept on the record (services/trade_min/screening_review.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.trade_min import directory as dr
from services.trade_min import relationships as rl
from services.trade_min import screening_review as sr
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dr.reset_directory()
    monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[]))
    monkeypatch.setattr(rl, "_tracks", AsyncMock(return_value={}))
    yield
    dr.reset_directory()


def _v(sanctions="review", email="ok", age_days=1, **kw) -> CounterpartyValidation:
    return CounterpartyValidation(status="unverified", sanctions=sanctions, email=email,
                                  sanctions_reason="possible match: Gulf Foods LLC (OFAC)" if sanctions == "review" else None,
                                  evidence_url="https://evidence.example/gulf.pdf",
                                  checked_at=NOW - timedelta(days=age_days), **kw)


async def _add(name="Gulf Foods", role="buyer", website="https://gulf.ae", **v) -> CounterpartyRecord:
    store = await dr.get_directory()
    rec = CounterpartyRecord(id=dr.record_key(role, name, website), role=role, name=name, website=website,
                             country="AE" if role == "buyer" else "IN", email=f"desk@{website.split('//')[-1]}",
                             source="apify_company_search", hs_codes=["090422"], markets=["AE"],
                             first_seen_at=NOW, last_seen_at=NOW, validation=_v(**v))
    await store.upsert(rec)
    await store.set_validation(rec)
    return await store.get(rec.id)


async def test_the_queue_lists_only_review_results_oldest_first():
    newer = await _add("Gulf Foods", age_days=1)
    older = await _add("Delta Spices", role="supplier", website="https://delta.in", age_days=9)
    await _add("Clear Co", website="https://clear.ae", sanctions="clear")
    await _add("Blocked Co", website="https://blocked.ae", sanctions="block")
    assert [r.id for r in await sr.queue()] == [older.id, newer.id]
    assert [r.id for r in await sr.queue("buyer")] == [newer.id]


async def test_clear_verifies_and_keeps_the_audit_trail():
    rec = await _add()
    saved = await sr.resolve(rec.id, "clear", "different company: other address and owners", "ops@portless.in")
    assert saved.validation.sanctions == "clear" and saved.validation.status == "verified"
    assert "staff cleared" in saved.validation.sanctions_reason and "ops@portless.in" in saved.validation.sanctions_reason
    (review,) = saved.screening_reviews
    assert review.decision == "clear" and review.by == "ops@portless.in"
    assert review.previous_sanctions == "review" and review.previous_reason.startswith("possible match")
    assert review.evidence_url == "https://evidence.example/gulf.pdf"
    assert await sr.queue() == []
    stored = await (await dr.get_directory()).get(rec.id)
    assert stored.screening_reviews[0].reason.startswith("different company")


async def test_fail_blocks_the_company():
    rec = await _add()
    saved = await sr.resolve(rec.id, "fail", "same company as the OFAC entry", "ops@portless.in")
    assert saved.validation.sanctions == "block" and saved.validation.status == "failed"
    assert any("blocked" in r for r in saved.validation.reasons)


async def test_clear_with_an_undeliverable_email_stays_unverified():
    rec = await _add(email="invalid")
    saved = await sr.resolve(rec.id, "clear", "not the listed entity", "ops@portless.in")
    assert saved.validation.sanctions == "clear" and saved.validation.status == "unverified"


async def test_refusals():
    rec = await _add(sanctions="clear")
    with pytest.raises(sr.NotInReview):
        await sr.resolve(rec.id, "clear", "why not", "ops")
    review = await _add("Gulf Two", website="https://gulf2.ae")
    with pytest.raises(ValueError):
        await sr.resolve(review.id, "clear", "   ", "ops")
    assert await sr.resolve("buyer:nope", "clear", "reason", "ops") is None


class TestRoutes:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.trade_min.router import _staff, router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@portless.in", is_portless=True)
        return TestClient(app)

    async def test_list_and_decide_over_http(self):
        rec = await _add()
        c = self._client()
        listed = c.get("/trade-min/screening/review")
        assert listed.status_code == 200 and [r["id"] for r in listed.json()] == [rec.id]
        assert c.post(f"/trade-min/buyers/{rec.id}/screening-review", json={"decision": "clear"}).status_code == 422
        assert c.post(f"/trade-min/suppliers/{rec.id}/screening-review",
                      json={"decision": "clear", "reason": "not listed"}).status_code == 404
        r = c.post(f"/trade-min/buyers/{rec.id}/screening-review", json={"decision": "fail", "reason": "listed entity"})
        assert r.status_code == 200 and r.json()["validation"]["status"] == "failed"
        assert r.json()["screening_reviews"][0]["by"] == "ops@portless.in"
        again = c.post(f"/trade-min/buyers/{rec.id}/screening-review", json={"decision": "clear", "reason": "changed mind"})
        assert again.status_code == 409
        assert c.get("/trade-min/screening/review").json() == []


async def test_decision_is_stored_in_mongo_too(monkeypatch):
    from mongomock_motor import AsyncMongoMockClient

    store = dr.MongoDirectory(AsyncMongoMockClient()["t"]["trade_counterparties"])
    monkeypatch.setattr(dr, "get_directory", AsyncMock(return_value=store))
    rec = CounterpartyRecord(id="buyer:gulf.ae", role="buyer", name="Gulf Foods", website="https://gulf.ae",
                             country="AE", email="desk@gulf.ae", source="apify_company_search", hs_codes=["090422"],
                             markets=["AE"], first_seen_at=NOW, last_seen_at=NOW, validation=_v())
    await store.upsert(rec)
    assert [r.id for r in await sr.queue()] == [rec.id]
    saved = await sr.resolve(rec.id, "clear", "not the listed entity", "ops@portless.in")
    assert saved.validation.status == "verified" and saved.screening_reviews[0].decision == "clear"
    # a later scan of the same company keeps the decision and its trail
    again = await store.upsert(rec.model_copy(update={"validation": None, "screening_reviews": []}))
    assert again.validation.sanctions == "clear" and len(again.screening_reviews) == 1
