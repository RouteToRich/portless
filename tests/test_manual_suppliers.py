"""Local suppliers added by hand and their CSV import (services/trade_min/manual_suppliers.py)."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.trade_min import directory as dr
from services.trade_min import manual_suppliers as ms
from services.trade_min import relationships as rl
from services.trade_min.schemas import CounterpartyValidation, SellerCandidate

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dr.reset_directory()
    monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[]))
    monkeypatch.setattr(rl, "_tracks", AsyncMock(return_value={}))
    checked = []

    async def fake_validate(rec):
        checked.append(rec.name)
        rec.validation = CounterpartyValidation(status="verified", sanctions="clear", email="ok",
                                                gst="active", iec="active")
        return rec
    monkeypatch.setattr("services.trade_min.validation.validate_record", fake_validate)
    monkeypatch.setattr("shared.currency.usd_rate", AsyncMock(return_value=88.0))
    yield checked
    dr.reset_directory()


def _in(**kw):
    base = dict(name="Thanjavur Rice Mill", contact_person="R. Kumar", email="sales@tjrice.in", phone="+91 98450 12345",
                website="https://tjrice.in", address="Plot 14, SIPCOT, Thanjavur 613005", gstin="33abcde1234f1z5",
                iec="0512345678", hs_codes="100630, 100640", grades="Ponni; Sona Masoori", capacity_mt_per_month=400,
                moq_mt=25, usual_price=61600, price_currency="INR", price_basis="EXW", port_of_loading="INMAA",
                port_km=350, bank_name="SBI", bank_ifsc="sbin0001234", bank_account_last4="4321")
    base.update(kw)
    return ms.ManualSupplierIn(**base)


async def test_add_stores_the_facts_and_runs_the_checks(_fresh):
    rec = await ms.add(_in(), "ops@portless.in")
    assert rec.source == "manual" and rec.gstin == "33ABCDE1234F1Z5" and rec.hs_codes == ["100630", "100640"]
    # INR 61,600 at 88 = USD 700 ex-works + the road to the port (350 km) = FOB-equivalent
    assert rec.estimated_fob_price_usd == round(700.0 + (350 * 3.5 + 250) / 96.0, 2)
    assert rec.manual["grades"] == ["Ponni", "Sona Masoori"] and rec.manual["bank_account_last4"] == "4321"
    assert rec.manual["added_by"] == "ops@portless.in" and rec.validation.status == "verified"
    assert _fresh == ["Thanjavur Rice Mill"]


@pytest.mark.parametrize("change,why", [({"name": "Other", "website": None, "phone": None}, "GSTIN"),
                                        ({"name": "Other", "gstin": None, "phone": None}, "website"),
                                        ({"name": "Other", "gstin": None, "website": None, "phone": "098450 12345"}, "phone")])
async def test_duplicates_are_refused_unless_forced(change, why):
    first = await ms.add(_in(), "ops")
    with pytest.raises(ms.DuplicateSupplier) as e:
        await ms.add(_in(**change), "ops")
    assert e.value.existing_id == first.id and e.value.why == why
    assert (await ms.add(_in(**change), "ops", force=True)).id != first.id


def test_input_rules():
    with pytest.raises(Exception):
        _in(gstin="NOT-A-GSTIN")
    with pytest.raises(Exception):
        _in(bank_account_last4="123456789012")  # never the full account number


async def test_a_scan_never_overwrites_what_staff_entered():
    rec = await ms.add(_in(), "ops")
    scanned = dr._supplier_record(SellerCandidate(
        candidate_id="x", company_name="TJ Rice Mills Pvt Ltd", location_city="Thanjavur", location_state="TN",
        hs_code="100630", estimated_fob_price_usd=650.0, source="apify_company_search", website="https://tjrice.in",
        email="info@tjrice.in", phone="+91 44 0000 0000", supplier_to_port_km=300.0), "100630", "AE", "found", None, NOW)
    merged = await (await dr.get_directory()).upsert(scanned)
    assert merged.id == rec.id and merged.source == "manual"
    assert (merged.name, merged.email) == ("Thanjavur Rice Mill", "sales@tjrice.in")
    assert merged.estimated_fob_price_usd == rec.estimated_fob_price_usd


async def test_edit_and_delete(monkeypatch):
    rec = await ms.add(_in(), "ops")
    edited = await ms.edit(rec.id, _in(moq_mt=50, address="Plot 15, SIPCOT, Thanjavur 613005"), "lead@portless.in")
    assert edited.manual["moq_mt"] == 50 and edited.address.startswith("Plot 15") and edited.manual["edited_by"] == "lead@portless.in"
    assert await ms.edit("supplier:nope", _in(), "ops") is None
    monkeypatch.setattr(rl, "annotate", AsyncMock(return_value=[edited.model_copy(update={"relationship": {"deals_active": 1}})]))
    with pytest.raises(ValueError):
        await ms.delete(rec.id)
    monkeypatch.setattr(rl, "annotate", AsyncMock(return_value=[edited.model_copy(update={"relationship": {}})]))
    assert await ms.delete(rec.id) and await (await dr.get_directory()).get(rec.id) is None


CSV = """legal_name,email,phone,gstin,hs,capacity,price,currency,basis
Kaveri Traders,sales@kaveri.in,+91 99000 11111,33ABCDE1234F1Z5,100630,200,62000,INR,EXW
Kaveri Traders again,other@kaveri.in,+91 99000 22222,33ABCDE1234F1Z5,100630,,,USD,FOB
,missing@name.in,,,,,,,
Delta Agro,info@deltaagro.in,+91 98000 33333,,0712 0904,100,700,USD,FOB
"""


async def test_csv_dry_run_then_import(_fresh):
    report = await ms.import_csv(CSV, "ops", dry_run=True)
    assert [a["name"] for a in report["accepted"]] == ["Kaveri Traders", "Delta Agro"]
    assert report["duplicates"][0]["row"] == 3 and report["invalid"][0]["row"] == 4
    assert report["created"] == [] and _fresh == [] and "paid Apify runs" in report["checks_note"]
    report = await ms.import_csv(CSV, "ops", dry_run=False)
    assert [c["row"] for c in report["created"]] == [2, 5] and all(c["check"] == "verified" for c in report["created"])
    again = await ms.import_csv(CSV, "ops", dry_run=True)
    assert again["accepted"] == [] and len(again["duplicates"]) == 3


class TestRoutes:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.trade_min.router import _staff, router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@portless.in", is_portless=True)
        return TestClient(app)

    async def test_add_duplicate_edit_import_delete_over_http(self):
        c = self._client()
        body = _in().model_dump()
        r = c.post("/trade-min/suppliers", json=body)
        assert r.status_code == 201 and r.json()["source"] == "manual"
        rid = r.json()["id"]
        dup = c.post("/trade-min/suppliers", json={**body, "name": "Again"})
        assert dup.status_code == 409 and dup.json()["detail"]["existing_id"] == rid
        assert c.post("/trade-min/suppliers", json={**body, "gstin": "bad"}).status_code == 422
        assert c.put(f"/trade-min/suppliers/{rid}", json={**body, "moq_mt": 40}).json()["manual"]["moq_mt"] == 40
        rep = c.post("/trade-min/suppliers/import?dry_run=true", content=CSV.encode(), headers={"content-type": "text/csv"})
        assert rep.status_code == 200 and rep.json()["dry_run"] is True
        assert c.delete(f"/trade-min/suppliers/{rid}").status_code == 204
        assert c.delete(f"/trade-min/suppliers/{rid}").status_code == 404


async def test_hand_added_suppliers_are_standby_candidates():
    from types import SimpleNamespace as NS
    from shared.orchestration import supplier_commitment as sc
    await ms.add(_in(), "ops")  # EXW INR 61,600 → USD 700 + inland for 350 km
    await ms.add(_in(name="Far Mill", website="https://far.in", gstin=None, phone="+91 90000 00000",
                     email="far@far.in", hs_codes="090422"), "ops")  # another product
    await ms.add(_in(name="No Mail Mill", website="https://nomail.in", gstin=None, phone="+91 91111 11111",
                     email=None), "ops")
    deal = NS(deal_id="D1", hs_code="10063010", match_results=[], supplier_offers=[])
    rows = await sc.standby_suppliers(deal)
    assert [r["company_name"] for r in rows] == ["Thanjavur Rice Mill"]
    expected = round(700.0 + (350 * 3.5 + 250) / 96.0, 2)
    assert rows[0]["price"] == expected and rows[0]["source"] == "directory_manual"
