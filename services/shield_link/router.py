"""Shield-Link HTTP router.

Routes:
  GET  /shield-link/qc/providers    Supported QC provider catalog (buyer-facing)
  POST /shield-link/qc/quote        Quote QC inspection cost for a provider
  POST /shield-link/qc              Book QC inspection (PO_ISSUED → SUPPLIER_DISPATCH_READY)
  POST /shield-link/qc/webhook      QC provider status / result webhook
  GET  /shield-link/deals/{id}/qc   Inspection status, date, result, reports, supplier readiness
  POST /shield-link/deals/{id}/qc/poll       Poll the API provider (QIMA) now
  POST /shield-link/deals/{id}/qc-readiness  Supplier's goods-ready date (staff) → books the inspection
  POST /shield-link/qc/{ref}/status          Operator relay of a portal provider's status / result (staff)
  POST /shield-link/qc/{tpo_id}/reschedule   Cancel + rebook on a new date (staff)
  GET  /shield-link/qc/{tpo_id}/documents/{i} Kept inspection report
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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from shared.auth.dependencies import AuthUser, require_portless
from shared.webhooks import parse_webhook_body, signature_required

from . import qc as qc_lifecycle
from .schemas import (
    ChaFilingRequest,
    DealOrdersResponse,
    DealQcResponse,
    QcReadinessRequest,
    QcRescheduleRequest,
    QcStatusUpdateRequest,
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
        raise HTTPException(422, detail=str(exc))
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
        raise HTTPException(422, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(422, detail=str(exc))
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
    except InvalidDealState as exc:
        raise HTTPException(422, detail=str(exc))
    except Exception as exc:
        logger.error("QC webhook processing error: %s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Webhook processing failed")


# QC lifecycle — status, readiness, reports, reschedule
# (aliases so tests and the route table can patch them on this module)
get_deal_qc = qc_lifecycle.get_deal_qc
poll_qc = qc_lifecycle.poll_qc
record_qc_status = qc_lifecycle.record_qc_status
get_qc_document = qc_lifecycle.get_qc_document
reschedule_qc = qc_lifecycle.reschedule


@router.get("/deals/{deal_id}/qc", response_model=DealQcResponse)
async def api_deal_qc(deal_id: str) -> DealQcResponse:
    """The deal's inspection: provider status, scheduled date, result, defects,
    kept reports, the supplier's goods-ready date and the next step."""
    return await get_deal_qc(deal_id)


@router.post("/deals/{deal_id}/qc/poll")
async def api_poll_qc(deal_id: str) -> dict[str, Any]:
    """Ask the API provider (QIMA) for the order status now; a report found is applied."""
    return await poll_qc(deal_id, force=True)


@router.post("/qc/{ref}/status")
async def api_record_qc_status(
    ref: str, req: QcStatusUpdateRequest, user: AuthUser = Depends(require_portless),
) -> dict[str, Any]:
    """Relay what the provider's portal or e-mail says (SGS SMART, BV OneSource
    Connect have no API): CONFIRMED date, INSPECTED, REPORT_ISSUED with PASS /
    FAIL (moves the deal like the webhook), CANCELLED. ``ref`` = TPO id or the
    booking reference."""
    req = req.model_copy(update={"source": f"operator:{user.email or user.user_id}"})
    try:
        out = await record_qc_status(ref, req)
        from shared.audit_log import staff_relay
        await staff_relay(user, "qc.status_relayed", out.get("deal_id") if isinstance(out, dict) else ref,
                          ref=ref, update=req)
        return out
    except TpoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(422, detail=str(exc))


@router.get("/qc/{tpo_id}/documents/{index}")
async def api_qc_document(tpo_id: str, index: int) -> Response:
    """A kept inspection report (PDF), streamed from Drive / disk."""
    try:
        data, mime, name = await get_qc_document(tpo_id, index)
    except TpoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    return Response(data, media_type=mime, headers={"content-disposition": f'inline; filename="{name}"'})


