"""Phone check for phone-only local suppliers: numbering plan, verdict with an active GST, staff
WhatsApp confirmation (shared/phone_check.py, services/trade_min/validation.py)."""
from __future__ import annotations

import pytest

from services.trade_min.schemas import CounterpartyValidation
from services.trade_min.validation import decide
from shared import phone_check


@pytest.mark.parametrize("raw,status,kind,e164", [
    ("+91 98450 12345", "valid", "mobile", "+919845012345"),
    ("098450-12345", "valid", "mobile", "+919845012345"),
    ("9845012345", "valid", "mobile", "+919845012345"),
    ("0712 2551234", "valid", "landline", "+917122551234"),
    ("1800 123 4567", "invalid", "other", None),
    ("98450 1234", "invalid", None, None),
    ("", "missing", None, None),
    ("+971 4 123 4567", "valid", "other", "+97141234567"),
])
def test_indian_and_international_numbers(raw, status, kind, e164):
    r = phone_check.check(raw, "IN")
    assert r["status"] == status and r["kind"] == kind
    if status == "valid":
        assert r["e164"] == e164


def test_toll_free_refused_and_multi_number_field():
    assert phone_check.check("1800-180-1551", "IN")["reason"].startswith("toll-free")
    assert phone_check.check("12345 / 98450 12345", "IN")["e164"] == "+919845012345"
    assert phone_check.check("04 123 4567", "AE")["e164"] == "+97141234567"
    assert phone_check.whatsapp_link("+91 98450 12345") == "https://wa.me/919845012345"


def _v(**kw):
    base = dict(sanctions="clear", email="missing", gst="active", iec="active", phone="valid")
    base.update(kw)
    return CounterpartyValidation(**base)


def test_phone_only_supplier_verified_with_active_gst():
    out = decide(_v(), role="supplier", country="IN", has_phone=True)
    assert out.status == "verified" and any("phone-only supplier" in r for r in out.reasons)


@pytest.mark.parametrize("change", [{"gst": "unchecked"}, {"phone": "invalid"}, {"sanctions": "review"}])
def test_phone_only_needs_gst_valid_number_and_clear_screening(change):
    assert decide(_v(**change), role="supplier", country="IN", has_phone=True).status == "unverified"


def test_buyers_and_foreign_suppliers_still_need_email():
    assert decide(_v(), role="buyer", country="AE", has_phone=True).status == "unverified"
    assert decide(_v(), role="supplier", country="VN", has_phone=True).status == "unverified"


def test_whatsapp_confirmation_route(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.trade_min import router as tm_router
    from services.trade_min.schemas import CounterpartyRecord
    from shared.auth.dependencies import get_current_user

    rec = CounterpartyRecord.model_construct(id="supplier:ramesh-traders", role="supplier", name="Ramesh Traders",
                                             country="IN", phone="98450 12345", email=None,
                                             validation=CounterpartyValidation(sanctions="clear", email="missing", gst="active"))
    saved = []

    class Store:
        async def get(self, rid):
            return rec if rid == rec.id else None

        async def set_validation(self, r):
            saved.append(r)

    async def get_dir():
        return Store()

    async def with_open(rows):
        return rows

    async def relay(*a, **k):
        return None

    monkeypatch.setattr(tm_router.directory, "get_directory", get_dir)
    monkeypatch.setattr(tm_router.directory, "with_open_opportunities", with_open)
    monkeypatch.setattr("shared.audit_log.staff_relay", relay)
    app = FastAPI()
    app.include_router(tm_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="ops", user_id="u", is_portless=True, org_id="o")
    c = TestClient(app)
    r = c.post("/trade-min/suppliers/supplier:ramesh-traders/phone-check", json={"whatsapp": "confirmed"})
    assert r.status_code == 200, r.text
    v = saved[-1].validation
    assert v.whatsapp == "confirmed" and v.phone_e164 == "+919845012345" and v.status == "verified"
    rec.phone = "123"
    assert c.post("/trade-min/suppliers/supplier:ramesh-traders/phone-check", json={"whatsapp": "confirmed"}).status_code == 422
