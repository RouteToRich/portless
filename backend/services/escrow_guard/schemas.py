"""Request/response models for the Escrow-Guard service."""
from __future__ import annotations

from pydantic import BaseModel


class EscrowHoldRequest(BaseModel):
    deal_id: str
    provider: str
    amount: float
    currency: str


class EscrowResponse(BaseModel):
    escrow_id: str
    deal_id: str
    amount: float
    currency: str
    status: str = "PENDING"
    ext_ref: str | None = None


class PayoutRequest(BaseModel):
    deal_id: str
    payee: str
    amount: float
    currency: str
    idempotency_key: str


class PayoutResponse(BaseModel):
    entry_id: str
    deal_id: str
    payee: str
    amount: float
    currency: str
    status: str = "REQUESTED"
