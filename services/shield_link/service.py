"""Shield-Link service — business logic.

Orchestrates third-party physical-inspection and logistics providers:

  book_qc()              PO_ISSUED → SUPPLIER_DISPATCH_READY
                         Books SGS/Intertek inspection; emits qc.booked.

  handle_qc_result()     SUPPLIER_DISPATCH_READY → QC_PASSED | QC_FAILED
                         Inbound provider webhook; transitions deal; emits
                         qc.passed or qc.failed.

  book_freight()         QC_PASSED → IN_TRANSIT
                         Books Cogoport/Freightos container; emits freight.booked.

  handle_freight_milestone()
                         Updates TPO on each milestone; transitions deal →
                         CLEARED when milestone == "ARRIVED"/"CLEARED".
                         Emits freight.milestone.

  file_cha()             Submits customs docs to ICEGATE via registered CHA.
                         Emits customs.filed (complementary to DocuGen).

All mutating paths:
  • Idempotency key deduplication
  • Deal state validation via can_transition()
  • Connector call with graceful stub fallback (no API key → stub response)
  • Persist ThirdPartyOrderRecord
  • Transition deal status
  • Emit domain event

Exceptions
----------
DealNotFoundError     404 — unknown deal_id
InvalidDealState      422 — deal not in required state
TpoNotFoundError      404 — unknown tpo_id
ConnectorCallError    502 — provider returned error
WebhookVerifyError    401 — webhook signature invalid
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from shared.bus import get_publisher
from shared.connectors.qc_providers import (
    QC_PROVIDER_CATALOG,
    cheapest_qc_provider,
    get_qc_connector,
    resolve_qc_provider,
)
from shared.connectors.shields import (
    ConnectorError,
    get_freight_connector,
)
from shared.domain.events import DomainEvent, EventTopic
from shared.domain.state_machine import can_transition
from shared.webhooks import signature_valid

from .config import settings
from .repository import get_repository
from .schemas import (
    ChaFilingRequest,
    DealOrdersResponse,
    FreightBookingRequest,
    FreightMilestonePayload,
    FreightQuoteRequest,
    FreightQuoteResponse,
    LoadingRecordRequest,
    QcBookingRequest,
    QcProviderInfo,
    QcProvidersResponse,
    QcQuoteRequest,
    QcQuoteResponse,
    QcWebhookPayload,
    RecordMilestoneRequest,
    ThirdPartyOrderRecord,
    ThirdPartyOrderResponse,
)

logger = logging.getLogger(__name__)

_MILESTONE_ORDER = ["DEPARTED", "IN_TRANSIT", "ARRIVED", "CLEARED", "DELIVERED"]


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DealNotFoundError(Exception):
    def __init__(self, deal_id: str) -> None:
        super().__init__(f"Deal {deal_id!r} not found")
        self.deal_id = deal_id


class InvalidDealState(Exception):
    def __init__(self, deal_id: str, current: str, required: str) -> None:
        super().__init__(f"Deal {deal_id!r} is {current!r}; required {required!r}")
        self.deal_id = deal_id
        self.current = current
        self.required = required


class TpoNotFoundError(Exception):
    def __init__(self, tpo_id: str) -> None:
        super().__init__(f"Third-party order {tpo_id!r} not found")


class ConnectorCallError(Exception):
    """Provider returned a non-retryable error."""


class WebhookVerifyError(Exception):
    """Inbound webhook signature verification failed."""


class UnknownQcProviderError(Exception):
    def __init__(self, provider: str) -> None:
        supported = ", ".join(sorted(QC_PROVIDER_CATALOG))
        super().__init__(f"Unknown QC provider {provider!r}; supported: {supported}")
        self.provider = provider


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------

def _new_tpo_id(order_type: str) -> str:
    return f"TPO-{order_type}-{uuid4().hex[:10].upper()}"


# ---------------------------------------------------------------------------
# Deal bridge
# ---------------------------------------------------------------------------

CUSTOMS_FILING_STATUSES = frozenset({"QC_PASSED", "IN_TRANSIT", "CLEARED"})


async def _assert_platform_running(what: str) -> None:
    from shared.orchestration import kill_switch
    await kill_switch.assert_running(what)


async def _fetch_deal(deal_id: str) -> Any:
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        return await get_deal(deal_id)
    except Exception as exc:
        if "not found" in str(exc).lower():
            raise DealNotFoundError(deal_id) from exc
        raise


async def _transition_deal(deal_id: str, target: str, reason: str) -> None:
    try:
        from services.deals.service import update_status  # type: ignore[import]
        from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
        from shared.domain.enums import DealStatus
        await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus(target), reason=reason))
    except Exception as exc:
        logger.error("ShieldLink: deal transition %s→%s failed: %s", deal_id, target, exc)
        raise


# ---------------------------------------------------------------------------
# Event helper
# ---------------------------------------------------------------------------

async def _emit(topic: EventTopic, deal_id: str, payload: dict) -> None:
    try:
        await get_publisher().publish(DomainEvent(topic=topic, deal_id=deal_id, payload=payload))
    except Exception as exc:
        logger.warning("ShieldLink: emit %s failed: %s", topic, exc)


# ---------------------------------------------------------------------------
# QC provider catalog + quoting
# ---------------------------------------------------------------------------

def list_qc_providers() -> QcProvidersResponse:
    """Supported QC inspection providers — the buyer-facing catalog."""
    return QcProvidersResponse(
        default_provider=settings.default_qc_provider,
        providers=[QcProviderInfo(**meta) for meta in QC_PROVIDER_CATALOG.values()],
    )


async def quote_qc(req: QcQuoteRequest) -> QcQuoteResponse:
    """Quote the inspection cost for one provider (read-only, no TPO written).

    Called before/at negotiation start so the QC cost of the buyer's opted
    provider is baked into the CIF price floor.
    """
    provider = resolve_qc_provider(req.provider) if req.provider else settings.default_qc_provider
    if provider is None or provider not in QC_PROVIDER_CATALOG:
        raise UnknownQcProviderError(str(req.provider))

    connector = get_qc_connector(provider)
    try:
        result = await connector.quote({
            "deal_id": req.deal_id or "",
            "location": req.location or "",
            "hs_code": req.hs_code or "",
            "quantity_mt": req.quantity_mt or 0.0,
        })
    except ConnectorError as exc:
        raise ConnectorCallError(f"QC quote failed for provider {provider}: {exc}") from exc

    meta = QC_PROVIDER_CATALOG[provider]
    fee = float(result.get("fee_usd") or meta["indicative_fee_usd"])
    per_unit = result.get("qc_cost_per_unit_usd")
    if per_unit is None and req.quantity_mt:
        per_unit = round(fee / req.quantity_mt, 4)
    return QcQuoteResponse(
        provider=provider,
        display_name=meta["display_name"],
        available=bool(result.get("available", True)),
        inspection_date=result.get("inspection_date"),
        fee_usd=fee,
        qc_cost_per_unit_usd=per_unit,
        currency=str(result.get("currency", "USD")),
        evidence=result.get("evidence") or meta["evidence"],
        stub=bool(result.get("stub", False)),
    )


async def _buyer_opted_qc_provider(deal_id: str) -> str | None:
    """Buyer's opted QC provider captured during negotiation, if any."""
    try:
        from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
        session = await _neg_repo().get_session_by_deal(deal_id)
        return resolve_qc_provider(getattr(session, "qc_provider", None)) if session else None
    except Exception as exc:
        logger.debug("ShieldLink: could not read negotiation qc_provider for %s: %s", deal_id, exc)
        return None


