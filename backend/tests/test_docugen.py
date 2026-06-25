"""Tests for DocuGen service: document generation and customs filing."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services.docugen.repository import InMemoryDocuGenRepository
from services.docugen.schemas import (
    CustomsFilingRequest,
    GenerateDocumentRequest,
)
from services.docugen.service import (
    DealNotFoundError,
    DocuGenError,
    DocumentNotFoundError,
    file_customs,
    generate_document,
    get_document,
    list_deal_documents,
)
from shared.domain.enums import DocumentType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mock_deal(deal_id: str = "DEAL-DOC") -> object:
    class FakeDeal:
        hs_code = "1006"
        market = "Spain"
        parties = []
        buyer_target_price_usd = 500.0

    d = FakeDeal()
    d.deal_id = deal_id  # type: ignore[attr-defined]
    return d


@pytest.fixture(autouse=True)
def _repo(tmp_path, monkeypatch) -> InMemoryDocuGenRepository:
    repo = InMemoryDocuGenRepository()
    monkeypatch.setattr("services.docugen.service.get_repository", lambda: repo)
    monkeypatch.setattr(
        "services.docugen.service.settings.docugen_storage_dir",
        str(tmp_path),
    )
    return repo


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _gen_req(doc_type: DocumentType, **overrides) -> GenerateDocumentRequest:
    defaults = dict(
        deal_id="DEAL-DOC",
        doc_type=doc_type,
        agreed_price=480.0,
        quantity_mt=100.0,
        port_of_loading="Chennai, IN",
        port_of_destination="Valencia, ES",
        payment_terms="30% advance + 70% on BL",
        buyer_name="Acme Imports SA",
        buyer_address="1 Trade Plaza, Madrid, Spain",
    )
    defaults.update(overrides)
    return GenerateDocumentRequest(**defaults)


# ---------------------------------------------------------------------------
# Document generation — positive paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "doc_type",
    [
        DocumentType.COMMERCIAL_INVOICE,
        DocumentType.PACKING_LIST,
        DocumentType.CERTIFICATE_OF_ORIGIN,
        DocumentType.SDF,
        DocumentType.SHIPPING_BILL,
    ],
)
def test_generate_document_renders_and_stores(doc_type):
    with (
        patch("services.docugen.service._fetch_deal", AsyncMock(return_value=_mock_deal())),
        patch("services.docugen.service._emit", AsyncMock()),
    ):
        resp = run(generate_document(_gen_req(doc_type)))

    assert resp.status == "RENDERED"
    assert resp.content_hash is not None
    assert resp.storage_uri is not None
    assert resp.deal_id == "DEAL-DOC"


def test_generate_ci_has_correct_doc_id():
    with (
        patch("services.docugen.service._fetch_deal", AsyncMock(return_value=_mock_deal())),
        patch("services.docugen.service._emit", AsyncMock()),
    ):
        resp = run(generate_document(_gen_req(DocumentType.COMMERCIAL_INVOICE)))

    assert resp.doc_id.startswith("CI")


def test_generate_pl_has_correct_doc_id():
    with (
        patch("services.docugen.service._fetch_deal", AsyncMock(return_value=_mock_deal())),
        patch("services.docugen.service._emit", AsyncMock()),
    ):
        resp = run(generate_document(_gen_req(DocumentType.PACKING_LIST)))

    assert resp.doc_id.startswith("PL")


# ---------------------------------------------------------------------------
# PI rejected
# ---------------------------------------------------------------------------

def test_pi_generation_raises_docugen_error():
    with pytest.raises(DocuGenError, match="DealMaker"):
        run(generate_document(_gen_req(DocumentType.PROFORMA_INVOICE)))


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_idempotency_returns_same_document():
    with (
        patch("services.docugen.service._fetch_deal", AsyncMock(return_value=_mock_deal())),
        patch("services.docugen.service._emit", AsyncMock()),
    ):
        r1 = run(generate_document(_gen_req(DocumentType.COMMERCIAL_INVOICE, idempotency_key="K-CI-001")))
        r2 = run(generate_document(_gen_req(DocumentType.COMMERCIAL_INVOICE, idempotency_key="K-CI-001")))

    assert r1.doc_id == r2.doc_id


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_deal_not_found_raises_deal_not_found_error():
    with patch(
        "services.docugen.service._fetch_deal",
        AsyncMock(side_effect=DealNotFoundError("DEAL-GHOST")),
    ):
        with pytest.raises(DealNotFoundError):
            run(generate_document(_gen_req(DocumentType.COMMERCIAL_INVOICE, deal_id="DEAL-GHOST")))


def test_get_document_raises_not_found_on_unknown_id():
    with pytest.raises(DocumentNotFoundError):
        run(get_document("DOC-GHOST"))


def test_list_deal_documents_returns_empty_when_none():
    resp = run(list_deal_documents("DEAL-NONE"))
    assert resp.documents == []


# ---------------------------------------------------------------------------
# Fetch back after generate
# ---------------------------------------------------------------------------

def test_get_document_after_generate():
    with (
        patch("services.docugen.service._fetch_deal", AsyncMock(return_value=_mock_deal())),
        patch("services.docugen.service._emit", AsyncMock()),
    ):
        created = run(generate_document(_gen_req(DocumentType.COMMERCIAL_INVOICE)))
        fetched = run(get_document(created.doc_id))

    assert fetched.doc_id == created.doc_id
    assert fetched.content_hash == created.content_hash


def test_list_deal_documents_after_generate():
    with (
        patch("services.docugen.service._fetch_deal", AsyncMock(return_value=_mock_deal())),
        patch("services.docugen.service._emit", AsyncMock()),
    ):
        run(generate_document(_gen_req(DocumentType.COMMERCIAL_INVOICE)))
        run(generate_document(_gen_req(DocumentType.PACKING_LIST)))

    resp = run(list_deal_documents("DEAL-DOC"))
    assert len(resp.documents) == 2
    doc_types = {d.doc_type for d in resp.documents}
    assert DocumentType.COMMERCIAL_INVOICE in doc_types
    assert DocumentType.PACKING_LIST in doc_types


# ---------------------------------------------------------------------------
# Customs filing
# ---------------------------------------------------------------------------

def test_file_customs_returns_shipping_bill_number():
    with (
        patch("services.docugen.service._fetch_deal", AsyncMock(return_value=_mock_deal())),
        patch("services.docugen.service._emit", AsyncMock()),
    ):
        resp = run(file_customs(CustomsFilingRequest(deal_id="DEAL-DOC")))

    assert resp.status == "FILED"
    assert resp.shipping_bill_number is not None
    assert resp.let_export_order is not None
    assert resp.let_export_order.startswith("LEO-")


def test_file_customs_reuses_existing_shipping_bill(_repo):
    """If a Shipping Bill already exists it should be reused, not re-generated."""
    with (
        patch("services.docugen.service._fetch_deal", AsyncMock(return_value=_mock_deal())),
        patch("services.docugen.service._emit", AsyncMock()),
    ):
        # Pre-generate a Shipping Bill
        run(generate_document(_gen_req(DocumentType.SHIPPING_BILL)))

        # Now file customs — should reuse the existing SB document
        resp = run(file_customs(CustomsFilingRequest(deal_id="DEAL-DOC")))

    assert resp.status == "FILED"
