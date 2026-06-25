"""HTTP routes for the Negotiation Agent."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import service
from .email_history import EmailHistoryRecord
from .schemas import (
    NegotiationResponse,
    NegotiationRound,
    NegotiationSession,
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
    try:
        return await service.run_round(negotiation_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 409, detail=str(exc))


@router.get("/negotiations/{negotiation_id}", response_model=NegotiationSession)
async def get_negotiation(negotiation_id: str) -> NegotiationSession:
    try:
        return await service.get_negotiation(negotiation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/negotiations/{negotiation_id}/rounds", response_model=list[NegotiationRound])
async def get_rounds(negotiation_id: str) -> list[NegotiationRound]:
    return await service.list_rounds(negotiation_id)


@router.get("/negotiations/{negotiation_id}/emails", response_model=list[EmailHistoryRecord])
async def get_email_history(negotiation_id: str) -> list[EmailHistoryRecord]:
    """Return all outbound drafts and inbound replies for a negotiation.

    Records are ordered oldest-first.  Each record carries a ``status`` that
    tracks the email through its lifecycle:
    draft → pending_approval → sent | modified_sent | rejected | failed
    Inbound emails from buyer/supplier have status=received.
    """
    return await service.list_email_history(negotiation_id)


@router.get("/negotiations", response_model=list[NegotiationSession])
async def list_negotiations(deal_id: str | None = None) -> list[NegotiationSession]:
    return await service.list_negotiations(deal_id=deal_id)
