"""HTTP routes for the Sourcing service."""
from __future__ import annotations

from fastapi import APIRouter

from . import service
from .schemas import QuoteIntakeRequest, QuoteResponse, RfqRequest, RfqResponse

router = APIRouter(prefix="/sourcing", tags=["sourcing"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "sourcing"}


@router.post("/rfq", response_model=RfqResponse, status_code=201)
async def dispatch_rfq(payload: RfqRequest) -> RfqResponse:
    return await service.dispatch_rfq(payload)


@router.post("/quotes", response_model=QuoteResponse, status_code=201)
async def receive_quote(payload: QuoteIntakeRequest) -> QuoteResponse:
    return await service.receive_quote(payload)
