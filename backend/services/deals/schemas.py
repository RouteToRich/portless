"""Request/response models for the Deals service."""
from __future__ import annotations

from pydantic import BaseModel

from shared.domain.enums import AutonomyLevel, DealStatus
from shared.domain.models import Deal, Party


class CreateDealRequest(BaseModel):
    operator_id: str
    hs_code: str
    autonomy_profile: AutonomyLevel = AutonomyLevel.L0
    margin_floor_pct: float | None = None
    parties: list[Party] = []


class UpdateStatusRequest(BaseModel):
    target_status: DealStatus
    reason: str | None = None


class DealResponse(Deal):
    pass
