"""Request/response models for the API Gateway service."""
from __future__ import annotations

from pydantic import BaseModel

from shared.domain.enums import AutonomyLevel
from shared.domain.models import Deal, Party


class CreateDealRequest(BaseModel):
    operator_id: str
    hs_code: str
    autonomy_profile: AutonomyLevel = AutonomyLevel.L0
    margin_floor_pct: float | None = None
    parties: list[Party] = []


class DealResponse(Deal):
    pass
