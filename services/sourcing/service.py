"""Business logic for the Sourcing service.

Pipeline position
-----------------
Deals (MATCHING) → Sourcing → Deals (NEGOTIATING)

``dispatch_rfq``
    Sends automated RFQs to supplier(s) via IndiaMART / TradeIndia connectors.
    Falls back to marking the dispatch as queued when no connector is available.

``receive_quote``
    Ingests a supplier quote and runs a three-step concurrent pipeline:
      1. Fetch deal (buyer_target_price, winning match scores)
      2. Verify supplier credentials (GSTN + DGFT + MCA + Udyam) concurrently
      3. Re-score via Match Engine with real verification data
    After scoring: if decision == SOURCE the deal transitions MATCHING → NEGOTIATING.

All inter-service calls use direct in-process imports (combined monolith).  In
a microservice deployment swap them for HTTP client calls.

Exception isolation
-------------------
Every connector call and inter-service call is wrapped in try/except.
A failure in GSTN verification should never prevent a quote from being ingested.
The deal transition is best-effort — if it fails the quote is still persisted.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from shared.bus import get_publisher
from shared.connectors import get_connector
from shared.domain.events import DomainEvent, EventTopic

from .config import settings
from .repository import get_repository
from .schemas import (
    DispatchResult,
    QuoteIntakeRequest,
    QuoteRecord,
    RfqRecord,
    RfqRequest,
    RfqResponse,
    SupplierRef,
    VerificationResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class RfqNotFoundError(Exception):
    def __init__(self, rfq_id: str) -> None:
        super().__init__(f"RFQ {rfq_id!r} not found")
        self.rfq_id = rfq_id


class QuoteNotFoundError(Exception):
    def __init__(self, quote_id: str) -> None:
        super().__init__(f"Quote {quote_id!r} not found")
        self.quote_id = quote_id


# ---------------------------------------------------------------------------
# ID factories
# ---------------------------------------------------------------------------

def _new_rfq_id() -> str:
    return f"RFQ-{uuid4().hex[:12].upper()}"


def _new_quote_id() -> str:
    return f"QUOTE-{uuid4().hex[:12].upper()}"


# ---------------------------------------------------------------------------
# Event helper
# ---------------------------------------------------------------------------

async def _emit_event(topic: EventTopic, deal_id: str, payload: dict[str, Any]) -> None:
    try:
        await get_publisher().publish(
            DomainEvent(topic=topic, deal_id=deal_id, payload=payload)
        )
    except Exception as exc:
        logger.warning("Failed to emit %s for deal %s: %s", topic.value, deal_id, exc)


# ---------------------------------------------------------------------------
# Connector helpers
# ---------------------------------------------------------------------------

def _build_connector(name: str):
    """Instantiate a connector safely; returns None when env var is missing."""
    try:
        return get_connector(name)
    except KeyError as exc:
        logger.warning("Connector %r unavailable — missing env var: %s", name, exc)
        return None
    except Exception as exc:
        logger.warning("Connector %r init failed: %s", name, exc)
        return None


async def _safe_call(connector_name: str, method: str, request: dict) -> dict | None:
    """Call connector.quote() or connector.book() safely; returns None on failure."""
    try:
        conn = get_connector(connector_name)
        fn = getattr(conn, method)
        return await fn(request)
    except Exception as exc:
        logger.warning("Connector %s.%s() failed: %s", connector_name, method, exc)
        return None


# ---------------------------------------------------------------------------
# RFQ dispatch helpers
# ---------------------------------------------------------------------------

def _build_rfq_message(payload: RfqRequest) -> str:
    if payload.message:
        return payload.message
    return settings.rfq_message_template.format(
        quantity_mt=payload.quantity_mt or "N/A",
        hs_code=payload.hs_code,
        target_price_usd=payload.target_price_usd or "TBD",
        deal_id=payload.deal_id,
        validity_days=payload.validity_days,
    )


async def _dispatch_one(
    rfq_id: str,
    supplier: SupplierRef,
    message: str,
    payload: RfqRequest,
) -> DispatchResult:
    """Dispatch RFQ to one supplier via IndiaMART, TradeIndia, or portal fallback."""
    contact_id = supplier.contact_id
    source = supplier.source

    # IndiaMART
    if source == "indiamart" and contact_id:
        result = await _safe_call(
            "indiamart",
            "book",
            {
                "contact_id": contact_id,
                "message": message,
                "quantity": f"{payload.quantity_mt} MT" if payload.quantity_mt else "",
                "buyer_name": "PortLess Trade Agent",
            },
        )
        if result is not None:
            return DispatchResult(
                supplier_id=supplier.supplier_id,
                contact_id=contact_id,
                channel="indiamart",
                ext_rfq_id=str(result.get("rfq_id") or rfq_id),
                success=True,
            )

    # TradeIndia
    if source == "tradeindia" and contact_id:
        result = await _safe_call(
            "tradeindia",
            "book",
            {
                "contact_id": contact_id,
                "message": message,
                "quantity": f"{payload.quantity_mt} MT" if payload.quantity_mt else "",
            },
        )
        if result is not None:
            return DispatchResult(
                supplier_id=supplier.supplier_id,
                contact_id=contact_id,
                channel="tradeindia",
                ext_rfq_id=str(result.get("rfq_id") or rfq_id),
                success=True,
            )

    # E-mail — the channel that works for every supplier the Apify actors /
    # contact finder gave an address for (and the only one that needs no
    # marketplace API key). The subject token lets the reply map back here.
    if supplier.email:
        ext_id = await _dispatch_by_email(rfq_id, supplier, message, payload)
        if ext_id == _NOT_DELIVERED:
            return DispatchResult(
                supplier_id=supplier.supplier_id,
                contact_id=contact_id,
                channel="email",
                email=supplier.email,
                success=False,
                error="not delivered — no outbound mailbox configured (GMAIL_SENDER_ADDRESS); RFQ logged only",
            )
        if ext_id is not None:
            return DispatchResult(
                supplier_id=supplier.supplier_id,
                contact_id=contact_id,
                channel="email",
                ext_rfq_id=ext_id,
                email=supplier.email,
                success=True,
            )

    # Portal fallback — treated as "queued for manual dispatch" rather than failed
    return DispatchResult(
        supplier_id=supplier.supplier_id,
        contact_id=contact_id,
        channel="portal",
        ext_rfq_id=None,
        success=True,
    )


_NOT_DELIVERED = "__not_delivered__"


def rfq_subject(rfq_id: str, hs_code: str, quantity_mt: float | None) -> str:
    qty = f"{quantity_mt:g} MT " if quantity_mt else ""
    return f"Request for quotation — {qty}HS {hs_code} [RFQ:{rfq_id}]"


async def _dispatch_by_email(
    rfq_id: str, supplier: SupplierRef, message: str, payload: RfqRequest,
) -> str | None:
    """Send the RFQ through the Email Gateway. Returns the Message-ID (or a
    log-only marker) on success, None when the gateway refused the send."""
    try:
        from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
        from services.email_gateway.sender import send_email  # type: ignore[import]

        body = (
            f"Dear {supplier.company_name or 'Supplier'},\n\n{message}\n\n"
            "Please reply to this e-mail with:\n"
            "  - your best FOB price per MT in USD, and how long it is valid\n"
            "  - grade / specification you supply (purity, packaging)\n"
            "  - minimum order quantity and monthly capacity (MT)\n"
            "  - lead time from order to FOB, and port of loading\n"
            "  - certificates you can provide (COA, ISO, FSSAI, Halal…)\n"
            "  - payment terms\n"
            "  - your GSTIN and IEC, so we can verify them\n\n"
            "Keep the reference in the subject line so your quote reaches the right desk.\n\n"
            "Best regards,\nPortLess Trade Operations\n"
        )
        result = await send_email(SendEmailRequest(
            to_address=supplier.email or "",
            subject=rfq_subject(rfq_id, payload.hs_code, payload.quantity_mt),
            body=body,
            negotiation_id="",
            round_no=0,
            email_id=f"RFQ{rfq_id.replace('-', '')}{supplier.supplier_id.replace('-', '')[:12]}".upper(),
        ))
        outcome = getattr(result, "outcome", None)
        outcome_val = getattr(outcome, "value", outcome)
        if str(outcome_val) == "logged":
            logger.warning("RFQ %s to %s (%s) NOT delivered — no outbound mailbox configured",
                           rfq_id, supplier.email, supplier.supplier_id)
            return _NOT_DELIVERED
        logger.info("RFQ %s e-mailed to %s (%s): %s", rfq_id, supplier.email, supplier.supplier_id, outcome_val)
        return getattr(result, "message_id", None) or f"email:{outcome_val}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("RFQ %s e-mail to %s failed: %s", rfq_id, supplier.email, exc)
        return None


async def receive_quote_by_email(
    rfq_id: str,
    from_address: str,
    unit_price_usd: float,
    body_text: str = "",
    company_name: str | None = None,
) -> QuoteRecord:
    """Inbound supplier reply to an ``[RFQ:<id>]`` e-mail → formal quote.

    The sender address is matched against the RFQ's e-mail dispatches to find
    the supplier id; GSTIN / IEC are picked out of the body when quoted.
    Everything else goes through the normal ``receive_quote`` pipeline
    (verification → Match Engine re-score → deal transition).
    """
    rfq = await get_rfq(rfq_id)
    sender = (from_address or "").strip().lower()
    supplier_id: str | None = None
    for d in rfq.dispatch_results:
        if d.email and d.email.strip().lower() == sender:
            supplier_id = d.supplier_id
            break
    if supplier_id is None:
        # Same domain as a dispatched address (sales@ replied from a colleague's mailbox).
        domain = sender.split("@")[-1]
        for d in rfq.dispatch_results:
            if d.email and d.email.strip().lower().split("@")[-1] == domain:
                supplier_id = d.supplier_id
                break
    if supplier_id is None:
        if len(rfq.supplier_ids) == 1:
            supplier_id = rfq.supplier_ids[0]
        else:
            raise QuoteNotFoundError(f"{rfq_id}:sender:{from_address}")

    gstin = _find_pattern(body_text, r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
    iec = _find_pattern(body_text, r"\bIEC[\s:#-]*([A-Z0-9]{10})\b", group=1)
    fields: dict[str, Any] = {}
    if body_text:
        try:
            from shared.orchestration.extraction import extract_supplier
            fields = await extract_supplier(body_text)
        except Exception as exc:  # noqa: BLE001 — a quote without details is still a quote
            logger.warning("RFQ %s: offer details not read from the reply: %s", rfq_id, exc)
    validity = fields.get("validity_days")
    return await receive_quote(QuoteIntakeRequest(
        rfq_id=rfq_id,
        deal_id=rfq.deal_id,
        supplier_id=supplier_id,
        company_name=company_name or "",
        unit_price_usd=unit_price_usd,
        quantity_mt=rfq.quantity_mt,
        gstin=gstin,
        iec_number=iec,
        payment_terms=fields.get("payment_terms"),
        lead_time_days=fields.get("lead_time_days"),
        port_of_loading=fields.get("port_of_loading"),
        validity_days=int(validity) if validity and 1 <= int(validity) <= 90 else 30,
        grade=fields.get("grade"),
        moq_mt=fields.get("moq_mt"),
        capacity_mt_per_month=fields.get("capacity_mt_per_month"),
        certifications=fields.get("certifications") or [],
        supplier_email=sender or None,
    ))


def _find_pattern(text: str, pattern: str, group: int = 0) -> str | None:
    import re
    m = re.search(pattern, text or "", re.IGNORECASE)
    if not m:
        return None
    return m.group(group).upper()


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

async def _verify_supplier(
    supplier_id: str,
    gstin: str | None,
    iec: str | None,
    cin: str | None,
    udyam_number: str | None,
) -> VerificationResult:
    """Run concurrent regulatory verification.  All checks are best-effort."""
    now = datetime.now(timezone.utc)

    tasks: dict[str, asyncio.Task] = {}
    if gstin:
        tasks["gstn"] = asyncio.create_task(
            _safe_call("gstn", "quote", {"gstin": gstin})
        )
    if iec:
        tasks["dgft_iec"] = asyncio.create_task(
            _safe_call("dgft", "quote", {"iec": iec})
        )
        tasks["dgft_denied"] = asyncio.create_task(
            _safe_call("dgft", "book", {"supplier_id": supplier_id})
        )
    if cin:
        tasks["mca"] = asyncio.create_task(
            _safe_call("mca", "quote", {"cin": cin})
        )
    if udyam_number:
        tasks["udyam"] = asyncio.create_task(
            _safe_call("udyam", "quote", {"udyam_number": udyam_number})
        )

    if tasks:
        raw_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        results: dict[str, Any] = {
            k: v for k, v in zip(tasks.keys(), raw_results)
            if not isinstance(v, BaseException) and v is not None
        }
    else:
        results = {}

    # Unpack results — conservative defaults (pass-through) when API unavailable
    gst_verified = True
    iec_active = True
    dgft_denied = False
    age_years = 0.0
    udyam_registered = False

    if "gstn" in results:
        gst_verified = results["gstn"].get("status", "").upper() == "ACTIVE"

    if "dgft_iec" in results:
        iec_active = results["dgft_iec"].get("status", "").upper() == "ACTIVE"

    if "dgft_denied" in results:
        dgft_denied = bool(results["dgft_denied"].get("on_denied_list", False))

    if "mca" in results:
        age_years = float(results["mca"].get("age_years", 0))

    if "udyam" in results:
        udyam_registered = bool(results["udyam"].get("registered", False))

    return VerificationResult(
        supplier_id=supplier_id,
        gst_verified=gst_verified,
        iec_active=iec_active,
        dgft_denied=dgft_denied,
        business_age_years=age_years,
        udyam_registered=udyam_registered,
        annual_shipment_count=0,
        verified_at=now,
        raw=results,
    )


# ---------------------------------------------------------------------------
# Inter-service helpers (direct in-process calls)
# ---------------------------------------------------------------------------

async def _fetch_deal(deal_id: str) -> dict[str, Any] | None:
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        deal = await get_deal(deal_id)
        return deal.model_dump()
    except Exception as exc:
        logger.warning("Could not fetch deal %s: %s", deal_id, exc)
        return None


async def _rescore(
    deal_id: str,
    supplier_id: str,
    buyer_target_price: float,
    supplier_quote_price: float,
    verification: VerificationResult,
    supplier_to_port_km: float,
) -> dict[str, Any] | None:
    """Re-score supplier via Match Engine with real verification data."""
    try:
        from services.match_engine.service import score_match  # type: ignore[import]
        from services.match_engine.schemas import ScoreRequest, SupplierVerification  # type: ignore[import]

        req = ScoreRequest(
            deal_id=deal_id,
            supplier_id=supplier_id,
            buyer_target_price=buyer_target_price,
            supplier_quote_price=supplier_quote_price,
            currency="USD",
            verification=SupplierVerification(
                gst_verified=verification.gst_verified,
                iec_active=verification.iec_active,
                dgft_denied=verification.dgft_denied,
            ),
            supplier_to_port_km=supplier_to_port_km,
            business_age_years=verification.business_age_years,
            udyam_registered=verification.udyam_registered,
            annual_shipment_count=verification.annual_shipment_count,
        )
        resp = await score_match(req)
        return {
            "match_id": resp.match_id,
            "composite_score": resp.composite_score,
            "gatekeeper_passed": resp.gatekeeper_passed,
            "decision": resp.decision,
        }
    except Exception as exc:
        logger.warning(
            "Re-score failed for deal %s / supplier %s: %s", deal_id, supplier_id, exc
        )
        return None


async def _update_deal_financials(
    deal_id: str, supplier_id: str, cms: float
) -> None:
    """Update deal's winning_supplier_id and winning_cms if this score beats the old one."""
    try:
        from services.deals.service import get_deal, update_financials  # type: ignore[import]
        from services.deals.schemas import UpdateFinancialsRequest  # type: ignore[import]

        deal = await get_deal(deal_id)
        if deal.winning_cms is None or cms > deal.winning_cms:
            await update_financials(
                deal_id,
                UpdateFinancialsRequest(
                    winning_supplier_id=supplier_id,
                    winning_cms=cms,
                ),
            )
    except Exception as exc:
        logger.warning("Could not update deal financials for %s: %s", deal_id, exc)


