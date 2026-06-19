"""Request/response models for the Negotiator service."""
from __future__ import annotations

from pydantic import BaseModel


class NegotiationPolicy(BaseModel):
    target_margin_pct: float
    margin_floor_pct: float
    max_rounds: int = 5
    concession_step: float
    deadline: str | None = None


class StartNegotiationRequest(BaseModel):
    deal_id: str
    buyer_target_price: float
    supplier_quote_price: float
    policy: NegotiationPolicy
    batna: str | None = None


class RoundRequest(BaseModel):
    counter_price: float
    party: str  # "buyer" | "supplier"


class NegotiationResponse(BaseModel):
    negotiation_id: str
    deal_id: str
    status: str  # STARTED | ROUND | AGREED | FAILED | ESCALATED
    round_no: int
    current_bid: float | None = None
    current_ask: float | None = None
