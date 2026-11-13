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

from .pair_match import PairMatch

# ---------------------------------------------------------------------------
# Primitive type aliases
# ---------------------------------------------------------------------------

OpportunityStatus = Literal["ACTIVE", "MARGINAL", "EXPIRED", "CONVERTED"]
DataSource = Literal[
    # Free / active sources
    "comtrade", "census", "wits_tariff",
    "alibaba_rfq", "gdelt_disruption", "eu_ted",
    "indiamart", "tradeindia",
    # PortLess-owned Apify actors (shared/connectors/apify.py)
    "apify_us_importers", "apify_alibaba_rfq", "apify_export_markets",
    "apify_india_suppliers", "apify_company_search",
    # Legacy / paid sources (retired from the default pipeline; kept so older
    # persisted opportunities still validate)
    "volza", "macmap", "trademo", "importgenius",
]
# web_search: a company found by searching the web for "<product> importers, <CC>"
# (apify_company_search) — a named buyer with a checked contact, no shipment record.
SignalType = Literal["historical", "manifest", "rfq", "tender", "disruption", "web_search"]


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

    worldwide_search: bool = True
    product_names: dict[str, str] | None = None
    """HS code → the product name to search for in THIS scan ("basmati rice"), chosen by the
    operator in the console. Wins over the catalogue and the built-in keyword list; without a
    product name a code is not web-searched (a bare number finds junk)."""
    """Also search importers and manufacturers / exporters of each product in
    every country (one search pair per product, kept in the buyer / supplier
    directory only). The console's "Worldwide" toggle; False → only the scanned
    markets' buyers and India's suppliers."""


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


ValidationStatus = Literal["verified", "unverified", "failed"]


class CounterpartyValidation(BaseModel):
    """Is this buyer / supplier a real company PortLess may contact and trade with?

    Set at scan time by ``services/trade_min/validation.py`` for the top
    counterparties of an opportunity, and kept on the directory record.

    - ``verified``   — sanctions screening ``clear``, a deliverable (or catch-all)
      e-mail, and no registry answered "inactive / denied".
    - ``failed``     — sanctions ``block``, GST / IEC inactive, or on the DGFT
      denied list: never shown by default, never contacted, never a deal party.
    - ``unverified`` — anything not checked yet or needing a person (sanctions
      ``review``, undeliverable e-mail, no Apify token, outside the top N).

    The GST / IEC registries are checked when the number is known and the
    registry answers; a registry that is not configured is a reason, not a
    failure — the supplier is verified again at quote intake (sourcing).
    """

    status: ValidationStatus = "unverified"
    reasons: list[str] = Field(default_factory=list)
    email: str | None = None
    """``ok`` / ``risky`` (catch-all) / ``invalid`` / ``unverified`` / ``missing``."""
    sanctions: str = "unchecked"
    """``clear`` / ``review`` / ``block`` / ``unchecked``."""
    sanctions_reason: str | None = None
    evidence_url: str | None = None
    gst: str | None = None
    """Indian suppliers: ``active`` / ``inactive`` / ``unchecked`` / ``missing``."""
    iec: str | None = None
    dgft_denied: str | None = None
    """``clear`` / ``denied`` / ``unchecked`` (the DGFT list is a stub today → unchecked)."""
    checked_at: datetime | None = None


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
    """Buyer-side unit price. Legacy manifest sources left the unit implicit;
    the Apify actors always set ``price_unit`` (``USD/MT``) so the Match Engine
    corridor is unit-consistent. Used by MatchEngineSeed.buyer_target_price_usd."""
    price_unit: str | None = None
    contact_url: str | None = None
    # ── Contact + fit (Apify buyer actors) ───────────────────────────────
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    fit_score: int | None = None
    """Actor's 0–100 fit score (AI tier) or rule score."""
    fit_reason: str | None = None
    pitch_angle: str | None = None
    """Opening angle for the first outreach e-mail, from the row's own facts."""
    quantity: float | None = None
    quantity_unit: str | None = None
    shipments_12m: int | None = None
    hs_codes: list[str] = Field(default_factory=list)
    email_status: str | None = None
    """What the source said about ``email`` (``ok`` / ``risky`` / ``invalid`` / ``unverified``)."""
    validation: CounterpartyValidation | None = None
    """Scan-time check; None = not checked (outside the top N or validation off)."""


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
    # ── Contact + credentials (Apify india-supplier actor) ────────────────
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    gstin: str | None = None
    iec: str | None = None
    udyam: str | None = None
    export_shipments_12m: int | None = None
    """Export shipments in the last 12 months (customs records) — reliability input."""
    fit_score: int | None = None
    country: str | None = "IN"
    """ISO-2 country of the supplier — India for the corridor's supplier search;
    the worldwide search records the company's own country (None when unknown)."""
    price_unit: str | None = "USD/MT"
    """``estimated_fob_price_usd`` is normalised to USD per MT (kg prices × 1000)."""
    email_status: str | None = None
    validation: CounterpartyValidation | None = None
    """Scan-time check; None = not checked (outside the top N or validation off)."""
    fob_estimate_usd_per_mt: float | None = None
    """No price from the supplier yet: the price book's estimate (pre-deal rescore). Pair
    matching uses it as an *estimate* (lower price confidence), never as a quote."""
    fob_estimate_basis: str | None = None
    address: str | None = None
    """Facility / office address when the contact finder found one (pre-deal rescore)."""


