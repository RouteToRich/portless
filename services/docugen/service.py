"""DocuGen service — business logic layer.

Generates export and customs documents from Jinja2 templates:
  CI            — Commercial Invoice
  PL            — Packing List
  CoO           — Certificate of Origin
  SDF           — Shipper's Declaration Form
  SHIPPING_BILL — ICEGATE Shipping Bill

Each document:
  • Is idempotency-safe (re-submitting the same key returns the cached record)
  • Is rendered to an HTML file in DOCUGEN_STORAGE_DIR
  • Has its SHA-256 content hash stored for integrity verification
  • Emits the appropriate domain event

``file_customs()`` assembles the Shipping Bill and simulates ICEGATE submission
(stub for the real ICEGATE API; emits customs.filed when complete).

Exceptions
----------
DealNotFoundError       — 404 on unknown deal_id
DocuGenError            — 422 on missing required context fields
DocumentNotFoundError   — 404 on unknown doc_id
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape

from shared.bus import get_publisher
from shared.connectors.google_drive import GoogleDriveError, get_connector as get_drive_connector
from shared.domain.enums import DocumentType
from shared.domain.events import DomainEvent, EventTopic

from .config import settings
from .repository import get_repository
from .schemas import (
    CustomsFilingRequest,
    CustomsFilingResponse,
    DealDocumentsListResponse,
    DocumentRecord,
    DocumentResponse,
    GenerateDocumentRequest,
)

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)

# Maps DocumentType to template file name
_TEMPLATE_MAP: dict[DocumentType, str] = {
    DocumentType.COMMERCIAL_INVOICE: "ci.html.j2",
    DocumentType.PACKING_LIST: "pl.html.j2",
    DocumentType.CERTIFICATE_OF_ORIGIN: "coo.html.j2",
    DocumentType.SDF: "sdf.html.j2",
    DocumentType.SHIPPING_BILL: "shipping_bill.html.j2",
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DealNotFoundError(Exception):
    def __init__(self, deal_id: str) -> None:
        super().__init__(f"Deal {deal_id!r} not found")
        self.deal_id = deal_id


class DocuGenError(Exception):
    """Missing required context; maps to 422."""


class DocumentNotFoundError(Exception):
    def __init__(self, doc_id: str) -> None:
        super().__init__(f"Document {doc_id!r} not found")
        self.doc_id = doc_id


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------

def _new_doc_id(doc_type: DocumentType) -> str:
    prefix = doc_type.value.replace("_", "")[:4].upper()
    return f"{prefix}-{uuid4().hex[:12].upper()}"


# ---------------------------------------------------------------------------
# Deal bridge
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


# ---------------------------------------------------------------------------
# Document storage helpers
# ---------------------------------------------------------------------------

def _storage_dir() -> Path:
    p = Path(settings.docugen_storage_dir)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # e.g. PermissionError creating a drive-root path on Windows — never
        # let local-fallback storage kill document issuance.
        fallback = Path(tempfile.gettempdir()) / "portless-docs" / "docugen"
        logger.warning(
            "DocuGen: cannot create storage dir %s (%s) — falling back to %s",
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
                "DocuGen: Drive upload failed for %s (deal %s), falling back to local disk: %s",
                filename, deal_id, exc,
            )
    else:
        logger.warning(
            "DocuGen: Google Drive not configured (GOOGLE_DRIVE_CLIENT_ID/"
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
# Event helper
# ---------------------------------------------------------------------------

async def _emit(topic: EventTopic, deal_id: str, payload: dict) -> None:
    try:
        await get_publisher().publish(
            DomainEvent(topic=topic, deal_id=deal_id, payload=payload)
        )
    except Exception as exc:
        logger.warning("DocuGen: failed to emit %s for deal %s: %s", topic, deal_id, exc)


# ---------------------------------------------------------------------------
# Party helpers (graceful — never raise)
# ---------------------------------------------------------------------------

def _party_name(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        if (getattr(p, "role", "") or "").lower() == role.lower():
            return getattr(p, "legal_name", None) or getattr(p, "name", None) or getattr(p, "company_name", None)
    return None


def _party_address(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        if (getattr(p, "role", "") or "").lower() == role.lower():
            return getattr(p, "address", None)
    return None


def _party_vat(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        if (getattr(p, "role", "") or "").lower() == role.lower():
            return getattr(p, "vat_number", None)
    return None


def _party_iec(deal: Any) -> str | None:
    return settings.portless_entity_iec or None


_HS_DESCRIPTIONS: dict[str, str] = {
    "1006": "Rice", "1001": "Wheat", "1507": "Soybean Oil",
    "1511": "Palm Oil", "1701": "Sugar", "0901": "Coffee",
    "0902": "Tea", "2401": "Tobacco", "7201": "Pig Iron",
    "7207": "Semi-finished Steel Products", "2601": "Iron Ore",
    "2701": "Coal", "2711": "LNG / LPG",
}


def _hs_description(hs_code: str) -> str:
    prefix = re.sub(r"\D", "", hs_code)[:4]
    return _HS_DESCRIPTIONS.get(prefix, f"Commodity (HS {hs_code})")


def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Context builders (one per document type)
# ---------------------------------------------------------------------------

def _build_ci_context(req: GenerateDocumentRequest, deal: Any, doc_id: str) -> dict:
    qty = req.quantity_mt or getattr(deal, "quantity_mt", 0.0) or 0.0
    price = req.agreed_price or getattr(deal, "buyer_target_price_usd", 0.0) or 0.0
    total = qty * price
    return {
        "doc_id": doc_id,
        "deal_id": req.deal_id,
        "invoice_date": req.invoice_date.isoformat() if req.invoice_date else _today(),
        "exporter_name": settings.portless_entity_name,
        "exporter_address": settings.portless_entity_address,
        "exporter_gstin": settings.portless_entity_gstin,
        "exporter_iec": settings.portless_entity_iec,
        "exporter_pan": settings.portless_entity_pan,
        "buyer_name": req.buyer_name or _party_name(deal, "buyer") or "Buyer",
        "buyer_address": req.buyer_address or _party_address(deal, "buyer") or "",
        "buyer_vat_number": req.buyer_vat_number or _party_vat(deal, "buyer") or "",
        "hs_code": deal.hs_code,
        "description": _hs_description(deal.hs_code),
        "quantity_mt": qty,
        "unit_price": price,
        "currency": req.currency,
        "total_value": total,
        "incoterm": req.incoterm or "CIF",
        "port_of_loading": req.port_of_loading or "",
        "port_of_destination": req.port_of_destination or getattr(deal, "market", ""),
        "payment_terms": req.payment_terms or "30% advance, 70% on BL",
        "country_of_origin": req.country_of_origin,
    }


def _build_pl_context(req: GenerateDocumentRequest, deal: Any, doc_id: str) -> dict:
    return {
        "doc_id": doc_id,
        "deal_id": req.deal_id,
        "packing_date": _today(),
        "exporter_name": settings.portless_entity_name,
        "exporter_address": settings.portless_entity_address,
        "buyer_name": req.buyer_name or _party_name(deal, "buyer") or "Buyer",
        "buyer_address": req.buyer_address or _party_address(deal, "buyer") or "",
        "hs_code": deal.hs_code,
        "description": _hs_description(deal.hs_code),
        "no_of_packages": req.no_of_packages or 1,
        "package_type": req.package_type or "Jute bags",
        "net_weight_mt": req.net_weight_mt or req.quantity_mt or 0.0,
        "gross_weight_mt": req.gross_weight_mt or (req.quantity_mt or 0.0) * 1.02,
        "marks_and_numbers": req.marks_and_numbers or f"{doc_id[:8]}",
        "port_of_loading": req.port_of_loading or "",
        "port_of_destination": req.port_of_destination or getattr(deal, "market", ""),
    }


def _build_coo_context(req: GenerateDocumentRequest, deal: Any, doc_id: str) -> dict:
    return {
        "doc_id": doc_id,
        "deal_id": req.deal_id,
        "issue_date": _today(),
        "exporter_name": settings.portless_entity_name,
        "exporter_address": settings.portless_entity_address,
        "exporter_iec": settings.portless_entity_iec,
        "buyer_name": req.buyer_name or _party_name(deal, "buyer") or "Buyer",
        "buyer_address": req.buyer_address or _party_address(deal, "buyer") or "",
        "hs_code": deal.hs_code,
        "description": _hs_description(deal.hs_code),
        "quantity_mt": req.quantity_mt or 0.0,
        "net_weight_mt": req.net_weight_mt or req.quantity_mt or 0.0,
        "port_of_loading": req.port_of_loading or "",
        "port_of_destination": req.port_of_destination or getattr(deal, "market", ""),
        "country_of_origin": req.country_of_origin,
        "chamber_name": req.coo_chamber or "Federation of Indian Export Organisations (FIEO)",
    }


def _build_sdf_context(req: GenerateDocumentRequest, deal: Any, doc_id: str) -> dict:
    qty = req.quantity_mt or 0.0
    price = req.agreed_price or getattr(deal, "buyer_target_price_usd", 0.0) or 0.0
    fob_value = qty * price
    return {
        "doc_id": doc_id,
        "deal_id": req.deal_id,
        "sdf_date": _today(),
        "exporter_name": settings.portless_entity_name,
        "exporter_address": settings.portless_entity_address,
        "exporter_iec": settings.portless_entity_iec,
        "buyer_name": req.buyer_name or _party_name(deal, "buyer") or "Buyer",
        "buyer_address": req.buyer_address or _party_address(deal, "buyer") or "",
        "hs_code": deal.hs_code,
        "description": _hs_description(deal.hs_code),
        "quantity_mt": qty,
        "currency": req.currency,
        "fob_value": fob_value,
        "payment_terms": req.payment_terms or "30% advance, 70% on BL",
        "country_of_destination": req.port_of_destination or getattr(deal, "market", ""),
        "port_of_loading": req.port_of_loading or "",
        "port_of_destination": req.port_of_destination or getattr(deal, "market", ""),
        "vessel_name": req.vessel_name,
        "voyage_number": req.voyage_number,
        "bill_of_lading_number": req.bill_of_lading_number,
    }


def _build_shipping_bill_context(req: GenerateDocumentRequest, deal: Any, doc_id: str) -> dict:
    qty = req.quantity_mt or 0.0
    price = req.agreed_price or getattr(deal, "buyer_target_price_usd", 0.0) or 0.0
    fob_value = qty * price
    return {
        "doc_id": doc_id,
        "deal_id": req.deal_id,
        "sb_date": _today(),
        "exporter_name": settings.portless_entity_name,
        "exporter_address": settings.portless_entity_address,
        "exporter_iec": settings.portless_entity_iec,
        "buyer_name": req.buyer_name or _party_name(deal, "buyer") or "Buyer",
        "buyer_address": req.buyer_address or _party_address(deal, "buyer") or "",
        "hs_code": deal.hs_code,
        "description": _hs_description(deal.hs_code),
        "quantity_mt": qty,
        "unit_price": price,
        "currency": req.currency,
        "fob_value": fob_value,
        "port_of_loading": req.port_of_loading or "",
        "port_of_destination": req.port_of_destination or getattr(deal, "market", ""),
        "country_of_destination": req.port_of_destination or getattr(deal, "market", ""),
        "vessel_name": req.vessel_name,
        "voyage_number": req.voyage_number,
        "bill_of_lading_number": req.bill_of_lading_number,
        "let_export_order": req.let_export_order,
        "rodtep_declaration": req.rodtep_declaration,
        "draw_back_claim": req.draw_back_claim,
    }


_CONTEXT_BUILDERS = {
    DocumentType.COMMERCIAL_INVOICE: _build_ci_context,
    DocumentType.PACKING_LIST: _build_pl_context,
    DocumentType.CERTIFICATE_OF_ORIGIN: _build_coo_context,
    DocumentType.SDF: _build_sdf_context,
    DocumentType.SHIPPING_BILL: _build_shipping_bill_context,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_document(req: GenerateDocumentRequest) -> DocumentResponse:
    """Render a trade document from a Jinja2 template and persist the record.

    Idempotency: if ``idempotency_key`` matches an existing document, the
    existing record is returned without re-rendering.

    PI generation is owned by DealMaker; attempting to generate a PI here
    raises DocuGenError and the caller should use DealMaker instead.
    """
    if req.doc_type == DocumentType.PROFORMA_INVOICE:
        raise DocuGenError(
            "PI generation is owned by the DealMaker service. "
            "Use POST /dealmaker/pi instead."
        )

    template_name = _TEMPLATE_MAP.get(req.doc_type)
    if template_name is None:
        raise DocuGenError(f"No template registered for document type {req.doc_type.value!r}")

    repo = get_repository()

    # ── Idempotency check ─────────────────────────────────────────────────
    if req.idempotency_key:
        existing = await repo.get_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            logger.info(
                "DocuGen: returning existing %s doc %s (idempotency_key=%s)",
                req.doc_type.value,
                existing.doc_id,
                req.idempotency_key,
            )
            return DocumentResponse.from_record(existing)

    # ── Fetch deal ────────────────────────────────────────────────────────
    deal = await _fetch_deal(req.deal_id)

    now = datetime.now(timezone.utc)
    doc_id = _new_doc_id(req.doc_type)

    # Create a PENDING record first so a crash during rendering leaves a trace
    pending = DocumentRecord(
        doc_id=doc_id,
        deal_id=req.deal_id,
        doc_type=req.doc_type,
        status="PENDING",
        created_at=now,
        idempotency_key=req.idempotency_key,
    )
    await repo.save(pending)

    # ── Build context and render ──────────────────────────────────────────
    try:
        builder = _CONTEXT_BUILDERS[req.doc_type]
        ctx = builder(req, deal, doc_id)

        tmpl = _jinja_env.get_template(template_name)
        html = tmpl.render(**ctx)

        content_hash = _sha256(html)
        storage_uri = await _write_document(req.deal_id, f"{doc_id}.html", html)

        rendered = pending.model_copy(update={
            "status": "RENDERED",
            "storage_uri": storage_uri,
            "content_hash": content_hash,
            "rendered_at": datetime.now(timezone.utc),
            "context_snapshot": {k: str(v) for k, v in ctx.items()},
        })
        await repo.save(rendered)

    except Exception as exc:
        failed = pending.model_copy(update={"status": "FAILED"})
        await repo.save(failed)
        logger.error(
            "DocuGen: failed to render %s for deal %s: %s",
            req.doc_type.value,
            req.deal_id,
            exc,
        )
        raise DocuGenError(
            f"Failed to render {req.doc_type.value} for deal {req.deal_id}: {exc}"
        ) from exc

    # ── Emit domain event ─────────────────────────────────────────────────
    _topic_map = {
        DocumentType.SHIPPING_BILL: EventTopic.CUSTOMS_FILED,
    }
    topic = _topic_map.get(req.doc_type)
    if topic:
        await _emit(topic, req.deal_id, {"doc_id": doc_id, "doc_type": req.doc_type.value})

    logger.info(
        "DocuGen: rendered %s (%s) for deal %s — hash=%s",
        req.doc_type.value,
        doc_id,
        req.deal_id,
        content_hash[:16],
    )
    return DocumentResponse.from_record(rendered)


async def get_document(doc_id: str) -> DocumentResponse:
    """Fetch a document record by ID."""
    repo = get_repository()
    doc = await repo.get(doc_id)
    if doc is None:
        raise DocumentNotFoundError(doc_id)
    return DocumentResponse.from_record(doc)


async def list_deal_documents(deal_id: str) -> DealDocumentsListResponse:
    """Return all DocuGen documents for a deal."""
    repo = get_repository()
    docs = await repo.list_by_deal(deal_id)
    return DealDocumentsListResponse(
        deal_id=deal_id,
        documents=[DocumentResponse.from_record(d) for d in docs],
    )


async def load_stored_document(storage_uri: str | None) -> tuple[bytes, str]:
    """Resolve a stored document URI to (raw bytes, MIME type).

    Handles both storage backends: Google Drive links (streamed through the
    backend's own OAuth credentials — the files are private to the connected
    account, so their webViewLinks aren't openable by operators logged into
    other Google accounts) and the local-disk file:// fallback.
    Raises FileNotFoundError when the URI is absent/unresolvable and
    GoogleDriveError when the Drive fetch itself fails."""
    from shared.connectors.google_drive import extract_drive_file_id

    if not storage_uri:
        raise FileNotFoundError("Document has no stored file (not rendered yet)")
    file_id = extract_drive_file_id(storage_uri)
    if file_id:
        drive = get_drive_connector()
        if not drive.is_configured():
            raise FileNotFoundError(
                "Document is stored in Google Drive but Drive credentials are not configured"
            )
        return await asyncio.to_thread(drive.download_file, file_id)
    if storage_uri.startswith("file://"):
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        path = Path(url2pathname(urlparse(storage_uri).path))
        if not path.is_file():
            raise FileNotFoundError(f"Stored document file missing on disk: {path}")
        return path.read_bytes(), "text/html"
    raise FileNotFoundError(f"Unrecognized document storage URI: {storage_uri[:80]}")


async def get_document_file(doc_id: str) -> tuple[bytes, str, str]:
    """Return (content, mime_type, filename) for a rendered document.

    Backs the UI's View/Download buttons — streams the file regardless of
    where it lives (Drive or local disk)."""
    repo = get_repository()
    doc = await repo.get(doc_id)
    if doc is None:
        raise DocumentNotFoundError(doc_id)
    content, mime = await load_stored_document(doc.storage_uri)
    filename = f"{doc.deal_id}-{doc.doc_type.value}.html"
    return content, mime, filename


async def file_customs(req: CustomsFilingRequest) -> CustomsFilingResponse:
    """Assemble and submit the Shipping Bill to ICEGATE.

    In production this method:
      1. Loads or generates the Shipping Bill document
      2. POSTs the XML/JSON payload to ICEGATE
      3. Records the Let Export Order (LEO) number
      4. Emits ``customs.filed``

    Currently: generates the Shipping Bill HTML and returns a stub LEO so that
    the system can proceed without a live ICEGATE connection.  Wire in the real
    ICEGATE HTTP call when the API key is available.
    """
    # Try to load a pre-generated Shipping Bill; create one on-the-fly if absent
    repo = get_repository()
    sb_doc: DocumentRecord | None = None

    if req.shipping_bill_doc_id:
        sb_doc = await repo.get(req.shipping_bill_doc_id)

    if sb_doc is None:
        sb_doc_rec = await repo.get_by_deal_and_type(
            req.deal_id, DocumentType.SHIPPING_BILL
        )
        if sb_doc_rec and sb_doc_rec.status == "RENDERED":
            sb_doc = sb_doc_rec

    if sb_doc is None:
        # Auto-generate a Shipping Bill document for this deal
        try:
            deal = await _fetch_deal(req.deal_id)
            gen_req = GenerateDocumentRequest(
                deal_id=req.deal_id,
                doc_type=DocumentType.SHIPPING_BILL,
                rodtep_declaration=req.rodtep_declaration,
                draw_back_claim=req.draw_back_claim,
            )
            resp = await generate_document(gen_req)
            sb_doc = await repo.get(resp.doc_id)
        except DealNotFoundError:
            raise
        except Exception as exc:
            logger.error("DocuGen: auto-generate Shipping Bill failed: %s", exc)
            return CustomsFilingResponse(
                deal_id=req.deal_id,
                status="FAILED",
                message=f"Could not generate Shipping Bill: {exc}",
            )

    # ── ICEGATE submission (stub) ──────────────────────────────────────────
    if settings.icegate_api_key:
        # Production path: POST to ICEGATE EDI
        # Response would contain the Shipping Bill Number and LEO.
        logger.info(
            "DocuGen: ICEGATE API key present but HTTP submission not yet wired — "
            "falling back to stub LEO for deal %s",
            req.deal_id,
        )

    leo = f"LEO-{uuid4().hex[:8].upper()}"
    sb_number = f"SB{datetime.now().strftime('%Y%m%d')}{uuid4().hex[:6].upper()}"

    await _emit(
        EventTopic.CUSTOMS_FILED,
        req.deal_id,
        {
            "shipping_bill_number": sb_number,
            "let_export_order": leo,
            "doc_id": sb_doc.doc_id if sb_doc else None,
            "rodtep": req.rodtep_declaration,
        },
    )

    logger.info(
        "DocuGen: customs filed for deal %s — SB %s / LEO %s",
        req.deal_id,
        sb_number,
        leo,
    )
    return CustomsFilingResponse(
        deal_id=req.deal_id,
        shipping_bill_number=sb_number,
        let_export_order=leo,
        status="FILED",
        message="Stub submission; wire ICEGATE_API_KEY for live filing.",
    )
