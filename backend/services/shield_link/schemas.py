"""Request/response models for the Shield-Link service."""
from __future__ import annotations

from shared.domain.models import ThirdPartyOrder
from pydantic import BaseModel


class QcBookingRequest(BaseModel):
    deal_id: str
    provider: str
    pickup_ready_at: str | None = None


class FreightBookingRequest(BaseModel):
    deal_id: str
    provider: str
    incoterm: str | None = None
    origin_port: str | None = None
    destination_port: str | None = None


class ChaFilingRequest(BaseModel):
    deal_id: str
    provider: str
    documents: list[str] = []


class ThirdPartyOrderResponse(ThirdPartyOrder):
    pass