# ---------------------------------------------------------------------------
# QC booking
# ---------------------------------------------------------------------------

def _supplier_facts(deal: Any) -> dict[str, Any]:
    """The inspection site: the supplier party of the deal."""
    for p in getattr(deal, "parties", None) or []:
        role = getattr(p, "role", "")
        if (getattr(role, "value", role) or "").lower() == "supplier":
            address = getattr(p, "address", None) or ""
            parts = [s.strip() for s in address.split(",") if s.strip()]
            pin = next(iter(re.findall(r"\b\d{6}\b", address)), None)
            return {
                "name": getattr(p, "legal_name", None), "contact_name": getattr(p, "legal_name", None),
                "email": getattr(p, "email", None), "phone": getattr(p, "phone", None), "address": address or None,
                "city": parts[-2] if len(parts) >= 2 else None, "postcode": pin,
                "country_code": (getattr(p, "country", None) or "IN").upper()[:2],
            }
    return {"country_code": "IN"}


async def _po_number(deal_id: str) -> str | None:
    try:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        po = await _dm_repo().get_po_by_deal(deal_id)
        return po.po_id if po else None
    except Exception:  # noqa: BLE001
        return None


def _product_name(hs_code: str | None) -> str:
    try:
        from shared.documents.facts import product_name
        return product_name(hs_code or "") or f"HS {hs_code}"
    except Exception:  # noqa: BLE001
        return f"HS {hs_code}"


