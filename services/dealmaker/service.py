"""DealMaker service — business logic layer.

Orchestrates PI and PO document generation:

  issue_pi()     NEGOTIATION_AGREED → PI_SENT
                 Renders pi.html.j2, stores to disk, emits pi.sent.
  accept_pi()    PI_SENT → PI_ACCEPTED
                 Validates PI status, records acceptance, emits pi.accepted.
  issue_po()     ESCROW_FUNDED → PO_ISSUED
                 Enforces margin guard, renders po.html.j2, stores, emits po.issued.

All mutating paths:
  • Validate deal state via ``can_transition`` (422 on failure)
  • Check idempotency key — return existing record if already processed
  • Fetch deal record in-process (avoids HTTP when combined monolith)
  • Render Jinja2 → write to ``DEALMAKER_STORAGE_DIR``
  • Compute SHA-256 of rendered document (integrity anchor)
  • Persist to repository (MongoDB or in-memory fallback)
  • Transition deal status via deals service
  • Emit domain event (NoopPublisher → swap for NATS/Kafka)

Exceptions
----------
DealNotFoundError   — re-raised from deals service; maps to 404
InvalidDealState    — deal is not in the required state; maps to 422
MarginGuardError    — PO net margin below floor; maps to 422
PiNotFoundError     — PI record absent; maps to 404
PoNotFoundError     — PO record absent; maps to 404
PiStateError        — PI status does not allow the requested operation; maps to 422
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


from shared.bus import get_publisher
from shared.documents.render import environment as document_environment, render_document
from shared.connectors.google_drive import GoogleDriveError, get_connector as get_drive_connector
from shared.domain.events import DomainEvent, EventTopic
from shared.domain.state_machine import can_transition

from .config import settings
from .repository import get_repository
from .schemas import (
    AcceptPiRequest,
    DealDocumentsResponse,
    IssuePiRequest,
    IssuePoRequest,
    ExtendPiRequest,
    PiRecord,
    PiResponse,
    PoRecord,
    PoResponse,
)

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = document_environment(_TEMPLATES_DIR)

# What a PI / PO cannot be issued without: the document is rendered DRAFT otherwise.
_PI_EXPORTER_FIELDS = ("address", "iec")
_PO_EXPORTER_FIELDS = ("address", "gstin")
_QTY_TOLERANCE_PCT = 5


def _render_pi(*, pi_id: str, deal_id: str, deal: Any, buyer_name: str, buyer_address: str, buyer_vat_number: str | None,
               product_description: str, quantity_mt: float, agreed_price: float, currency: str,
               total_value: float, incoterm: str, port_of_destination: str, payment_terms: str,
               validity_days: int, issued_at: datetime, expires_at: datetime,
               qc_provider: str | None, escrow_provider: str | None, packing: str = "") -> str:
    from shared.documents.facts import party
    buyer = party(deal, "buyer")
    return render_document(_jinja_env, "pi.html.j2", {
        "pi_id": pi_id, "deal_id": deal_id, "buyer_name": buyer_name, "buyer_address": buyer_address,
        "buyer_country": buyer.get("country") or "", "buyer_vat_number": buyer_vat_number or "",
        "hs_code": deal.hs_code, "product_description": product_description, "quantity_mt": quantity_mt,
        "agreed_price": agreed_price, "currency": currency, "total_value": total_value, "incoterm": incoterm,
        "port_of_destination": port_of_destination, "port_of_loading": getattr(deal, "port_of_loading", "") or "",
        "payment_terms": payment_terms, "validity_days": validity_days, "issued_at": issued_at,
        "expires_at": expires_at, "qc_provider": qc_provider, "escrow_provider": escrow_provider,
        "tolerance_pct": _QTY_TOLERANCE_PCT, "packing": packing,
    }, exporter_fields=_PI_EXPORTER_FIELDS, required={"buyer address": buyer_address})


async def _packing_of(deal_id: str) -> str:
    """The buyer's packing requirements agreed in the negotiation, as one line."""
    try:
        from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
        from shared.orchestration import packing
        session = await _neg_repo().get_session_by_deal(deal_id)
        return packing.describe(getattr(session, "packing", None)) if session else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("DealMaker: packing for %s unavailable: %s", deal_id, exc)
        return ""


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DealNotFoundError(Exception):
    def __init__(self, deal_id: str) -> None:
        super().__init__(f"Deal {deal_id!r} not found")
        self.deal_id = deal_id


