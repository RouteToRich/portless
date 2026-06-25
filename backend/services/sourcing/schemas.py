"""Request/response models for the Sourcing service.

Pipeline role
-------------
After the Deals service creates a deal (status=MATCHING) the Sourcing service:

  1. ``dispatch_rfq`` — sends an automated RFQ to the winning supplier(s) via
     IndiaMART / TradeIndia connectors.
  2. ``receive_quote`` — ingests a formal supplier quote, runs concurrent
     regulatory verification (GSTN + DGFT + MCA + Udyam), re-scores the
     supplier via Match Engine with real verification data, updates the deal,
     and transitions it MATCHING → NEGOTIATING when the re-scored decision is
     SOURCE.

Verification fields in ``QuoteIntakeRequest``
-----------------------------------------------
Suppliers are expected to provide their GSTIN, IEC, and optionally CIN and
Udyam number alongside their quote.  The Sourcing service verifies these
numbers against government registries and populates ``VerificationResult``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class SupplierRef(BaseModel):
    """A supplier to dispatch an RFQ to, with enough metadata to route it."""

    supplier_id: str
    """candidate_id from SellerCandidate / MatchResult."""

    contact_id: str | None = None
    """IndiaMART / TradeIndia contact ID used by the connector's book() method."""

    source: Literal["indiamart", "tradeindia", "other"] = "indiamart"
    """Which connector to use for this supplier."""

    company_name: str = ""


class DispatchResult(BaseModel):
    """Per-supplier RFQ dispatch outcome."""

    supplier_id: str
    contact_id: str | None = None
    channel: str
    """Connector used: ``"indiamart"`` | ``"tradeindia"`` | ``"portal"`` | ``"failed"``."""
    ext_rfq_id: str | None = None
    """External RFQ reference returned by the connector."""
    success: bool
    error: str | None = None


class VerificationResult(BaseModel):
    """Regulatory verification outcome for one supplier (GSTN + DGFT + MCA + Udyam)."""

    supplier_id: str
    gst_verified: bool = True
    """True if GSTN confirms ACTIVE registration.  Defaults to True when GSTN
    check is skipped (no GSTIN provided or API unavailable)."""
    iec_active: bool = True
    """True if DGFT confirms IEC is ACTIVE and not cancelled."""
    dgft_denied: bool = False
    """True if entity appears on DGFT's denied-party list."""
    business_age_years: float = 0.0
    """Company age from MCA incorporation date.  0.0 when MCA check unavailable."""
    udyam_registered: bool = False
    """True if Udyam registration confirmed as active."""
    annual_shipment_count: int = 0
    """Placeholder — resolved by shipping manifest data in a future phase."""
    verified_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)
    """Raw responses from each connector, keyed by connector name."""


# ---------------------------------------------------------------------------
# RFQ models
# ---------------------------------------------------------------------------

class RfqRequest(BaseModel):
    deal_id: str
    hs_code: str
    suppliers: list[SupplierRef]
    """List of suppliers to contact.  Pass ``contact_id`` for portal dispatch."""
    quantity_mt: float | None = None
    target_price_usd: float | None = None
    """Buyer target CIF price to communicate to suppliers."""
    message: str | None = None
    """Custom RFQ message; auto-generated from template when omitted."""
    validity_days: int = Field(default=7, ge=1, le=60)


class RfqResponse(BaseModel):
    rfq_id: str
    deal_id: str
    dispatched_count: int
    failed_count: int
    dispatch_results: list[DispatchResult]
    status: Literal["PENDING", "PARTIAL", "COMPLETE", "FAILED"]


class RfqRecord(BaseModel):
    """Persisted RFQ document."""

    rfq_id: str
    deal_id: str
    hs_code: str
    supplier_ids: list[str]
    dispatch_results: list[DispatchResult] = Field(default_factory=list)
    quantity_mt: float | None = None
    target_price_usd: float | None = None
    message: str = ""
    status: Literal["PENDING", "PARTIAL", "COMPLETE", "FAILED"]
    dispatched_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------------------
# Quote models
# ---------------------------------------------------------------------------

class QuoteIntakeRequest(BaseModel):
    """Payload when a supplier submits a formal quote (inbound webhook / portal)."""

    rfq_id: str
    deal_id: str
    supplier_id: str
    company_name: str = ""

    unit_price_usd: float = Field(gt=0)
    """Supplier's quoted FOB price per unit in USD."""
    currency: str = "USD"
    quantity_mt: float | None = None
    validity_days: int = Field(default=30, ge=1, le=90)
    incoterm: str = "FOB"
    port_of_loading: str | None = None
    lead_time_days: int | None = None
    payment_terms: str | None = None

    # Regulatory credentials (optional but strongly recommended)
    gstin: str | None = None
    iec_number: str | None = None
    cin_number: str | None = None
    udyam_number: str | None = None


class QuoteRecord(BaseModel):
    """Full persisted quote document."""

    quote_id: str
    rfq_id: str
    deal_id: str
    supplier_id: str
    company_name: str = ""

    unit_price_usd: float
    currency: str = "USD"
    quantity_mt: float | None = None
    validity_days: int = 30
    incoterm: str = "FOB"
    port_of_loading: str | None = None
    lead_time_days: int | None = None
    payment_terms: str | None = None

    gstin: str | None = None
    iec_number: str | None = None
    cin_number: str | None = None
    udyam_number: str | None = None

    verification: VerificationResult | None = None
    """Populated after GSTN/DGFT/MCA/Udyam verification runs."""

    re_scored_match_id: str | None = None
    re_scored_cms: float | None = None
    """Composite Match Score recomputed with real verification data."""
    re_scored_decision: Literal["SOURCE", "DISCARD"] | None = None

    status: Literal["RECEIVED", "VERIFIED", "ACCEPTED", "REJECTED", "EXPIRED"]
    received_at: datetime
    updated_at: datetime


# Alias — keeps old import in router working
QuoteResponse = QuoteRecord
