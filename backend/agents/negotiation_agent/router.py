"""HTTP routes for the Negotiation Agent."""
from __future__ import annotations

from fastapi import APIRouter

from . import service
from .schemas import (
    NegotiationResponse,
    RoundRequest,
    StartNegotiationRequest,
)

router = APIRouter(prefix="/negotiation-agent", tags=["negotiation-agent"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "negotiation_agent"}


@router.post("/negotiations", response_model=NegotiationResponse, status_code=201)
async def start(payload: StartNegotiationRequest) -> NegotiationResponse:
    return await service.start_negotiation(payload)


@router.post("/negotiations/{negotiation_id}/round", response_model=NegotiationResponse)
async def run_round(negotiation_id: str, payload: RoundRequest) -> NegotiationResponse:
    return await service.run_round(negotiation_id, payload)


@router.get("/negotiations/{negotiation_id}", response_model=NegotiationResponse)
async def get_negotiation(negotiation_id: str) -> NegotiationResponse:
    return await service.get_negotiation(negotiation_id)
