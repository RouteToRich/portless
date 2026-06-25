"""Request/response models for the Negotiation Agent (§4.3.1).

Three-agent LangGraph pipeline
-------------------------------
Orchestrator → routes to → BuyerFacingAgent | SupplierFacingAgent → loops back.

Convergence rule (deterministic — never LLM)
--------------------------------------------
  |ask - bid| <= concession_step   AND   net_margin >= margin_floor_pct
→ emit ``negotiation.agreed``

HITL gates
----------
  • First outbound message (configurable)
  • Margin within 2 % of floor
  • Max rounds exhausted without convergence
  • Operator override via UpdateRoundRequest
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums / literals
# ---------------------------------------------------------------------------

NegotiationStatus = Literal[
    "STARTED", "ROUND", "AGREED", "FAILED", "HITL_PENDING", "ESCALATED"
]

AgentRouting = Literal[
    "CALL_BUYER_AGENT",
    "CALL_SUPPLIER_AGENT",
    "HITL_APPROVAL",
    "NEGOTIATION_AGREED",
    "NEGOTIATION_FAILED",
]

Party = Literal["buyer", "supplier"]

Channel = Literal["email", "portal", "whatsapp", "telegram"]


# ---------------------------------------------------------------------------
# Cost envelope — from Match Engine
# ---------------------------------------------------------------------------

class CostEnvelope(BaseModel):
    """Non-negotiable per-unit costs passed from the Match Engine."""

    travel_cost: Decimal = Field(default=Decimal("0"), description="Inland transport cost USD/unit")
    qc_cost: Decimal = Field(default=Decimal("0"), description="SGS / BV inspection cost USD/unit")
    doc_cost: Decimal = Field(default=Decimal("0"), description="Documentation & compliance USD/unit")

    def total(self) -> float:
        return float(self.travel_cost + self.qc_cost + self.doc_cost)


# ---------------------------------------------------------------------------
# Policy — controls negotiation behavior
# ---------------------------------------------------------------------------

class NegotiationPolicy(BaseModel):
    target_margin_pct: float = Field(default=12.0, ge=0.0, le=100.0)
    margin_floor_pct: float = Field(default=8.0, ge=0.0, le=100.0)
    max_rounds: int = Field(default=5, ge=1, le=20)
    concession_step: float = Field(default=0.10, gt=0)
    """Maximum USD/unit price gap at which convergence is declared."""
    deadline: str | None = None
    rag_context: str | None = None
    cost_envelope: CostEnvelope | None = None


# ---------------------------------------------------------------------------
# Rich negotiated terms (parsed from agent LLM output)
# ---------------------------------------------------------------------------

class NegotiatedTerms(BaseModel):
    unit_price: float = Field(description="Agreed or countered price (USD or INR)")
    currency: str = Field(default="USD")
    moq: float = Field(default=0.0, description="Minimum order quantity in MT")
    lead_time_days: int = Field(default=0)
    payment_terms: str = Field(default="")
    logistics_incoterm: str = Field(default="FOB")
    quality_specifications: str = Field(default="")
    packaging_standard: str = Field(default="")


# ---------------------------------------------------------------------------
# Orchestrator JSON output (parsed from LLM)
# ---------------------------------------------------------------------------

class OrchestratorDecision(BaseModel):
    trade_match_id: str = ""
    buyer_round: int = 0
    supplier_round: int = 0
    current_estimated_margin_pct: float
    action_routing: AgentRouting
    reasoning: str = ""
    margin_at_current: float
    hitl_required: bool = False


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class StartNegotiationRequest(BaseModel):
    deal_id: str
    trade_match_id: str
    buyer_id: str
    seller_id: str
    product_code: str
    """HS code — used to load RAG product context."""
    buyer_country_code: str
    buyer_target_price: float = Field(gt=0)
    supplier_quote_price: float = Field(gt=0)
    date_of_expectation: str | None = None
    date_of_packaging: str | None = None
    composite_match_score: float | None = None
    cost_envelope: CostEnvelope | None = None
    policy: NegotiationPolicy = Field(default_factory=NegotiationPolicy)
    batna: str | None = None
    """Best Alternative To Negotiated Agreement — operator's fallback strategy."""


class RoundRequest(BaseModel):
    counter_price: float = Field(gt=0)
    party: Party
    channel: Channel = "portal"
    inbound_message: str | None = None
    """Full text of the inbound reply (email body / portal message)."""
    attachment_refs: list[str] | None = None
    """References to parsed attachment content (PDF/Excel quotes)."""
    operator_override_price: float | None = None
    """HITL override: operator manually sets the accepted price."""


# ---------------------------------------------------------------------------
# Persisted models
# ---------------------------------------------------------------------------

class NegotiationSession(BaseModel):
    """Full negotiation session document stored in MongoDB."""

    negotiation_id: str
    deal_id: str
    trade_match_id: str
    buyer_id: str
    seller_id: str
    product_code: str
    buyer_country_code: str
    buyer_target_price: float
    supplier_quote_price: float
    composite_match_score: float | None = None
    policy: NegotiationPolicy
    cost_envelope: CostEnvelope | None = None
    batna: str | None = None
    rag_context: str = ""

    status: NegotiationStatus = "STARTED"
    round_no: int = 0
    current_bid: float | None = None
    """Buyer's most recent offer (USD)."""
    current_ask: float | None = None
    """Supplier's most recent ask (USD-equivalent)."""
    agreed_price: float | None = None
    realized_cms: float | None = None
    """Final CMS recomputed by Match Engine after agreement."""
    margin_at_current: float | None = None
    hitl_required: bool = False

    created_at: datetime
    updated_at: datetime


class NegotiationRound(BaseModel):
    """One round's complete record: inbound → orchestrator → agent → outbound."""

    round_id: str
    negotiation_id: str
    deal_id: str
    round_no: int
    party: str
    """The party whose counter-offer triggered this round: 'buyer' | 'supplier' | 'init'."""
    channel: Channel = "portal"
    inbound_price: float | None = None
    inbound_message: str | None = None
    outbound_message: str = ""
    negotiated_terms: dict[str, Any] = Field(default_factory=dict)
    orchestrator_routing: AgentRouting | None = None
    orchestrator_reasoning: str = ""
    margin_at_round: float | None = None
    hitl_required: bool = False
    escalation_flag: bool = False
    llm_reasoning: str = ""
    created_at: datetime


# ---------------------------------------------------------------------------
# API response
# ---------------------------------------------------------------------------

class NegotiationResponse(BaseModel):
    """Unified API response for start_negotiation and run_round."""

    negotiation_id: str
    deal_id: str
    status: NegotiationStatus
    round_no: int
    current_bid: float | None = None
    current_ask: float | None = None
    agreed_price: float | None = None
    margin_at_current: float | None = None
    hitl_required: bool = False
    reasoning: str | None = None
    outbound_buyer_message: str | None = None
    outbound_supplier_message: str | None = None
    negotiated_terms: NegotiatedTerms | None = None
