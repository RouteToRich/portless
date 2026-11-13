"""Product names next to HS codes in the console (shared/product_label.py, GET /trade-min/hs-names)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.trade_min.router import router
from shared.auth import middleware
from shared.product_label import product_label


@pytest.mark.parametrize("code,name", [
    ("10063020", "basmati rice"),       # Indian 8-digit tariff line first
    ("10063010", "parboiled rice"),
    ("10063090", "non-basmati rice"),
    ("10063099", "milled rice"),        # unknown 8-digit line → its 6-digit subheading
    ("100640", "broken rice"),
    ("1006", "rice"),
    ("09041100", "black pepper"),
    ("99999999", None),                 # not curated (the console falls back to the official list)
    ("", None),
])
def test_product_label_reads_8_then_6_then_4_digits(code, name):
    assert product_label(code) == name


def test_hs_names_endpoint_returns_a_name_and_the_official_text_per_code():
    app = FastAPI()
    app.include_router(router)
    r = TestClient(app).get("/trade-min/hs-names", params={"codes": "10063020, 1520 ,847130,abc,,"})
    assert r.status_code == 200
    body = r.json()
    assert body["names"]["10063020"] == "basmati rice" and body["names"]["1520"] == "crude glycerine"
    assert body["names"]["847130"].startswith("Automatic data processing machines")
    assert body["names"]["abc"] is None and body["descriptions"]["abc"] is None
    assert "milled" in body["descriptions"]["10063020"]


@pytest.mark.parametrize("code,name", [
    ("090422", "chilli powder"),          # never "pepper" from the mixed 0904 heading
    ("090411", "black pepper"),
    ("290543", "mannitol"),
    ("100199", "wheat"),                   # curated heading beats the long official text
    ("520100", "raw cotton"),
    ("847130", "Automatic data processing machines; portable, weighing not more than…"),
    ("85", None),                          # replaced below: chapters get their official text
])
def test_every_code_in_the_data_gets_a_readable_name(code, name):
    from shared import hs_names
    got = hs_names.display_name(code)
    if code == "85":
        assert got and got.startswith("Electrical machinery")
    else:
        assert got == name


def test_the_official_list_covers_every_6_digit_code():
    from shared import hs_names
    table = hs_names._nomenclature()
    assert sum(1 for k in table if len(k) == 6) > 5000 and "_source" not in table


def test_hs_names_endpoint_caps_the_list():
    app = FastAPI()
    app.include_router(router)
    codes = ",".join(str(100000 + i) for i in range(150))
    assert len(TestClient(app).get("/trade-min/hs-names", params={"codes": codes}).json()["names"]) == 100


def test_organisation_users_may_read_hs_names():
    """Their deals page shows HS codes too; the names are nothing deal-specific."""
    assert ("GET", "/trade-min/hs-names") in middleware._NON_PORTLESS_ALLOWED
    assert ("GET", "/trade-min/catalogue") not in middleware._NON_PORTLESS_ALLOWED


@pytest.mark.parametrize("query,first", [
    ("basmati", "10063020"),
    ("1006", "100610"),
    ("chilli powder", "090422"),
    ("mannitol", "290543"),
])
def test_hs_search_by_words_or_code(query, first):
    from shared import hs_names
    results = hs_names.search(query, 10)
    assert results and results[0]["code"] == first
    assert all(r["name"] and r["description"] for r in results)


def test_hs_search_endpoint():
    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/trade-min/hs-search", params={"q": "rice", "limit": 3}).json()
    assert len(body["results"]) == 3 and all(r["code"].startswith("1006") for r in body["results"])
    assert TestClient(app).get("/trade-min/hs-search", params={"q": ""}).json() == {"results": []}


async def test_a_scan_searches_the_product_name_the_operator_picked(monkeypatch):
    """A code without a catalogue line is web-searched under the name picked in the console."""
    from services.trade_min import service
    from services.trade_min.schemas import ScanRequest
    seen = {}

    async def one(hs, market, config):
        seen[(hs, market)] = (service._keyword_for(hs), service._products_for(hs))
        return None

    monkeypatch.setattr(service, "_scan_one", one)
    monkeypatch.setattr(service, "_worldwide_counterparties", lambda hs: _none())
    await service.scan_opportunities(ScanRequest(hs_codes=["080132"], target_markets=["AE"], worldwide_search=False,
                                                 product_names={"080132": "shelled cashew kernels"}))
    assert seen[("080132", "AE")] == ("shelled cashew kernels", ["shelled cashew kernels"])
    assert service._keyword_for("080132") != "shelled cashew kernels"   # the next scan starts clean


async def _none():
    return None


async def test_a_skipped_actor_run_is_never_cached(monkeypatch):
    """No token / no run budget → the connector answers a stub. Caching it would hide the real
    result for the whole TTL (a week for the company search) once the token works."""
    from services.trade_min import service
    stored = []

    class Stub:
        async def quote(self, req):
            return {"companies": [], "total": 0, "stub": True}

    class Live:
        async def quote(self, req):
            return {"companies": [{"name": "Real Co"}], "total": 1}

    monkeypatch.setattr(service.settings, "trade_cache_enabled", True)
    monkeypatch.setattr(service, "cache_get", lambda key: _value(None))
    monkeypatch.setattr(service, "cache_set", lambda key, name, value, ttl: _record(stored, value))
    monkeypatch.setattr(service, "_build_connector", lambda name: Stub())
    assert (await service._quote_raw("apify_export_markets", {"hs_code": "100610"}))["stub"] is True
    assert stored == []
    monkeypatch.setattr(service, "_build_connector", lambda name: Live())
    await service._quote_raw("apify_export_markets", {"hs_code": "100610"})
    assert stored == [{"companies": [{"name": "Real Co"}], "total": 1}]


async def _value(v):
    return v


async def _record(stored, value):
    stored.append(value)
