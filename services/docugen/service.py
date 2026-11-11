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

from shared.bus import get_publisher
from shared.documents.facts import deal_facts
from shared.documents.render import environment as document_environment, render_document
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
_jinja_env = document_environment(_TEMPLATES_DIR)


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
    prefix = _PREFIX.get(doc_type) or doc_type.value.replace("_", "")[:4].upper()
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


_COUNTRY = {
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "QA": "Qatar", "OM": "Oman", "KW": "Kuwait", "BH": "Bahrain",
    "US": "United States", "GB": "United Kingdom", "DE": "Germany", "NL": "Netherlands", "FR": "France", "IT": "Italy",
    "ES": "Spain", "BE": "Belgium", "SG": "Singapore", "MY": "Malaysia", "ID": "Indonesia", "VN": "Vietnam",
    "BD": "Bangladesh", "LK": "Sri Lanka", "NP": "Nepal", "KE": "Kenya", "NG": "Nigeria", "ZA": "South Africa",
    "EG": "Egypt", "IR": "Iran", "IQ": "Iraq", "TR": "Turkey", "CN": "China", "JP": "Japan", "KR": "South Korea",
    "AU": "Australia", "CA": "Canada", "BJ": "Benin", "SN": "Senegal", "CI": "Côte d'Ivoire", "GH": "Ghana",
    "TZ": "Tanzania", "MZ": "Mozambique", "PH": "Philippines", "TH": "Thailand", "IN": "India",
}

_PREFIX = {
    DocumentType.COMMERCIAL_INVOICE: "CI", DocumentType.PACKING_LIST: "PL", DocumentType.CERTIFICATE_OF_ORIGIN: "COO",
    DocumentType.SDF: "SDF", DocumentType.SHIPPING_BILL: "SHIP", DocumentType.SALES_CONTRACT: "SC",
    DocumentType.INSPECTION_ORDER: "IO", DocumentType.SHIPPING_INSTRUCTIONS: "SI",
    DocumentType.INSURANCE_DECLARATION: "INS", DocumentType.ACCEPTANCE_CERTIFICATE: "DAC",
    DocumentType.REMITTANCE_ADVICE: "RA", DocumentType.SETTLEMENT_STATEMENT: "SOA", DocumentType.CREDIT_NOTE: "CN",
}

_QTY_TOLERANCE_PCT = 5


def _country_name(code: str | None) -> str:
    c = (code or "").strip()
    return _COUNTRY.get(c.upper(), c)


def _hs_description(hs_code: str) -> str:
    from shared.documents.facts import product_name
    return product_name(hs_code)


def _today() -> str:
    return date.today().isoformat()


def _qc_name(provider: str | None) -> str:
    """The inspection agency's display name (``sgs`` → ``SGS``, ``bureau_veritas`` → ``Bureau Veritas``)."""
    if not provider:
        return "the independent inspection agency"
    try:
        from shared.connectors.qc_providers import QC_PROVIDER_CATALOG  # type: ignore[import]
        entry = QC_PROVIDER_CATALOG.get(provider.lower()) or {}
        if entry.get("display_name"):
            return str(entry["display_name"])
    except Exception:  # noqa: BLE001
        pass
    return provider.replace("_", " ").title()


def _needs_phyto(hs_code: str) -> bool:
    """Plants and plant products (HS chapters 06–14) travel with a phytosanitary certificate."""
    digits = re.sub(r"\D", "", hs_code or "")
    return digits[:2].isdigit() and 6 <= int(digits[:2]) <= 14


def _rows(f: dict, req: GenerateDocumentRequest) -> list[dict]:
    """One packing row per container, the inspector's loaded weight split evenly."""
    containers = f["container_numbers"] or [None]
    seals = f["seal_numbers"]
    net_total = req.net_weight_mt or f["shipped_quantity_mt"]
    gross_total = req.gross_weight_mt
    n = len(containers)
    return [{
        "container": c, "seal": seals[i] if i < len(seals) else None,
        "packages": req.no_of_packages if n == 1 else None,
        "net": net_total / n, "gross": (gross_total / n) if gross_total else None,
    } for i, c in enumerate(containers)]


