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
PayoutKind = Literal["supplier_payout", "supplier_balance"]
"""``supplier_payout`` — the main disbursement at CLEARED (capped by the funds
released from escrow so far); ``supplier_balance`` — the remainder once the
final tranche settled."""


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
    status: Literal["PENDING", "PARTIALLY_FUNDED", "FUNDED", "RELEASED", "REFUNDED", "FAILED"]
    ext_ref: str | None = None
    client_secret: str | None = None
    funded_at: datetime | None = None
    funded_amount: float | None = None
    """Actual amount confirmed by the funded webhook, normalized to major units.
    Reconciled against ``amount`` (the requested principal) on funding."""
    shortfall: float | None = None
    """``amount − funded_amount`` while the provider confirmed less than the
    principal (status ``PARTIALLY_FUNDED``); None once fully funded."""
    released_at: datetime | None = None
    settled_at: datetime | None = None
    """Set when every tranche has left the escrow (money side of the trade closed)."""
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
    fee_pct: float | None = None
    """Provider fee (%) quoted when the escrow was opened — deducted from every release."""
    fees_total: float = 0.0
    """Estimated provider fees on the amounts released so far."""
    processed_events: list[str] = Field(default_factory=list)
    """Provider webhook event ids already applied (replay protection)."""

    # -- final-tranche release authority (buyer acceptance / inspection window) --
    inspection_deadline: datetime | None = None
    delivery_confirmed_at: datetime | None = None
    """Goods delivered (carrier milestone / operator); the window opens as soon
    as every proof-gated tranche has released."""
    inspection_opened_at: datetime | None = None
    inspection_trigger: str | None = None
    """What opened the window: ``delivery`` / ``proof`` / ``fallback`` / ``operator``."""
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
    kind: PayoutKind = "supplier_payout"
    total_owed: float | None = None
    """What the supplier is owed in total (PO value) — the deal is PAID only
    once completed payouts reach it."""
    processed_events: list[str] = Field(default_factory=list)


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
    kind: PayoutKind = "supplier_payout"
    total_owed: float | None = None
    """Supplier's total entitlement (PO value). When set, the deal only moves to
    PAID once completed payouts reach it; the remainder is paid as a
    ``supplier_balance`` payout after the final tranche settles."""


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
    """Inbound webhook confirming buyer funds are held.

    ``amount`` is the TOTAL the provider holds for this escrow (Stripe: minor
    units; neutral providers: major units). A total below the principal keeps
    the escrow ``PARTIALLY_FUNDED`` and the deal in ESCROW_PENDING.
    """
    ext_ref: str
    status: str
    amount: int | float | None = None
    currency: str | None = None
    event_id: str | None = None
    """Provider event id — a replayed id is acknowledged and ignored."""
    payer_name: str | None = None
    """Account holder the funds came from (compliance: must be the buyer)."""
    payer_country: str | None = None


class PayoutWebhookPayload(BaseModel):
    """Inbound webhook from Payoneer confirming payout status."""
    ext_ref: str
    status: Literal["PROCESSING", "COMPLETED", "FAILED"]
    amount: float | None = None
    currency: str | None = None
    notes: str | None = None
    event_id: str | None = None


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
    fee_pct: float | None = None
    fees_total: float = 0.0
    shortfall: float | None = None
    settled_at: datetime | None = None
    tranches: list[TrancheSpec] = Field(default_factory=list)
    inspection_deadline: datetime | None = None
    delivery_confirmed_at: datetime | None = None
    inspection_opened_at: datetime | None = None
    inspection_trigger: str | None = None
    buyer_accepted_at: datetime | None = None
    disputed: bool = False
    disputed_at: datetime | None = None

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
            fee_pct=r.fee_pct,
            fees_total=r.fees_total,
            shortfall=r.shortfall,
            settled_at=r.settled_at,
            tranches=r.tranches,
            inspection_deadline=r.inspection_deadline,
            delivery_confirmed_at=r.delivery_confirmed_at,
            inspection_opened_at=r.inspection_opened_at,
            inspection_trigger=r.inspection_trigger,
            buyer_accepted_at=r.buyer_accepted_at,
            disputed=r.disputed,
            disputed_at=r.disputed_at,
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
    kind: str = "supplier_payout"
    total_owed: float | None = None

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
            kind=r.kind,
            total_owed=r.total_owed,
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
    escrow_id: str | None = None
    status: str | None = None
    principal: float = 0.0
    """Requested escrow amount (held)."""
    funded_amount: float | None = None
    """Actual amount the funded webhook confirmed."""
    shortfall: float | None = None
    released_total: float = 0.0
    refunded_total: float = 0.0
    outstanding: float = 0.0
    """principal − released_total − refunded_total (still in escrow)."""
    fees_total: float = 0.0
    """Estimated provider fees on the released amounts."""
    net_to_portless: float = 0.0
    """released_total − fees_total — cash PortLess actually received."""
    payouts_total: float = 0.0
    """Sum of COMPLETED supplier payouts."""
    payouts_pending: float = 0.0
    """Sum of REQUESTED / PROCESSING supplier payouts (committed, not yet confirmed)."""
    available_for_payout: float = 0.0
    """What can still be paid to the supplier now: net_to_portless − completed −
    pending payouts, never reaching into ``retained_until_acceptance``."""
    retained_until_acceptance: float = 0.0
    """Part of the supplier's money held back until the buyer accepts the goods
    (SUPPLIER_RETENTION_ENABLED); 0 once the acceptance tranche has released."""
    supplier_owed: float | None = None
    """Supplier's total entitlement (Purchase Order value) when known."""
    supplier_balance_due: float | None = None
    """supplier_owed − payouts_total (None when the PO value is unknown)."""
    realized_margin: float | None = None
    """net_to_portless − payouts_total — PortLess's realised margin so far."""
    tranches: list[TrancheSpec] = Field(default_factory=list)
    payouts: list[PayoutResponse] = Field(default_factory=list)
    inspection_deadline: datetime | None = None
    disputed: bool = False
    settled: bool = False
    reconciled: bool = True
    issues: list[str] = Field(default_factory=list)
