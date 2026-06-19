"""Request/response models for the HITL Approval service."""
from __future__ import annotations

from pydantic import BaseModel

from shared.domain.models import Approval


class CreateApprovalRequest(BaseModel):
    deal_id: str
    action: str
    summary: str | None = None
    financial_impact_usd: float | None = None
    risk_flags: list[str] = []
    expires_at: str | None = None
    default_on_timeout: str = "REJECT"


class ResolveApprovalRequest(BaseModel):
    decision: str  # APPROVE | REJECT
    resolved_by: str


class ApprovalResponse(Approval):
    pass