class InvalidDealState(Exception):
    """Raised when the deal is not in the required state for this operation."""
    def __init__(self, deal_id: str, current: str, required: str) -> None:
        super().__init__(
            f"Deal {deal_id!r} is in state {current!r}; required {required!r}"
        )
        self.deal_id = deal_id
        self.current = current
        self.required = required


class MarginGuardError(Exception):
    """Raised when PO issuance fails the net-margin floor check."""
    def __init__(self, deal_id: str, net_margin_pct: float, floor_pct: float) -> None:
        super().__init__(
            f"Deal {deal_id!r}: net margin {net_margin_pct:.2f}% is below "
            f"the floor of {floor_pct:.2f}%"
        )
        self.deal_id = deal_id
        self.net_margin_pct = net_margin_pct
        self.floor_pct = floor_pct


class PiNotFoundError(Exception):
    def __init__(self, pi_id: str) -> None:
        super().__init__(f"Proforma Invoice {pi_id!r} not found")
        self.pi_id = pi_id


class PoNotFoundError(Exception):
    def __init__(self, po_id: str) -> None:
        super().__init__(f"Purchase Order {po_id!r} not found")
        self.po_id = po_id


class MarginUnknownError(Exception):
    """A PO cannot be issued when no buyer price exists to check its margin against."""
    def __init__(self, deal_id: str) -> None:
        super().__init__(
            f"Deal {deal_id!r}: no buyer price on the deal or its Proforma Invoice — "
            "the margin cannot be checked, so the PO is refused"
        )
        self.deal_id = deal_id


class PiExpiredError(Exception):
    """A buyer cannot accept a Proforma Invoice past its validity."""
    def __init__(self, pi_id: str, expires_at: datetime) -> None:
        super().__init__(
            f"PI {pi_id!r} expired on {expires_at.date()} — extend it before recording an acceptance"
        )
        self.pi_id = pi_id
        self.expires_at = expires_at


def pi_expiry(pi: Any) -> datetime:
    expires_at = pi.expires_at
    return expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)


def is_pi_expired(pi: Any, now: datetime | None = None) -> bool:
    return pi_expiry(pi) <= (now or datetime.now(timezone.utc))


class PiStateError(Exception):
    """Raised when a PI action is not valid for the PI's current status."""
    def __init__(self, pi_id: str, current: str, allowed: str) -> None:
        super().__init__(
            f"PI {pi_id!r} is in status {current!r}; operation requires {allowed!r}"
        )
        self.pi_id = pi_id
        self.current = current
        self.allowed = allowed


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

def _new_pi_id() -> str:
    return f"PI-{uuid4().hex[:12].upper()}"


def _new_po_id() -> str:
    return f"PO-{uuid4().hex[:12].upper()}"


# ---------------------------------------------------------------------------
# Deal service bridge (in-process in combined monolith)
# ---------------------------------------------------------------------------

async def _fetch_deal(deal_id: str) -> Any:
    """Fetch DealRecord in-process; raises DealNotFoundError on miss."""
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        return await get_deal(deal_id)
    except Exception as exc:
        exc_str = str(exc)
        if "not found" in exc_str.lower():
            raise DealNotFoundError(deal_id) from exc
        raise


async def _transition_deal(deal_id: str, target_status: str, reason: str) -> None:
    """Transition deal status via the deals service; raises on failure."""
    try:
        from services.deals.service import update_status  # type: ignore[import]
        from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
        from shared.domain.enums import DealStatus

        target = DealStatus(target_status)
        req = UpdateStatusRequest(target_status=target, reason=reason)
        await update_status(deal_id, req)
    except Exception as exc:
        logger.error(
            "DealMaker: failed to transition deal %s to %s: %s",
            deal_id,
            target_status,
            exc,
        )
        raise


