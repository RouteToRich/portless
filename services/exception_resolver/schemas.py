"""Request/response models for the Exception-Resolver service."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ExceptionCategory(str, Enum):
    QC_FAILED = "QC_FAILED"
    FREIGHT_DELAY = "FREIGHT_DELAY"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    NEGOTIATION_FAILED = "NEGOTIATION_FAILED"
    CUSTOMS_REJECTED = "CUSTOMS_REJECTED"
    DOCUMENT_ERROR = "DOCUMENT_ERROR"
    CONNECTOR_ERROR = "CONNECTOR_ERROR"
    MARGIN_BREACH = "MARGIN_BREACH"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    CALLBACK_FAILED = "CALLBACK_FAILED"
    UNKNOWN = "UNKNOWN"


class ExceptionSeverity(str, Enum):
    LOW = "LOW"          # Auto-fixable, minimal business impact
    MEDIUM = "MEDIUM"    # Delay but recoverable without escalation
    HIGH = "HIGH"        # Significant business impact, likely needs review
    CRITICAL = "CRITICAL"  # Deal at immediate risk of cancellation


class ExceptionStatus(str, Enum):
    OPEN = "OPEN"
    ANALYZING = "ANALYZING"
    REMEDIATING = "REMEDIATING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    UNRESOLVABLE = "UNRESOLVABLE"


class RemediationDecision(str, Enum):
    AUTO_FIX = "AUTO_FIX"   # System attempts automated fix
    ESCALATE = "ESCALATE"   # Create HITL approval for human review
    CANCEL = "CANCEL"        # Cancel deal, notify parties
    MONITOR = "MONITOR"      # Log and watch, no immediate action


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class RemediationStep(BaseModel):
    step: int
    description: str
    status: str = "PENDING"  # PENDING | DONE | FAILED | SKIPPED
    executed_at: datetime | None = None
    result: str | None = None


class AgentAnalysis(BaseModel):
    category: ExceptionCategory
    severity: ExceptionSeverity
    summary: str
    root_cause: str
    remediation_steps: list[str]
    decision: RemediationDecision
    decision_rationale: str
    estimated_resolution_hours: int
    llm_model: str = ""
    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Core record (persisted)
# ---------------------------------------------------------------------------

class ExceptionRecord(BaseModel):
    exception_id: str = Field(default_factory=lambda: f"EXC-{uuid4().hex[:10].upper()}")
    deal_id: str
    source_event: str = ""          # EventTopic value that triggered this
    source_service: str = ""        # Service that emitted the deal.exception
    raw_detail: str = ""            # Original error detail / description
    context: dict[str, Any] = Field(default_factory=dict)

    # Agent outputs
    status: ExceptionStatus = ExceptionStatus.OPEN
    category: ExceptionCategory | None = None
    severity: ExceptionSeverity | None = None
    analysis: AgentAnalysis | None = None
    steps: list[RemediationStep] = Field(default_factory=list)

    # Outcome
    approval_id: str | None = None   # HITL approval raised for ESCALATE
    resolution_note: str | None = None
    auto_fix_attempts: int = 0

    # Timestamps
    raised_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# API request/response
# ---------------------------------------------------------------------------

class RaiseExceptionRequest(BaseModel):
    deal_id: str
    source_event: str = ""
    source_service: str = ""
    detail: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class ExceptionResponse(BaseModel):
    exception_id: str
    deal_id: str
    source_event: str
    source_service: str
    status: ExceptionStatus
    category: ExceptionCategory | None = None
    severity: ExceptionSeverity | None = None
    summary: str | None = None
    root_cause: str | None = None
    decision: RemediationDecision | None = None
    remediation_steps: list[RemediationStep] = []
    approval_id: str | None = None
    resolution_note: str | None = None
    auto_fix_attempts: int = 0
    raised_at: datetime
    resolved_at: datetime | None = None
    updated_at: datetime

    @classmethod
    def from_record(cls, rec: ExceptionRecord) -> "ExceptionResponse":
        return cls(
            exception_id=rec.exception_id,
            deal_id=rec.deal_id,
            source_event=rec.source_event,
            source_service=rec.source_service,
            status=rec.status,
            category=rec.category,
            severity=rec.severity,
            summary=rec.analysis.summary if rec.analysis else None,
            root_cause=rec.analysis.root_cause if rec.analysis else None,
            decision=rec.analysis.decision if rec.analysis else None,
            remediation_steps=rec.steps,
            approval_id=rec.approval_id,
            resolution_note=rec.resolution_note,
            auto_fix_attempts=rec.auto_fix_attempts,
            raised_at=rec.raised_at,
            resolved_at=rec.resolved_at,
            updated_at=rec.updated_at,
        )


class ExceptionListResponse(BaseModel):
    items: list[ExceptionResponse]
    total: int
