"""Request/response models for the DealMaker service.

DealMaker owns two document types:
  PI (Proforma Invoice) — issued to the buyer after NEGOTIATION_AGREED.
  PO (Purchase Order)   — issued to the supplier after ESCROW_FUNDED.

Both documents are rendered from Jinja2 HTML templates, hashed with SHA-256,
and persisted to the repository (MongoDB or in-memory fallback).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Proforma Invoice
# ---------------------------------------------------------------------------

class IssuePiRequest(BaseModel):
    """Fields required to generate and send a Proforma Invoice to the buyer.

    The service fetches the deal record to derive buyer name/address,
    supplier details, HS code, and market.  Provide only the fields that
    are not yet stored on the deal.
    """

    deal_id: str
    agreed_price: float
    """Final negotiated CIF unit price in USD."""
    currency: str = "USD"
    quantity_mt: float
    """Shipment quantity in metric tonnes."""
    incoterm: str = "CIF"
    port_of_destination: str
    """Port where cargo will be delivered (e.g. 'Valencia, ES')."""
    payment_terms: str
    """e.g. '30% advance + 70% on BL'"""
    validity_days: int = 7
    """Days from issuance before the PI expires."""

    # Optional rich fields — derived from the deal record when absent
    buyer_name: str | None = None
    buyer_address: str | None = None
    buyer_vat_number: str | None = None
    product_description: str | None = None

    # Buyer selections captured on the PI — flow downstream to Shield-Link (which
    # QC team to book) and Escrow-Guard (which neutral escrow to open). See
    # workflow_dealmaker.md §3.1. ``escrow_provider`` None → Escrow-Guard uses its
    # configured default.
    qc_provider: Literal["sgs", "bureau_veritas", "intertek", "cotecna", "qima"] = "sgs"
    escrow_provider: Literal["tazapay", "escrow_com"] | None = None

    # Idempotency — re-submitting the same key returns the existing PI
    idempotency_key: str | None = None


class PiRecord(BaseModel):
    """Proforma Invoice as stored in the repository."""

    pi_id: str
    deal_id: str
    status: Literal["DRAFT", "SENT", "ACCEPTED", "SUPERSEDED"] = "SENT"

    # Document content
    agreed_price: float
    currency: str
    quantity_mt: float
    total_value: float
    incoterm: str
    port_of_destination: str
    payment_terms: str
    validity_days: int
    buyer_name: str
    buyer_address: str
    buyer_vat_number: str | None

    # Buyer selections captured on the PI (target — see workflow_dealmaker.md §3.1)
    qc_provider: str | None = None
    escrow_provider: str | None = None

    # Derived / rendered
    storage_uri: str | None = None
    content_hash: str | None = None

    # Timestamps
    issued_at: datetime
    expires_at: datetime
    accepted_at: datetime | None = None

    # Idempotency
    idempotency_key: str | None = None


class AcceptPiRequest(BaseModel):
    """Buyer acceptance signal — transitions PI → ACCEPTED and deal → PI_ACCEPTED."""
    accepted_by: str | None = None
    """Identifier of the person or system confirming acceptance (audit trail)."""
    notes: str | None = None
    buyer_email: str | None = None
    """Buyer's communication email, if known at acceptance time. Best-effort:
    registers/updates a Portless-admin-visible Organization entry for the
    buyer (named after the PI's buyer_name) with this email attached, so a
    later signup from that address can be matched to the right org — see
    services/auth/service.py::find_or_create_organization_for_deal."""


class PiResponse(BaseModel):
    """API response for PI operations."""

    pi_id: str
    deal_id: str
    status: str
    agreed_price: float
    currency: str
    quantity_mt: float
    total_value: float
    incoterm: str
    port_of_destination: str
    payment_terms: str
    validity_days: int
    buyer_name: str
    storage_uri: str | None = None
    content_hash: str | None = None
    issued_at: datetime
    expires_at: datetime
    accepted_at: datetime | None = None

    @classmethod
    def from_record(cls, r: PiRecord) -> "PiResponse":
        return cls(**r.model_dump())


# ---------------------------------------------------------------------------
# Purchase Order
# ---------------------------------------------------------------------------

class IssuePoRequest(BaseModel):
    """Fields required to generate and send a Purchase Order to the supplier.

    The service fetches the deal record to derive HS code, market, and
    winning supplier details.  Provide the financial and logistic fields
    not yet stored on the deal.
    """

    deal_id: str
    agreed_price: float
    """Final negotiated FOB unit price in USD."""
    currency: str = "USD"
    quantity_mt: float
    incoterm: str = "FOB"
    port_of_loading: str
    """Port of loading (e.g. 'Chennai, IN')."""
    delivery_deadline: date
    payment_terms: str
    """e.g. '30% advance + 70% on BL'"""

    # Optional rich fields — derived from deal record when absent
    supplier_name: str | None = None
    supplier_address: str | None = None
    supplier_gstin: str | None = None
    supplier_iec: str | None = None
    product_description: str | None = None

    # Quality / packing spec (forwarded verbatim to the template)
    quality_spec: str | None = None
    """e.g. 'AGMARK Grade 1, moisture < 12 %, SGS CRF required'"""

    # Idempotency — re-submitting the same key returns the existing PO
    idempotency_key: str | None = None


class PoRecord(BaseModel):
    """Purchase Order as stored in the repository."""

    po_id: str
    deal_id: str
    status: Literal["ISSUED", "CONFIRMED", "CANCELLED"] = "ISSUED"

    # Document content
    agreed_price: float
    currency: str
    quantity_mt: float
    total_value: float
    incoterm: str
    port_of_loading: str
    delivery_deadline: date
    payment_terms: str
    supplier_name: str
    supplier_address: str
    supplier_gstin: str | None
    supplier_iec: str | None
    quality_spec: str | None

    # Margin gate result (recorded for audit)
    net_margin_pct: float | None = None
    margin_floor_pct: float | None = None

    # Derived / rendered
    storage_uri: str | None = None
    content_hash: str | None = None

    # Timestamps
    issued_at: datetime
    confirmed_at: datetime | None = None

    # Idempotency
    idempotency_key: str | None = None


class PoResponse(BaseModel):
    """API response for PO operations."""

    po_id: str
    deal_id: str
    status: str
    agreed_price: float
    currency: str
    quantity_mt: float
    total_value: float
    incoterm: str
    port_of_loading: str
    delivery_deadline: date
    payment_terms: str
    supplier_name: str
    net_margin_pct: float | None = None
    storage_uri: str | None = None
    content_hash: str | None = None
    issued_at: datetime

    @classmethod
    def from_record(cls, r: PoRecord) -> "PoResponse":
        return cls(**r.model_dump())


# ---------------------------------------------------------------------------
# List response
# ---------------------------------------------------------------------------

class DealDocumentsResponse(BaseModel):
    """Summary of all DealMaker documents for a given deal."""
    deal_id: str
    pi: PiResponse | None = None
    po: PoResponse | None = None
