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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape

from shared.bus import get_publisher
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
    PiRecord,
    PiResponse,
    PoRecord,
    PoResponse,
)

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)


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
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _write_document(deal_id: str, filename: str, content: str) -> str:
    """Store rendered HTML under Google Drive (Portless/<deal_id>/<filename>)
    when configured, else write to local disk. Returns the storage URI."""
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
    tmpl = _jinja_env.get_template("pi.html.j2")
    html = tmpl.render(
        pi_id=pi_id,
        deal_id=req.deal_id,
        seller_name=settings.portless_entity_name,
        seller_address=settings.portless_entity_address,
        seller_iec=settings.portless_entity_iec or "",
        seller_gstin=settings.portless_entity_gstin or "",
        buyer_name=buyer_name,
        buyer_address=buyer_address,
        buyer_vat_number=buyer_vat_number or "",
        hs_code=deal.hs_code,
        product_description=product_description,
        quantity_mt=req.quantity_mt,
        agreed_price=req.agreed_price,
        currency=req.currency,
        total_value=total_value,
        incoterm=req.incoterm,
        port_of_destination=req.port_of_destination,
        port_of_loading=getattr(deal, "port_of_loading", ""),
        payment_terms=req.payment_terms,
        validity_days=req.validity_days,
        issued_at=now.strftime("%Y-%m-%d"),
        expires_at=expires_at.strftime("%Y-%m-%d"),
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
      net_margin_pct = (buyer_target_price - po_price) / buyer_target_price * 100
      When buyer_target_price is unavailable the margin guard is skipped with a warning.

    Side-effects:
      • Renders ``po.html.j2`` → writes to ``DEALMAKER_STORAGE_DIR``
      • Persists ``PoRecord`` with net_margin_pct for audit
      • Transitions deal → ``PO_ISSUED``
      • Emits ``po.issued``
    """
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
    floor = settings.po_margin_floor_pct
    buyer_price: float | None = getattr(deal, "buyer_target_price_usd", None)
    if buyer_price and buyer_price > 0:
        net_margin_pct: float | None = (buyer_price - req.agreed_price) / buyer_price * 100.0
    else:
        logger.warning(
            "DealMaker: buyer_target_price_usd not set on deal %s — margin guard skipped",
            req.deal_id,
        )
        net_margin_pct = None

    if net_margin_pct is not None and net_margin_pct < floor:
        raise MarginGuardError(req.deal_id, net_margin_pct, floor)

    # ── Derive party details ──────────────────────────────────────────────
    supplier_name = req.supplier_name or _party_name(deal, "supplier") or "Supplier"
    supplier_address = req.supplier_address or _party_address(deal, "supplier") or ""
    supplier_gstin = req.supplier_gstin or _party_gstin(deal)
    supplier_iec = req.supplier_iec or _party_iec(deal)
    product_description = req.product_description or _hs_description(deal.hs_code)
    buyer_name = _party_name(deal, "buyer") or settings.portless_entity_name

    now = datetime.now(timezone.utc)
    po_id = _new_po_id()
    total_value = req.agreed_price * req.quantity_mt
    delivery_deadline_str = (
        req.delivery_deadline.isoformat()
        if isinstance(req.delivery_deadline, date)
        else str(req.delivery_deadline)
    )

    # ── Render template ───────────────────────────────────────────────────
    tmpl = _jinja_env.get_template("po.html.j2")
    html = tmpl.render(
        po_id=po_id,
        deal_id=req.deal_id,
        buyer_name=buyer_name,
        buyer_address=settings.portless_entity_address,
        supplier_name=supplier_name,
        supplier_address=supplier_address,
        supplier_gstin=supplier_gstin or "",
        supplier_iec=supplier_iec or "",
        hs_code=deal.hs_code,
        product_description=product_description,
        quantity_mt=req.quantity_mt,
        agreed_price=req.agreed_price,
        currency=req.currency,
        total_value=total_value,
        incoterm=req.incoterm,
        port_of_loading=req.port_of_loading,
        payment_terms=req.payment_terms,
        delivery_deadline=delivery_deadline_str,
        quality_spec=req.quality_spec or "",
        issued_at=now.strftime("%Y-%m-%d"),
    )

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
        net_margin_pct if net_margin_pct is not None else 0.0,
    )
    return PoResponse.from_record(po_record)


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


# ---------------------------------------------------------------------------
# Party-field extraction helpers (graceful — never raise)
# ---------------------------------------------------------------------------

def _party_name(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        party_role = getattr(p, "role", "") or ""
        if party_role.lower() == role.lower():
            return getattr(p, "name", None) or getattr(p, "company_name", None)
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
    prefix = re.sub(r"\D", "", hs_code)[:4]
    return _HS_DESCRIPTIONS.get(prefix, f"Commodity (HS {hs_code})")