async def _register_buyer_organization(buyer_name: str, buyer_email: str | None) -> None:
    """Best-effort: register/update a Portless-visible Organization entry for
    the buyer once the deal is signed (PI accepted). Never raises — a lookup
    failure here must never block PI acceptance, which already committed.
    """
    if not buyer_email:
        return
    try:
        from services.auth.service import find_or_create_organization_for_deal  # type: ignore[import]

        await find_or_create_organization_for_deal(buyer_name, buyer_email)
    except Exception as exc:
        logger.warning(
            "DealMaker: could not register organization for buyer %r (%s): %s",
            buyer_name, buyer_email, exc,
        )


# ---------------------------------------------------------------------------
# Document storage helpers
# ---------------------------------------------------------------------------

def _storage_dir() -> Path:
    p = Path(settings.dealmaker_storage_dir)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # e.g. PermissionError creating a drive-root path on Windows — never
        # let local-fallback storage kill document issuance.
        fallback = Path(tempfile.gettempdir()) / "portless-docs" / "dealmaker"
        logger.warning(
            "DealMaker: cannot create storage dir %s (%s) — falling back to %s",
            p, exc, fallback,
        )
        fallback.mkdir(parents=True, exist_ok=True)
        p = fallback
    return p


async def _write_document(deal_id: str, filename: str, content: str) -> str:
    """Store rendered HTML in Google Drive (Portless/<deal_id>/<filename>).

    Drive is the primary document store; local disk is an emergency fallback
    only (Drive unconfigured, or an upload failed) so issuance never blocks.
    Every local write is logged loudly — a document landing on disk in
    production means the Drive setup needs attention. Returns the storage URI.
    """
    drive = get_drive_connector()
    if drive.is_configured():
        try:
            result = await asyncio.to_thread(drive.upload_document, deal_id, filename, content)
            return result["web_view_link"] or f"gdrive://{result['file_id']}"
        except GoogleDriveError as exc:
            logger.error(
                "DealMaker: Drive upload failed for %s (deal %s), falling back to local disk: %s",
                filename, deal_id, exc,
            )
    else:
        logger.warning(
            "DealMaker: Google Drive not configured (GOOGLE_DRIVE_CLIENT_ID/"
            "CLIENT_SECRET/REFRESH_TOKEN) — %s (deal %s) written to local disk. "
            "Drive is the intended primary document store; run "
            "scripts/google_drive_authorize.py to enable it.",
            filename, deal_id,
        )

    dest = _storage_dir() / filename
    dest.write_text(content, encoding="utf-8")
    return dest.as_uri()


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Event emission helper
# ---------------------------------------------------------------------------

async def _emit(topic: EventTopic, deal_id: str, payload: dict) -> None:
    try:
        await get_publisher().publish(
            DomainEvent(topic=topic, deal_id=deal_id, payload=payload)
        )
    except Exception as exc:
        logger.warning("DealMaker: failed to emit %s for deal %s: %s", topic, deal_id, exc)


# ---------------------------------------------------------------------------
# PI — Proforma Invoice
# ---------------------------------------------------------------------------