@router.post("/qc/{tpo_id}/reschedule", response_model=ThirdPartyOrderResponse, status_code=status.HTTP_201_CREATED)
async def api_reschedule_qc(
    tpo_id: str, req: QcRescheduleRequest, user: AuthUser = Depends(require_portless),
) -> ThirdPartyOrderResponse:
    """Move the inspection to a new date: cancelled at the provider (QIMA by
    API), booked again. The deal stays at SUPPLIER_DISPATCH_READY."""
    try:
        out = await reschedule_qc(tpo_id, req)
        from shared.audit_log import staff_relay
        await staff_relay(user, "qc.rescheduled", getattr(out, "deal_id", None), tpo_id=tpo_id, request=req)
        return out
    except TpoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(422, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/deals/{deal_id}/qc-readiness")
async def api_qc_readiness(
    deal_id: str, req: QcReadinessRequest, user: AuthUser = Depends(require_portless),
) -> dict[str, Any]:
    """The supplier's goods-ready date (heard by phone or corrected): recorded,
    and the inspection is booked for it when the deal is at PO_ISSUED."""
    from shared.orchestration import qc_schedule, supplier_commitment
    ready = qc_schedule.to_date(req.ready_date)
    if ready is None:
        raise HTTPException(422, detail="ready_date must be an ISO date (YYYY-MM-DD)")
    try:
        out = await supplier_commitment.record_qc_readiness(
            deal_id, ready, by=f"operator:{user.email or user.user_id}", produced_pct=req.produced_pct,
            packed_pct=req.packed_pct, note=req.note,
        )
        from shared.audit_log import staff_relay
        await staff_relay(user, "supplier.ready_date_relayed", deal_id, request=req)
        return out
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(422, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


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
        raise HTTPException(422, detail=str(exc))
    except ConnectorCallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc))


class ManualCustomsRequest(BaseModel):
    shipping_bill_number: str = Field(min_length=3, max_length=40)
    shipping_bill_date: str = Field(description="YYYY-MM-DD")
    let_export_order_date: str | None = None
    port: str | None = None
    evidence: str | None = Field(default=None, max_length=300)
    """Where the copy is (Drive link, e-mail from the CHA)."""


@router.post("/deals/{deal_id}/customs/manual", response_model=ThirdPartyOrderResponse, status_code=201)
async def api_manual_customs(deal_id: str, req: ManualCustomsRequest,
                             user: AuthUser = Depends(require_portless)) -> ThirdPartyOrderResponse:
    """Record a Shipping Bill the customs broker filed by hand (ICEGATE not connected)."""
    from datetime import date as _date
    try:
        _date.fromisoformat(req.shipping_bill_date)
    except ValueError:
        raise HTTPException(422, detail="shipping_bill_date must be YYYY-MM-DD")
    from .service import record_manual_customs
    try:
        out = await record_manual_customs(deal_id, shipping_bill_number=req.shipping_bill_number,
                                          shipping_bill_date=req.shipping_bill_date,
                                          let_export_order_date=req.let_export_order_date, port=req.port,
                                          by=user.email or user.user_id, evidence=req.evidence)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDealState as exc:
        raise HTTPException(422, detail=str(exc))
    from shared.audit_log import staff_relay
    await staff_relay(user, "customs.manual_filing_recorded", deal_id, request=req)
    return out


@router.post("/freight/{ref}/milestone")
async def api_record_milestone(ref: str, req: RecordMilestoneRequest,
                               user: AuthUser = Depends(require_portless)) -> dict[str, Any]:
    """Record a carrier milestone by TPO id or carrier reference (staff relay without a
    signed webhook). DELIVERED opens the buyer's acceptance window on the escrow — so
    the person and the milestone go into the audit log."""
    try:
        out = await record_milestone(ref, req)
        from shared.audit_log import staff_relay
        await staff_relay(user, "freight.milestone_relayed", out.get("deal_id") if isinstance(out, dict) else ref,
                          ref=ref, milestone=req)
        return out
    except TpoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/deals/{deal_id}/loading")
