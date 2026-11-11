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
  POST /escrow-guard/webhooks/tazapay       Tazapay event (signed) → the escrow is re-read
  POST /escrow-guard/webhooks/escrow-com    Escrow.com event (?token=) → the transaction is re-read
  POST /escrow-guard/escrows/{id}/poll      Read the escrow on its provider now
  POST /escrow-guard/escrows/{id}/tranches/{tid}/provider-confirmed   Staff relay of a provider payout
  GET  /escrow-guard/proof/{id}/{tid}       Signed proof sheet for the provider (exp + sig)
  GET  /escrow-guard/health
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from shared.auth.dependencies import AuthUser, require_portless
from shared.webhooks import parse_webhook_body, signature_required

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
    ProviderReleaseConfirmation,
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
    confirm_release_by_staff,
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
    handle_provider_webhook,
    list_deal_ledgers,
    list_escrow_providers,
    open_inspection_window,
    poll_provider,
    refund,
    release_tranche,
    request_buyer_acceptance,
    request_payout,
    request_refund,
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


async def _require_approved_gate(approval_id: str | None, action: str, expect: dict[str, Any]) -> None:
    """Release, acceptance and payout run from their approval gate. Over HTTP
    they are only a retry of that gate, naming it and the same money."""
    from services.hitl_approval.service import GateNotApproved, assert_gate_approved  # type: ignore[import]

    try:
        await assert_gate_approved(approval_id, action, expect)
    except GateNotApproved as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc))


@router.post("/escrows/{escrow_id}/release", response_model=EscrowResponse)
async def api_release_tranche(escrow_id: str, req: ReleaseTrancheRequest) -> EscrowResponse:
    """Retry the release of an approved RELEASE_ESCROW_TRANCHE gate (403 without one)."""
    await _require_approved_gate(req.approval_id, "RELEASE_ESCROW_TRANCHE",
                                 {"escrow_id": escrow_id, "tranche_id": req.tranche_id})
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
    """Carry out an approved CONFIRM_BUYER_ACCEPTANCE gate (403 without one)."""
    await _require_approved_gate(req.approval_id, "CONFIRM_BUYER_ACCEPTANCE", {"escrow_id": escrow_id})
    try:
        return await accept_escrow(escrow_id, req)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/escrows/{escrow_id}/request-acceptance", status_code=status.HTTP_202_ACCEPTED)
async def api_request_acceptance(escrow_id: str, req: AcceptEscrowRequest, request: Request) -> dict[str, Any]:
    """An operator relays the buyer's acceptance of the goods: opens the
    CONFIRM_BUYER_ACCEPTANCE gate. Approving it releases the final tranche."""
    caller = getattr(request.state, "user", None)
    if caller is not None:
        req.accepted_by = caller.email or caller.user_id
    try:
        return await request_buyer_acceptance(escrow_id, req)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("/escrows/{escrow_id}/dispute", response_model=EscrowResponse)
async def api_dispute_escrow(escrow_id: str, req: DisputeEscrowRequest, request: Request) -> EscrowResponse:
    """Buyer raises a dispute within the inspection window — blocks auto-release."""
    caller = getattr(request.state, "user", None)
    if caller is not None:
        req.raised_by = caller.email or caller.user_id
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


