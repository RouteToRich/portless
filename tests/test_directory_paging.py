"""Buyers & Suppliers paging and sorting: the whole matching directory is scored, filtered
and sorted before a page is cut, the total is right beyond 200 companies, and every sort
key orders as named (rows without a value last)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.trade_min import directory as dr
from services.trade_min import relationships as rl
from services.trade_min.router import SORTS, _book, router
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dr.reset_directory()
    monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[]))
    monkeypatch.setattr(rl, "_tracks", AsyncMock(return_value={}))
    yield
    dr.reset_directory()


def _rec(i: int, role="buyer", **kw) -> CounterpartyRecord:
    base = dict(id=f"{role}:c{i:03d}.example", role=role, name=f"Company {i:03d}", website=f"https://c{i:03d}.example",
                email=f"buy@c{i:03d}.example", country="AE", source="apify_company_search", hs_codes=["090422"],
                markets=["AE"], first_seen_at=NOW - timedelta(days=i), last_seen_at=NOW - timedelta(hours=i))
    base.update(kw)
    return CounterpartyRecord(**base)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


async def _seed(n: int, role="buyer", **kw):
    store = await dr.get_directory()
    for i in range(n):
        await store.upsert(_rec(i, role, **kw))


class TestPaging:
    async def test_total_and_pages_beyond_200(self):
        await _seed(250)
        c = _client()
        first = c.get("/trade-min/directory/buyers/page", params={"limit": 100, "sort": "name"}).json()
        assert first["total"] == 250 and len(first["items"]) == 100
        assert first["items"][0]["name"] == "Company 000"
        last = c.get("/trade-min/directory/buyers/page", params={"limit": 100, "offset": 200, "sort": "name"}).json()
        assert len(last["items"]) == 50 and last["items"][-1]["name"] == "Company 249"
        assert last["order"] == "asc" and last["sort"] == "name"

    async def test_filters_apply_before_the_page(self):
        await _seed(30)
        store = await dr.get_directory()
        await store.set_contact_preference("buyer:c005.example", {"do_not_contact": True})
        await store.set_contact_preference("buyer:c006.example", {"do_not_contact": True})
        body = _client().get("/trade-min/directory/buyers/page", params={"contactable": True, "limit": 10}).json()
        assert body["total"] == 28 and len(body["items"]) == 10

    async def test_suppliers_page(self):
        await _seed(3, role="supplier", country="IN")
        body = _client().get("/trade-min/directory/suppliers/page").json()
        assert body["total"] == 3 and body["sort"] == "score" and body["order"] == "desc"

    def test_unknown_role_is_refused(self):
        assert _client().get("/trade-min/directory/partners/page").status_code == 422


class TestSorting:
    def _rows(self):
        a = _rec(1, name="Beta", country="DE", fit_score=50, times_seen=1)
        b = _rec(2, name="alpha", country="AE", fit_score=90, times_seen=5)
        c = _rec(3, name="Gamma", country=None, fit_score=None, times_seen=2)
        a.score, b.score, c.score = 40.0, 70.0, 10.0
        a.relationship = {"stage": "replied", "last_reply_at": (NOW - timedelta(days=1)).isoformat(), "target_price_usd_per_mt": 3000}
        b.relationship = {"stage": "new"}
        c.relationship = {"stage": "traded", "last_contact_at": (NOW - timedelta(days=5)).isoformat(), "target_price_usd_per_mt": 2500}
        return [a, b, c]

    @pytest.mark.parametrize(("sort", "order", "expected"), [
        ("score", None, ["alpha", "Beta", "Gamma"]),
        ("name", None, ["alpha", "Beta", "Gamma"]),
        ("name", "desc", ["Gamma", "Beta", "alpha"]),
        ("country", None, ["alpha", "Beta", "Gamma"]),          # no country last
        ("fit", None, ["alpha", "Beta", "Gamma"]),
        ("stage", None, ["Gamma", "Beta", "alpha"]),            # traded > replied > new
        ("activity", None, ["Beta", "Gamma", "alpha"]),         # never contacted last
        ("price", None, ["Gamma", "Beta", "alpha"]),            # lowest first, no price last
        ("price", "desc", ["Beta", "Gamma", "alpha"]),          # no price still last
        ("seen", None, ["alpha", "Gamma", "Beta"]),
        ("recent", None, ["Beta", "alpha", "Gamma"]),
        ("added", None, ["Beta", "alpha", "Gamma"]),
    ])
    def test_every_sort(self, sort, order, expected):
        rows = _book(self._rows(), stage=None, contactable=False, sort=sort, order=order)
        assert [r.name for r in rows] == expected

    def test_unknown_sort_falls_back_to_recent(self):
        rows = _book(self._rows(), stage=None, contactable=False, sort="bogus")
        assert [r.name for r in rows] == ["Beta", "alpha", "Gamma"]
        assert set(SORTS) >= {"score", "recent", "added", "activity", "stage", "name", "country", "fit", "price", "seen"}

    async def test_list_route_accepts_order(self):
        await _seed(3)
        names = [r["name"] for r in _client().get("/trade-min/buyers", params={"sort": "name", "order": "desc"}).json()]
        assert names == ["Company 002", "Company 001", "Company 000"]
