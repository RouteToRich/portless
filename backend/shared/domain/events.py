"""Event catalog and the canonical envelope (LLD 4.4 Event-Driven Backbone).

REST is the current inter-service transport; this catalog documents the domain
events each service emits so the future move to a real bus (NATS/Kafka) is a
drop-in swap of the publisher, not a re-modelling exercise.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class EventTopic(str, Enum):
    OPPORTUNITY_DETECTED = "opportunity.detected"
    DEAL_CREATED = "deal.created"
    MATCH_SCORED = "match.scored"
    QUOTE_REQUESTED = "quote.requested"
    QUOTE_RECEIVED = "quote.received"
    NEGOTIATION_STARTED = "negotiation.started"
    NEGOTIATION_ROUND = "negotiation.round"
    NEGOTIATION_AGREED = "negotiation.agreed"
    NEGOTIATION_FAILED = "negotiation.failed"
    PI_SENT = "pi.sent"
    PI_ACCEPTED = "pi.accepted"
    ESCROW_FUNDED = "escrow.funded"
    PO_ISSUED = "po.issued"
    QC_BOOKED = "qc.booked"
    QC_PASSED = "qc.passed"
    QC_FAILED = "qc.failed"
    FREIGHT_BOOKED = "freight.booked"
    FREIGHT_MILESTONE = "freight.milestone"
    CUSTOMS_FILED = "customs.filed"
    CUSTOMS_CLEARED = "customs.cleared"
    PAYOUT_REQUESTED = "payout.requested"
    PAYOUT_COMPLETED = "payout.completed"
    ESCROW_TRANCHE_RELEASED = "escrow.tranche_released"
    """One staged tranche left the neutral escrow for PortLess (proof-gated
    release, buyer acceptance, or inspection-window lapse)."""
    ESCROW_SETTLED = "escrow.settled"
    """Every tranche of the escrow is released — the money side of the trade
    is closed (PortLess margin realised, supplier balance payable)."""
    ESCROW_FUNDING_SHORTFALL = "escrow.funding_shortfall"
    """The provider confirmed less than the Proforma Invoice total — the deal
    stays ESCROW_PENDING until the balance arrives."""
    COUNTERPARTY_SCREENED = "counterparty.screened"
    """Sanctions / registry screening finished for a deal party (verdict in payload)."""
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    DEAL_EXCEPTION = "deal.exception"


class DomainEvent(BaseModel):
    """Canonical event envelope (carried over REST today, a bus tomorrow)."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    topic: EventTopic
    deal_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