@router.post("/escrows/{escrow_id}/request-refund", status_code=status.HTTP_202_ACCEPTED)
async def api_request_refund(escrow_id: str, req: RefundRequest, request: Request) -> dict[str, Any]:
    """An operator asks to refund the undisbursed balance: opens the
    REFUND_ESCROW gate. Approving it sends the money back to the buyer."""
    if not req.reason.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A refund needs a reason")
    caller = getattr(request.state, "user", None)
    if caller is not None:
        req.requested_by = caller.email or caller.user_id
    try:
        return await request_refund(escrow_id, req)
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("/escrows/{escrow_id}/refund", response_model=EscrowResponse)
async def api_refund_escrow(escrow_id: str, req: RefundRequest) -> EscrowResponse:
    """Retry the refund of an approved REFUND_ESCROW gate (403 without one)."""
    await _require_approved_gate(req.approval_id, "REFUND_ESCROW", {"escrow_id": escrow_id})
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
    """Retry the payout of an approved RELEASE_SUPPLIER_PAYOUT gate (403 without one)."""
    await _require_approved_gate(req.approval_id, "RELEASE_SUPPLIER_PAYOUT", {
        "deal_id": req.deal_id, "amount": req.amount, "idempotency_key": req.idempotency_key,
    })
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
        return await handle_escrow_funded_webhook(
            payload, headers, raw_body, require_signature=signature_required(request),
        )
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
        return await handle_payout_webhook(
            payload, headers, raw_body, require_signature=signature_required(request),
        )
    except WebhookVerifyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except PayoutNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error("Payout webhook error: %s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Webhook processing failed")


async def _provider_webhook(provider: str, request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    headers = dict(request.headers)
    token = request.query_params.get("token")
    if token:
        headers["x-portless-webhook-token"] = token
    try:
        return await handle_provider_webhook(provider, headers, raw_body,
                                             require_signature=signature_required(request))
    except WebhookVerifyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConnectorCallError as exc:  # provider read failed — let the provider retry
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/webhooks/tazapay")
async def api_tazapay_webhook(request: Request) -> dict[str, Any]:
    """Tazapay event (``signature`` header, HMAC-SHA256 base64 over event id +
    body + created_at). The escrow is fetched from Tazapay before anything changes."""
    return await _provider_webhook("tazapay", request)


@router.post("/webhooks/escrow-com")
async def api_escrow_com_webhook(request: Request) -> dict[str, Any]:
    """Escrow.com event. Register ``…/webhooks/escrow-com?token=<ESCROW_COM_WEBHOOK_SECRET>``
    with Escrow.com (it does not sign); the transaction is always fetched first."""
    return await _provider_webhook("escrow_com", request)


# ---------------------------------------------------------------------------
# Provider reads + proof sheet
# ---------------------------------------------------------------------------

@router.post("/escrows/{escrow_id}/poll", response_model=EscrowResponse)
async def api_poll_provider(escrow_id: str) -> EscrowResponse:
    """Read the escrow on Tazapay / Escrow.com now (the watcher does it every ``ESCROW_POLL_MINUTES``)."""
    try:
        return EscrowResponse.from_record(await poll_provider(escrow_id, source="poll"))
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/escrows/{escrow_id}/tranches/{tranche_id}/provider-confirmed", response_model=EscrowResponse)
async def api_provider_confirmed(
    escrow_id: str, tranche_id: str, req: ProviderReleaseConfirmation,
    user: AuthUser = Depends(require_portless),
) -> EscrowResponse:
    """Staff record a payout seen on the provider's dashboard (provider reference
    + reason, audited on the escrow). Only for a stage already SUBMITTED."""
    try:
        return await confirm_release_by_staff(
            escrow_id, tranche_id, provider_ref=req.provider_ref, reason=req.reason,
            confirmed_by=user.email or user.user_id,
        )
    except HoldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/proof/{escrow_id}/{tranche_id}", response_class=HTMLResponse)
async def api_proof_sheet(escrow_id: str, tranche_id: str, exp: str | None = None,
                          sig: str | None = None) -> HTMLResponse:
    """The tranche's proof sheet for the escrow provider — only with a valid, unexpired signature."""
    from shared.orchestration import escrow_proof
    if not escrow_proof.verify(escrow_id, tranche_id, exp, sig):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid or expired proof link")
    page = await escrow_proof.render(escrow_id, tranche_id)
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such escrow stage")
    return HTMLResponse(page)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict[str, str]:
    return {"service": "escrow-guard", "status": "ok"}