async def _email_booking_desk(connector: Any, tpo_id: str, deal_id: str, booking: dict[str, Any]) -> dict[str, Any]:
    """Portal providers: the booking request goes to the provider's booking desk
    (``<PREFIX>_BOOKING_EMAIL``). Without one an operator enters it on the portal."""
    to = getattr(connector, "booking_email", "")
    if not to:
        return {"outcome": "operator", "detail": f"no {connector.env_prefix}_BOOKING_EMAIL — enter the request on {connector.portal_name}"}
    site = booking.get("inspection_site") or {}
    product = booking.get("product") or {}
    sampling = booking.get("sampling") or {}
    body = (
        f"Dear {connector.display_name} booking team,\n\n"
        f"Please book the inspection below and confirm the date and your job number by reply, quoting "
        f"our reference {booking['portless_ref']}.\n\n"
        f"  Our reference      : {booking['portless_ref']} (deal {deal_id}, PO {booking.get('po_number') or '-'})\n"
        f"  Services           : {', '.join(booking.get('services') or [])}\n"
        f"  Inspection date    : {booking.get('inspection_date')}\n"
        f"  Goods ready        : {booking.get('goods_ready_date') or booking.get('inspection_date')}\n"
        f"  Product            : {product.get('description')} (HS {product.get('hs_code')}), {product.get('quantity_mt')} MT\n"
        f"  Inspection site    : {site.get('company')}, {site.get('address') or '-'}, {site.get('country') or ''}\n"
        f"  Site contact       : {site.get('contact') or '-'} {site.get('phone') or ''} {site.get('email') or ''}\n"
        f"  Sampling           : {sampling.get('standard')} level {sampling.get('level')}, AQL critical "
        f"{sampling.get('aql_critical')} / major {sampling.get('aql_major')} / minor {sampling.get('aql_minor')}\n"
        f"  Readiness          : 100% produced, at least {(booking.get('readiness') or {}).get('packed_pct_min', 80)}% packed\n\n"
        "Please send the report (PDF with photos) and, for loading supervision, the container and seal numbers "
        "to this address.\n\nPortLess Trade Desk"
    )
    try:
        from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
        from services.email_gateway.sender import send_email  # type: ignore[import]
        res = await send_email(SendEmailRequest(
            to_address=to, subject=f"Inspection booking request {booking['portless_ref']} — {product.get('description')}",
            body=body, email_id=f"QC-{tpo_id}",
        ))
        return {"outcome": str(getattr(res.outcome, "value", res.outcome)), "to": to, "message_id": res.message_id}
    except Exception as exc:  # noqa: BLE001 — the booking is recorded; an operator re-sends it
        logger.error("ShieldLink: booking request e-mail to %s failed (deal %s): %s", to, deal_id, exc)
        return {"outcome": "failed", "to": to, "detail": str(exc)}


