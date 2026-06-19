"""Request/response models for the Exception-Resolver service."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class RaiseExceptionRequest(BaseModel):
    deal_id: str
    category: str | None = None
    detail: str | None = None
    context: dict[str, Any] = {}


class ExceptionResponse(BaseModel):
    exception_id: str
    deal_id: str
    category: str | None = None
    status: str = "OPEN"  # OPEN | REMEDIATING | RESOLVED | ESCALATED
    remediation_plan: list[str] = []
