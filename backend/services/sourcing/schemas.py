"""Request/response models for the Sourcing service."""
from __future__ import annotations

from pydantic import BaseModel

from shared.domain.models import Quote


class RfqRequest(BaseModel):
    deal_id: str
    hs_code: str
    supplier_ids: list[str]
    quantity: float | None = None


class RfqResponse(BaseModel):
    rfq_id: str
    deal_id: str
    dispatched_to: list[str]


class QuoteIntakeRequest(Quote):
    pass


class QuoteResponse(Quote):
    pass
