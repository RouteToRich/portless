"""Request/response models for the Trade-Min service (FR-1, LLD 4.5, §10).

EnrichedOpportunity is the canonical output of the 5-phase pipeline.

Next-phase hand-off
-------------------
When the Deals service or DealMaker agent creates a deal from an opportunity,
it calls ``GET /trade-min/opportunities/{id}/match-seed`` which returns a
``MatchEngineSeed``.  That object pre-populates all fields required by the
Match Engine's ``ScoreRequest`` except ``deal_id`` and ``verification`` (GST /
IEC / DGFT), which are resolved later in the Sourcing phase.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Primitive type aliases
# ---------------------------------------------------------------------------

OpportunityStatus = Literal["ACTIVE", "MARGINAL", "EXPIRED", "CONVERTED"]
DataSource = Literal[
    "comtrade", "volza", "macmap", "trademo", "importgenius",
    "alibaba_rfq", "gdelt_disruption", "eu_ted",
    "indiamart", "tradeindia", "simulated",
]
SignalType = Literal["historical", "manifest", "rfq", "tender", "disruption"]


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    hs_codes: list[str] | None = None
    """HS codes to scan; uses configured defaults when omitted."""

    target_markets: list[str] | None = None
    """ISO-3166 alpha-2 destination markets; uses configured defaults when omitted."""

    sources: list[DataSource] | None = None
    """Deprecated — all available connectors are always used.  Kept for BC."""

    min_arbitrage_rank: float = Field(default=0.0, ge=0.0, le=100.0)
    """Filter threshold — opportunities below this rank are excluded."""

    force_refresh: bool = False
    """When True, skip any cached results and run a fresh scan."""


# ---------------------------------------------------------------------------
# Phase sub-models
# ---------------------------------------------------------------------------

class DemandSignal(BaseModel):
    """One connector's weighted contribution to the demand index."""

    source: DataSource
    signal_type: SignalType
    urgency_tier: int = Field(ge=1, le=4)
    normalized_value: float = Field(ge=0.0, le=100.0)
    weight: float = Field(ge=0.0, le=1.0)
    """0.0 for live signals (additive) vs 0.0–0.40 for Phase-1 connectors."""
    raw_summary: str
    fetched_at: datetime


class BuyerSignal(BaseModel):
    """A named overseas buyer identified from manifests, RFQs, or tenders."""

    buyer_name: str
    buyer_country: str
    company_id: str | None = None
    last_import_date: str | None = None
    """ISO-8601 date of the last recorded import from Volza / ImportGenius."""
    annual_volume_mt: float | None = None
    risk_score: float | None = None
    """Trademo risk score 0–100 (lower is safer)."""
    signal_type: SignalType
    source: DataSource
    urgency_tier: int = Field(ge=1, le=4)
    estimated_order_value_usd: float | None = None
    """Total shipment/tender value in USD (not unit price)."""
    unit_price_usd: float | None = None
    """Unit CIF price per kg/unit from Volza manifests.
    Used by MatchEngineSeed.buyer_target_price_usd."""
    contact_url: str | None = None


class TariffData(BaseModel):
    """MacMap tariff profile for an HS-code × country-pair (Phase 3)."""

    hs_code: str
    origin_country: str = "IN"
    destination_country: str
    applied_duty_pct: float
    """MFN / preferential applied tariff rate (%)."""
    gst_vat_pct: float | None = None
    """Destination country VAT / GST on food imports (%)."""
    ntb_flags: list[str] = Field(default_factory=list)
    """Non-tariff barrier codes from MacMap NTM result."""
    rules_of_origin: str | None = None
    total_landed_cost_add_pct: float
    """applied_duty_pct + gst_vat_pct — the net tariff overhead to subtract
    from gross price gap when computing net_price_gap_pct."""
    source: DataSource = "macmap"
    fetched_at: datetime


class SellerCandidate(BaseModel):
    """A verified Indian manufacturer/exporter discovered in Phase 4."""

    candidate_id: str
    company_name: str
    location_city: str
    location_state: str
    hs_code: str
    estimated_fob_price_usd: float | None = None
    """Average of min_price + max_price from IndiaMART / TradeIndia listing."""
    annual_capacity_mt: float | None = None
    source: DataSource
    listing_url: str | None = None
    contact_id: str | None = None
    supplier_to_port_km: float | None = None
    """Static lookup: Indian state → km to nearest major seaport.
    Pre-seeded into ScoreRequest.supplier_to_port_km for the Match Engine."""