async def issue_pi(req: IssuePiRequest) -> PiResponse:
    """Issue a Proforma Invoice for the buyer.

    Pre-conditions:
      • Deal must be in ``NEGOTIATION_AGREED`` state.
      • Idempotency key (if supplied) must not match an existing PI.

    Side-effects:
      • Renders ``pi.html.j2`` → writes to ``DEALMAKER_STORAGE_DIR``
      • Persists ``PiRecord``
      • Transitions deal → ``PI_SENT``
      • Emits ``pi.sent``
    """
    repo = get_repository()

    # ── Idempotency check ─────────────────────────────────────────────────
    if req.idempotency_key:
        existing = await repo.get_pi_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            logger.info(
                "DealMaker: returning existing PI %s (idempotency_key=%s)",
                existing.pi_id,
                req.idempotency_key,
            )
            return PiResponse.from_record(existing)

    # ── Fetch deal and validate state ─────────────────────────────────────
    deal = await _fetch_deal(req.deal_id)
    deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)

    if deal_status != "NEGOTIATION_AGREED":
        raise InvalidDealState(req.deal_id, deal_status, "NEGOTIATION_AGREED")

    from shared.domain.enums import DealStatus
    if not can_transition(DealStatus(deal_status), DealStatus.PI_SENT):
        raise InvalidDealState(req.deal_id, deal_status, "NEGOTIATION_AGREED")

    # ── Derive party details from deal when not supplied ──────────────────
    buyer_name = req.buyer_name or _party_name(deal, "buyer") or "Buyer"
    buyer_address = req.buyer_address or _party_address(deal, "buyer") or ""
    buyer_vat_number = req.buyer_vat_number or _party_vat(deal, "buyer")
    product_description = req.product_description or _hs_description(deal.hs_code)

    now = datetime.now(timezone.utc)
    pi_id = _new_pi_id()
    total_value = req.agreed_price * req.quantity_mt
    expires_at = now + timedelta(days=req.validity_days)

    # ── Render template ───────────────────────────────────────────────────
    packing_spec = await _packing_of(req.deal_id)
    html = _render_pi(
        pi_id=pi_id, deal_id=req.deal_id, deal=deal, buyer_name=buyer_name, buyer_address=buyer_address,
        buyer_vat_number=buyer_vat_number, product_description=product_description,
        quantity_mt=req.quantity_mt, agreed_price=req.agreed_price, currency=req.currency,
        total_value=total_value, incoterm=req.incoterm, port_of_destination=req.port_of_destination,
        payment_terms=req.payment_terms, validity_days=req.validity_days, issued_at=now,
        expires_at=expires_at, qc_provider=req.qc_provider, escrow_provider=req.escrow_provider,
        packing=packing_spec,
    )

    content_hash = _sha256(html)
    storage_uri = await _write_document(req.deal_id, f"{pi_id}.html", html)

    # ── Persist record ────────────────────────────────────────────────────
    pi_record = PiRecord(
        pi_id=pi_id,
        deal_id=req.deal_id,
        status="SENT",
        agreed_price=req.agreed_price,
        currency=req.currency,
        quantity_mt=req.quantity_mt,
        total_value=total_value,
        incoterm=req.incoterm,
        port_of_destination=req.port_of_destination,
        payment_terms=req.payment_terms,
        validity_days=req.validity_days,
        buyer_name=buyer_name,
        buyer_address=buyer_address,
        buyer_vat_number=buyer_vat_number,
        qc_provider=req.qc_provider,
        escrow_provider=req.escrow_provider,
        packing_spec=packing_spec or None,
        storage_uri=storage_uri,
        content_hash=content_hash,
        issued_at=now,
        expires_at=expires_at,
        idempotency_key=req.idempotency_key,
    )
    await repo.save_pi(pi_record)

    # ── State transition + event ──────────────────────────────────────────
    await _transition_deal(req.deal_id, "PI_SENT", f"PI {pi_id} issued to buyer")
    await _emit(
        EventTopic.PI_SENT,
        req.deal_id,
        {
            "pi_id": pi_id,
            "buyer_name": buyer_name,
            "total_value": total_value,
            "currency": req.currency,
            "expires_at": expires_at.isoformat(),
            "content_hash": content_hash,
        },
    )

    logger.info(
        "DealMaker: PI %s issued for deal %s (%.4f %s x %.3f MT = %.2f %s)",
        pi_id,
        req.deal_id,
        req.agreed_price,
        req.currency,
        req.quantity_mt,
        total_value,
        req.currency,
    )
    return PiResponse.from_record(pi_record)


