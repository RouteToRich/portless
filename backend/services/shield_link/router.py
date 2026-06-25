"""Shield-Link HTTP router.

Routes:
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

from .schemas import (
    ChaFilingRequest,
    DealOrdersResponse,
    FreightBookingRequest,
    FreightMilestonePayload,
    QcBookingRequest,
    QcWebhookPayload,
    ThirdPartyOrderResponse,
)
from .service import (
    ConnectorCallError,
    DealNotFoundError,
    InvalidDealState,
    TpoNotFoundError,
    WebhookVerifyError,
    book_freight,
    book_qc,
    file_cha,
    get_tpo,
    handle_freight_milestone,
    handle_qc_result,
    list_deal_orders,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shield-link", tags=["shield-link"])


# ---------------------------------------------------------------------------
# QC
# ---------------------------------------------------------------------------

@router.post("/qc", response_model=ThirdPartyOrderResponse, status_code=status.HTTP_201_CREATED)
async def api_book_qc(req: QcBookingRequest) -> ThirdPartyOrderResponse:
    """Book a QC inspection for a deal in PO_ISSUED state."""
    try:
        return await book_qc(req)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/qc/webhook")
async def api_qc_webhook(request: Request) -> dict[str, Any]:
    """Receive QC result from provider webhook (SGS/Intertek).

    Validates HMAC signature on x-sgs-signature header.
    PASS → QC_PASSED; FAIL → QC_FAILED.
    """
    raw_body = await request.body()
    payload_data = await request.json()
    headers = dict(request.headers)
    ext_ref = payload_data.get("ext_ref", "")
    try:
        payload = QcWebhookPayload(**payload_data)
        return await handle_qc_result(
            ext_ref=ext_ref,
            payload=payload,
            raw_headers=headers,
            raw_body=raw_body,
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


@router.post("/freight/webhook")
async def api_freight_webhook(request: Request) -> dict[str, Any]:
    """Receive freight milestone from provider (Cogoport/Freightos).

    Milestones: DEPARTED → IN_TRANSIT → ARRIVED → CLEARED.
    ARRIVED/CLEARED trigger deal → CLEARED.
    """
    raw_body = await request.body()
    payload_data = await request.json()
    headers = dict(request.headers)
    ext_ref = payload_data.get("ext_ref", "")
    try:
        payload = FreightMilestonePayload(**payload_data)
        return await handle_freight_milestone(
            ext_ref=ext_ref,
            payload=payload,
            raw_headers=headers,
            raw_body=raw_body,
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
