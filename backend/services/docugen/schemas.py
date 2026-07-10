"""Request/response models for the DocuGen service.

DocuGen renders export / customs documents from Jinja2 templates:
  CI   — Commercial Invoice
  PL   — Packing List
  CoO  — Certificate of Origin
  SDF  — Shipper's Declaration Form
  SHIPPING_BILL — Customs Shipping Bill (ICEGATE)

PI generation is delegated to DealMaker; this service renders the post-PI
documents that accompany physical shipment and customs clearance.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from shared.domain.enums import DocumentType


# ---------------------------------------------------------------------------
# Common line-item model
# ---------------------------------------------------------------------------

class LineItem(BaseModel):
    """One commodity line in a trade document."""
    hs_code: str
    description: str
    quantity_mt: float
    unit_price: float
    currency: str = "USD"
    total_value: float | None = None

    def computed_total(self) -> float:
        return self.total_value if self.total_value is not None else self.quantity_mt * self.unit_price


# ---------------------------------------------------------------------------
# Document record (stored in repository)
# ---------------------------------------------------------------------------

class DocumentRecord(BaseModel):
    """One rendered document stored in the DocuGen repository."""

    doc_id: str
    deal_id: str
    doc_type: DocumentType
    status: Literal["PENDING", "RENDERED", "FAILED"] = "PENDING"

    # Rendered artifact
    storage_uri: str | None = None
    content_hash: str | None = None

    # Audit timestamps
    created_at: datetime
    rendered_at: datetime | None = None

    # Idempotency
    idempotency_key: str | None = None

    # Raw template context snapshot (for audit / re-render)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Generate request
# ---------------------------------------------------------------------------

class GenerateDocumentRequest(BaseModel):
    """Generate one export/customs document for a deal.

    The service fetches the deal record in-process and derives party details,
    HS code, and financials from it.  Only the fields below that cannot be
    inferred must be provided.
    """

    deal_id: str
    doc_type: DocumentType

    # ── Financial / logistic overrides ────────────────────────────────────
    invoice_date: date | None = None
    agreed_price: float | None = None
    currency: str = "USD"
    quantity_mt: float | None = None
    incoterm: str | None = None
    port_of_loading: str | None = None
    port_of_destination: str | None = None
    payment_terms: str | None = None

    # ── Party overrides (auto-derived from deal when absent) ──────────────
    buyer_name: str | None = None
    buyer_address: str | None = None
    buyer_vat_number: str | None = None
    supplier_name: str | None = None
    supplier_address: str | None = None
    supplier_gstin: str | None = None
    supplier_iec: str | None = None

    # ── Document-specific fields ──────────────────────────────────────────
    marks_and_numbers: str | None = None
    """Carton/bale marks for packing list."""
    no_of_packages: int | None = None
    package_type: str | None = None
    """e.g. 'Jute bags', 'PP woven bags'"""
    gross_weight_mt: float | None = None
    net_weight_mt: float | None = None

    # CoO specific
    country_of_origin: str = "India"
    coo_chamber: str | None = None
    """Issuing chamber of commerce name."""

    # SDF / Shipping Bill
    vessel_name: str | None = None
    voyage_number: str | None = None
    bill_of_lading_number: str | None = None
    let_export_order: str | None = None

    # Shipping Bill extras
    rodtep_declaration: bool = True
    draw_back_claim: bool = False

    # Idempotency
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# API responses
# ---------------------------------------------------------------------------

class DocumentResponse(BaseModel):
    """API response for a generated document."""

    doc_id: str
    deal_id: str
    doc_type: DocumentType
    status: str
    storage_uri: str | None = None
    content_hash: str | None = None
    created_at: datetime
    rendered_at: datetime | None = None

    @classmethod
    def from_record(cls, r: DocumentRecord) -> "DocumentResponse":
        return cls(
            doc_id=r.doc_id,
            deal_id=r.deal_id,
            doc_type=r.doc_type,
            status=r.status,
            storage_uri=r.storage_uri,
            content_hash=r.content_hash,
            created_at=r.created_at,
            rendered_at=r.rendered_at,
        )


class DealDocumentsListResponse(BaseModel):
    """All generated documents for a deal."""
    deal_id: str
    documents: list[DocumentResponse]


# ---------------------------------------------------------------------------
# Customs filing
# ---------------------------------------------------------------------------

class CustomsFilingRequest(BaseModel):
    """Trigger ICEGATE Shipping Bill filing for a deal.

    The service assembles the payload from the deal record and previously
    generated trade documents.  Only supply fields not already on the deal.
    """
    deal_id: str
    shipping_bill_doc_id: str | None = None
    """Pre-generated Shipping Bill doc to submit; generated on-the-fly if absent."""
    rodtep_declaration: bool = True
    draw_back_claim: bool = False


class CustomsFilingResponse(BaseModel):
    deal_id: str
    shipping_bill_number: str | None = None
    let_export_order: str | None = None
    status: Literal["FILED", "PENDING", "FAILED"] = "FILED"
    message: str | None = None
