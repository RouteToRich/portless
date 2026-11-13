"""Request/response models for the Deals service (LLD 4.2).

DealRecord is the canonical document stored in MongoDB and returned by all
API endpoints.  It is a superset of the thin shared-domain ``Deal`` DTO.

Orchestration flow
------------------
When ``opportunity_id`` is provided at deal creation the service:
  1. Fetches a ``MatchEngineSeed`` from Trade-Min
  2. Scores each seller candidate via Match Engine (concurrent)
  3. Picks the highest-scoring SOURCE candidate as ``winning_supplier_id``
  4. Transitions status DRAFT → MATCHING when ≥1 SOURCE candidate found

``match_results`` captures the full scoring breakdown for every candidate so
the front-end can show the buyer/operator a ranked comparison table.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from shared.domain.enums import AutonomyLevel, DealStatus, EscrowStatus, FreightStatus, QcStatus
from shared.domain.models import Party


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class MatchResult(BaseModel):
    """Composite Match Score computed by Match Engine for one seller candidate."""

    match_id: str
    supplier_id: str
    company_name: str = ""
    composite_score: float
    gatekeeper_passed: bool
    arbitrage_score: float
    proximity_score: float
    reliability_score: float
    decision: Literal["SOURCE", "DISCARD"]
    fob_price_usd: float | None = None
    supplier_to_port_km: float | None = None
    export_shipments_12m: int | None = None
    """Supplier's export shipments in the last 12 months (customs records via
    the supplier actor) — the reliability input the Match Engine lacked."""
    verification: Literal["verified", "unverified", "failed"] = "unverified"
    """The opportunity's scan-time check of this supplier (trade_min/validation.py).
    ``unverified`` = scored provisionally: GST / IEC are checked for real at quote
    intake (sourcing). Never read as "verified" unless the check said so."""
    verification_reasons: list[str] = Field(default_factory=list)


class BuyerRequirement(BaseModel):
    """What one buyer wants — read from their reply (LLM gateway, regex fallback)
    or entered by an operator. Every field is optional: buyers reveal it bit by
    bit, and the match board shows what is still missing."""

    grade: str | None = None
    """Grade / specification ("USP 99.7%", "refined", "ASTA 500 bold")."""
    packaging: str | None = None
    quantity_mt: float | None = None
    delivery_window: str | None = None
    """Free text as the buyer wrote it ("March", "within 6 weeks")."""
    delivery_by_days: int | None = None
    """Days from now the goods must arrive, when the window could be read as a number."""
    incoterm: str | None = None
    destination_port: str | None = None
    target_price_usd_per_mt: float | None = None
    target_price_incoterm: str | None = None
    target_price_basis: Literal["stated", "counter", "accepted_offer"] | None = None
    """How we know the price: they named it, countered our indicative offer, or accepted it."""
    certifications: list[str] = Field(default_factory=list)
    payment_terms: str | None = None
    notes: str | None = None
    source: str | None = None
    """``email`` (extracted from a reply) | ``operator``."""
    confidence: float | None = None
    updated_at: datetime | None = None


class IndicativeOffer(BaseModel):
    """A price range PortLess sent a buyer (SEND_INDICATIVE_OFFER gate) to learn
    what they will pay — their reaction is the price discovery."""

    offer_id: str
    """The supplier offer the range was built from."""
    price_low_usd_per_mt: float
    price_high_usd_per_mt: float
    incoterm: str = "CIF"
    destination_port: str | None = None
    quantity_mt: float | None = None
    sent_at: datetime | None = None
    response: Literal["accepted", "countered", "declined"] | None = None
    responded_at: datetime | None = None


class SupplierOffer(BaseModel):
    """What one supplier offers — from a quote (RFQ reply / portal) or an operator."""

    offer_id: str
    supplier_id: str
    company_name: str = ""
    email: str | None = None
    grade: str | None = None
    fob_price_usd_per_mt: float | None = None
    moq_mt: float | None = None
    capacity_mt_per_month: float | None = None
    lead_time_days: int | None = None
    port_of_loading: str | None = None
    certifications: list[str] = Field(default_factory=list)
    payment_terms: str | None = None
    validity_days: int | None = None
    gst_verified: bool | None = None
    iec_active: bool | None = None
    verification_failed: bool = False
    """GST inactive / IEC inactive / DGFT denied — never matched."""
    quote_id: str | None = None
    source: str | None = None
    """``quote`` | ``operator``."""
    origin_deal_id: str | None = None
    """The prospect the quote came in on (offers are matched across prospects of the same HS code)."""
    received_at: datetime | None = None


class BuyerCandidate(BaseModel):
    """One buyer the scan identified for this deal — the outreach funnel works
    through them in order until one is interested (see
    shared/orchestration/outreach.py)."""

    candidate_id: str
    legal_name: str
    country: str
    email: str | None = None
    website: str | None = None
    phone: str | None = None
    source: str | None = None
    fit_score: int | None = None
    pitch_angle: str | None = None
    quantity_mt: float | None = None
    validation_status: str | None = None
    """Scan-time check of this buyer (``verified`` / ``unverified``; ``failed`` ones are never candidates)."""
    status: Literal["pending", "contacted", "followed_up", "interested", "declined", "no_reply", "selected"] = "pending"
    contacted_at: datetime | None = None
    followed_up_at: datetime | None = None
    replied_at: datetime | None = None
    reply_preview: str | None = None
    email_id: str | None = None
    send_error: str | None = None
    """Why the last e-mail to this candidate did not go out (e.g. no mailbox configured)."""
    send_attempts: int = 0
    requirement: BuyerRequirement | None = None
    """What this buyer wants (grade, quantity, port, target price…) — the match board's buyer side."""
    indicative_offer: IndicativeOffer | None = None
    reply_address: str | None = None
    """The address the buyer actually replied from (becomes the buyer party's e-mail)."""


