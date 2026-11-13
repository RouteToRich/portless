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