# ---------------------------------------------------------------------------
# MatchEngineSeed — the next-phase bridge object
# ---------------------------------------------------------------------------

class MatchEngineSeed(BaseModel):
    """Pre-computed values from an EnrichedOpportunity for Match Engine seeding.

    Returned by ``GET /trade-min/opportunities/{id}/match-seed``.

    The Deals service or DealMaker agent populates a ``ScoreRequest`` using:
      buyer_target_price_usd  → ScoreRequest.buyer_target_price
      top_sellers[i].fob_price_usd → ScoreRequest.supplier_quote_price
      top_sellers[i].supplier_to_port_km → ScoreRequest.supplier_to_port_km

    Fields NOT populated here (must be resolved by Sourcing phase):
      deal_id        — set when the deal is created
      verification   — GSTN / IEC / DGFT checks run by the Sourcing agent
      business_age_years, udyam_registered, annual_shipment_count
                     — resolved via MCA / Udyam connectors
    """

    opportunity_id: str
    hs_code: str
    market: str

    # Buyer-side inputs for Match Engine arbitrage score
    buyer_target_price_usd: float | None
    """Average CIF unit price (USD/kg) from Volza manifests.
    None when no Volza data available — caller must estimate or skip scoring."""

    # Seller candidates ranked by estimated FOB price (cheapest first)
    top_sellers: list[dict[str, Any]] = Field(default_factory=list)
    """Each entry: {supplier_id, company_name, location, fob_price_usd,
    supplier_to_port_km, annual_capacity_mt, source, listing_url, contact_id}"""

    # Opportunity-level metadata
    net_price_gap_pct: float
    urgency_tier: int = Field(ge=1, le=4)
    confidence_score: float = Field(ge=0.0, le=100.0)
    tariff_data: TariffData | None = None

    # Counts for the caller to decide how many parallel match requests to fire
    buyer_signals_count: int = 0
    seller_candidates_count: int = 0


# ---------------------------------------------------------------------------
# Main output — EnrichedOpportunity
# ---------------------------------------------------------------------------

class EnrichedOpportunity(BaseModel):
    """Full opportunity enriched across all 5 pipeline phases.

    Backward-compatible superset of the old ``OpportunityResponse``.

    Match Engine field pre-seeding (§10.5)
    ----------------------------------------
    buyer_signals[].unit_price_usd         → ScoreRequest.buyer_target_price
    seller_candidates[].estimated_fob_price_usd → ScoreRequest.supplier_quote_price
    seller_candidates[].supplier_to_port_km → ScoreRequest.supplier_to_port_km

    Call ``GET /trade-min/opportunities/{id}/match-seed`` to get a structured
    ``MatchEngineSeed`` ready for ScoreRequest construction.
    """

    # ── Identity ──────────────────────────────────────────────────────────
    opportunity_id: str
    hs_code: str
    market: str
    currency: str = "USD"
    status: OpportunityStatus = "ACTIVE"
    scanned_at: datetime
    scan_version: int = 1

    # ── Phase 1 — historical demand ───────────────────────────────────────
    demand_signals: list[DemandSignal] = Field(default_factory=list)
    demand_index: float = Field(ge=0.0, le=100.0)
    price_gap_pct: float
    """Gross price gap before tariff deduction."""
    source: DataSource

    # ── Phase 2 — live demand signals ─────────────────────────────────────
    buyer_signals: list[BuyerSignal] = Field(default_factory=list)
    active_rfq_count: int = 0
    active_tender_count: int = 0
    urgency_tier: int = Field(default=1, ge=1, le=4)

    # ── Phase 3 — tariff gate ─────────────────────────────────────────────
    tariff_data: TariffData | None = None
    net_price_gap_pct: float
    """Gross price gap minus total_landed_cost_add_pct.  This is the real
    margin the exporter can extract — the metric the Match Engine ranks on."""
    tariff_viable: bool = True

    # ── Phase 4 — seller discovery ────────────────────────────────────────
    seller_candidates: list[SellerCandidate] = Field(default_factory=list)

    # ── Phase 5 — composite scoring ───────────────────────────────────────
    arbitrage_rank: float
    confidence_score: float = Field(ge=0.0, le=100.0)
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Legacy alias — keeps existing router / client code compiling during migration
# ---------------------------------------------------------------------------

OpportunityResponse = EnrichedOpportunity