async def accept_pi(pi_id: str, req: AcceptPiRequest) -> PiResponse:
    """Record buyer acceptance of a Proforma Invoice.

    Pre-conditions:
      • PI must exist and be in ``SENT`` status.

    Side-effects:
      • Updates PI status to ``ACCEPTED``
      • Transitions deal → ``PI_ACCEPTED``
      • Emits ``pi.accepted``
      • Best-effort: registers an Organization entry for the buyer with
        ``req.buyer_email`` (see _register_buyer_organization) if supplied
    """
    repo = get_repository()
    pi = await repo.get_pi(pi_id)
    if pi is None:
        raise PiNotFoundError(pi_id)

    if pi.status != "SENT":
        raise PiStateError(pi_id, pi.status, "SENT")
    if is_pi_expired(pi):
        raise PiExpiredError(pi_id, pi_expiry(pi))

    now = datetime.now(timezone.utc)
    updated = pi.model_copy(update={"status": "ACCEPTED", "accepted_at": now})
    await repo.save_pi(updated)

    await _transition_deal(pi.deal_id, "PI_ACCEPTED", f"PI {pi_id} accepted by buyer")
    await _register_buyer_organization(pi.buyer_name, req.buyer_email)
    await _emit(
        EventTopic.PI_ACCEPTED,
        pi.deal_id,
        {
            "pi_id": pi_id,
            "accepted_by": req.accepted_by,
            "accepted_at": now.isoformat(),
            "notes": req.notes,
        },
    )

    logger.info("DealMaker: PI %s accepted for deal %s", pi_id, pi.deal_id)
    return PiResponse.from_record(updated)


async def extend_pi(pi_id: str, req: ExtendPiRequest, by: str) -> PiResponse:
    """Give an unaccepted PI a new validity (staff decision, audited on the
    record). The document is rendered again so it states the new date."""
    repo = get_repository()
    pi = await repo.get_pi(pi_id)
    if pi is None:
        raise PiNotFoundError(pi_id)
    if pi.status != "SENT":
        raise PiStateError(pi_id, pi.status, "SENT")

    deal = await _fetch_deal(pi.deal_id)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=req.validity_days)
    html = _render_pi(
        pi_id=pi.pi_id, deal_id=pi.deal_id, deal=deal, buyer_name=pi.buyer_name, buyer_address=pi.buyer_address,
        buyer_vat_number=pi.buyer_vat_number, product_description=_hs_description(deal.hs_code),
        quantity_mt=pi.quantity_mt, agreed_price=pi.agreed_price, currency=pi.currency,
        total_value=pi.total_value, incoterm=pi.incoterm, port_of_destination=pi.port_of_destination,
        payment_terms=pi.payment_terms, validity_days=req.validity_days, issued_at=pi.issued_at,
        expires_at=expires_at, qc_provider=pi.qc_provider, escrow_provider=pi.escrow_provider,
        packing=pi.packing_spec or "",
    )
    storage_uri = await _write_document(pi.deal_id, f"{pi.pi_id}.html", html)
    updated = pi.model_copy(update={
        "expires_at": expires_at,
        "validity_days": req.validity_days,
        "storage_uri": storage_uri,
        "content_hash": _sha256(html),
        "extensions": [*pi.extensions, {
            "by": by, "reason": req.reason, "at": now.isoformat(),
            "previous_expires_at": pi_expiry(pi).isoformat(), "new_expires_at": expires_at.isoformat(),
        }],
    })
    await repo.save_pi(updated)
    logger.info("DealMaker: PI %s extended to %s by %s", pi_id, expires_at.date(), by)
    return PiResponse.from_record(updated)


async def get_pi(pi_id: str) -> PiResponse:
    """Fetch a PI by ID; raises PiNotFoundError on miss."""
    repo = get_repository()
    pi = await repo.get_pi(pi_id)
    if pi is None:
        raise PiNotFoundError(pi_id)
    return PiResponse.from_record(pi)


# ---------------------------------------------------------------------------
# PO — Purchase Order
# ---------------------------------------------------------------------------

