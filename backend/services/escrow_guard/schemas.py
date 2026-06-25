"""Request/response models for the Escrow-Guard service.

Escrow lifecycle:
  PENDING    → PaymentIntent created, awaiting buyer capture
  FUNDED     → Webhook confirms funds received (escrow.funded)
  RELEASED   → Supplier payout dispatched (payout.requested)
  REFUNDED   → Buyer refunded (deal cancelled/disputed)
  FAILED     → Provider error; deal remains ESCROW_PENDING

Payout lifecycle:
  REQUESTED  → Payout initiated with provider
  PROCESSING → Provider acknowledged
  COMPLETED  → Provider confirmed delivery
  FAILED     → Provider returned error; manual intervention needed
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Persisted records
# ---------------------------------------------------------------------------

class EscrowHoldRecord(BaseModel):
    """One escrow hold — maps to a single buyer payment intent."""

    escrow_id: str
    deal_id: str
    provider: str
    amount: float
    currency: str
    status: Literal["PENDING", "FUNDED", "RELEASED", "REFUNDED", "FAILED"]
    ext_ref: str | None = None
    client_secret: str | None = None
    funded_at: datetime | None = None
    released_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class PayoutRecord(BaseModel):
    """One payout entry — maps to a single supplier disbursement."""

    payout_id: str
    deal_id: str
    payee_id: str
    amount: float
    currency: str
    status: Literal["REQUESTED", "PROCESSING", "COMPLETED", "FAILED"]
    ext_ref: str | None = None
    description: str | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EscrowHoldRequest(BaseModel):
    deal_id: str
    provider: str = "stripe"
    amount: float
    currency: str = "USD"
    idempotency_key: str | None = None


class PayoutRequest(BaseModel):
    deal_id: str
    payee_id: str
    """Registered payee ID in the payout provider (e.g. Payoneer payee ID)."""
    amount: float
    currency: str = "USD"
    description: str | None = None
    idempotency_key: str


class EscrowFundedWebhookPayload(BaseModel):
    """Inbound webhook confirming buyer funds are held."""
    ext_ref: str
    status: str
    amount: int | None = None
    currency: str | None = None


class PayoutWebhookPayload(BaseModel):
    """Inbound webhook from Payoneer confirming payout status."""
    ext_ref: str
    status: Literal["PROCESSING", "COMPLETED", "FAILED"]
    amount: float | None = None
    currency: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class EscrowResponse(BaseModel):
    escrow_id: str
    deal_id: str
    provider: str
    amount: float
    currency: str
    status: str
    ext_ref: str | None = None
    client_secret: str | None = None
    """Stripe client_secret for buyer-side capture (frontend use only)."""
    funded_at: datetime | None = None
    created_at: datetime

    @classmethod
    def from_record(cls, r: EscrowHoldRecord) -> "EscrowResponse":
        return cls(
            escrow_id=r.escrow_id,
            deal_id=r.deal_id,
            provider=r.provider,
            amount=r.amount,
            currency=r.currency,
            status=r.status,
            ext_ref=r.ext_ref,
            client_secret=r.client_secret,
            funded_at=r.funded_at,
            created_at=r.created_at,
        )


class PayoutResponse(BaseModel):
    payout_id: str
    deal_id: str
    payee_id: str
    amount: float
    currency: str
    status: str
    ext_ref: str | None = None
    completed_at: datetime | None = None
    created_at: datetime

    @classmethod
    def from_record(cls, r: PayoutRecord) -> "PayoutResponse":
        return cls(
            payout_id=r.payout_id,
            deal_id=r.deal_id,
            payee_id=r.payee_id,
            amount=r.amount,
            currency=r.currency,
            status=r.status,
            ext_ref=r.ext_ref,
            completed_at=r.completed_at,
            created_at=r.created_at,
        )


class DealEscrowSummary(BaseModel):
    deal_id: str
    hold: EscrowResponse | None = None
    payouts: list[PayoutResponse] = Field(default_factory=list)
