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

from shared.webhooks import parse_webhook_body

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
    clear_dispute,
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
    list_deal_ledgers,
    list_escrow_providers,
    open_inspection_window,
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


@router.post("/escrows/{escrow_id}/open-window", response_model=EscrowResponse)
async def api_open_inspection_window(escrow_id: str, req: DisputeEscrowRequest | None = None) -> EscrowResponse:
    """Operator confirms delivery → the buyer's acceptance window starts
    (idempotent; waits for pending proof tranches)."""
    try:
        return await open_inspection_window(
            escrow_id, reason=(req.reason if req else "operator confirmed delivery"), trigger="operator",
        )
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("/escrows/{escrow_id}/clear-dispute", response_model=EscrowResponse)
async def api_clear_dispute(escrow_id: str, req: DisputeEscrowRequest) -> EscrowResponse:
    """Lift a buyer dispute (re-inspection agreed / resolved in PortLess's favour)."""
    try:
        return await clear_dispute(escrow_id, reason=req.reason)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


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
    """Per-trade money ledger (held / released / refunded / outstanding / fees /
    payouts / supplier balance) with a reconciliation check (`reconciled` + `issues`)."""
    return await get_deal_ledger(deal_id)


@router.get("/deals/{deal_id}/journal")
async def api_deal_journal(deal_id: str, format: str = "json") -> Any:
    """Double-entry journal for the deal (escrow funding, tranche releases with
    fees and revenue, refunds, supplier payouts). ``?format=csv`` for the books."""
    from fastapi.responses import Response
    from .journal import build_journal, journal_csv
    try:
        journal = await build_journal(deal_id)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if format == "csv":
        return Response(
            content=journal_csv(journal), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="journal-{deal_id}.csv"'},
        )
    return journal.model_dump(mode="json")


@router.get("/journals")
async def api_all_journals(format: str = "json") -> Any:
    """Every deal's journal (``?format=csv`` concatenates them for the accountant)."""
    from fastapi.responses import Response
    from .journal import build_all_journals, journal_csv
    journals = await build_all_journals()
    if format == "csv":
        body = ""
        for i, j in enumerate(journals):
            csv_text = journal_csv(j)
            body += csv_text if i == 0 else "\n".join(csv_text.splitlines()[1:]) + ("\n" if csv_text.strip() else "")
        return Response(content=body, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="journals.csv"'})
    return [j.model_dump(mode="json") for j in journals]


@router.get("/ledgers", response_model=list[DealLedger])
async def api_list_ledgers() -> list[DealLedger]:
    """Every escrow's ledger, newest first — the Money & Margin escrow view."""
    return await list_deal_ledgers()


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@router.post("/webhooks/escrow-funded")
async def api_escrow_funded_webhook(request: Request) -> dict[str, Any]:
    """Stripe/provider webhook confirming buyer funds are held in escrow.

    Transitions deal → ESCROW_FUNDED; emits escrow.funded.
    """
    raw_body = await request.body()
    payload = parse_webhook_body(raw_body, EscrowFundedWebhookPayload)
    headers = dict(request.headers)
    try:
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
    payload = parse_webhook_body(raw_body, PayoutWebhookPayload)
    headers = dict(request.headers)
    try:
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
