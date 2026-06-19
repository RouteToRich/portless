"""HTTP routes for the Shield-Link service."""
from __future__ import annotations

from fastapi import APIRouter

from . import service
from .schemas import (
    ChaFilingRequest,
    FreightBookingRequest,
    QcBookingRequest,
    ThirdPartyOrderResponse,
)

router = APIRouter(prefix="/shield-link", tags=["shield-link"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "shield-link"}


@router.post("/qc", response_model=ThirdPartyOrderResponse, status_code=201)
async def book_qc(payload: QcBookingRequest) -> ThirdPartyOrderResponse:
    return await service.book_qc(payload)


@router.post("/freight", response_model=ThirdPartyOrderResponse, status_code=201)
async def book_freight(payload: FreightBookingRequest) -> ThirdPartyOrderResponse:
    return await service.book_freight(payload)


@router.post("/cha", response_model=ThirdPartyOrderResponse, status_code=201)
async def file_cha(payload: ChaFilingRequest) -> ThirdPartyOrderResponse:
    return await service.file_cha(payload)