def _freight_split(f: dict, total_value: float) -> tuple[float | None, float | None, float | None, str]:
    """(freight, insurance, FOB value, basis) — FOB is what the shipping bill and SDF declare."""
    incoterm = (f["incoterm"] or "").upper()
    if incoterm in ("FOB", "FCA", "EXW"):
        return 0.0, 0.0, total_value, ""
    sheet = getattr(f["session"], "cost_sheet", None)
    if sheet is None:
        return None, None, None, ""
    qty = f["shipped_quantity_mt"]
    freight = sum(line.usd_per_mt for line in sheet.lines if line.key == "ocean_freight") * qty
    insurance = (sum(line.pct_of_price for line in sheet.lines if line.key == "marine_insurance") / 100.0 * total_value
                 if incoterm in ("CIF", "CIP") else 0.0)
    basis = (f"FOB = {incoterm} value less ocean freight ({freight:,.2f})"
             + (f" and insurance ({insurance:,.2f})" if insurance else "") + ", from the deal's cost sheet.")
    return round(freight, 2), round(insurance, 2), round(total_value - freight - insurance, 2), basis


async def _invoice_ref(deal_id: str) -> str | None:
    docs = await get_repository().list_by_deal(deal_id)
    cis = [d for d in docs if d.doc_type == DocumentType.COMMERCIAL_INVOICE and d.status == "RENDERED"]
    return cis[-1].doc_id if cis else None


async def _common(req: GenerateDocumentRequest, f: dict, doc_id: str) -> dict:
    from shared.orchestration.continuations import payment_terms_for
    buyer = dict(f["buyer"])
    buyer["legal_name"] = req.buyer_name or buyer.get("legal_name") or "Buyer"
    buyer["address"] = req.buyer_address or buyer.get("address") or ""
    buyer["vat_number"] = req.buyer_vat_number or buyer.get("vat_number") or ""
    buyer["country"] = _country_name(buyer.get("country") or f["market"])
    supplier = dict(f["supplier"])
    supplier["legal_name"] = req.supplier_name or supplier.get("legal_name") or "Supplier"
    supplier["address"] = req.supplier_address or supplier.get("address") or ""
    supplier["gstin"] = req.supplier_gstin or supplier.get("gstin") or ""
    unit_price = req.agreed_price or f["unit_price"] or getattr(f["deal"], "buyer_target_price_usd", None) or 0.0
    shipped = req.quantity_mt or f["shipped_quantity_mt"]
    hold, pi = f["hold"], f["pi"]
    destination = req.port_of_destination or f["port_of_destination"]
    quantity_note = ""
    if f["loaded_quantity_mt"] and abs(f["loaded_quantity_mt"] - f["quantity_mt"]) > 0.001:
        quantity_note = (f"Contract quantity {f['quantity_mt']:,.3f} MT; quantity loaded and invoiced "
                         f"{f['loaded_quantity_mt']:,.3f} MT (within the ±{_QTY_TOLERANCE_PCT}% tolerance).")
    return {
        "doc_id": doc_id, "deal_id": req.deal_id,
        "doc_date": req.invoice_date or date.today(),
        "buyer": buyer, "supplier": supplier, "product": f["product"], "hs_code": f["hs_code"],
        "currency": f["currency"], "unit_price": unit_price,
        "quantity_mt": f["quantity_mt"], "shipped_quantity_mt": shipped, "quantity_note": quantity_note,
        "total_value": round(unit_price * shipped, 2),
        "incoterm": req.incoterm or f["incoterm"],
        "port_of_loading": req.port_of_loading or f["port_of_loading"],
        "port_of_destination": destination,
        "destination_country": _country_name(f["market"]),
        "containers": f["container_numbers"], "seals": f["seal_numbers"],
        "vessel": req.vessel_name or f["vessel"], "voyage": req.voyage_number or f["voyage"],
        "bl_number": req.bill_of_lading_number or f["bl_number"], "etd": f["etd"], "eta": f["eta"],
        "marks": req.marks_and_numbers or f"PORTLESS / {req.deal_id} / {(destination or f['market'] or '').split(',')[0].upper()}",
        "packages": req.no_of_packages, "package_type": req.package_type or "",
        "pi_id": getattr(pi, "pi_id", None), "pi_accepted_at": getattr(pi, "accepted_at", None),
        "payment_terms": req.payment_terms or f["payment_terms"] or payment_terms_for(),
        "escrow_id": getattr(hold, "escrow_id", None),
        "escrow_provider": (getattr(hold, "provider", None) or getattr(pi, "escrow_provider", None) or "").replace("_", ".").title() or None,
        "qc_provider_name": _qc_name(f["qc_provider"]), "qc_ref": f["qc_ref"],
        "po_id": getattr(f["po"], "po_id", None),
        "quality_spec": getattr(f["po"], "quality_spec", None) or f"Specification as agreed in the deal correspondence ({req.deal_id})",
        "tolerance_pct": _QTY_TOLERANCE_PCT, "notify_party": None,
        "needs_phyto": _needs_phyto(f["hs_code"]),
        "invoice_ref": await _invoice_ref(req.deal_id),
        "shipping_bill_number": f["shipping_bill_number"],
        "let_export_order": req.let_export_order or f["let_export_order"],
    }


