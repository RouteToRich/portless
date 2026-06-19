"""Request/response models for the DealMaker service."""
from __future__ import annotations

from pydantic import BaseModel


class IssuePiRequest(BaseModel):
    deal_id: str
    buyer_id: str
    agreed_price: float
    currency: str
    incoterm: str | None = None


class PiResponse(BaseModel):
    pi_id: str
    deal_id: str
    storage_uri: str | None = None
    status: str = "SENT"


class IssuePoRequest(BaseModel):
    deal_id: str
    supplier_id: str
    agreed_price: float
    currency: str


class PoResponse(BaseModel):
    po_id: str
    deal_id: str
    storage_uri: str | None = None
    status: str = "ISSUED"
