"""HTTP routes for the Trade-Min service."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from . import service
from .schemas import OpportunityResponse, ScanRequest

router = APIRouter(prefix="/trade-min", tags=["trade-min"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "trade-min"}


@router.post("/scan", response_model=list[OpportunityResponse])
async def scan(payload: ScanRequest) -> list[OpportunityResponse]:
    return await service.scan_opportunities(payload)


@router.get("/opportunities", response_model=list[OpportunityResponse])
async def list_opportunities() -> list[OpportunityResponse]:
    return await service.list_opportunities()


@router.get("/opportunities/{opportunity_id}", response_model=OpportunityResponse)
async def get_opportunity(opportunity_id: str) -> OpportunityResponse:
    result = await service.get_opportunity(opportunity_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Opportunity {opportunity_id!r} not found",
        )
    return result
