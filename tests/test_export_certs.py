"""Export certificates before the shipment sails: phytosanitary, APEDA RCAC (basmati), origin."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.orchestration import export_certs as ec


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("EXPORT_CERTS_REQUIRED", "true")
    monkeypatch.setattr(ec, "_collection", lambda: None)
    ec.reset_for_tests()
    yield
    ec.reset_for_tests()


@pytest.mark.parametrize("hs,kinds", [
    ("10063020", ["certificate_of_origin", "phytosanitary", "apeda_rcac"]),   # basmati
    ("10063090", ["certificate_of_origin", "phytosanitary"]),                 # other rice
    ("09042211", ["certificate_of_origin", "phytosanitary"]),                 # chilli
    ("29054500", ["certificate_of_origin"]),                                  # glycerine
])
def test_required(hs, kinds):
    assert ec.required(hs) == kinds


async def test_recording_clears_the_missing_list():
    deal = SimpleNamespace(deal_id="D1", hs_code="10063020")
    assert (await ec.status(deal))["missing"] == ["certificate_of_origin", "phytosanitary", "apeda_rcac"]
    await ec.record("D1", "apeda_rcac", number="RCAC-2026-0099", issued_on="2026-11-10", issuer="APEDA",
                    evidence=None, by="ops")
    await ec.record("D1", "phytosanitary", number="PSC/12/2026", issued_on=None, issuer="PQ Mumbai", evidence=None, by="ops")
    assert (await ec.status(deal))["missing"] == ["certificate_of_origin"]
    with pytest.raises(ValueError):
        await ec.record("D1", "visa", number="x123", issued_on=None, issuer=None, evidence=None, by="ops")


async def test_shipment_check_raises_the_missing_certificates(monkeypatch):
    from shared.orchestration import compliance
    rec = compliance.ComplianceRecord(deal_id="D1", loading=compliance.LoadingRecord(
        container_numbers=["C1"], seal_numbers=["S1"], loaded_quantity_mt=25.0, hs_code="10063020",
        inspector_ref="Q", photos=["p"], source="inspector"))

    async def get_record(deal_id, **k):
        return rec

    async def save_record(r):
        return r

    async def pi_po(deal_id):
        return None, None
    monkeypatch.setattr(compliance, "get_record", get_record)
    monkeypatch.setattr(compliance, "save_record", save_record)
    monkeypatch.setattr(compliance, "_pi_po", pi_po)
    result = await compliance.check_shipment(SimpleNamespace(deal_id="D1", hs_code="10063020"))
    codes = {f.code for f in result.findings}
    assert {"shipment_cert_apeda_rcac", "shipment_cert_phytosanitary", "shipment_cert_certificate_of_origin"} <= codes
