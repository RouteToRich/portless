"""Request/response models for the HITL Approval service.

Approval lifecycle:
  PENDING      → Created, awaiting human decision
  APPROVED     → Operator explicitly approved; saga callback executed
  REJECTED     → Operator explicitly rejected; deal.exception emitted
  AUTO_APPROVED → L2 autonomy or L1 timeout fired with APPROVE default
  TIMED_OUT    → L1 timeout fired with REJECT default

Autonomy levels (from shared/domain/enums.py):
  L0 → Always ask, no timeout auto-resolution
  L1 → Ask, auto-resolve with default_on_timeout after expires_at
  L2 → Auto-approve immediately, notify operator (no blocking)
  L3 → Fully autonomous, caller should never reach HITL
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Persisted record
# ---------------------------------------------------------------------------

class ApprovalRecord(BaseModel):
    approval_id: str
    deal_id: str
    action: str
    """ApprovalAction value: RELEASE_SUPPLIER_PAYOUT | BOOK_FREIGHT | ISSUE_PO | FILE_CUSTOMS | RESOLVE_EXCEPTION | SEND_NEGOTIATION_EMAIL"""
    summary: str | None = None
    financial_impact_usd: float | None = None
    risk_flags: list[str] = Field(default_factory=list)
    autonomy_level: str = "L0"
    status: Literal["PENDING", "APPROVED", "REJECTED", "AUTO_APPROVED", "TIMED_OUT"]
    expires_at: datetime | None = None
    default_on_timeout: Literal["APPROVE", "REJECT"] = "REJECT"
    deep_link: str | None = None

    # Saga continuation — serialised request payload forwarded to the target
    # service when the approval is resolved with APPROVE.
    callback_payload: dict[str, Any] = Field(default_factory=dict)

    # Resolution fields
    decision: Literal["APPROVE", "REJECT"] | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    modified_body: str | None = None
    """For SEND_NEGOTIATION_EMAIL: operator-supplied replacement email body."""

    created_at: datetime
    updated_at: datetime
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------

class CreateApprovalRequest(BaseModel):
    deal_id: str
    action: str
    summary: str | None = None
    financial_impact_usd: float | None = None
    risk_flags: list[str] = Field(default_factory=list)
    autonomy_level: str = "L0"
    expires_at: str | None = None
    """ISO-8601 UTC datetime after which the approval auto-resolves (L1 only)."""
    default_on_timeout: Literal["APPROVE", "REJECT"] = "REJECT"
    callback_payload: dict[str, Any] = Field(default_factory=dict)
    """Full request body forwarded to the target service on APPROVE."""
    idempotency_key: str | None = None


class ResolveApprovalRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    resolved_by: str
    note: str | None = None
    modified_body: str | None = None
    """For SEND_NEGOTIATION_EMAIL approvals: provide a replacement email body
    to send instead of the LLM draft.  Only meaningful when decision=APPROVE."""


class ApprovalResponse(BaseModel):
    approval_id: str
    deal_id: str
    action: str
    summary: str | None = None
    financial_impact_usd: float | None = None
    risk_flags: list[str] = Field(default_factory=list)
    autonomy_level: str
    status: str
    expires_at: datetime | None = None
    default_on_timeout: str
    deep_link: str | None = None
    decision: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    modified_body: str | None = None
    created_at: datetime

    @classmethod
    def from_record(cls, r: ApprovalRecord) -> "ApprovalResponse":
        return cls(
            approval_id=r.approval_id,
            deal_id=r.deal_id,
            action=r.action,
            summary=r.summary,
            financial_impact_usd=r.financial_impact_usd,
            risk_flags=r.risk_flags,
            autonomy_level=r.autonomy_level,
            status=r.status,
            expires_at=r.expires_at,
            default_on_timeout=r.default_on_timeout,
            deep_link=r.deep_link,
            decision=r.decision,
            resolved_by=r.resolved_by,
            resolved_at=r.resolved_at,
            resolution_note=r.resolution_note,
            modified_body=r.modified_body,
            created_at=r.created_at,
        )


# ---------------------------------------------------------------------------
# SSE event envelope
# ---------------------------------------------------------------------------

class ApprovalSseEvent(BaseModel):
    """Envelope pushed over the SSE stream to connected UI clients."""
    event_type: Literal["approval_created", "approval_resolved", "heartbeat"]
    approval: ApprovalResponse | None = None
