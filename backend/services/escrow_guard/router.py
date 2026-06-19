"""HTTP routes for the Escrow-Guard service."""
from __future__ import annotations

from fastapi import APIRouter, Request

from . import service
from .schemas import (
    EscrowHoldRequest,
    EscrowResponse,
    PayoutRequest,
    PayoutResponse,
)

router = APIRouter(prefix="/escrow-guard", tags=["escrow-guard"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "escrow-guard"}


@router.post("/holds", response_model=EscrowResponse, status_code=201)
async def create_hold(payload: EscrowHoldRequest) -> EscrowResponse:
    return await service.create_hold(payload)


@router.post("/payouts", response_model=PayoutResponse, status_code=201)
async def request_payout(payload: PayoutRequest) -> PayoutResponse:
    return await service.request_payout(payload)


@router.post("/webhooks/escrow-funded")
async def escrow_funded_webhook(request: Request) -> dict[str, str]:
    return await service.handle_escrow_funded_webhook(request)