CounterpartyRole = Literal["buyer", "supplier"]


class CounterpartyRecord(BaseModel):
    """A buyer or supplier the scanner found — kept whether or not the scan
    produced an opportunity or a deal (``GET /trade-min/buyers`` / ``/suppliers``).
    One record per company (website domain, else name), merged across scans."""

    id: str
    """``<role>:<domain or name>``."""
    role: CounterpartyRole
    name: str
    country: str | None = None
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    contact_url: str | None = None
    source: str
    """Connector that found it (e.g. ``apify_company_search``)."""
    sources: list[str] = Field(default_factory=list)
    fit_score: int | None = None
    fit_reason: str | None = None
    gstin: str | None = None
    iec: str | None = None
    estimated_fob_price_usd: float | None = None
    """Suppliers: USD/MT when the source states a price."""
    hs_codes: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    """Buyers: their market. Suppliers: the markets they were scanned for."""
    opportunity_ids: list[str] = Field(default_factory=list)
    last_outcome: str = ""
    """What the latest scan that found it produced: ``opportunity <id>`` or why there was none."""
    times_seen: int = 1
    first_seen_at: datetime
    last_seen_at: datetime
    validation: CounterpartyValidation | None = None
    """Latest scan-time check (kept across scans until a newer one replaces it)."""


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
    """Buyer-side unit price (USD/MT): the mean of the buyer signals' real
    prices, else the market's Comtrade unit value (``price_benchmark_usd_per_mt``
    from the export-market actor). None when no real price exists — the
    caller must estimate (loudly) or skip scoring."""
    buyer_price_source: str | None = None
    """Where ``buyer_target_price_usd`` came from (``buyer_signals`` /
    ``apify_export_markets`` / None)."""
    top_buyers: list[dict[str, Any]] = Field(default_factory=list)
    """Top buyer signals (name, country, source, contact, fit) — seeds the deal's
    buyer party and the enrichment step."""

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
    price_benchmark_usd_per_mt: float | None = None
    """What the destination market actually pays per MT (UN Comtrade unit value
    via the export-market actor) — the real buyer price anchor, replacing the
    1.25× FOB heuristic when no named buyer carries a price."""
    price_benchmark_source: str | None = None

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

    # ── Buyer × seller pair matching (pair_match.py) ──────────────────────
    pair_matches: list[PairMatch] = Field(default_factory=list)
    """Scored buyer × seller pairs, one-to-one recommended pairs first."""
    best_match_score: float | None = None

    # ── Scan-time counterparty validation (validation.py) ─────────────────
    counterparty_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    """``{"buyers": {"verified": n, "unverified": n, "failed": n}, "suppliers": {...}}``
    over the whole opportunity — the radar's "N hidden" hint when it shows only
    verified counterparties."""
    counterparty_filter: Literal["verified", "all"] = "all"
    """Which counterparties ``buyer_signals`` / ``seller_candidates`` hold in this response."""

    # ── Pre-deal rescore (rescore.py) ─────────────────────────────────────
    rescored_at: datetime | None = None
    rescore: dict[str, Any] | None = None
    """The latest rescore: pairs before → after, what was filled, what still needs the deal."""


# ---------------------------------------------------------------------------
# Legacy alias — keeps existing router / client code compiling during migration
# ---------------------------------------------------------------------------

OpportunityResponse = EnrichedOpportunity
