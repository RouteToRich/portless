"""Shared data-transfer objects (DTOs) exchanged over REST between services.

These are deliberately thin Pydantic models — the persistence schema lives with
the owning service. They mirror the master-spec data model (LLD 4.7).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .enums import AutonomyLevel, DealStatus, PartyRole, ThirdPartyType


class AddressChange(BaseModel):
    """An address the party no longer has: what it was, where it came from, when and by what
    it was replaced (``Party.address_history``)."""
    address: str
    source: str | None = None
    """``operator`` · ``email:<sender check>`` · ``apify:<actor>`` … — who gave the old address."""
    replaced_at: datetime
    replaced_by: str | None = None
    """Source of the address that replaced it."""


class Party(BaseModel):
    party_id: str | None = None
    role: PartyRole
    legal_name: str
    country: str
    gstin: str | None = None
    iec: str | None = None
    vat_number: str | None = None
    verification_status: str | None = None
    credit_status: str | None = None
    risk_score: float | None = None

    # ── Contact (filled by the Apify company-contact-finder actor, the
    #    opportunity's buyer/seller signal, or an operator edit) ───────────
    email: str | None = None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    address_source: str | None = None
    """Who gave the current address (``operator``, ``email:<by>``, an actor …)."""
    address_history: list[AddressChange] = Field(default_factory=list)
    """Earlier addresses, oldest first — a supplier's facility address can be replaced by a
    later e-mail, so the operator can see that it changed and what it was."""
    contact_source: str | None = None
    """Where the contact came from: ``apify:<actor>``, ``opportunity``,
    ``operator`` — shown in the UI so a human knows how much to trust it."""
    contact_confidence: float | None = None
    """0–1 confidence of the website/email choice (actor's websiteConfidence)."""
    email_status: str | None = None
    """``ok`` (domain takes mail) · ``risky`` (no MX / catch-all) · ``invalid``
    · ``unverified`` (never checked)."""

    # ── Counterparty screening (Apify counterparty-check actor: OFAC / UN /
    #    EU / UK sanctions lists, GLEIF LEI, EU VIES) ───────────────────────
    sanctions_status: str | None = None
    """``clear`` · ``review`` · ``block`` · ``unchecked``."""
    sanctions_reason: str | None = None
    sanctions_checked_at: datetime | None = None
    sanctions_evidence_url: str | None = None
    """The screening actor's evidence certificate for that result (linked from the badge and
    the evidence pack)."""
    lei: str | None = None
    vat_valid: bool | None = None
    enriched_at: datetime | None = None


class Deal(BaseModel):
    deal_id: str
    operator_id: str
    hs_code: str
    status: DealStatus = DealStatus.DRAFT
    autonomy_profile: AutonomyLevel = AutonomyLevel.L0
    margin_floor_pct: float | None = None
    est_margin_usd: float | None = None
    realized_margin_usd: float | None = None
    est_margin_pct: float | None = None
    realized_margin_pct: float | None = None
    parties: list[Party] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Quote(BaseModel):
    quote_id: str | None = None
    deal_id: str
    supplier_id: str
    unit_price: float
    currency: str
    valid_until: datetime | None = None


class MatchScore(BaseModel):
    match_id: str | None = None
    deal_id: str
    composite_score: float
    gatekeeper_passed: bool
    arbitrage_score: float
    proximity_score: float
    reliability_score: float


class ThirdPartyOrder(BaseModel):
    tpo_id: str | None = None
    deal_id: str
    type: ThirdPartyType
    provider: str
    ext_ref: str | None = None
    status: str | None = None
    crf_hash: str | None = None
    eta: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Approval(BaseModel):
    approval_id: str | None = None
    deal_id: str
    action: str
    summary: str | None = None
    financial_impact_usd: float | None = None
    risk_flags: list[str] = Field(default_factory=list)
    status: str = "PENDING"
    expires_at: datetime | None = None
    default_on_timeout: str = "REJECT"
    deep_link: str | None = None
