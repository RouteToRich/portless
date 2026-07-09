"""Escrow-Guard HTTP router.

Routes:
  POST /escrow-guard/holds                  Create escrow hold (ESCROW_PENDING)
  GET  /escrow-guard/holds/{escrow_id}      Get hold by ID
  GET  /escrow-guard/deals/{deal_id}/hold   Get hold by deal
  POST /escrow-guard/payouts                Request supplier payout (CLEARED)
  GET  /escrow-guard/payouts/{payout_id}    Get payout by ID
  GET  /escrow-guard/deals/{deal_id}/summary Full escrow summary for a deal
  POST /escrow-guard/webhooks/escrow-funded  Provider confirms funds received
  POST /escrow-guard/webhooks/payout        Provider confirms payout status
  GET  /escrow-guard/health
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from .schemas import (
    AcceptEscrowRequest,
    CreateEscrowRequest,
    DealEscrowSummary,
    DealLedger,
    DisputeEscrowRequest,
    EscrowFundedWebhookPayload,
    EscrowHoldRequest,
    EscrowResponse,
    PayoutRequest,
    PayoutResponse,
    PayoutWebhookPayload,
    RefundRequest,
    ReleaseTrancheRequest,
)
from .service import (
    ConnectorCallError,
    DealNotFoundError,
    HoldNotFoundError,
    InvalidDealState,
    PayoutNotFoundError,
    WebhookVerifyError,
    accept_escrow,
    create_escrow,
    create_hold,
    dispute_escrow,
    get_deal_ledger,
    get_deal_summary,
    get_hold,
    get_hold_by_deal,
    get_payout,
    handle_escrow_funded_webhook,
    handle_payout_webhook,
    list_escrow_providers,
    refund,
    release_tranche,
    request_payout,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/escrow-guard", tags=["escrow-guard"])


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------

@router.post("/holds", response_model=EscrowResponse, status_code=status.HTTP_201_CREATED)
async def api_create_hold(req: EscrowHoldRequest) -> EscrowResponse:
    """Create buyer-side escrow hold for a deal in ESCROW_PENDING state."""
    try:
        return await create_hold(req)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("/holds/{escrow_id}", response_model=EscrowResponse)
async def api_get_hold(escrow_id: str) -> EscrowResponse:
    try:
        return await get_hold(escrow_id)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/deals/{deal_id}/hold", response_model=EscrowResponse)
async def api_get_hold_by_deal(deal_id: str) -> EscrowResponse:
    try:
        return await get_hold_by_deal(deal_id)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# Neutral staged-release escrow (Tazapay / Escrow.com)
# ---------------------------------------------------------------------------

@router.get("/providers")
async def api_list_providers() -> dict[str, Any]:
    """List supported neutral-escrow providers + their pay-in methods."""
    return {"providers": await list_escrow_providers()}


@router.post("/escrows", response_model=EscrowResponse, status_code=status.HTTP_201_CREATED)
async def api_create_escrow(req: CreateEscrowRequest) -> EscrowResponse:
    """Open a neutral, staged-release escrow with the buyer's chosen provider."""
    try:
        return await create_escrow(req)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("/escrows/{escrow_id}", response_model=EscrowResponse)
async def api_get_escrow(escrow_id: str) -> EscrowResponse:
    try:
        return await get_hold(escrow_id)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/escrows/{escrow_id}/release", response_model=EscrowResponse)
async def api_release_tranche(escrow_id: str, req: ReleaseTrancheRequest) -> EscrowResponse:
    """Release one tranche after its independent proof (CRF / shipping bill) is authenticated."""
    try:
        return await release_tranche(escrow_id, req)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/escrows/{escrow_id}/accept", response_model=EscrowResponse)
async def api_accept_escrow(escrow_id: str, req: AcceptEscrowRequest) -> EscrowResponse:
    """Buyer acceptance — releases the final (ACCEPTED-trigger) tranche(s)."""
    try:
        return await accept_escrow(escrow_id, req)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/escrows/{escrow_id}/dispute", response_model=EscrowResponse)
async def api_dispute_escrow(escrow_id: str, req: DisputeEscrowRequest) -> EscrowResponse:
    """Buyer raises a dispute within the inspection window — blocks auto-release."""
    try:
        return await dispute_escrow(escrow_id, req)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("/escrows/{escrow_id}/refund", response_model=EscrowResponse)
async def api_refund_escrow(escrow_id: str, req: RefundRequest) -> EscrowResponse:
    """Refund the undisbursed balance to the buyer (dispute / failure path)."""
    try:
        return await refund(escrow_id, req)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------

@router.post("/payouts", response_model=PayoutResponse, status_code=status.HTTP_201_CREATED)
async def api_request_payout(req: PayoutRequest) -> PayoutResponse:
    """Initiate supplier payout for a deal in CLEARED state."""
    try:
        return await request_payout(req)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("/payouts/{payout_id}", response_model=PayoutResponse)
async def api_get_payout(payout_id: str) -> PayoutResponse:
    try:
        return await get_payout(payout_id)
    except PayoutNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/deals/{deal_id}/summary", response_model=DealEscrowSummary)
async def api_deal_summary(deal_id: str) -> DealEscrowSummary:
    return await get_deal_summary(deal_id)


@router.get("/deals/{deal_id}/ledger", response_model=DealLedger)
async def api_deal_ledger(deal_id: str) -> DealLedger:
    """Per-trade money ledger (held / released / refunded / outstanding / payouts)
    with a reconciliation check (`reconciled` + `issues`)."""
    return await get_deal_ledger(deal_id)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@router.post("/webhooks/escrow-funded")
async def api_escrow_funded_webhook(request: Request) -> dict[str, Any]:
    """Stripe/provider webhook confirming buyer funds are held in escrow.

    Transitions deal → ESCROW_FUNDED; emits escrow.funded.
    """
    raw_body = await request.body()
    payload_data = await request.json()
    headers = dict(request.headers)
    try:
        payload = EscrowFundedWebhookPayload(**payload_data)
        return await handle_escrow_funded_webhook(payload, headers, raw_body)
    except WebhookVerifyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error("Escrow funded webhook error: %s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Webhook processing failed")


@router.post("/webhooks/payout")
async def api_payout_webhook(request: Request) -> dict[str, Any]:
    """Payoneer webhook confirming payout status.

    COMPLETED → deal → PAID; emits payout.completed.
    FAILED    → emits deal.exception.
    """
    raw_body = await request.body()
    payload_data = await request.json()
    headers = dict(request.headers)
    try:
        payload = PayoutWebhookPayload(**payload_data)
        return await handle_payout_webhook(payload, headers, raw_body)
    except WebhookVerifyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except PayoutNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error("Payout webhook error: %s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Webhook processing failed")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict[str, str]:
    return {"service": "escrow-guard", "status": "ok"}