class StatusHistoryEntry(BaseModel):
    """One state-machine transition recorded at the instant it was committed."""

    status: DealStatus
    transitioned_at: datetime
    reason: str | None = None


# ---------------------------------------------------------------------------
# Core deal document (MongoDB + API response)
# ---------------------------------------------------------------------------

class DealRecord(BaseModel):
    """Full Deals-service document — owned by this service, stored in MongoDB.

    This is a richer superset of the thin ``shared.domain.models.Deal`` DTO:
      - ``market`` — destination country (ISO 3166 alpha-2) from Trade-Min
      - ``opportunity_id`` — back-reference to the originating Trade-Min opportunity
      - ``match_results`` — full ME score breakdown for every scored candidate
      - ``winning_supplier_id`` / ``winning_cms`` — best SOURCE decision
      - ``status_history`` — immutable audit trail of every status transition
    """

    # ── Identity ─────────────────────────────────────────────────────────
    deal_id: str
    operator_id: str
    org_id: str
    """Owning organization (see services/auth/organization.py) — stamped from
    the authenticated caller at creation, never client-supplied. Every list/
    get is scoped to this unless the caller is Portless platform staff."""
    hs_code: str
    market: str
    status: DealStatus = DealStatus.DRAFT
    autonomy_profile: AutonomyLevel = AutonomyLevel.L0

    # ── Trade-Min linkage ─────────────────────────────────────────────────
    opportunity_id: str | None = None

    # ── Counterparty organizations ────────────────────────────────────────
    buyer_org_id: str | None = None
    seller_org_id: str | None = None
    """Auto-provisioned when the negotiation is agreed (see
    agents/negotiation_agent/service.py::finalize_agreement →
    services/auth/service.py::find_or_create_organization_for_deal).
    A user assigned to either organization sees this deal in their portal —
    list/get scoping matches the caller's org against org_id (owner),
    buyer_org_id, and seller_org_id."""

    # ── Match Engine results ──────────────────────────────────────────────
    match_results: list[MatchResult] = Field(default_factory=list)
    winning_supplier_id: str | None = None
    winning_cms: float | None = None
    buyer_target_price_usd: float | None = None
    """Buyer-side unit price (USD/MT) from the buyer signals or the market benchmark."""
    buyer_price_source: str | None = None
    quantity_mt: float | None = None
    """Shipment quantity the buyer asked for (RFQ lead / negotiation); the
    pipeline default applies when None."""
    buyer_candidates: list[BuyerCandidate] = Field(default_factory=list)
    """Buyer outreach funnel: every buyer the scan identified, in contact order."""
    supplier_offers: list[SupplierOffer] = Field(default_factory=list)
    """Supplier side of the match board: every quote received on this prospect."""
    selected_pair: dict[str, Any] | None = None
    """The buyer × supplier pair confirmed at CONFIRM_MATCH (prospect → deal), with its scores."""

    # ── Financial ─────────────────────────────────────────────────────────
    margin_floor_pct: float | None = None
    est_margin_usd: float | None = None
    realized_margin_usd: float | None = None
    est_margin_pct: float | None = None
    realized_margin_pct: float | None = None

    # ── Parties ───────────────────────────────────────────────────────────
    parties: list[Party] = Field(default_factory=list)

    # ── Inspection (shared/orchestration/qc_status.py keeps these current) ─
    qc_status: QcStatus | None = None
    """Where the pre-shipment inspection stands (None before the PO)."""
    qc_status_detail: str | None = None
    """One line for the operator: provider, date, order number, reason."""
    qc_status_at: datetime | None = None

    # ── Escrow (shared/orchestration/escrow_status.py keeps these current) ─
    escrow_provider: str | None = None
    """``tazapay`` | ``escrow_com`` — the buyer's / operator's choice before the
    escrow opens, the provider actually holding the money after."""
    escrow_provider_source: str | None = None
    """Who chose it: ``buyer`` (their e-mail) / ``operator`` / ``pi`` / ``default``."""
    escrow_status: EscrowStatus | None = None
    """Where the buyer's money stands at the provider (None before the escrow opens)."""
    escrow_status_detail: str | None = None
    escrow_status_at: datetime | None = None

    # ── Inspection plan (shared/orchestration/inspection_plan.py) ──────────
    inspection_types: list[str] = Field(default_factory=list)
    """QC phases the buyer asked for: DUPRO / PSI / PEO (PSI always included). Empty → default."""
    inspection_types_source: str | None = None
    inspections: list[dict[str, Any]] = Field(default_factory=list)
    """One line per phase: {type, label, status, provider, inspection_date, result, order_number}."""

    # ── Freight (shared/orchestration/freight_status.py keeps these current) ─
    freight_status: FreightStatus | None = None
    freight_status_detail: str | None = None
    freight_status_at: datetime | None = None

    # ── Audit ─────────────────────────────────────────────────────────────
    status_history: list[StatusHistoryEntry] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# API request shapes
