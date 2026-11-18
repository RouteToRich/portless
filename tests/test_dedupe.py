"""Duplicate companies: suggestions and staff merge (services/trade_min/dedupe.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.trade_min import dedupe
from services.trade_min import directory as dr
from services.trade_min import relationships as rl
from services.trade_min.schemas import CounterpartyRecord

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dr.reset_directory()
    monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[]))
    monkeypatch.setattr(rl, "_tracks", AsyncMock(return_value={}))
    yield
    dr.reset_directory()


def _rec(name, site, email=None, phone=None, country="AE", role="buyer", **kw):
    return CounterpartyRecord(id=dr.record_key(role, name, f"https://{site}"), role=role, name=name,
                              website=f"https://{site}", country=country, email=email, phone=phone,
                              source=kw.pop("source", "apify_company_search"), hs_codes=kw.pop("hs", ["090422"]),
                              markets=["AE"], first_seen_at=kw.pop("first", NOW), last_seen_at=NOW, **kw)


@pytest.mark.parametrize("a,b", [("Gulf Foods LLC", "Gulf Foods Trading"), ("Al Noor General Trading L.L.C", "Al Noor")])
def test_name_core_drops_legal_forms(a, b):
    assert dedupe.name_core(a) == dedupe.name_core(b)


def test_suggestions_group_by_domain_phone_or_name():
    rows = [
        _rec("Gulf Foods LLC", "gulf.ae", email="buy@gulf.ae"),
        _rec("Gulf Group", "gulfgroup.com", email="import@gulf.ae"),            # same company domain
        _rec("Delta Spices", "delta.ae", phone="+971 4 123 4567"),
        _rec("Delta Spice Traders", "deltaspice.com", phone="04-1234567 ext"),  # last 9 digits differ? see below
        _rec("Noor Trading", "noor.ae"), _rec("Noor LLC", "noor-uae.com"),      # same name core, same country
        _rec("Noor Trading", "noor.de", country="DE"),                          # other country: not joined
        _rec("Lone Co", "lone.ae", email="x@gmail.com"), _rec("Other Co", "other.ae", email="y@gmail.com"),  # free-mail
    ]
    groups = dedupe.suggestions(rows)
    sets = [set(g["names"]) for g in groups]
    assert {"Gulf Foods LLC", "Gulf Group"} in sets
    assert {"Noor Trading", "Noor LLC"} in sets
    assert not any({"Lone Co", "Other Co"} <= s for s in sets)
    gulf = next(g for g in groups if "Gulf Group" in g["names"])
    assert gulf["reasons"] == ["e-mail domain", "name"]


async def test_merge_folds_history_into_the_kept_record():
    store = await dr.get_directory()
    keep = await store.upsert(_rec("Gulf Foods LLC", "gulf.ae", email="buy@gulf.ae", hs=["090422"]))
    dup = await store.upsert(_rec("Gulf Group", "gulfgroup.com", phone="+971 4 000 0000", hs=["100630"],
                                  source="apify_india_suppliers", first=NOW - timedelta(days=30),
                                  do_not_contact=True, dnc_reason="asked by phone"))
    merged = await dedupe.merge("buyer", keep.id, [dup.id], "ops@portless.in")
    assert merged.hs_codes == ["090422", "100630"] and merged.phone == "+971 4 000 0000"
    assert merged.do_not_contact and merged.dnc_reason == "asked by phone"
    assert merged.merged_from == [dup.id] and merged.aliases == ["Gulf Group"] and merged.merged_by == "ops@portless.in"
    assert merged.first_seen_at == dup.first_seen_at and merged.times_seen == 2
    assert await store.get(dup.id) is None and (await store.get(keep.id)).aliases == ["Gulf Group"]


async def test_merge_refusals():
    store = await dr.get_directory()
    a = await store.upsert(_rec("Gulf Foods", "gulf.ae"))
    s = await store.upsert(_rec("Mill One", "mill.in", role="supplier", country="IN"))
    with pytest.raises(LookupError):
        await dedupe.merge("buyer", a.id, [s.id], "ops")  # other role
    with pytest.raises(ValueError):
        await dedupe.merge("buyer", a.id, [a.id], "ops")
    with pytest.raises(LookupError):
        await dedupe.merge("buyer", "buyer:nope", [a.id], "ops")


async def test_merge_in_mongo(monkeypatch):
    from mongomock_motor import AsyncMongoMockClient
    store = dr.MongoDirectory(AsyncMongoMockClient()["t"]["trade_counterparties"])
    monkeypatch.setattr(dr, "get_directory", AsyncMock(return_value=store))
    a = await store.upsert(_rec("Gulf Foods", "gulf.ae", email="buy@gulf.ae"))
    b = await store.upsert(_rec("Gulf Foods Trading", "gulftrading.ae", email="ops@gulf.ae"))
    assert len(await dedupe.duplicates("buyer")) == 1
    await dedupe.merge("buyer", a.id, [b.id], "ops")
    assert await store.get(b.id) is None and (await store.get(a.id)).merged_from == [b.id]


async def test_routes():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.trade_min.router import _staff, router
    store = await dr.get_directory()
    a = await store.upsert(_rec("Gulf Foods", "gulf.ae", email="buy@gulf.ae"))
    b = await store.upsert(_rec("Gulf Foods Trading", "gulftrading.ae", email="ops@gulf.ae"))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@portless.in", is_portless=True)
    c = TestClient(app)
    groups = c.get("/trade-min/directory/buyers/duplicates").json()
    assert len(groups) == 1 and set(groups[0]["ids"]) == {a.id, b.id}
    r = c.post("/trade-min/directory/buyers/merge", json={"keep": a.id, "merge": [b.id]})
    assert r.status_code == 200 and r.json()["merged_from"] == [b.id]
    assert c.post("/trade-min/directory/buyers/merge", json={"keep": a.id, "merge": [b.id]}).status_code == 404