async def _record_offer(payload: QuoteIntakeRequest, quote: QuoteRecord,
                        verification: VerificationResult | None) -> None:
    """The quote as a supplier offer on the prospect's match board, and as a
    supplier-side price observation in the price book."""
    try:
        from services.deals.schemas import SupplierOffer  # type: ignore[import]
        from services.deals.service import get_deal, upsert_supplier_offer  # type: ignore[import]
        from shared import pricebook

        failed = bool(verification and (not verification.gst_verified or not verification.iec_active
                                        or getattr(verification, "dgft_denied", False)))
        gst_checked = bool(verification and "gstn" in verification.raw)
        iec_checked = bool(verification and "dgft_iec" in verification.raw)
        email = payload.supplier_email
        if not email:
            try:
                rfq = await get_rfq(payload.rfq_id)
                email = next((d.email for d in rfq.dispatch_results
                              if d.supplier_id == payload.supplier_id and d.email), None)
            except Exception:  # noqa: BLE001
                email = None
        offer = SupplierOffer(
            offer_id=f"OFR-{quote.quote_id.removeprefix('QUOTE-').removeprefix('QT-')}",
            supplier_id=payload.supplier_id, company_name=payload.company_name, email=email,
            grade=payload.grade, fob_price_usd_per_mt=payload.unit_price_usd,
            moq_mt=payload.moq_mt, capacity_mt_per_month=payload.capacity_mt_per_month,
            lead_time_days=payload.lead_time_days, port_of_loading=payload.port_of_loading,
            certifications=payload.certifications, payment_terms=payload.payment_terms,
            validity_days=payload.validity_days,
            gst_verified=(verification.gst_verified if gst_checked else None),
            iec_active=(verification.iec_active if iec_checked else None),
            verification_failed=failed, quote_id=quote.quote_id, source="quote",
            origin_deal_id=payload.deal_id, received_at=quote.received_at,
        )
        await upsert_supplier_offer(payload.deal_id, offer)
        deal = await get_deal(payload.deal_id)
        await pricebook.record(pricebook.PriceObservation(
            hs_code=deal.hs_code, side="supplier", kind="supplier_quote",
            price_usd_per_mt=payload.unit_price_usd, grade=payload.grade, incoterm=payload.incoterm,
            quantity_mt=payload.quantity_mt, deal_id=payload.deal_id, counterparty_id=payload.supplier_id,
        ))
    except Exception as exc:  # noqa: BLE001 — the quote itself is already saved
        logger.warning("Could not record the offer for quote %s: %s", quote.quote_id, exc)