Spec = tuple[dict, tuple[str, ...], dict]


async def _ci(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    return c, ("address", "iec", "gstin", "lut_arn"), {
        "buyer address": c["buyer"]["address"], "unit price (from the PI)": c["unit_price"],
        "port of loading": c["port_of_loading"], "port of discharge": c["port_of_destination"]}


async def _pl(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    rows = _rows(f, req)
    c.update(rows=rows, net_weight_mt=sum(r["net"] for r in rows), gross_weight_mt=req.gross_weight_mt,
             loading_source=getattr(f["loading"], "inspector_ref", None) or (getattr(f["loading"], "source", None) if f["loading"] else None))
    return c, ("address",), {"number of packages": req.no_of_packages, "gross weight": req.gross_weight_mt,
                             "container number (inspector's loading record)": c["containers"]}


async def _coo(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    c.update(country_of_origin=req.country_of_origin,
             chamber_name=req.coo_chamber or "Federation of Indian Export Organisations (FIEO)")
    return c, ("address", "iec"), {"buyer address": c["buyer"]["address"]}


async def _sdf(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    freight, insurance, fob, basis = _freight_split(f, c["total_value"])
    c.update(fob_value=fob, fob_basis=basis, freight_value=freight, insurance_value=insurance)
    return c, ("iec", "ad_code"), {"FOB value (freight / insurance split)": fob}


async def _shipping_bill(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    freight, insurance, fob, basis = _freight_split(f, c["total_value"])
    hs_policy = None
    try:
        from shared.orchestration import compliance
        status, note = await compliance.hs_policy_status(f["hs_code"])
        hs_policy = f"{status}{': ' + note if note else ''}"
    except Exception:  # noqa: BLE001
        pass
    c.update(fob_value=fob, fob_basis=basis, freight_value=freight, insurance_value=insurance, hs_policy=hs_policy,
             rodtep_declaration=req.rodtep_declaration, draw_back_claim=req.draw_back_claim)
    return c, ("iec", "gstin", "ad_code", "lut_arn"), {
        "FOB value (freight / insurance split)": fob, "port of loading": c["port_of_loading"],
        "commercial invoice": c["invoice_ref"]}


async def _sales_contract(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    import os
    from services.escrow_guard.config import settings as eg
    c.update(governing_law=(os.getenv("PORTLESS_CONTRACT_LAW") or "").strip(),
             shipment_days=int(os.getenv("CONTRACT_SHIPMENT_DAYS", "45") or 45),
             funding_days=int(os.getenv("ESCROW_FUNDING_GRACE_DAYS", "14") or 14),
             acceptance_hours=int(eg.inspection_window_hours))
    c["total_value"] = round(c["unit_price"] * c["quantity_mt"], 2)
    return c, ("address", "iec"), {
        "buyer address": c["buyer"]["address"], "port of destination": c["port_of_destination"],
        "governing law and dispute forum (PORTLESS_CONTRACT_LAW)": c["governing_law"]}


async def _inspection_order(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    po = f["po"]
    c.update(ready_by=getattr(po, "delivery_deadline", None),
             quantity_mt=getattr(po, "quantity_mt", None) or c["quantity_mt"])
    return c, ("address",), {"supplier address (inspection site)": c["supplier"]["address"]}


async def _shipping_instructions(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    freight = f["freight"]
    route = getattr(getattr(f["session"], "cost_sheet", None), "supplier_route", None)
    c.update(rows=_rows(f, req), freight_provider=(getattr(freight, "provider", None) or "").title() or None,
             booking_ref=getattr(freight, "ext_ref", None),
             place_of_receipt=getattr(route, "city", None) or getattr(route, "state", None) or c["supplier"].get("address"),
             bl_release="Telex / express release on the shipper's written instruction, after the buyer's escrow milestone")
    return c, ("address",), {"consignee address": c["buyer"]["address"], "port of discharge": c["port_of_destination"],
                             "container number (inspector's loading record)": c["containers"]}


async def _insurance(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    import os
    route = getattr(getattr(f["session"], "cost_sheet", None), "supplier_route", None)
    c.update(sum_insured=round(c["total_value"] * 1.10, 2), policy_number=(os.getenv("PORTLESS_MARINE_POLICY") or "").strip(),
             cover_clauses=(os.getenv("PORTLESS_MARINE_CLAUSES") or "(A)").strip(),
             place_of_receipt=getattr(route, "city", None) or getattr(route, "state", None))
    return c, ("address",), {"marine open policy number (PORTLESS_MARINE_POLICY)": c["policy_number"]}


async def _acceptance(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    from services.escrow_guard.config import settings as eg
    hold = f["hold"]
    final = next((t for t in (getattr(hold, "tranches", None) or []) if t.trigger == "ACCEPTED"), None)
    basis = req.acceptance_basis or "buyer_accepted"
    c.update(basis=basis, delivered_at=getattr(f["freight"], "delivered_at", None) or getattr(hold, "delivery_confirmed_at", None) or getattr(hold, "inspection_window_opened_at", None),
             accepted_at=getattr(final, "released_at", None) or getattr(hold, "buyer_accepted_at", None) or date.today(),
             acceptance_ref=getattr(hold, "accepted_by", None), acceptance_hours=int(eg.inspection_window_hours),
             final_pct=getattr(final, "pct", 0.0), final_amount=getattr(final, "amount", None) or (getattr(hold, "amount", 0.0) * getattr(final, "pct", 0.0)))
    return c, (), {}


async def _remittance(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    payouts = [p for p in f["payouts"] if p.status == "COMPLETED"]
    payout = next((p for p in payouts if p.payout_id == req.payout_id), payouts[-1] if payouts else None)
    po = f["po"]
    po_value = getattr(po, "total_value", None) or getattr(payout, "total_owed", None) or 0.0
    upto = [p for p in payouts if payout is None or p.created_at <= payout.created_at]
    paid = sum(p.amount for p in upto)
    retained = 0.0
    try:
        from services.escrow_guard.service import get_deal_ledger
        ledger = await get_deal_ledger(req.deal_id)
        retained = float(getattr(ledger, "retained_until_acceptance", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        pass
    kinds = {"supplier_payout": "Payment against the purchase order", "supplier_balance": "Balance payment"}
    c.update(amount=getattr(payout, "amount", 0.0), paid_at=getattr(payout, "completed_at", None),
             payment_ref=getattr(payout, "ext_ref", None) or getattr(payout, "payout_id", None),
             kind_label=kinds.get(getattr(payout, "kind", ""), "Supplier payment"), po_value=po_value,
             paid_to_date=paid, retained=retained, balance=max(po_value - paid, 0.0), provider="Payoneer",
             currency=getattr(payout, "currency", None) or c["currency"])
    return c, ("address",), {"completed supplier payout": payout}


async def _settlement(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    from shared.documents.render import tranche_schedule
    milestones = {t["trigger"]: t["label"] for t in tranche_schedule()}
    hold = f["hold"]
    movements: list[dict] = []
    if hold is not None and getattr(hold, "funded_at", None):
        movements.append({"date": hold.funded_at, "label": "Paid into escrow by the buyer", "ref": hold.ext_ref,
                          "paid_in": hold.funded_amount or hold.amount})
    for t in getattr(hold, "tranches", None) or []:
        if t.status == "RELEASED":
            movements.append({"date": t.released_at, "label": f"Released to seller — {t.tranche_id} ({t.pct:.0%}): {milestones.get(t.trigger, t.trigger).lower()}",
                              "ref": t.ext_ref, "released": t.amount or round(hold.amount * t.pct, 2)})
    refunded = float(getattr(hold, "refunded_total", 0.0) or 0.0)
    if refunded:
        movements.append({"date": getattr(hold, "updated_at", None), "label": "Refunded to the buyer", "ref": None, "released": refunded})
    funded = float(getattr(hold, "funded_amount", None) or getattr(hold, "amount", 0.0) or 0.0)
    released = sum(m.get("released", 0.0) for m in movements if m["label"].startswith("Released"))
    c.update(movements=movements, funded=funded, released=released, refunded=refunded,
             balance=round(funded - released - refunded, 2), contract_value=getattr(f["pi"], "total_value", None) or getattr(hold, "amount", 0.0),
             settled_at=getattr(hold, "settled_at", None) or date.today(), currency=getattr(hold, "currency", None) or c["currency"])
    return c, (), {"escrow record": hold}


async def _credit_note(req: GenerateDocumentRequest, f: dict, c: dict) -> Spec:
    hold = f["hold"]
    released = sum((t.amount or 0.0) for t in (getattr(hold, "tranches", None) or []) if t.status == "RELEASED")
    amount = req.refund_amount if req.refund_amount is not None else float(getattr(hold, "refunded_total", 0.0) or 0.0)
    c.update(amount=amount, reason=req.refund_reason or "Refund of the undisbursed escrow balance",
             contract_value=getattr(f["pi"], "total_value", None) or getattr(hold, "amount", 0.0), released=released,
             currency=getattr(hold, "currency", None) or c["currency"])
    return c, ("address",), {"refund amount": amount}


_SPECS: dict[DocumentType, tuple[str, Any]] = {
    DocumentType.COMMERCIAL_INVOICE: ("ci.html.j2", _ci),
    DocumentType.PACKING_LIST: ("pl.html.j2", _pl),
    DocumentType.CERTIFICATE_OF_ORIGIN: ("coo.html.j2", _coo),
    DocumentType.SDF: ("sdf.html.j2", _sdf),
    DocumentType.SHIPPING_BILL: ("shipping_bill.html.j2", _shipping_bill),
    DocumentType.SALES_CONTRACT: ("sales_contract.html.j2", _sales_contract),
    DocumentType.INSPECTION_ORDER: ("inspection_order.html.j2", _inspection_order),
    DocumentType.SHIPPING_INSTRUCTIONS: ("shipping_instructions.html.j2", _shipping_instructions),
    DocumentType.INSURANCE_DECLARATION: ("insurance_declaration.html.j2", _insurance),
    DocumentType.ACCEPTANCE_CERTIFICATE: ("acceptance_certificate.html.j2", _acceptance),
    DocumentType.REMITTANCE_ADVICE: ("remittance_advice.html.j2", _remittance),
    DocumentType.SETTLEMENT_STATEMENT: ("settlement_statement.html.j2", _settlement),
    DocumentType.CREDIT_NOTE: ("credit_note.html.j2", _credit_note),
}
_TEMPLATE_MAP: dict[DocumentType, str] = {k: v[0] for k, v in _SPECS.items()}


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
        facts = await deal_facts(req.deal_id, deal)
        common = await _common(req, facts, doc_id)
        ctx, exporter_fields, required = await _SPECS[req.doc_type][1](req, facts, common)
        html = render_document(_jinja_env, template_name, ctx, exporter_fields=exporter_fields, required=required)
        from shared.documents import issuer
        draft_reasons = issuer.missing(exporter_fields) + [k for k, v in required.items() if v in (None, "", 0, 0.0, [])]

        content_hash = _sha256(html)
        storage_uri = await _write_document(req.deal_id, f"{doc_id}.html", html)

        rendered = pending.model_copy(update={
            "status": "RENDERED",
            "storage_uri": storage_uri,
            "content_hash": content_hash,
            "rendered_at": datetime.now(timezone.utc),
            "context_snapshot": {k: str(v) for k, v in ctx.items() if not isinstance(v, (dict, list))},
            "draft": bool(draft_reasons),
            "draft_reasons": draft_reasons,
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
    from services.shield_link.service import CUSTOMS_FILING_STATUSES  # type: ignore[import]
    from shared.orchestration import kill_switch

    await kill_switch.assert_running(f"Customs filing for deal {req.deal_id}")
    deal_for_status = await _fetch_deal(req.deal_id)
    deal_status = getattr(deal_for_status.status, "value", str(deal_for_status.status))
    if deal_status not in CUSTOMS_FILING_STATUSES:
        raise DocuGenError(
            f"Deal {req.deal_id!r} is {deal_status!r}; customs can be filed only at "
            f"{' or '.join(sorted(CUSTOMS_FILING_STATUSES))}"
        )

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