async def issue_po(req: IssuePoRequest) -> PoResponse:
    """Issue a Purchase Order to the supplier.

    Pre-conditions:
      • Deal must be in ``ESCROW_FUNDED`` state.
      • Net margin of this PO must be >= ``po_margin_floor_pct``.

    Margin calculation:
      net_margin_pct = (buyer_price - po_price) / buyer_price * 100
      buyer_price is the PI's agreed price, else the deal's target price.
      With neither the margin cannot be checked and the PO is refused.

    Side-effects:
      • Renders ``po.html.j2`` → writes to ``DEALMAKER_STORAGE_DIR``
      • Persists ``PoRecord`` with net_margin_pct for audit
      • Transitions deal → ``PO_ISSUED``
      • Emits ``po.issued``
    """
    from shared.orchestration import kill_switch
    await kill_switch.assert_running(f"Purchase order for deal {req.deal_id}")
    repo = get_repository()

    # ── Idempotency check ─────────────────────────────────────────────────
    if req.idempotency_key:
        existing = await repo.get_po_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            logger.info(
                "DealMaker: returning existing PO %s (idempotency_key=%s)",
                existing.po_id,
                req.idempotency_key,
            )
            return PoResponse.from_record(existing)

    # ── Fetch deal and validate state ─────────────────────────────────────
    deal = await _fetch_deal(req.deal_id)
    deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)

    if deal_status != "ESCROW_FUNDED":
        raise InvalidDealState(req.deal_id, deal_status, "ESCROW_FUNDED")

    from shared.domain.enums import DealStatus
    if not can_transition(DealStatus(deal_status), DealStatus.PO_ISSUED):
        raise InvalidDealState(req.deal_id, deal_status, "ESCROW_FUNDED")

    # ── Margin guard ──────────────────────────────────────────────────────
    # The PI's agreed price is what the buyer actually pays into escrow, so it
    # is the revenue this PO is measured against. The deal's target price is
    # only the buyer's opening wish: checking against it blocked profitable
    # POs when the agreed price was higher, and — worse — passed losing POs
    # when the buyer had negotiated below the target.
    floor = settings.po_margin_floor_pct
    pi = await repo.get_pi_by_deal(req.deal_id)
    buyer_price: float | None = pi.agreed_price if pi is not None else None
    if not buyer_price or buyer_price <= 0:
        buyer_price = getattr(deal, "buyer_target_price_usd", None)
    if not buyer_price or buyer_price <= 0:
        raise MarginUnknownError(req.deal_id)
    # Net of every PortLess cost (freight, inspection, customs, documents,
    # insurance, escrow fee) when the negotiation worked out a cost sheet;
    # the gross spread otherwise.
    sheet = None
    try:
        from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
        session = await _neg_repo().get_session_by_deal(req.deal_id)
        sheet = getattr(session, "cost_sheet", None) if session is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("DealMaker: cost sheet lookup for %s failed: %s", req.deal_id, exc)
    if sheet:
        from shared.orchestration import trade_costs
        net_margin_pct = trade_costs.net_margin_pct(sheet, buyer_price, req.agreed_price)
    else:
        net_margin_pct = (buyer_price - req.agreed_price) / buyer_price * 100.0

    if net_margin_pct < floor:
        raise MarginGuardError(req.deal_id, net_margin_pct, floor)

    # ── Derive party details ──────────────────────────────────────────────
    supplier_name = req.supplier_name or _party_name(deal, "supplier") or "Supplier"
    supplier_address = req.supplier_address or _party_address(deal, "supplier") or ""
    supplier_gstin = req.supplier_gstin or _party_gstin(deal)
    supplier_iec = req.supplier_iec or _party_iec(deal)
    product_description = req.product_description or _hs_description(deal.hs_code)
    # PortLess is the purchaser on the PO: the supplier never sees the overseas buyer.
    buyer_name = settings.portless_entity_name

    now = datetime.now(timezone.utc)
    po_id = _new_po_id()
    total_value = req.agreed_price * req.quantity_mt
    delivery_deadline_str = (
        req.delivery_deadline.isoformat()
        if isinstance(req.delivery_deadline, date)
        else str(req.delivery_deadline)
    )

    # ── Render template ───────────────────────────────────────────────────
    packing_spec = await _packing_of(req.deal_id)
    html = render_document(_jinja_env, "po.html.j2", {
        "po_id": po_id, "deal_id": req.deal_id, "buyer_name": buyer_name,
        "supplier_name": supplier_name, "supplier_address": supplier_address,
        "supplier_gstin": supplier_gstin or "", "supplier_iec": supplier_iec or "",
        "hs_code": deal.hs_code, "product_description": product_description,
        "quantity_mt": req.quantity_mt, "agreed_price": req.agreed_price, "currency": req.currency,
        "total_value": total_value, "incoterm": req.incoterm, "port_of_loading": req.port_of_loading,
        "payment_terms": req.payment_terms, "delivery_deadline": req.delivery_deadline,
        "quality_spec": req.quality_spec or "", "packing": packing_spec, "issued_at": now,
    }, exporter_fields=_PO_EXPORTER_FIELDS, required={"supplier address": supplier_address})

    content_hash = _sha256(html)
    storage_uri = await _write_document(req.deal_id, f"{po_id}.html", html)

    # ── Persist record ────────────────────────────────────────────────────
    po_record = PoRecord(
        po_id=po_id,
        deal_id=req.deal_id,
        status="ISSUED",
        agreed_price=req.agreed_price,
        currency=req.currency,
        quantity_mt=req.quantity_mt,
        total_value=total_value,
        incoterm=req.incoterm,
        port_of_loading=req.port_of_loading,
        delivery_deadline=req.delivery_deadline,
        payment_terms=req.payment_terms,
        supplier_name=supplier_name,
        supplier_address=supplier_address,
        supplier_gstin=supplier_gstin,
        supplier_iec=supplier_iec,
        quality_spec=req.quality_spec,
        packing_spec=packing_spec or None,
        net_margin_pct=net_margin_pct,
        margin_floor_pct=floor,
        storage_uri=storage_uri,
        content_hash=content_hash,
        issued_at=now,
        idempotency_key=req.idempotency_key,
    )
    await repo.save_po(po_record)

    # ── State transition + event ──────────────────────────────────────────
    await _transition_deal(req.deal_id, "PO_ISSUED", f"PO {po_id} issued to supplier")
    await _emit(
        EventTopic.PO_ISSUED,
        req.deal_id,
        {
            "po_id": po_id,
            "supplier_name": supplier_name,
            "total_value": total_value,
            "currency": req.currency,
            "net_margin_pct": net_margin_pct,
            "delivery_deadline": delivery_deadline_str,
            "content_hash": content_hash,
        },
    )

    logger.info(
        "DealMaker: PO %s issued for deal %s (%.4f %s x %.3f MT = %.2f %s, margin=%.2f%%)",
        po_id,
        req.deal_id,
        req.agreed_price,
        req.currency,
        req.quantity_mt,
        total_value,
        req.currency,
        net_margin_pct,
    )

    await _patch_deal_est_margin(req.deal_id, po_record)
    return PoResponse.from_record(po_record)


