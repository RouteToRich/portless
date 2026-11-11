"""Every trade document: one design, real deal facts, DRAFT when a required fact is missing,
and each side only sees what is theirs (shared/documents, services/docugen, services/dealmaker)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.dealmaker import repository as dm_repo_mod
from services.dealmaker.repository import InMemoryDealMakerRepository
from services.dealmaker.schemas import PiRecord, PoRecord
from services.docugen import service as docugen
from services.docugen.repository import InMemoryDocuGenRepository
from services.docugen.schemas import GenerateDocumentRequest
from services.escrow_guard import repository as eg_repo_mod
from services.escrow_guard.repository import InMemoryEscrowGuardRepository
from services.escrow_guard.schemas import EscrowHoldRecord, PayoutRecord, TrancheSpec
from services.shield_link import repository as sl_repo_mod
from services.shield_link.repository import InMemoryShieldLinkRepository
from services.shield_link.schemas import ThirdPartyOrderRecord
from shared.documents.render import amount_words
from shared.domain.enums import DocumentType, PartyRole
from shared.domain.models import Party
from shared.orchestration import compliance, continuations, supplier_commitment
from tests.factories import make

NOW = datetime.now(timezone.utc)
BUYER, SUPPLIER = "Gulf Staples Trading LLC", "Cauvery Rice Mills Pvt Ltd"
FULL_EXPORTER = {
    "PORTLESS_ADDRESS": "4th Floor, 1 Anna Salai, Chennai 600002", "PORTLESS_IEC": "0412345678",
    "PORTLESS_GSTIN": "33AAACP1234Q1Z5", "PORTLESS_LUT_ARN": "AD330326000123X", "PORTLESS_AD_CODE": "6390005",
    "PORTLESS_CONTRACT_LAW": "Laws of India; disputes by arbitration in Chennai.", "PORTLESS_MARINE_POLICY": "OMP-2026-77",
}


class _NoDrive:
    def is_configured(self) -> bool:
        return False


@pytest.fixture
def deal(monkeypatch, tmp_path):
    for name in FULL_EXPORTER:
        monkeypatch.delenv(name, raising=False)
    compliance.reset_for_tests()
    supplier_commitment.reset_for_tests()
    monkeypatch.setattr(supplier_commitment, "_mongo", lambda: None)
    docs = InMemoryDocuGenRepository()
    monkeypatch.setattr(docugen, "get_repository", lambda: docs)
    monkeypatch.setattr(docugen.settings, "docugen_storage_dir", str(tmp_path))
    monkeypatch.setattr(docugen, "get_drive_connector", lambda: _NoDrive())
    monkeypatch.setattr(dm_repo_mod, "_repository", InMemoryDealMakerRepository())
    monkeypatch.setattr(eg_repo_mod, "_repository", InMemoryEscrowGuardRepository())
    monkeypatch.setattr(sl_repo_mod, "_repository", InMemoryShieldLinkRepository())

    record = SimpleNamespace(
        deal_id="D-DOC", hs_code="10063020", market="AE", status="CLEARED", quantity_mt=20.0,
        buyer_target_price_usd=1200.0, opportunity_id=None,
        parties=[
            Party(role=PartyRole.BUYER, legal_name=BUYER, country="AE", address="Office 12, Al Khaleej Road, Dubai",
                  email="buyer@gulf.invalid", vat_number="100234567800003"),
            Party(role=PartyRole.SUPPLIER, party_id="SUP-1", legal_name=SUPPLIER, country="IN",
                  address="14 Mill Road, Thanjavur", gstin="33AAAAA0000A1Z5", email="sales@cauvery.invalid"),
        ])
    monkeypatch.setattr("services.deals.service.get_deal", AsyncMock(return_value=record))
    sheet = SimpleNamespace(
        lines=[SimpleNamespace(key="ocean_freight", usd_per_mt=60.0, pct_of_price=0.0),
               SimpleNamespace(key="marine_insurance", usd_per_mt=0.0, pct_of_price=0.5)],
        supplier_route=SimpleNamespace(city="Thanjavur", state="Tamil Nadu", port="Tuticorin"))
    session = SimpleNamespace(cost_sheet=sheet, quantity_mt=20.0, qc_provider="sgs")

    class NegRepo:
        async def get_session_by_deal(self, _deal_id):
            return session
    monkeypatch.setattr("agents.negotiation_agent.repository.get_repository", lambda: NegRepo())
    return record


async def _full_trade(deal) -> None:
    """PI 1,250 CIF Jebel Ali, PO 1,000, inspector loaded 19.8 MT, freight booked, escrow settled, one payout."""
    dm = dm_repo_mod._repository
    await dm.save_pi(make(PiRecord, pi_id="PI-1", deal_id=deal.deal_id, agreed_price=1250.0, currency="USD",
                          quantity_mt=20.0, total_value=25000.0, incoterm="CIF", port_of_destination="Jebel Ali, AE",
                          payment_terms="100% into neutral escrow (Tazapay) before production; released 30% on Purchase Order",
                          buyer_name=BUYER, buyer_address="Office 12, Dubai", escrow_provider="tazapay",
                          issued_at=NOW, expires_at=NOW + timedelta(days=7), accepted_at=NOW, status="ACCEPTED"))
    await dm.save_po(make(PoRecord, po_id="PO-1", deal_id=deal.deal_id, agreed_price=1000.0, currency="USD",
                          quantity_mt=20.0, total_value=20000.0, incoterm="FOB", port_of_loading="Tuticorin, IN",
                          delivery_deadline=date.today() + timedelta(days=21), supplier_name=SUPPLIER,
                          supplier_address="14 Mill Road", quality_spec="Broken max 5%, moisture max 14%", issued_at=NOW))
    sl = sl_repo_mod._repository
    await sl.save(make(ThirdPartyOrderRecord, tpo_id="Q1", deal_id=deal.deal_id, order_type="QC", provider="sgs",
                       status="COMPLETED", ext_ref="SGS-88", qc_result="PASS", created_at=NOW, updated_at=NOW))
    await sl.save(make(ThirdPartyOrderRecord, tpo_id="F1", deal_id=deal.deal_id, order_type="FREIGHT", provider="cogoport",
                       status="BOOKED", ext_ref="COGO-9", origin_port="Tuticorin, IN", destination_port="Jebel Ali, AE",
                       vessel="MSC AURORA", voyage="FA612W", etd="2026-12-01", eta="2026-12-12",
                       delivered_at=NOW, provider_payload={"bl_number": "MEDU1234567"}, created_at=NOW, updated_at=NOW))
    await compliance.record_loading(deal.deal_id, compliance.LoadingRecord(
        container_numbers=["MSCU1234565"], seal_numbers=["SGS-SEAL-4411"], loaded_quantity_mt=19.8,
        hs_code="10063020", inspector_ref="SGS-88", source="qc_webhook"))
    eg = eg_repo_mod._repository
    tranches = [TrancheSpec(tranche_id=t, trigger=trg, pct=p, amount=25000 * p, status="RELEASED", released_at=NOW)
                for t, trg, p in (("T1", "PO_ISSUED", .3), ("T2", "QC_PASSED", .4), ("T3", "CLEARED", .2), ("T4", "ACCEPTED", .1))]
    await eg.save_hold(make(EscrowHoldRecord, escrow_id="ESC-1", deal_id=deal.deal_id, provider="tazapay", amount=25000.0,
                            currency="USD", status="RELEASED", funded_at=NOW, funded_amount=25000.0, tranches=tranches,
                            settled_at=NOW, buyer_accepted_at=NOW, created_at=NOW, updated_at=NOW))
    await eg.save_payout(make(PayoutRecord, payout_id="PAY-1", deal_id=deal.deal_id, payee_id="SUP-1", amount=18000.0,
                              currency="USD", status="COMPLETED", ext_ref="PYN-551", completed_at=NOW, created_at=NOW,
                              updated_at=NOW, idempotency_key="k1", kind="supplier_payout", total_owed=20000.0))


def _html(tmp_path, doc) -> str:
    from urllib.parse import unquote, urlparse
    return open(unquote(urlparse(doc.storage_uri).path).lstrip("/") if re.match(r"^/[A-Za-z]:", unquote(urlparse(doc.storage_uri).path))
                else unquote(urlparse(doc.storage_uri).path), encoding="utf-8").read()


async def _gen(doc_type, **kw):
    return await docugen.generate_document(GenerateDocumentRequest(deal_id="D-DOC", doc_type=doc_type, **kw))


BUYER_FACING = (DocumentType.COMMERCIAL_INVOICE, DocumentType.PACKING_LIST, DocumentType.CERTIFICATE_OF_ORIGIN,
                DocumentType.SALES_CONTRACT, DocumentType.SHIPPING_INSTRUCTIONS, DocumentType.INSURANCE_DECLARATION,
                DocumentType.ACCEPTANCE_CERTIFICATE, DocumentType.SETTLEMENT_STATEMENT, DocumentType.CREDIT_NOTE)
SUPPLIER_FACING = (DocumentType.INSPECTION_ORDER, DocumentType.REMITTANCE_ADVICE)


# ---------------------------------------------------------------------------
# Every document renders from the deal's facts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc_type", [t for t in DocumentType if t != DocumentType.PROFORMA_INVOICE])
async def test_every_document_renders_from_the_deal(deal, tmp_path, monkeypatch, doc_type):
    for k, v in FULL_EXPORTER.items():
        monkeypatch.setenv(k, v)
    await _full_trade(deal)
    doc = await _gen(doc_type, refund_amount=500.0 if doc_type == DocumentType.CREDIT_NOTE else None)
    html = _html(tmp_path, doc)
    assert doc.status == "RENDERED" and doc.content_hash
    assert "{{" not in html and "{%" not in html
    assert "12 Commerce Street" not in html, "the services' placeholder address is never printed"
    assert not re.search(r"None", re.sub(r"<[^>]+>", " ", html)), "a missing value printed as None"
    assert "Rice" in html or "rice" in html


async def test_a_missing_exporter_fact_makes_the_document_draft(deal, tmp_path):
    await _full_trade(deal)
    doc = await _gen(DocumentType.COMMERCIAL_INVOICE)
    html = _html(tmp_path, doc)
    assert doc.draft and any("PORTLESS_ADDRESS" in r for r in doc.draft_reasons)
    assert "DRAFT — NOT VALID FOR USE" in html


async def test_with_every_fact_present_the_shipping_documents_are_final(deal, tmp_path, monkeypatch):
    for k, v in FULL_EXPORTER.items():
        monkeypatch.setenv(k, v)
    await _full_trade(deal)
    await _gen(DocumentType.COMMERCIAL_INVOICE)
    for doc_type in (DocumentType.COMMERCIAL_INVOICE, DocumentType.SDF, DocumentType.SHIPPING_BILL,
                     DocumentType.CERTIFICATE_OF_ORIGIN, DocumentType.SALES_CONTRACT, DocumentType.SHIPPING_INSTRUCTIONS,
                     DocumentType.INSURANCE_DECLARATION):
        doc = await _gen(doc_type)
        assert not doc.draft, f"{doc_type.value}: {doc.draft_reasons}"
    pl = await _gen(DocumentType.PACKING_LIST)
    assert pl.draft and "number of packages" in pl.draft_reasons
    pl = await _gen(DocumentType.PACKING_LIST, no_of_packages=396, gross_weight_mt=19.9)
    assert not pl.draft


# ---------------------------------------------------------------------------
# The figures are the real ones
# ---------------------------------------------------------------------------

async def test_invoice_uses_the_pi_price_the_loaded_quantity_and_the_escrow_terms(deal, tmp_path):
    await _full_trade(deal)
    html = _html(tmp_path, await _gen(DocumentType.COMMERCIAL_INVOICE))
    assert "24,750.00" in html                       # 19.8 MT loaded × 1,250 (PI), not 20 × the buyer's 1,200 target
    assert "within the ±5% tolerance" in html
    assert "30% advance" not in html and "neutral escrow" in html
    assert "MSCU1234565" in html and "SGS-SEAL-4411" in html and "MEDU1234567" in html and "MSC AURORA" in html
    assert "US Dollars Twenty-Four Thousand Seven Hundred Fifty Only" in html


async def test_fob_value_takes_freight_and_insurance_off_the_cif_invoice(deal, tmp_path):
    await _full_trade(deal)
    html = _html(tmp_path, await _gen(DocumentType.SDF))
    # 24,750 CIF − 60 × 19.8 freight (1,188) − 0.5% insurance (123.75) = 23,438.25
    assert "23,438.25" in html


async def test_fob_split_reads_the_cost_sheet_as_the_session_stores_it(deal, tmp_path, monkeypatch):
    """The negotiation session keeps ``cost_sheet`` as a plain dict (Mongo); SDF and the
    shipping bill must read it like the model, not fail with 'dict' has no 'lines'."""
    session = SimpleNamespace(quantity_mt=20.0, qc_provider="sgs", cost_sheet={"quantity_mt": 20.0, "lines": [
        {"key": "ocean_freight", "label": "Ocean freight", "usd_per_mt": 60.0},
        {"key": "marine_insurance", "label": "Marine insurance", "pct_of_price": 0.5}]})

    class NegRepo:
        async def get_session_by_deal(self, _deal_id):
            return session
    monkeypatch.setattr("agents.negotiation_agent.repository.get_repository", lambda: NegRepo())
    await _full_trade(deal)
    record = await _gen(DocumentType.SDF)
    assert record.status == "RENDERED"
    assert "23,438.25" in _html(tmp_path, record)


async def test_sales_contract_is_for_the_contract_quantity(deal, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTLESS_CONTRACT_LAW", "Laws of India; arbitration in Chennai.")
    await _full_trade(deal)
    html = _html(tmp_path, await _gen(DocumentType.SALES_CONTRACT))
    assert "25,000.00" in html and "Laws of India" in html and "110%" in html
    assert "determined at loading by SGS" in html


async def test_remittance_advice_shows_paid_to_date_and_balance(deal, tmp_path):
    await _full_trade(deal)
    html = _html(tmp_path, await _gen(DocumentType.REMITTANCE_ADVICE, payout_id="PAY-1"))
    assert "18,000.00" in html and "2,000.00" in html and "PYN-551" in html


# ---------------------------------------------------------------------------
# Each side only sees what is theirs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc_type", BUYER_FACING)
async def test_buyer_documents_never_name_the_supplier_or_its_price(deal, tmp_path, doc_type):
    await _full_trade(deal)
    html = _html(tmp_path, await _gen(doc_type, refund_amount=500.0 if doc_type == DocumentType.CREDIT_NOTE else None))
    assert SUPPLIER not in html and "Cauvery" not in html
    assert "20,000.00" not in html and "1,000.00" not in html


@pytest.mark.parametrize("doc_type", SUPPLIER_FACING)
async def test_supplier_documents_never_name_the_buyer(deal, tmp_path, doc_type):
    await _full_trade(deal)
    html = _html(tmp_path, await _gen(doc_type, payout_id="PAY-1"))
    assert BUYER not in html and "Gulf Staples" not in html and "25,000" not in html


# ---------------------------------------------------------------------------
# Issued along the trade
# ---------------------------------------------------------------------------

async def test_insurance_declaration_only_when_portless_insures(monkeypatch, deal):
    issued = AsyncMock()
    monkeypatch.setattr(continuations, "_issue_document", issued)
    await dm_repo_mod._repository.save_pi(make(PiRecord, pi_id="PI-F", deal_id="D-FOB", incoterm="FOB", issued_at=NOW, expires_at=NOW))
    await continuations._issue_freight_documents("D-FOB")
    assert [c.args[1] for c in issued.await_args_list] == ["SHIPPING_INSTRUCTIONS"]
    await dm_repo_mod._repository.save_pi(make(PiRecord, pi_id="PI-C", deal_id="D-CIF", incoterm="CIF", issued_at=NOW, expires_at=NOW))
    issued.reset_mock()
    await continuations._issue_freight_documents("D-CIF")
    assert [c.args[1] for c in issued.await_args_list] == ["SHIPPING_INSTRUCTIONS", "INSURANCE_DECLARATION"]


@pytest.mark.parametrize("event, basis", [("buyer_accepted", "buyer_accepted"), ("auto_release_window_lapsed", "window_lapsed")])
async def test_final_tranche_issues_the_acceptance_certificate(monkeypatch, deal, event, basis):
    from shared.domain.events import DomainEvent, EventTopic
    issued = AsyncMock()
    monkeypatch.setattr(continuations, "_issue_document", issued)
    monkeypatch.setattr(continuations, "_enabled", lambda: True)
    await continuations.on_escrow_tranche_released(DomainEvent(
        topic=EventTopic.ESCROW_TRANCHE_RELEASED, deal_id="D-DOC", payload={"trigger": "ACCEPTED", "event": event}))
    call = next(c for c in issued.await_args_list if c.args[1] == "ACCEPTANCE_CERTIFICATE")
    assert call.kwargs["acceptance_basis"] == basis


async def test_issuing_twice_returns_the_same_document(deal):
    await _full_trade(deal)
    first = await continuations._issue_document("D-DOC", "SETTLEMENT_STATEMENT")
    again = await continuations._issue_document("D-DOC", "SETTLEMENT_STATEMENT")
    assert first.doc_id == again.doc_id and first.doc_id.startswith("SOA-")


def test_amount_in_words():
    assert amount_words(25000, "USD") == "US Dollars Twenty-Five Thousand Only"
    assert amount_words(1234.5, "USD") == "US Dollars One Thousand Two Hundred Thirty-Four and Cents Fifty Only"
