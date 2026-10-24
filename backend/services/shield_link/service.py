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

from .config import settings
from .repository import get_repository
from .schemas import (
    ChaFilingRequest,
    DealOrdersResponse,
    FreightBookingRequest,
    FreightMilestonePayload,
    FreightQuoteRequest,
    FreightQuoteResponse,
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

async def book_qc(req: QcBookingRequest, *, reinspection: bool = False) -> ThirdPartyOrderResponse:
    """Book a QC inspection with the configured provider.

    Pre-condition: deal must be in ``PO_ISSUED`` state.
    Post: deal → ``SUPPLIER_DISPATCH_READY``, emits ``qc.booked``.

    ``reinspection=True`` (QC_FAILED playbook): the deal already sits in
    ``SUPPLIER_DISPATCH_READY`` after the rework — a fresh booking is made
    without a deal transition.
    """
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
    now = datetime.now(timezone.utc)
    tpo_id = _new_tpo_id("QC")

    # Create PENDING record first for crash-safety
    record = ThirdPartyOrderRecord(
        tpo_id=tpo_id,
        deal_id=req.deal_id,
        order_type="QC",
        provider=provider,
        status="PENDING",
        pickup_ready_at=req.pickup_ready_at,
        idempotency_key=req.idempotency_key,
        created_at=now,
        updated_at=now,
    )
    await repo.save(record)

    # Call connector
    connector = get_qc_connector(provider)
    try:
        result = await connector.book({
            "deal_id": req.deal_id,
            "location": req.location or "",
            "pickup_ready_at": req.pickup_ready_at or "",
            "hs_code": req.hs_code or getattr(deal, "hs_code", ""),
            "quantity_mt": req.quantity_mt or 0.0,
        })
    except ConnectorError as exc:
        failed = record.model_copy(update={"status": "FAILED", "updated_at": datetime.now(timezone.utc)})
        await repo.save(failed)
        raise ConnectorCallError(f"QC booking failed for deal {req.deal_id}: {exc}") from exc

    updated = record.model_copy(update={
        "status": "BOOKED",
        "ext_ref": result.get("ext_ref"),
        "provider_payload": result,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(updated)

    if not reinspection:
        await _transition_deal(req.deal_id, "SUPPLIER_DISPATCH_READY", f"QC booked — ref {result.get('ext_ref')}")
    await _emit(EventTopic.QC_BOOKED, req.deal_id, {
        "tpo_id": tpo_id,
        "provider": provider,
        "ext_ref": result.get("ext_ref"),
        "inspection_date": result.get("inspection_date"),
        "stub": result.get("stub", False),
        "reinspection": reinspection,
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
) -> dict[str, str]:
    """Process QC inspection result webhook.

    Verifies provider signature, updates TPO, transitions deal to
    QC_PASSED or QC_FAILED, and emits the appropriate event.
    """
    repo = get_repository()

    # Locate the TPO by ext_ref
    tpo = await repo.get_by_ext_ref(ext_ref)
    if tpo is None:
        raise TpoNotFoundError(ext_ref)

    # Verify webhook signature
    connector = get_qc_connector(tpo.provider)
    try:
        valid = await connector.webhook_verify(raw_headers, raw_body)
    except Exception:
        valid = False
    if not valid:
        raise WebhookVerifyError(f"QC webhook signature invalid for ext_ref={ext_ref}")

    now = datetime.now(timezone.utc)
    qc_result = payload.result  # "PASS" or "FAIL"

    updated = tpo.model_copy(update={
        "status": "COMPLETED" if qc_result == "PASS" else "FAILED",
        "qc_result": qc_result,
        "qc_report_url": payload.report_url,
        "crf_hash": payload.crf_hash,
        "qc_completed_at": now,
        "updated_at": now,
    })
    await repo.save(updated)

    if qc_result == "PASS":
        try:
            await _transition_deal(tpo.deal_id, "QC_PASSED", f"QC passed — ref {ext_ref}")
        except Exception as exc:
            logger.error("ShieldLink: QC_PASSED transition failed for deal %s: %s", tpo.deal_id, exc)
        await _emit(EventTopic.QC_PASSED, tpo.deal_id, {
            "tpo_id": tpo.tpo_id,
            "ext_ref": ext_ref,
            "crf_hash": payload.crf_hash,
            "report_url": payload.report_url,
        })
    else:
        try:
            await _transition_deal(tpo.deal_id, "QC_FAILED", f"QC failed — ref {ext_ref}, notes: {payload.notes}")
        except Exception as exc:
            logger.error("ShieldLink: QC_FAILED transition failed for deal %s: %s", tpo.deal_id, exc)
        await _emit(EventTopic.QC_FAILED, tpo.deal_id, {
            "tpo_id": tpo.tpo_id,
            "ext_ref": ext_ref,
            "notes": payload.notes,
        })

    logger.info("ShieldLink: QC result %s processed for deal %s", qc_result, tpo.deal_id)
    return {"status": "ok", "result": qc_result, "deal_id": tpo.deal_id}


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
    try:
        valid = await connector.webhook_verify(raw_headers, raw_body)
    except Exception:
        valid = False
    if not valid:
        raise WebhookVerifyError(f"Freight webhook signature invalid for ext_ref={ext_ref}")

    return await _apply_milestone(
        tpo, payload.milestone, location=payload.location, timestamp=payload.timestamp,
        notes=payload.notes, source="webhook",
    )


async def _apply_milestone(
    tpo: ThirdPartyOrderRecord,
    milestone: str,
    *,
    location: str | None = None,
    timestamp: str | None = None,
    notes: str | None = None,
    source: str = "webhook",
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
    )


async def poll_tracking(deal_id: str) -> dict[str, Any]:
    """Ask the carrier-tracking actor where the deal's container is and record
    the latest milestone. Returns ``{"status": ...}`` — ``no_booking`` /
    ``no_reference`` / ``stub`` (actor not configured) / ``unchanged`` / ``ok``.
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
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("ShieldLink: tracking poll failed for deal %s: %s", deal_id, exc)
        return {"status": "error", "deal_id": deal_id, "error": str(exc)}
    if result.get("stub"):
        return {"status": "stub", "deal_id": deal_id, "tpo_id": tpo.tpo_id}
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

    Does not require a specific deal status; can run alongside freight booking.
    Emits customs.filed.
    """
    repo = get_repository()

    if req.idempotency_key:
        existing = await repo.get_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return ThirdPartyOrderResponse.from_record(existing)

    deal = await _fetch_deal(req.deal_id)
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