async def _patch_deal_est_margin(deal_id: str, po: PoRecord) -> None:
    """Write est_margin_usd / est_margin_pct onto the deal once both contract values are known.

    Estimated margin = PI total (buyer revenue) − PO total (supplier cost) −
    PortLess's own estimated costs from the deal's cost sheet (freight,
    inspection, customs, documents, insurance, escrow fee; the contingency is a
    pricing buffer, not a cost) — the same basis as the realised margin, so the
    two cards compare like with like. ``est_margin_pct`` is that profit as a %
    of the PI total. Best-effort: a failure here must never block PO issuance."""
    try:
        pi = await get_repository().get_pi_by_deal(deal_id)
        if pi is None:
            return
        costs = 0.0
        try:
            from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
            from shared.orchestration import trade_costs
            session = await _neg_repo().get_session_by_deal(deal_id)
            sheet = getattr(session, "cost_sheet", None) if session is not None else None
            if sheet and pi.quantity_mt:
                lines = trade_costs.cost_usd(sheet, pi.total_value / pi.quantity_mt, pi.quantity_mt)
                costs = sum(v for k, v in lines.items() if k != "contingency")
        except Exception as exc:  # noqa: BLE001
            logger.debug("DealMaker: cost sheet for est margin of %s unavailable: %s", deal_id, exc)
        est = round(pi.total_value - po.total_value - costs, 2)
        pct = round(est / pi.total_value * 100.0, 2) if pi.total_value else None
        from services.deals.schemas import UpdateFinancialsRequest  # type: ignore[import]
        from services.deals.service import update_financials  # type: ignore[import]

        await update_financials(deal_id, UpdateFinancialsRequest(est_margin_usd=est, est_margin_pct=pct))
        logger.info("DealMaker: est_margin_usd=%.2f (%.2f%%) patched onto deal %s", est, pct or 0.0, deal_id)
    except Exception as exc:
        logger.warning("DealMaker: could not patch est margin for deal %s: %s", deal_id, exc)


