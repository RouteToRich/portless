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
    name_source: str | None = None
    """``domain`` when the name could only be read from the website address."""
    flags: list[str] = Field(default_factory=list)
    """Data-quality warnings (quality.py), e.g. ``role_suspect`` (looks like an
    Indian exporter, not an importer in the market)."""


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
    """The import duty subtracted from the gross price gap (applied_duty_pct).
    VAT / GST is shown in ``gst_vat_pct`` but NOT subtracted: a business importer
    reclaims it, so it is not a cost of the trade."""
    verified: bool = True
    """False when no tariff source answered and the duty is an unconfirmed 0 %."""
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
    fit_reason: str | None = None
    """What the supplier's own site says it does (search summary / page title)."""
    name_source: str | None = None
    """``domain`` when the name could only be read from the website address."""
    flags: list[str] = Field(default_factory=list)
    """Data-quality warnings (quality.py), e.g. ``product_mismatch``."""


CounterpartyRole = Literal["buyer", "supplier"]


class CompanyNote(BaseModel):
    """A staff note on a company (a call, a meeting), optionally with a follow-up date."""
    note_id: str
    text: str
    by: str
    at: datetime
    remind_at: datetime | None = None
    done_at: datetime | None = None
    done_by: str | None = None


class ScreeningReview(BaseModel):
    """A staff decision on a counterparty whose sanctions screening returned ``review``
    (a possible list match, or a list that was unavailable)."""
    decision: Literal["clear", "fail"]
    reason: str
    by: str
    at: datetime
    previous_sanctions: str
    previous_reason: str | None = None
    evidence_url: str | None = None
    """The screening certificate the person looked at."""


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
    """Every opportunity a scan ever found it in — history; many have since expired,
    become a deal or been replaced by a newer scan."""
    open_opportunity_ids: list[str] | None = None
    """Those of ``opportunity_ids`` the Opportunity Radar lists now. Filled when the
    directory is read, never stored; ``None`` = could not be worked out."""
    last_outcome: str = ""
    """What the latest scan that found it produced: ``opportunity <id>`` or why there was none."""
    times_seen: int = 1
    first_seen_at: datetime
    last_seen_at: datetime
    validation: CounterpartyValidation | None = None
    """Latest scan-time check (kept across scans until a newer one replaces it)."""
    flags: list[str] = Field(default_factory=list)
    """Data-quality flags from the scans (quality.py): role_suspect, product_mismatch, name_from_domain."""
    do_not_contact: bool = False
    """Never written to again: the company replied "not interested" (the introduction
    promises it) or staff set it. Survives every scan; outreach skips it in every prospect."""
    dnc_reason: str | None = None
    dnc_at: datetime | None = None
    merged_from: list[str] = Field(default_factory=list)
    """Directory ids folded into this record by a staff merge (dedupe.py)."""
    aliases: list[str] = Field(default_factory=list)
    """Other names the company was found under."""
    merged_at: datetime | None = None
    merged_by: str | None = None
    notes: list[CompanyNote] = Field(default_factory=list)
    """Staff notes and follow-up reminders (company_notes.py), newest last."""
    contacts_refreshed_at: datetime | None = None
    """Last contact-finder refresh when the company entered a prospect (contact_refresh.py)."""
    screening_reviews: list[ScreeningReview] = Field(default_factory=list)
    """Staff decisions on a sanctions ``review`` result (screening_review.py), oldest first —
    the audit trail: who cleared or failed it, why, and what the screening had said."""
    # ── computed on listing (relationships.py), not stored ────────────────
    relationship: dict[str, Any] | None = None
    """Where PortLess stands with the company across every prospect and deal: stage,
    prospects, last contact / reply, quotes, target price, deals won."""
    score: float | None = None
    """0–100: who to work with first (check, reachability, engagement, track record, price)."""
    score_basis: list[str] = Field(default_factory=list)


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
    profit: dict[str, Any] | None = None
    """The radar's profit block, carried into the deal as ``DealRecord.radar_estimate``."""
    lot_mt: float | None = None
    """The radar's lot — the deal's default quantity when no buyer named one."""
    flags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Main output — EnrichedOpportunity
# ---------------------------------------------------------------------------

class OpportunityProfit(BaseModel):
    """What PortLess can expect to earn on the corridor (profit.py).

    margin/MT = buyer price - landed cost (supplier FOB + freight + inspection +
    insurance + escrow fee); expected profit = margin/MT x lot x chance of closing.
    Every input names its basis so an estimate is never mistaken for a quote."""

    buyer_price_usd_per_mt: float | None = None
    buyer_price_basis: str | None = None
    """buyer_quote | price_book | india_to_market | market_average"""
    supplier_price_usd_per_mt: float | None = None
    supplier_price_basis: str | None = None
    """supplier_quote | price_book | mandi | india_export_average"""
    supplier_price_note: str | None = None
    landed_cost_usd_per_mt: float | None = None
    freight_usd_per_mt: float | None = None
    margin_usd_per_mt: float | None = None
    margin_pct: float | None = None
    container_mt: float = 20.0
    margin_per_container_usd: float | None = None
    lot_mt: float = 20.0
    lot_basis: str = "one container"
    p_close: float | None = None
    """Chance the corridor turns into a signed deal (0-1)."""
    p_close_basis: list[str] = Field(default_factory=list)
    expected_profit_usd: float | None = None
    estimated: bool = True
    """True while either price is an estimate (no quote on both sides)."""
    meets_floor: bool | None = None
    margin_floor_pct: float | None = None
    version: int = 0
    """profit.PROFIT_VERSION it was computed with; the list recomputes older ones."""
    # ── range, income, freight, yearly potential (to-do 16.3) ─────────────
    expected_profit_low_usd: float | None = None
    expected_profit_high_usd: float | None = None
    range_basis: str | None = None
    incentive_usd_per_mt: float | None = None
    """RoDTEP + duty drawback income per MT (export_incentives table), when confirmed."""
    incentive_basis: str | None = None
    freight_basis: str | None = None
    """``lane_quote`` (a forwarder quote for this lane, with its date) or ``region_estimate``."""
    container_type: str = "dry"
    supplier_price_low_usd_per_mt: float | None = None
    supplier_price_high_usd_per_mt: float | None = None
    usd_inr: float | None = None
    usd_inr_source: str | None = None
    mandi_trend: dict[str, Any] | None = None
    """From PortLess's own mandi price history: direction, change %, days of history."""
    annual_potential_usd: float | None = None
    annual_basis: str | None = None
    correction_factor: float | None = None
    """Realised ÷ estimated margin on past deals of this product (accuracy.py), applied to the margin."""
    correction_basis: str | None = None
    missing: list[dict[str, str]] = Field(default_factory=list)
    """What the estimate still lacks and where to get it: ``{what, how, action}``."""


