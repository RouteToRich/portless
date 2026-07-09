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
# Staged-release primitives (target multi-provider escrow model)
# ---------------------------------------------------------------------------

EscrowProviderName = Literal["tazapay", "escrow_com"]
TrancheTrigger = Literal["PO_ISSUED", "QC_PASSED", "CLEARED", "ACCEPTED"]
TrancheStatus = Literal["PENDING", "RELEASED"]


class TrancheSpec(BaseModel):
    """One slice of the escrowed principal, released on an independent proof."""

    tranche_id: str
    trigger: TrancheTrigger
    pct: float = Field(ge=0.0, le=1.0)
    amount: float | None = None
    status: TrancheStatus = "PENDING"
    proof_refs: list[str] = Field(default_factory=list)
    ext_ref: str | None = None
    released_at: datetime | None = None


# ---------------------------------------------------------------------------
# Persisted records
# ---------------------------------------------------------------------------

class EscrowHoldRecord(BaseModel):
    """One escrow hold — maps to a single buyer payment intent / escrow txn."""

    escrow_id: str
    deal_id: str
    provider: str
    amount: float
    currency: str
    status: Literal["PENDING", "FUNDED", "RELEASED", "REFUNDED", "FAILED"]
    ext_ref: str | None = None
    client_secret: str | None = None
    funded_at: datetime | None = None
    funded_amount: float | None = None
    """Actual amount confirmed by the funded webhook, normalized to major units.
    Reconciled against ``amount`` (the requested principal) on funding."""
    released_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None

    # -- staged multi-provider escrow (optional; empty for legacy Stripe holds) --
    payment_method: str | None = None
    qc_provider: str | None = None
    checkout_url: str | None = None
    tranches: list[TrancheSpec] = Field(default_factory=list)
    released_total: float = 0.0
    refunded_total: float = 0.0

    # -- final-tranche release authority (buyer acceptance / inspection window) --
    inspection_deadline: datetime | None = None
    buyer_accepted_at: datetime | None = None
    disputed: bool = False
    disputed_at: datetime | None = None
    reconciliation_flagged: bool = False
    """Set once the background sweep has raised a `deal.exception` for a ledger
    breach; reset when the ledger reconciles again (prevents duplicate alerts)."""


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


class TrancheInput(BaseModel):
    """A tranche in a custom release schedule (pcts should sum to 1.0)."""
    tranche_id: str
    trigger: TrancheTrigger
    pct: float = Field(ge=0.0, le=1.0)


class CreateEscrowRequest(BaseModel):
    """Open a neutral, staged-release escrow with the buyer's chosen provider."""
    deal_id: str
    provider: EscrowProviderName = "tazapay"
    amount: float
    currency: str = "USD"
    payment_method: str | None = None
    qc_provider: str | None = None
    tranche_schedule: list[TrancheInput] | None = None
    """Overrides the default 30/40/20/10 schedule when provided."""
    idempotency_key: str | None = None


class ReleaseTrancheRequest(BaseModel):
    """Release one proof-gated tranche after its independent proof is authenticated.

    Only tranches whose trigger is PO_ISSUED / QC_PASSED / CLEARED may be released
    this way. The final ``ACCEPTED``-trigger tranche is released **only** by buyer
    acceptance or inspection-window lapse — never by this call.
    """
    tranche_id: str
    proof_refs: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


class AcceptEscrowRequest(BaseModel):
    """Buyer acceptance — releases the final ``ACCEPTED``-trigger tranche(s)."""
    accepted_by: str | None = None
    notes: str | None = None


class DisputeEscrowRequest(BaseModel):
    """Buyer dispute within the inspection window — blocks auto-release."""
    reason: str
    raised_by: str | None = None


class RefundRequest(BaseModel):
    """Refund the undisbursed balance to the buyer (dispute / failure path)."""
    reason: str = ""


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
    funded_amount: float | None = None
    created_at: datetime

    # -- staged multi-provider escrow (defaults keep legacy holds valid) --
    payment_method: str | None = None
    checkout_url: str | None = None
    released_total: float = 0.0
    refunded_total: float = 0.0
    tranches: list[TrancheSpec] = Field(default_factory=list)
    inspection_deadline: datetime | None = None
    buyer_accepted_at: datetime | None = None
    disputed: bool = False

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
            funded_amount=r.funded_amount,
            created_at=r.created_at,
            payment_method=r.payment_method,
            checkout_url=r.checkout_url,
            released_total=r.released_total,
            refunded_total=r.refunded_total,
            tranches=r.tranches,
            inspection_deadline=r.inspection_deadline,
            buyer_accepted_at=r.buyer_accepted_at,
            disputed=r.disputed,
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


class DealLedger(BaseModel):
    """Money ledger for one trade, with a reconciliation check.

    Invariant (within tolerance): ``released_total + refunded_total +
    outstanding == principal``, and released tranche amounts sum to
    ``released_total``. ``reconciled`` is False and ``issues`` is populated when
    any of these break.
    """
    deal_id: str
    currency: str
    provider: str | None = None
    principal: float = 0.0
    """Requested escrow amount (held)."""
    funded_amount: float | None = None
    """Actual amount the funded webhook confirmed."""
    released_total: float = 0.0
    refunded_total: float = 0.0
    outstanding: float = 0.0
    """principal − released_total − refunded_total (still in escrow)."""
    payouts_total: float = 0.0
    """Sum of COMPLETED supplier payouts."""
    tranches: list[TrancheSpec] = Field(default_factory=list)
    reconciled: bool = True
    issues: list[str] = Field(default_factory=list)