async def get_po(po_id: str) -> PoResponse:
    """Fetch a PO by ID; raises PoNotFoundError on miss."""
    repo = get_repository()
    po = await repo.get_po(po_id)
    if po is None:
        raise PoNotFoundError(po_id)
    return PoResponse.from_record(po)


async def get_deal_documents(deal_id: str) -> DealDocumentsResponse:
    """Return all DealMaker documents for a deal (PI + PO, whichever exist)."""
    repo = get_repository()
    pi_rec = await repo.get_pi_by_deal(deal_id)
    po_rec = await repo.get_po_by_deal(deal_id)
    return DealDocumentsResponse(
        deal_id=deal_id,
        pi=PiResponse.from_record(pi_rec) if pi_rec else None,
        po=PoResponse.from_record(po_rec) if po_rec else None,
    )


async def get_pi_file(pi_id: str) -> tuple[bytes, str, str]:
    """Return (content, mime_type, filename) for a PI's rendered document.

    Streams from wherever the file lives (Google Drive via the backend's own
    credentials, or the local-disk fallback) — backs the UI's View/Download
    buttons. Raises FileNotFoundError / GoogleDriveError on retrieval failure."""
    from services.docugen.service import load_stored_document  # type: ignore[import]

    repo = get_repository()
    pi = await repo.get_pi(pi_id)
    if pi is None:
        raise PiNotFoundError(pi_id)
    content, mime = await load_stored_document(pi.storage_uri)
    return content, mime, f"{pi.deal_id}-{pi.pi_id}.html"


async def get_po_file(po_id: str) -> tuple[bytes, str, str]:
    """Return (content, mime_type, filename) for a PO's rendered document."""
    from services.docugen.service import load_stored_document  # type: ignore[import]

    repo = get_repository()
    po = await repo.get_po(po_id)
    if po is None:
        raise PoNotFoundError(po_id)
    content, mime = await load_stored_document(po.storage_uri)
    return content, mime, f"{po.deal_id}-{po.po_id}.html"


# ---------------------------------------------------------------------------
# Party-field extraction helpers (graceful — never raise)
# ---------------------------------------------------------------------------

def _party_name(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        party_role = getattr(p, "role", "") or ""
        if party_role.lower() == role.lower():
            return getattr(p, "legal_name", None) or getattr(p, "name", None) or getattr(p, "company_name", None)
    return None


def _party_address(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        party_role = getattr(p, "role", "") or ""
        if party_role.lower() == role.lower():
            return getattr(p, "address", None)
    return None


def _party_vat(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        party_role = getattr(p, "role", "") or ""
        if party_role.lower() == role.lower():
            return getattr(p, "vat_number", None)
    return None


def _party_gstin(deal: Any) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        if (getattr(p, "role", "") or "").lower() == "supplier":
            return getattr(p, "gstin", None)
    return None


def _party_iec(deal: Any) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        if (getattr(p, "role", "") or "").lower() == "supplier":
            return getattr(p, "iec", None)
    return None


_HS_DESCRIPTIONS: dict[str, str] = {
    "1006": "Rice",
    "1001": "Wheat",
    "1507": "Soybean Oil",
    "1511": "Palm Oil",
    "1701": "Sugar",
    "0901": "Coffee",
    "0902": "Tea",
    "2401": "Tobacco",
    "7201": "Pig Iron",
    "7207": "Semi-finished Steel Products",
    "2601": "Iron Ore",
    "2701": "Coal",
    "2711": "LNG / LPG",
}


def _hs_description(hs_code: str) -> str:
    from shared.documents.facts import product_name
    return product_name(hs_code)
