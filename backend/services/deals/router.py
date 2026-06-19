"""HTTP routes for the Deals service."""
from __future__ import annotations

from fastapi import APIRouter

from . import service
from .schemas import (
    CreateDealRequest,
    DealResponse,
    UpdateStatusRequest,
)

router = APIRouter(prefix="/deals", tags=["deals"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "deals"}


@router.get("", response_model=list[DealResponse])
async def list_deals(status: str | None = None) -> list[DealResponse]:
    return await service.list_deals(status)


@router.post("", response_model=DealResponse, status_code=201)
async def create_deal(payload: CreateDealRequest) -> DealResponse:
    return await service.create_deal(payload)


@router.get("/{deal_id}", response_model=DealResponse)
async def get_deal(deal_id: str) -> DealResponse:
    return await service.get_deal(deal_id)


@router.patch("/{deal_id}/status", response_model=DealResponse)
async def update_status(deal_id: str, payload: UpdateStatusRequest) -> DealResponse:
    return await service.update_status(deal_id, payload)