async def api_record_loading(deal_id: str, req: LoadingRecordRequest,
                             user: AuthUser = Depends(require_portless)) -> dict[str, Any]:
    """Supervised loading record from the inspector (container + seal numbers,
    loaded quantity, HS code, photos). Freight and the QC-stage tranche are
    gated only once this matches the PI / PO — recorded in the audit log."""
    try:
        out = await record_loading(deal_id, req)
        from shared.audit_log import staff_relay
        await staff_relay(user, "shipment.loading_relayed", deal_id, loading=req)
        return out
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/deals/{deal_id}/track")
async def api_poll_tracking(deal_id: str) -> dict[str, Any]:
    """Read the deal's shipment now: the carrier's own API (Maersk booking, events,
    B/L) or, for a forwarder booking, the carrier-tracking actor."""
    return await poll_tracking(deal_id)


@router.get("/deals/{deal_id}/shipment")
async def api_shipment(deal_id: str) -> dict[str, Any]:
    """Booking, containers, B/L / VGM state and the container event timeline."""
    from . import freight
    return await freight.shipment_view(deal_id)


@router.get("/deals/{deal_id}/demurrage")
async def api_demurrage(deal_id: str) -> dict[str, Any]:
    """Demurrage (container at the port) and detention (empty not returned) clocks against the free days."""
    from shared.orchestration import demurrage
    return await demurrage.status(deal_id)


class EmptyReturnedRequest(BaseModel):
    returned_at: str | None = None
    evidence: str | None = None


@router.post("/deals/{deal_id}/empty-returned")
async def api_empty_returned(deal_id: str, req: EmptyReturnedRequest,
                             user: AuthUser = Depends(require_portless)) -> dict[str, Any]:
    """Staff: the buyer returned the empty container to the carrier's depot (detention stops) — audited."""
    from datetime import datetime, timezone

    from shared.audit_log import staff_relay

    from .repository import get_repository as _repo
    orders = [o for o in await _repo().list_by_deal(deal_id) if o.order_type == "FREIGHT"]
    if not orders:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"deal {deal_id} has no freight booking")
    try:
        at = datetime.fromisoformat(req.returned_at) if req.returned_at else datetime.now(timezone.utc)
    except ValueError as exc:
        raise HTTPException(422, detail="returned_at must be an ISO date / time") from exc
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    tpo = orders[-1]
    await _repo().save(tpo.model_copy(update={"empty_returned_at": at, "updated_at": datetime.now(timezone.utc)}))
    await staff_relay(user, "shipment.empty_returned", deal_id, tpo_id=tpo.tpo_id, returned_at=at.isoformat(),
                      evidence=req.evidence)
    return {"deal_id": deal_id, "tpo_id": tpo.tpo_id, "empty_returned_at": at.isoformat()}


@router.post("/carrier/maersk/webhook")
async def api_maersk_webhook(request: Request) -> dict[str, Any]:
    """Maersk booking-status webhook. Register ``…/carrier/maersk/webhook?token=<MAERSK_WEBHOOK_SECRET>``
    (Maersk publishes no signature); the booking is always re-read before anything changes."""
    import json as _json

    from shared.connectors.maersk import MaerskConnector
    from shared.webhooks import signature_valid

    from . import freight
    from .repository import get_repository as _repo
    raw_body = await request.body()
    headers = dict(request.headers)
    token = request.query_params.get("token")
    if token:
        headers["x-portless-webhook-token"] = token
    if not await signature_valid(MaerskConnector(), headers, raw_body, require_signature=signature_required(request)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Maersk webhook token invalid")
    try:
        body = _json.loads(raw_body or b"{}")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Webhook body is not valid JSON") from exc
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    ref = (data.get("carrierBookingReference") or data.get("carrierBookingRequestReference")
           or (data.get("booking") or {}).get("carrierBookingReference"))
    tpo = await _repo().get_by_ext_ref(str(ref)) if ref else None
    if tpo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no booking {ref}")
    return await freight.sync_shipment(tpo, source="webhook")


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
        raise HTTPException(422, detail=str(exc))
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
