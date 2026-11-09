"""Shield-Link HTTP router.

Routes:
  GET  /shield-link/qc/providers    Supported QC provider catalog (buyer-facing)
  POST /shield-link/qc/quote        Quote QC inspection cost for a provider
  POST /shield-link/qc              Book QC inspection (PO_ISSUED → SUPPLIER_DISPATCH_READY)
  POST /shield-link/qc/webhook      QC provider result webhook
  POST /shield-link/freight         Book freight (QC_PASSED → IN_TRANSIT)
  POST /shield-link/freight/webhook Freight milestone webhook
  POST /shield-link/cha             Submit CHA customs filing
  GET  /shield-link/tpo/{tpo_id}    Retrieve a single third-party order
  GET  /shield-link/deals/{deal_id}/orders  All TPOs for a deal
  GET  /shield-link/health
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from shared.webhooks import parse_webhook_body, signature_required

from .schemas import (
    ChaFilingRequest,
    DealOrdersResponse,
    FreightBookingRequest,
    FreightMilestonePayload,
    FreightQuoteRequest,
    FreightQuoteResponse,
    LoadingRecordRequest,
    QcBookingRequest,
    QcProvidersResponse,
    QcQuoteRequest,
    QcQuoteResponse,
    QcWebhookPayload,
    RecordMilestoneRequest,
    ThirdPartyOrderResponse,
)
from .service import (
    ConnectorCallError,
    DealNotFoundError,
    InvalidDealState,
    TpoNotFoundError,
    UnknownQcProviderError,
    WebhookVerifyError,
    book_freight,
    book_qc,
    file_cha,
    get_tpo,
    handle_freight_milestone,
    handle_qc_result,
    list_deal_orders,
    list_qc_providers,
    poll_tracking,
    quote_freight,
    quote_qc,
    record_loading,
    record_milestone,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shield-link", tags=["shield-link"])


# ---------------------------------------------------------------------------
# QC
# ---------------------------------------------------------------------------

@router.get("/qc/providers", response_model=QcProvidersResponse)
async def api_qc_providers() -> QcProvidersResponse:
    """Supported QC inspection providers — buyer-facing catalog with
    acceptance, evidence (photos/video), India availability, indicative fee."""
    return list_qc_providers()


@router.post("/qc/quote", response_model=QcQuoteResponse)
async def api_quote_qc(req: QcQuoteRequest) -> QcQuoteResponse:
    """Quote the QC inspection cost for a provider (no order is created)."""
    try:
        return await quote_qc(req)
    except UnknownQcProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/qc", response_model=ThirdPartyOrderResponse, status_code=status.HTTP_201_CREATED)
async def api_book_qc(req: QcBookingRequest) -> ThirdPartyOrderResponse:
    """Book a QC inspection for a deal in PO_ISSUED state."""
    try:
        return await book_qc(req)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except UnknownQcProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/qc/webhook")
async def api_qc_webhook(request: Request) -> dict[str, Any]:
    """Receive QC result from provider webhook.

    Validates the HMAC signature of the TPO's booked provider (x-sgs-signature,
    x-qima-signature, x-bv-signature, x-intertek-signature, x-tuvsud-signature).
    PASS → QC_PASSED; FAIL → QC_FAILED.
    """
    raw_body = await request.body()
    payload = parse_webhook_body(raw_body, QcWebhookPayload)
    headers = dict(request.headers)
    try:
        return await handle_qc_result(
            ext_ref=payload.ext_ref,
            payload=payload,
            raw_headers=headers,
            raw_body=raw_body,
            require_signature=signature_required(request),
        )
    except WebhookVerifyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except TpoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error("QC webhook processing error: %s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Webhook processing failed")


# ---------------------------------------------------------------------------
# Freight
# ---------------------------------------------------------------------------

@router.post("/freight/quote", response_model=FreightQuoteResponse)
async def api_quote_freight(req: FreightQuoteRequest) -> FreightQuoteResponse:
    """Quote the freight cost for a lane (no booking created). Used at
    negotiation start to price ocean freight into the CIF floor."""
    try:
        return await quote_freight(req)
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/freight", response_model=ThirdPartyOrderResponse, status_code=status.HTTP_201_CREATED)
async def api_book_freight(req: FreightBookingRequest) -> ThirdPartyOrderResponse:
    """Book a freight container for a deal in QC_PASSED state."""
    try:
        return await book_freight(req)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/freight/{ref}/milestone")
async def api_record_milestone(ref: str, req: RecordMilestoneRequest) -> dict[str, Any]:
    """Record a carrier milestone by TPO id or carrier reference (operator /
    tracking integration without a signed webhook). DELIVERED opens the
    buyer's acceptance window on the escrow."""
    try:
        return await record_milestone(ref, req)
    except TpoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/deals/{deal_id}/loading")
async def api_record_loading(deal_id: str, req: LoadingRecordRequest) -> dict[str, Any]:
    """Supervised loading record from the inspector (container + seal numbers,
    loaded quantity, HS code, photos). Freight and the QC-stage tranche are
    gated only once this matches the PI / PO."""
    try:
        return await record_loading(deal_id, req)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/deals/{deal_id}/track")
async def api_poll_tracking(deal_id: str) -> dict[str, Any]:
    """Poll the carrier-tracking actor for the deal's container now."""
    return await poll_tracking(deal_id)


@router.post("/freight/webhook")
async def api_freight_webhook(request: Request) -> dict[str, Any]:
    """Receive freight milestone from provider (Cogoport/Freightos).

    Milestones: DEPARTED → IN_TRANSIT → ARRIVED → CLEARED.
    ARRIVED/CLEARED trigger deal → CLEARED.
    """
    raw_body = await request.body()
    payload = parse_webhook_body(raw_body, FreightMilestonePayload)
    headers = dict(request.headers)
    try:
        return await handle_freight_milestone(
            ext_ref=payload.ext_ref,
            payload=payload,
            raw_headers=headers,
            raw_body=raw_body,
            require_signature=signature_required(request),
        )
    except WebhookVerifyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except TpoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error("Freight webhook processing error: %s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Webhook processing failed")


# ---------------------------------------------------------------------------
# CHA
# ---------------------------------------------------------------------------

@router.post("/cha", response_model=ThirdPartyOrderResponse, status_code=status.HTTP_201_CREATED)
async def api_file_cha(req: ChaFilingRequest) -> ThirdPartyOrderResponse:
    """Submit customs documents to ICEGATE via registered CHA."""
    try:
        return await file_cha(req)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@router.get("/tpo/{tpo_id}", response_model=ThirdPartyOrderResponse)
async def api_get_tpo(tpo_id: str) -> ThirdPartyOrderResponse:
    try:
        return await get_tpo(tpo_id)
    except TpoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/deals/{deal_id}/orders", response_model=DealOrdersResponse)
async def api_list_deal_orders(deal_id: str) -> DealOrdersResponse:
    return await list_deal_orders(deal_id)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict[str, str]:
    return {"service": "shield-link", "status": "ok"}
