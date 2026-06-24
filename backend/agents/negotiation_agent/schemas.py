"""Request/response models for the Negotiation Agent."""
from __future__ import annotations

from pydantic import BaseModel


class CostEnvelope(BaseModel):
    """Non-negotiable costs passed from Match Engine."""
    travel_cost: float
    qc_cost: float
    doc_cost: float


class NegotiationPolicy(BaseModel):
    target_margin_pct: float
    margin_floor_pct: float
    max_rounds: int = 5
    concession_step: float
    deadline: str | None = None
    rag_context: str | None = None
    cost_envelope: CostEnvelope | None = None


class StartNegotiationRequest(BaseModel):
    deal_id: str
    trade_match_id: str
    buyer_id: str
    seller_id: str
    product_code: str
    buyer_country_code: str
    buyer_target_price: float
    supplier_quote_price: float
    date_of_expectation: str | None = None
    date_of_packaging: str | None = None
    composite_match_score: float | None = None
    cost_envelope: CostEnvelope | None = None
    policy: NegotiationPolicy
    batna: str | None = None


class RoundRequest(BaseModel):
    counter_price: float
    party: str  # "buyer" | "supplier"
    channel: str = "portal"  # "email" | "portal" | "sms" | "whatsapp"
    attachment_refs: list[str] | None = None


class NegotiationResponse(BaseModel):
    negotiation_id: str
    deal_id: str
    status: str  # STARTED | ROUND | AGREED | FAILED | ESCALATED
    round_no: int
    current_bid: float | None = None
    current_ask: float | None = None
    margin_at_current: float | None = None
    hitl_required: bool = False
    reasoning: str | None = None