async def book_qc(req: QcBookingRequest, *, reinspection: bool = False,
                  previous: ThirdPartyOrderRecord | None = None) -> ThirdPartyOrderResponse:
    """Book a QC inspection with the configured provider.

    Pre-condition: deal must be in ``PO_ISSUED`` state.
    Post: deal → ``SUPPLIER_DISPATCH_READY``, emits ``qc.booked``.

    The inspection day is the supplier's goods-ready date (``goods_ready_date``)
    or an explicit ``inspection_date``, never earlier than the agency's lead
    time (``shared.orchestration.qc_schedule``).

    ``reinspection=True`` (QC_FAILED playbook, or a reschedule): the deal
    already sits in ``SUPPLIER_DISPATCH_READY`` — a fresh booking is made
    without a deal transition.
    """
    await _assert_platform_running(f"QC booking for deal {req.deal_id}")
    repo = get_repository()

    if req.idempotency_key:
        existing = await repo.get_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return ThirdPartyOrderResponse.from_record(existing)

    deal = await _fetch_deal(req.deal_id)
    deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    from shared.domain.enums import DealStatus
    if reinspection:
        if deal_status != "SUPPLIER_DISPATCH_READY":
            raise InvalidDealState(req.deal_id, deal_status, "SUPPLIER_DISPATCH_READY")
    else:
        if deal_status != "PO_ISSUED":
            raise InvalidDealState(req.deal_id, deal_status, "PO_ISSUED")
        if not can_transition(DealStatus(deal_status), DealStatus.SUPPLIER_DISPATCH_READY):
            raise InvalidDealState(req.deal_id, deal_status, "PO_ISSUED")

    # Provider resolution: explicit request → buyer-opted (from the deal's
    # negotiation session) → fallback strategy ("cheapest" catalog provider by
    # default — a buyer who never opted gets the most economical inspector;
    # "default" books settings.default_qc_provider instead).
    if req.provider:
        provider = resolve_qc_provider(req.provider)
        if provider is None:
            raise UnknownQcProviderError(req.provider)
    else:
        provider = await _buyer_opted_qc_provider(req.deal_id)
        if provider is None:
            provider = (
                cheapest_qc_provider()
                if settings.qc_fallback_strategy == "cheapest"
                else settings.default_qc_provider
            )
    from shared.orchestration import qc_schedule
    now = datetime.now(timezone.utc)
    tpo_id = _new_tpo_id("QC")
    requested = qc_schedule.to_date(req.inspection_date)
    earliest = qc_schedule.earliest_inspection_date()
    inspection = (qc_schedule.next_business_day(requested) if requested and requested >= earliest
                  else qc_schedule.inspection_date_for(req.goods_ready_date))
    hs_code = req.hs_code or getattr(deal, "hs_code", "")
    supplier = _supplier_facts(deal)
    connector = get_qc_connector(provider)

    # Create PENDING record first for crash-safety
    record = ThirdPartyOrderRecord(
        tpo_id=tpo_id,
        deal_id=req.deal_id,
        order_type="QC",
        provider=provider,
        status="PENDING",
        channel=connector.channel,
        pickup_ready_at=req.pickup_ready_at,
        goods_ready_date=req.goods_ready_date,
        inspection_date=inspection.isoformat(),
        idempotency_key=req.idempotency_key,
        created_at=now,
        updated_at=now,
    )
    await repo.save(record)

    # Call connector
    try:
        result = await connector.book({
            "deal_id": req.deal_id,
            "tpo_id": tpo_id,
            "po_number": await _po_number(req.deal_id),
            "location": req.location or supplier.get("address") or "",
            "pickup_ready_at": req.pickup_ready_at or "",
            "goods_ready_date": req.goods_ready_date,
            "inspection_date": inspection.isoformat(),
            "ship_date": qc_schedule.ship_date_for(inspection).isoformat(),
            "hs_code": hs_code,
            "product_name": _product_name(hs_code),
            "quantity_mt": req.quantity_mt or 0.0,
            "supplier": supplier,
            "min_packed_pct": qc_schedule.min_packed_pct(),
            "previous_order_number": previous.provider_order_number if previous else None,
            # Pre-shipment inspection AND supervision of the container
            # stuffing + sealing, so what was inspected is what ships.
            "services": _qc_services(),
        })
    except ConnectorError as exc:
        failed = record.model_copy(update={"status": "FAILED", "updated_at": datetime.now(timezone.utc)})
        await repo.save(failed)
        raise ConnectorCallError(f"QC booking failed for deal {req.deal_id}: {exc}") from exc

    provider_status = result.get("provider_status") or "REQUESTED"
    if connector.channel == "portal" and result.get("booking_request"):
        result["booking_email_outcome"] = await _email_booking_desk(connector, tpo_id, req.deal_id, result["booking_request"])
    updated = record.model_copy(update={
        "status": "BOOKED",
        "ext_ref": result.get("ext_ref"),
        "channel": result.get("channel") or connector.channel,
        "provider_order_number": result.get("provider_order_number"),
        "provider_status": provider_status,
        "provider_status_code": result.get("provider_status_code"),
        "provider_status_text": result.get("provider_status_text"),
        "inspection_date": result.get("inspection_date") or inspection.isoformat(),
        "provider_payload": result,
        "status_history": [{"at": datetime.now(timezone.utc).isoformat(), "status": provider_status, "source": "booking",
                            "inspection_date": result.get("inspection_date") or inspection.isoformat()}],
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(updated)

    if not reinspection:
        await _transition_deal(req.deal_id, "SUPPLIER_DISPATCH_READY", f"QC booked — ref {result.get('ext_ref')}")
    await _emit(EventTopic.QC_BOOKED, req.deal_id, {
        "tpo_id": tpo_id,
        "provider": provider,
        "ext_ref": result.get("ext_ref"),
        "provider_order_number": updated.provider_order_number,
        "channel": updated.channel,
        "inspection_date": updated.inspection_date,
        "goods_ready_date": req.goods_ready_date,
        "loading_supervision": "LOADING_SUPERVISION" in _qc_services(),
        "stub": result.get("stub", False),
        "reinspection": reinspection,
        "rescheduled_from": previous.tpo_id if previous else None,
    })

    logger.info("ShieldLink: QC booked %s for deal %s (ref=%s)", tpo_id, req.deal_id, result.get("ext_ref"))
    return ThirdPartyOrderResponse.from_record(updated)


# ---------------------------------------------------------------------------
# QC result webhook
# ---------------------------------------------------------------------------

async def handle_qc_result(
    ext_ref: str,
    payload: QcWebhookPayload,
    raw_headers: dict[str, str],
    raw_body: bytes,
    *,
    require_signature: bool = False,
) -> dict[str, str]:
    """Process a QC provider webhook: a status update (CONFIRMED date,
    INSPECTED …) and / or the result.

    Verifies the provider signature, then hands over to
    ``qc.apply_update``: PASS → QC_PASSED, FAIL → QC_FAILED (once), report
    files listed in ``documents`` are downloaded and kept.
    """
    repo = get_repository()

    # Locate the TPO by ext_ref
    tpo = await repo.get_by_ext_ref(ext_ref)
    if tpo is None:
        raise TpoNotFoundError(ext_ref)

    # Verify webhook signature
    connector = get_qc_connector(tpo.provider)
    if not await signature_valid(connector, raw_headers, raw_body, require_signature=require_signature):
        raise WebhookVerifyError(f"QC webhook signature invalid for ext_ref={ext_ref}")

    from . import qc
    if payload.result is None and payload.status is None:
        raise InvalidDealState(tpo.deal_id, "webhook without result or status", "result or status")
    return await qc.apply_update(
        tpo, source="webhook", status=payload.status, result=payload.result,
        provider_order_number=payload.provider_order_number, inspection_date=payload.inspection_date,
        report_url=payload.report_url, crf_hash=payload.crf_hash, notes=payload.notes,
        documents=[d.model_dump() for d in payload.documents], defects=payload.defects or None,
        loading=qc.loading_from_payload(payload),
    )


# ---------------------------------------------------------------------------
# Freight quoting (read-only — no booking, no TPO)
# ---------------------------------------------------------------------------

_MT_PER_CONTAINER = 25.0
"""Working assumption for a 40HC of typical bulk commodity — used only to
convert a per-container rate into a per-unit cost for pricing. Real container
plans come from the booked provider at freight time."""


async def quote_freight(req: FreightQuoteRequest) -> FreightQuoteResponse:
    """Quote the lane's freight cost for one provider (read-only).

    Called at negotiation start so the ocean-freight component of a CIF price
    is priced into the margin floor before the first offer.
    """
    import math

    provider = (req.provider or settings.default_freight_provider).lower()
    connector = get_freight_connector(provider)
    try:
        result = await connector.quote({
            "deal_id": req.deal_id or "",
            "origin_port": req.origin_port or "",
            "destination_port": req.destination_port or "",
            "container_type": req.container_type,
            "hs_code": req.hs_code or "",
            "quantity_mt": req.quantity_mt or 0.0,
            "incoterm": req.incoterm,
        })
    except ConnectorError as exc:
        raise ConnectorCallError(f"Freight quote failed for provider {provider}: {exc}") from exc

    rate = float(result.get("rate_usd") or 0.0)
    qty = float(req.quantity_mt or 0.0)
    containers = max(1, math.ceil(qty / _MT_PER_CONTAINER)) if qty > 0 else 1
    total = round(rate * containers, 2)
    per_unit = round(total / qty, 4) if qty > 0 else None
    return FreightQuoteResponse(
        provider=str(result.get("provider", provider)),
        origin=result.get("origin") or req.origin_port,
        destination=result.get("destination") or req.destination_port,
        container_type=str(result.get("container_type", req.container_type)),
        rate_usd_per_container=rate,
        containers=containers,
        total_freight_usd=total,
        logistics_cost_per_unit_usd=per_unit,
        transit_days=result.get("transit_days"),
        valid_until=result.get("valid_until"),
        stub=bool(result.get("stub", False)),
    )


# ---------------------------------------------------------------------------
# Freight booking
# ---------------------------------------------------------------------------

async def book_freight(req: FreightBookingRequest) -> ThirdPartyOrderResponse:
    """Book a freight container with the configured provider.

    Pre-condition: deal must be in ``QC_PASSED`` state.
    Post: deal → ``IN_TRANSIT``, emits ``freight.booked``.
    """
    await _assert_platform_running(f"Freight booking for deal {req.deal_id}")
    repo = get_repository()

    if req.idempotency_key:
        existing = await repo.get_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return ThirdPartyOrderResponse.from_record(existing)

    deal = await _fetch_deal(req.deal_id)
    deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    if deal_status != "QC_PASSED":
        raise InvalidDealState(req.deal_id, deal_status, "QC_PASSED")

    from shared.domain.enums import DealStatus
    if not can_transition(DealStatus(deal_status), DealStatus.IN_TRANSIT):
        raise InvalidDealState(req.deal_id, deal_status, "QC_PASSED")

    provider = req.provider or settings.default_freight_provider
    now = datetime.now(timezone.utc)
    tpo_id = _new_tpo_id("FRT")

    record = ThirdPartyOrderRecord(
        tpo_id=tpo_id,
        deal_id=req.deal_id,
        order_type="FREIGHT",
        provider=provider,
        status="PENDING",
        incoterm=req.incoterm,
        origin_port=req.origin_port,
        destination_port=req.destination_port,
        idempotency_key=req.idempotency_key,
        created_at=now,
        updated_at=now,
    )
    await repo.save(record)

    connector = get_freight_connector(provider)
    try:
        result = await connector.book({
            "deal_id": req.deal_id,
            "origin_port": req.origin_port or "",
            "destination_port": req.destination_port or "",
            "container_type": req.container_type,
            "hs_code": req.hs_code or getattr(deal, "hs_code", ""),
            "quantity_mt": req.quantity_mt or 0.0,
            "incoterm": req.incoterm,
        })
    except ConnectorError as exc:
        failed = record.model_copy(update={"status": "FAILED", "updated_at": datetime.now(timezone.utc)})
        await repo.save(failed)
        raise ConnectorCallError(f"Freight booking failed for deal {req.deal_id}: {exc}") from exc

    updated = record.model_copy(update={
        "status": "BOOKED",
        "ext_ref": result.get("ext_ref"),
        "vessel": result.get("vessel"),
        "voyage": result.get("voyage"),
        "container_number": result.get("container_number"),
        "etd": result.get("etd"),
        "eta": result.get("eta"),
        "provider_payload": result,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(updated)

    await _transition_deal(req.deal_id, "IN_TRANSIT", f"Freight booked — ref {result.get('ext_ref')}")
    await _emit(EventTopic.FREIGHT_BOOKED, req.deal_id, {
        "tpo_id": tpo_id,
        "provider": provider,
        "ext_ref": result.get("ext_ref"),
        "vessel": result.get("vessel"),
        "voyage": result.get("voyage"),
        "etd": result.get("etd"),
        "eta": result.get("eta"),
        "stub": result.get("stub", False),
    })

    logger.info("ShieldLink: freight booked %s for deal %s (ref=%s)", tpo_id, req.deal_id, result.get("ext_ref"))
    return ThirdPartyOrderResponse.from_record(updated)


# ---------------------------------------------------------------------------
# Freight milestone webhook
# ---------------------------------------------------------------------------

async def handle_freight_milestone(
    ext_ref: str,
    payload: FreightMilestonePayload,
    raw_headers: dict[str, str],
    raw_body: bytes,
    *,
    require_signature: bool = False,
) -> dict[str, str]:
    """Process freight milestone webhook (DEPARTED / IN_TRANSIT / ARRIVED /
    CLEARED / DELIVERED).

    Transitions deal → CLEARED when milestone is ARRIVED or CLEARED (or
    DELIVERED while the deal is still IN_TRANSIT). Emits freight.milestone for
    all milestones; DELIVERED opens the buyer's acceptance window (escrow).
    """
    repo = get_repository()

    tpo = await repo.get_by_ext_ref(ext_ref)
    if tpo is None:
        raise TpoNotFoundError(ext_ref)

    connector = get_freight_connector(tpo.provider)
    if not await signature_valid(connector, raw_headers, raw_body, require_signature=require_signature):
        raise WebhookVerifyError(f"Freight webhook signature invalid for ext_ref={ext_ref}")

    return await _apply_milestone(
        tpo, payload.milestone, location=payload.location, timestamp=payload.timestamp,
        notes=payload.notes, source="webhook",
        seal_numbers=payload.seal_numbers, container_number=payload.container_number,
    )


async def _apply_milestone(
    tpo: ThirdPartyOrderRecord,
    milestone: str,
    *,
    location: str | None = None,
    timestamp: str | None = None,
    notes: str | None = None,
    source: str = "webhook",
    seal_numbers: list[str] | None = None,
    container_number: str | None = None,
) -> dict[str, str]:
    """Record one carrier milestone on the freight order (webhook, tracking
    poll or operator) and move the deal: ARRIVED/CLEARED → CLEARED; DELIVERED
    → CLEARED if still at sea, then ``freight.milestone`` lets the escrow open
    the buyer's acceptance window."""
    repo = get_repository()
    now = datetime.now(timezone.utc)
    ext_ref = tpo.ext_ref or tpo.tpo_id
    prev = tpo.last_milestone
    advance = prev is None or _MILESTONE_ORDER.index(milestone) >= _MILESTONE_ORDER.index(prev)
    if not advance:
        logger.info("ShieldLink: milestone %s for deal %s ignored (already at %s)", milestone, tpo.deal_id, prev)
        return {"status": "ignored", "milestone": milestone, "deal_id": tpo.deal_id, "last_milestone": prev or ""}

    new_status = "IN_PROGRESS" if milestone in ("DEPARTED", "IN_TRANSIT") else "COMPLETED"
    updated = tpo.model_copy(update={
        "status": new_status,
        "last_milestone": milestone,
        "last_milestone_at": now,
        "last_milestone_source": source,
        "delivered_at": now if milestone == "DELIVERED" and tpo.delivered_at is None else tpo.delivered_at,
        "container_number": container_number or tpo.container_number,
        "updated_at": now,
    })
    await repo.save(updated)

    await _emit(EventTopic.FREIGHT_MILESTONE, tpo.deal_id, {
        "tpo_id": tpo.tpo_id,
        "ext_ref": ext_ref,
        "milestone": milestone,
        "location": location,
        "timestamp": timestamp,
        "notes": notes,
        "source": source,
        "delivered_at": updated.delivered_at.isoformat() if updated.delivered_at else None,
        "seal_numbers": list(seal_numbers or []),
        "container_number": container_number,
    })

    if milestone in ("ARRIVED", "CLEARED"):
        try:
            await _transition_deal(tpo.deal_id, "CLEARED", f"Cargo {milestone} — ref {ext_ref}")
            await _emit(EventTopic.CUSTOMS_CLEARED, tpo.deal_id, {
                "tpo_id": tpo.tpo_id,
                "ext_ref": ext_ref,
                "milestone": milestone,
            })
        except Exception as exc:
            logger.error("ShieldLink: CLEARED transition failed for deal %s: %s", tpo.deal_id, exc)
    elif milestone == "DELIVERED":
        # Delivery without an earlier ARRIVED/CLEARED event (tracking actors
        # often report only the latest state): clear the deal first.
        try:
            deal = await _fetch_deal(tpo.deal_id)
            deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
            if deal_status == "IN_TRANSIT":
                await _transition_deal(tpo.deal_id, "CLEARED", f"Cargo DELIVERED — ref {ext_ref}")
                await _emit(EventTopic.CUSTOMS_CLEARED, tpo.deal_id, {
                    "tpo_id": tpo.tpo_id, "ext_ref": ext_ref, "milestone": milestone,
                })
        except Exception as exc:
            logger.error("ShieldLink: CLEARED-on-delivery transition failed for deal %s: %s", tpo.deal_id, exc)

    logger.info("ShieldLink: milestone %s (%s) for deal %s", milestone, source, tpo.deal_id)
    return {"status": "ok", "milestone": milestone, "deal_id": tpo.deal_id}


async def record_milestone(ref: str, req: RecordMilestoneRequest) -> dict[str, str]:
    """Operator / tracking-poll milestone by TPO id or carrier reference (no
    provider signature — the caller is already authenticated)."""
    repo = get_repository()
    tpo = await repo.get(ref)
    if tpo is None:
        tpo = await repo.get_by_ext_ref(ref)
    if tpo is None or tpo.order_type != "FREIGHT":
        raise TpoNotFoundError(ref)
    return await _apply_milestone(
        tpo, req.milestone, location=req.location, timestamp=req.timestamp, notes=req.notes, source=req.source,
        seal_numbers=req.seal_numbers, container_number=req.container_number,
    )


def _qc_services() -> list[str]:
    from shared.orchestration.compliance import loading_check_required
    return ["PRE_SHIPMENT_INSPECTION"] + (["LOADING_SUPERVISION"] if loading_check_required() else [])


async def record_loading(deal_id: str, req: LoadingRecordRequest) -> dict[str, Any]:
    """Store the supervised-loading record (container, seal, loaded quantity)
    and emit ``shipment.loading_recorded`` — the shipment compliance check then
    decides whether the QC tranche and the freight booking go ahead."""
    from shared.orchestration import compliance
    await _fetch_deal(deal_id)
    rec = await compliance.record_loading(deal_id, compliance.LoadingRecord(
        container_numbers=req.container_numbers, seal_numbers=req.seal_numbers,
        loaded_quantity_mt=req.loaded_quantity_mt, hs_code=req.hs_code,
        goods_description=req.goods_description, inspector_ref=req.inspector_ref,
        photos=req.photos, source=req.source,
    ))
    await _emit(EventTopic.LOADING_RECORDED, deal_id, {
        "container_numbers": rec.loading.container_numbers if rec.loading else [],
        "seal_numbers": rec.loading.seal_numbers if rec.loading else [],
        "source": req.source,
    })
    logger.info("ShieldLink: loading recorded for deal %s (seals %s)", deal_id,
                rec.loading.seal_numbers if rec.loading else [])
    return {"status": "ok", "deal_id": deal_id,
            "loading": rec.loading.model_dump(mode="json") if rec.loading else None}


async def poll_tracking(deal_id: str) -> dict[str, Any]:
    """Ask the carrier-tracking actor where the deal's container is and record
    the latest milestone. Returns ``{"status": ...}`` — ``no_booking`` /
    ``no_reference`` / ``stub`` (actor not configured) / ``source_refused`` (the
    Apify plan cannot run the carrier Actors) / ``no_event`` / ``unchanged`` / ``ok``.
    Never raises: a tracking failure must not break the watcher sweep."""
    repo = get_repository()
    orders = await repo.list_by_deal(deal_id)
    tpo = next((o for o in orders if o.order_type == "FREIGHT" and o.status != "FAILED"), None)
    if tpo is None:
        return {"status": "no_booking", "deal_id": deal_id}
    if not (tpo.container_number or tpo.ext_ref):
        return {"status": "no_reference", "deal_id": deal_id, "tpo_id": tpo.tpo_id}
    try:
        from shared.connectors.registry import get_connector
        connector = get_connector("apify_carrier_tracking")
        result = await connector.quote({
            "container_number": tpo.container_number,
            "booking_ref": tpo.ext_ref,
            "carrier": tpo.provider_payload.get("carrier") or tpo.provider,
            "reference": deal_id,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("ShieldLink: tracking poll failed for deal %s: %s", deal_id, exc)
        return {"status": "error", "deal_id": deal_id, "error": str(exc)}
    if result.get("stub"):
        return {"status": "stub", "deal_id": deal_id, "tpo_id": tpo.tpo_id}
    if result.get("source_refused"):
        return {"status": "source_refused", "deal_id": deal_id, "tpo_id": tpo.tpo_id,
                "detail": result.get("detail")}
    shipment = next((s for s in result.get("shipments") or [] if s.get("milestone")), None)
    if shipment is None:
        return {"status": "no_event", "deal_id": deal_id, "tpo_id": tpo.tpo_id}
    milestone = shipment["milestone"]
    if tpo.last_milestone == milestone:
        return {"status": "unchanged", "deal_id": deal_id, "milestone": milestone}
    applied = await _apply_milestone(
        tpo, milestone, location=shipment.get("location"), timestamp=shipment.get("timestamp"),
        notes=shipment.get("raw_status"), source="tracking",
    )
    return {**applied, "tpo_id": tpo.tpo_id}


# ---------------------------------------------------------------------------
# CHA filing
# ---------------------------------------------------------------------------

async def file_cha(req: ChaFilingRequest) -> ThirdPartyOrderResponse:
    """Submit customs docs to ICEGATE via registered CHA.

    Only for goods that passed QC and have not finished their journey
    (``CUSTOMS_FILING_STATUSES``); it can run alongside freight booking.
    Emits customs.filed.
    """
    await _assert_platform_running(f"Customs filing for deal {req.deal_id}")
    repo = get_repository()

    if req.idempotency_key:
        existing = await repo.get_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return ThirdPartyOrderResponse.from_record(existing)

    deal = await _fetch_deal(req.deal_id)
    deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    if deal_status not in CUSTOMS_FILING_STATUSES:
        raise InvalidDealState(req.deal_id, deal_status, " or ".join(sorted(CUSTOMS_FILING_STATUSES)))
    now = datetime.now(timezone.utc)
    tpo_id = _new_tpo_id("CHA")

    record = ThirdPartyOrderRecord(
        tpo_id=tpo_id,
        deal_id=req.deal_id,
        order_type="CHA",
        provider=req.provider,
        status="PENDING",
        idempotency_key=req.idempotency_key,
        created_at=now,
        updated_at=now,
    )
    await repo.save(record)

    from shared.connectors.shields import IcegateConnector
    connector = IcegateConnector()
    try:
        result = await connector.book({
            "deal_id": req.deal_id,
            "documents": req.documents,
            "rodtep_declaration": req.rodtep_declaration,
            "hs_code": getattr(deal, "hs_code", ""),
        })
    except ConnectorError as exc:
        failed = record.model_copy(update={"status": "FAILED", "updated_at": datetime.now(timezone.utc)})
        await repo.save(failed)
        raise ConnectorCallError(f"CHA filing failed for deal {req.deal_id}: {exc}") from exc

    updated = record.model_copy(update={
        "status": "COMPLETED",
        "ext_ref": result.get("shipping_bill_number"),
        "provider_payload": result,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(updated)

    await _emit(EventTopic.CUSTOMS_FILED, req.deal_id, {
        "tpo_id": tpo_id,
        "shipping_bill_number": result.get("shipping_bill_number"),
        "let_export_order": result.get("let_export_order"),
        "stub": result.get("stub", False),
    })

    logger.info("ShieldLink: CHA filed %s for deal %s", tpo_id, req.deal_id)
    return ThirdPartyOrderResponse.from_record(updated)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

async def get_tpo(tpo_id: str) -> ThirdPartyOrderResponse:
    repo = get_repository()
    tpo = await repo.get(tpo_id)
    if tpo is None:
        raise TpoNotFoundError(tpo_id)
    return ThirdPartyOrderResponse.from_record(tpo)


async def list_deal_orders(deal_id: str) -> DealOrdersResponse:
    repo = get_repository()
    orders = await repo.list_by_deal(deal_id)
    return DealOrdersResponse(
        deal_id=deal_id,
        orders=[ThirdPartyOrderResponse.from_record(o) for o in orders],
    )
