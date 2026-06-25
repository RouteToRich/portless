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

from shared.domain.enums import AutonomyLevel, DealStatus
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
    hs_code: str
    market: str
    status: DealStatus = DealStatus.DRAFT
    autonomy_profile: AutonomyLevel = AutonomyLevel.L0

    # ── Trade-Min linkage ─────────────────────────────────────────────────
    opportunity_id: str | None = None

    # ── Match Engine results ──────────────────────────────────────────────
    match_results: list[MatchResult] = Field(default_factory=list)
    winning_supplier_id: str | None = None
    winning_cms: float | None = None
    buyer_target_price_usd: float | None = None
    """Average CIF unit price from Volza manifests (via MatchEngineSeed)."""

    # ── Financial ─────────────────────────────────────────────────────────
    margin_floor_pct: float | None = None
    est_margin_usd: float | None = None
    realized_margin_usd: float | None = None

    # ── Parties ───────────────────────────────────────────────────────────
    parties: list[Party] = Field(default_factory=list)

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
    buyer_target_price_usd: float | None = None
    winning_supplier_id: str | None = None
    winning_cms: float | None = None
    extra: dict[str, Any] | None = None