# ---------------------------------------------------------------------------

class CreateDealRequest(BaseModel):
    operator_id: str
    hs_code: str
    market: str
    autonomy_profile: AutonomyLevel = AutonomyLevel.L0
    margin_floor_pct: float | None = None
    opportunity_id: str | None = None
    """When set, the service fetches the Trade-Min MatchEngineSeed and scores
    all candidate sellers via Match Engine before persisting the deal."""
    parties: list[Party] = Field(default_factory=list)


class UpdateStatusRequest(BaseModel):
    target_status: DealStatus
    reason: str | None = None


# ---------------------------------------------------------------------------
# Aliases for callers that import the old thin names
# ---------------------------------------------------------------------------

DealResponse = DealRecord


# ---------------------------------------------------------------------------
# Patch-request for financial fields (sourcing / negotiation phases)
# ---------------------------------------------------------------------------

class UpdateFinancialsRequest(BaseModel):
    est_margin_usd: float | None = None
    realized_margin_usd: float | None = None
    est_margin_pct: float | None = None
    realized_margin_pct: float | None = None
    buyer_target_price_usd: float | None = None
    winning_supplier_id: str | None = None
    winning_cms: float | None = None
    extra: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Party contact / screening (enrichment + operator corrections)
# ---------------------------------------------------------------------------

class UpdatePartyRequest(BaseModel):
    """Operator correction of a counterparty's identity / contact (PATCH
    /deals/{id}/parties/{role}). Only provided fields change; an operator-set
    contact is marked ``contact_source="operator"`` and is never overwritten by
    a later actor run."""

    legal_name: str | None = None
    country: str | None = None
    email: str | None = None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    gstin: str | None = None
    iec: str | None = None
    vat_number: str | None = None
    verification_status: str | None = None


class PartyEnrichment(BaseModel):
    legal_name: str | None = None
    country: str | None = None
    email: str | None = None
    email_status: str | None = None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    vat_number: str | None = None
    vat_valid: bool | None = None
    lei: str | None = None
    contact_source: str | None = None
    contact_confidence: float | None = None
    sanctions_status: str | None = None
    sanctions_reason: str | None = None
    screening: dict[str, Any] | None = None


class EnrichmentResult(BaseModel):
    """Outcome of ``POST /deals/{id}/enrich`` (Apify contact finder + sanctions
    screening + e-mail deliverability on the deal's parties)."""

    deal_id: str
    buyer: PartyEnrichment | None = None
    supplier: PartyEnrichment | None = None
    actors_used: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocked: bool = False
    deal: DealRecord