class ScanCorridorResult(BaseModel):
    hs_code: str
    market: str
    result: Literal["opportunity", "dropped", "error"]
    reason: str
    opportunity_id: str | None = None


class ScanReport(BaseModel):
    """What one scan did per product x market — including why corridors were dropped."""

    scan_id: str
    started_at: datetime
    finished_at: datetime | None = None
    corridors: list[ScanCorridorResult] = Field(default_factory=list)
    opportunities: int = 0
    dropped: int = 0
    errors: int = 0


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
    market_avg_usd_per_mt: float | None = None
    """What the market pays per MT from ALL origins (Comtrade unit value)."""
    india_to_market_usd_per_mt: float | None = None
    """What the market pays for India's goods per MT — the like-for-like buyer anchor."""
    india_world_fob_usd_per_mt: float | None = None
    """India's average export price to the whole world (FOB, every grade)."""
    import_value_usd: float | None = None
    import_growth_pct: float | None = None
    india_share_pct: float | None = None
    competitors: list[dict[str, Any]] = Field(default_factory=list)
    """Main origins supplying the market: country, share %, unit value USD/MT."""
    india_price_advantage_pct: float | None = None
    """How much cheaper India sells than the market's leading other origin (+ = cheaper)."""
    data_period: str | None = None
    """The year (or month) of the Comtrade figures behind the prices — old data is not today's price."""
    market_data_flags: list[str] = Field(default_factory=list)
    """The export-market actor's own flags: stale_period, you_lead, fast_growing …"""
    freight_pct: float | None = None
    """Estimated sea freight as % of the buyer price, subtracted in the net gap."""
    gap_suspect: bool = False
    """Gross gap above the plausible limit: most likely a different grade / origin
    mix, not a real arbitrage — check the price before trusting it."""
    gap_note: str | None = None

    # ── Phase 2 — live demand signals ─────────────────────────────────────
    buyer_signals: list[BuyerSignal] = Field(default_factory=list)
    active_rfq_count: int = 0
    active_tender_count: int = 0
    urgency_tier: int = Field(default=1, ge=1, le=4)

    # ── Phase 3 — tariff gate ─────────────────────────────────────────────
    tariff_data: TariffData | None = None
    net_price_gap_pct: float
    """Gross price gap minus import duty minus estimated freight (VAT is not a
    cost to a business importer and is not subtracted)."""
    tariff_viable: bool = True

    # ── Phase 4 — seller discovery ────────────────────────────────────────
    seller_candidates: list[SellerCandidate] = Field(default_factory=list)

    # ── Phase 5 — composite scoring ───────────────────────────────────────
    arbitrage_rank: float
    confidence_score: float = Field(ge=0.0, le=100.0)
    confidence_basis: list[str] = Field(default_factory=list)
    """One line per part of the confidence score (sources, live demand, tariff, suppliers, verified counterparties)."""
    expires_at: datetime | None = None

    # ── Buyer × seller pair matching (pair_match.py) ──────────────────────
    pair_matches: list[PairMatch] = Field(default_factory=list)
    """Scored buyer × seller pairs, one-to-one recommended pairs first."""
    best_match_score: float | None = None
    best_match_verified: bool | None = None
    """Whether ``best_match_score`` comes from a pair whose buyer and supplier are both verified."""

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

    # ── Expected profit (profit.py) ───────────────────────────────────────
    profit: OpportunityProfit | None = None
    flags: list[str] = Field(default_factory=list)
    """Opportunity-level warnings: price_check_needed, tariff_unverified, no_supplier_price …"""
    corridor_deals: list[dict[str, Any]] = Field(default_factory=list)
    """Deals already running for this product x market (filled when listed)."""
    export_policy: dict[str, Any] | None = None
    """India's export policy for the HS code and the destination check (compliance.py):
    ``{status, severity, message, destination_severity, destination_message}``."""
    market_check: dict[str, Any] | None = None
    """News verdict for the product × market (market_check.py): ``{verdict, summary, checked_at}``."""
    import_requirements: list[str] = Field(default_factory=list)
    """What the destination needs before the goods arrive (catalogue product line)."""
    scan_cost: dict[str, Any] | None = None
    """Apify runs and USD this opportunity's scan / rescore cost: ``{runs, usd}``."""
    readiness: list[dict[str, Any]] = Field(default_factory=list)
    """The ready-to-start checklist (readiness.py): ``{key, label, status ok|warn|fail, detail}``."""


# ---------------------------------------------------------------------------
# Legacy alias — keeps existing router / client code compiling during migration
# ---------------------------------------------------------------------------

OpportunityResponse = EnrichedOpportunity