async def _set_supplier_party(payload: QuoteIntakeRequest) -> None:
    """The winning quote's supplier becomes the deal's supplier party."""
    try:
        from services.deals.service import set_supplier_party  # type: ignore[import]
        await set_supplier_party(
            payload.deal_id, payload.supplier_id,
            company_name=payload.company_name or None,
            gstin=payload.gstin, iec=payload.iec_number,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not set the supplier party for %s: %s", payload.deal_id, exc)


async def _transition_to_negotiating(deal_id: str) -> None:
    """Transition deal MATCHING → NEGOTIATING on SOURCE decision."""
    try:
        from services.deals.service import update_status  # type: ignore[import]
        from services.deals.schemas import DealStatus, UpdateStatusRequest  # type: ignore[import]

        await update_status(
            deal_id,
            UpdateStatusRequest(
                target_status=DealStatus.NEGOTIATING,
                reason="Verified supplier quote received — Match Engine decision: SOURCE",
            ),
        )
        logger.info("Deal %s transitioned to NEGOTIATING", deal_id)
    except Exception as exc:
        logger.warning(
            "Could not transition deal %s to NEGOTIATING: %s", deal_id, exc
        )


def _get_supplier_port_km(deal: dict, supplier_id: str) -> float:
    """Look up supplier_to_port_km from the deal's match_results (default 300 km)."""
    for mr in deal.get("match_results", []):
        if mr.get("supplier_id") == supplier_id:
            return float(mr.get("supplier_to_port_km") or 300.0)
    return 300.0


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------

async def dispatch_rfq(payload: RfqRequest) -> RfqResponse:
    """Dispatch RFQs to suppliers via IndiaMART / TradeIndia connectors.

    Dispatches to all listed suppliers concurrently.  A portal fallback ensures
    that ``status=COMPLETE`` is achieved even when no connector is configured —
    the operator can then follow up manually.
    """
    now = datetime.now(timezone.utc)
    rfq_id = _new_rfq_id()
    message = _build_rfq_message(payload)

    dispatch_coros = [
        _dispatch_one(rfq_id, supplier, message, payload)
        for supplier in payload.suppliers
    ]
    raw = await asyncio.gather(*dispatch_coros, return_exceptions=True)

    dispatch_results: list[DispatchResult] = []
    for i, r in enumerate(raw):
        if isinstance(r, BaseException):
            logger.error(
                "Dispatch error for supplier %s: %s",
                payload.suppliers[i].supplier_id,
                r,
            )
            dispatch_results.append(
                DispatchResult(
                    supplier_id=payload.suppliers[i].supplier_id,
                    channel="failed",
                    success=False,
                    error=str(r),
                )
            )
        else:
            dispatch_results.append(r)

    dispatched_count = sum(1 for d in dispatch_results if d.success)
    failed_count = len(dispatch_results) - dispatched_count

    if dispatched_count == 0:
        rfq_status = "FAILED"
    elif dispatched_count < len(dispatch_results):
        rfq_status = "PARTIAL"
    else:
        rfq_status = "COMPLETE"

    rfq = RfqRecord(
        rfq_id=rfq_id,
        deal_id=payload.deal_id,
        hs_code=payload.hs_code,
        supplier_ids=[s.supplier_id for s in payload.suppliers],
        dispatch_results=dispatch_results,
        quantity_mt=payload.quantity_mt,
        target_price_usd=payload.target_price_usd,
        message=message,
        status=rfq_status,
        dispatched_at=now,
        expires_at=now + timedelta(days=payload.validity_days),
    )

    repo = get_repository()
    await repo.save_rfq(rfq)

    await _emit_event(
        EventTopic.QUOTE_REQUESTED,
        deal_id=payload.deal_id,
        payload={
            "rfq_id": rfq_id,
            "dispatched_count": dispatched_count,
            "failed_count": failed_count,
            "supplier_count": len(payload.suppliers),
            "hs_code": payload.hs_code,
        },
    )

    return RfqResponse(
        rfq_id=rfq_id,
        deal_id=payload.deal_id,
        dispatched_count=dispatched_count,
        failed_count=failed_count,
        dispatch_results=dispatch_results,
        status=rfq_status,
    )


async def receive_quote(payload: QuoteIntakeRequest) -> QuoteRecord:
    """Ingest a supplier quote, run regulatory verification, re-score, and advance the deal.

    Three-step concurrent pipeline:
      1. Verify supplier credentials (GSTN + DGFT + MCA + Udyam)
      2. Fetch deal (to get buyer_target_price + existing match scores)
      3. Re-score via Match Engine with real verification data

    After scoring:
      • Updates deal financials if new CMS beats the stored value.
      • Transitions deal MATCHING → NEGOTIATING when decision == SOURCE.
      • Emits ``quote.received``.
    """
    now = datetime.now(timezone.utc)
    quote_id = _new_quote_id()

    # ── Step 1 & 2: concurrent verification + deal fetch ─────────────────
    verification_task = asyncio.create_task(
        _verify_supplier(
            supplier_id=payload.supplier_id,
            gstin=payload.gstin,
            iec=payload.iec_number,
            cin=payload.cin_number,
            udyam_number=payload.udyam_number,
        )
    )
    deal_task = asyncio.create_task(_fetch_deal(payload.deal_id))

    verification_raw, deal = await asyncio.gather(
        verification_task, deal_task, return_exceptions=True
    )

    if isinstance(verification_raw, BaseException):
        logger.error("Verification pipeline error for quote %s: %s", quote_id, verification_raw)
        verification: VerificationResult | None = None
    else:
        verification = verification_raw

    if isinstance(deal, BaseException):
        logger.error("Deal fetch error for quote %s: %s", quote_id, deal)
        deal = None

    # Reliability inputs the Match Engine otherwise scores as zero: the
    # supplier's export history (Indian customs records via the supplier
    # actor / Zauba) captured on the deal's match result at creation.
    if deal and verification:
        for mr in deal.get("match_results", []) or []:
            if mr.get("supplier_id") == payload.supplier_id and mr.get("export_shipments_12m"):
                verification.annual_shipment_count = int(mr["export_shipments_12m"])
                break

    # ── Step 3: re-score with real verification data ──────────────────────
    re_scored_match_id: str | None = None
    re_scored_cms: float | None = None
    re_scored_decision: str | None = None

    if deal and verification:
        buyer_price = deal.get("buyer_target_price_usd")
        if buyer_price:
            port_km = _get_supplier_port_km(deal, payload.supplier_id)
            scored = await _rescore(
                deal_id=payload.deal_id,
                supplier_id=payload.supplier_id,
                buyer_target_price=buyer_price,
                supplier_quote_price=payload.unit_price_usd,
                verification=verification,
                supplier_to_port_km=port_km,
            )
            if scored:
                re_scored_match_id = scored.get("match_id")
                re_scored_cms = scored.get("composite_score")
                re_scored_decision = scored.get("decision")

    # ── Persist quote ─────────────────────────────────────────────────────
    quote = QuoteRecord(
        quote_id=quote_id,
        rfq_id=payload.rfq_id,
        deal_id=payload.deal_id,
        supplier_id=payload.supplier_id,
        company_name=payload.company_name,
        unit_price_usd=payload.unit_price_usd,
        currency=payload.currency,
        quantity_mt=payload.quantity_mt,
        validity_days=payload.validity_days,
        incoterm=payload.incoterm,
        port_of_loading=payload.port_of_loading,
        lead_time_days=payload.lead_time_days,
        payment_terms=payload.payment_terms,
        gstin=payload.gstin,
        iec_number=payload.iec_number,
        cin_number=payload.cin_number,
        udyam_number=payload.udyam_number,
        verification=verification,
        re_scored_match_id=re_scored_match_id,
        re_scored_cms=re_scored_cms,
        re_scored_decision=re_scored_decision,
        status="VERIFIED" if verification else "RECEIVED",
        received_at=now,
        updated_at=now,
    )

    repo = get_repository()
    await repo.save_quote(quote)

    # ── Update deal + advance state machine ───────────────────────────────
    if re_scored_cms is not None:
        await _update_deal_financials(payload.deal_id, payload.supplier_id, re_scored_cms)

    await _record_offer(payload, quote, verification)

    from shared.orchestration import matchmaking
    if (re_scored_decision == "SOURCE" and deal and deal.get("status") == "MATCHING"
            and not matchmaking.enabled()):
        await _set_supplier_party(payload)
        await _transition_to_negotiating(payload.deal_id)

    # ── Emit event ────────────────────────────────────────────────────────
    await _emit_event(
        EventTopic.QUOTE_RECEIVED,
        deal_id=payload.deal_id,
        payload={
            "quote_id": quote_id,
            "rfq_id": payload.rfq_id,
            "supplier_id": payload.supplier_id,
            "unit_price_usd": payload.unit_price_usd,
            "re_scored_cms": re_scored_cms,
            "re_scored_decision": re_scored_decision,
            "gst_verified": verification.gst_verified if verification else None,
            "iec_active": verification.iec_active if verification else None,
            # Whether the registry checks actually ran (the booleans above
            # default to True when a check is skipped).
            "gst_checked": bool(verification and "gstn" in verification.raw),
            "iec_checked": bool(verification and "dgft_iec" in verification.raw),
            "company_name": payload.company_name,
        },
    )

    return quote


async def get_rfq(rfq_id: str) -> RfqRecord:
    """Fetch an RFQ by ID.  Raises ``RfqNotFoundError`` when absent."""
    repo = get_repository()
    rfq = await repo.get_rfq(rfq_id)
    if rfq is None:
        raise RfqNotFoundError(rfq_id)
    return rfq


async def list_rfqs(deal_id: str) -> list[RfqRecord]:
    """List all RFQs for a deal."""
    return await get_repository().list_rfqs(deal_id)


async def get_quote(quote_id: str) -> QuoteRecord:
    """Fetch a quote by ID.  Raises ``QuoteNotFoundError`` when absent."""
    repo = get_repository()
    quote = await repo.get_quote(quote_id)
    if quote is None:
        raise QuoteNotFoundError(quote_id)
    return quote


async def list_quotes(
    deal_id: str | None = None,
    rfq_id: str | None = None,
) -> list[QuoteRecord]:
    """List quotes, optionally filtered by deal or RFQ."""
    return await get_repository().list_quotes(deal_id=deal_id, rfq_id=rfq_id)


async def verify_supplier(
    supplier_id: str,
    gstin: str | None = None,
    iec: str | None = None,
    cin: str | None = None,
    udyam_number: str | None = None,
) -> VerificationResult:
    """Manually trigger a regulatory verification without a quote.

    Useful for pre-screening suppliers before committing to an RFQ.
    """
    return await _verify_supplier(
        supplier_id=supplier_id,
        gstin=gstin,
        iec=iec,
        cin=cin,
        udyam_number=udyam_number,
    )
