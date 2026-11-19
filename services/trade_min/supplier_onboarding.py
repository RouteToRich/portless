"""Supplier onboarding documents and the bank check before the first payout (todo 5.5).

Local suppliers send their papers by WhatsApp or hand them over: GST certificate, cancelled
cheque or bank letter, FSSAI licence, APEDA RCMC, IEC. Staff attach each one to the supplier's
directory record (``POST /trade-min/suppliers/{id}/documents``): kind, file name, the file
itself (base64, stored in Google Drive under ``suppliers/<id>`` — local disk only as the loud
fallback) or a link, and its SHA-256. ``POST /trade-min/suppliers/{id}/bank-verified`` records
that a person checked the bank account against a cancelled cheque / bank letter on file
(staff, audited, names the document).

Before the **first** payout to a supplier whose bank account was never verified, the
``RELEASE_SUPPLIER_PAYOUT`` gate is created L0 with risk flag ``bank_not_verified``
(``SUPPLIER_BANK_VERIFICATION_REQUIRED``, default on; pinned off in tests/conftest.py and the
catalogue run): a person looks before money goes to an unchecked account.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import directory

logger = logging.getLogger(__name__)

KINDS = ("gst_certificate", "cancelled_cheque", "bank_letter", "fssai", "apeda_rcmc", "iec", "udyam",
         "iso", "organic", "other")
BANK_PROOF = ("cancelled_cheque", "bank_letter")


def verification_required() -> bool:
    return os.getenv("SUPPLIER_BANK_VERIFICATION_REQUIRED", "true").strip().lower() not in ("0", "false", "no", "off")


async def _supplier(record_id: str):
    store = await directory.get_directory()
    rid = record_id if record_id.startswith("supplier:") else f"supplier:{record_id}"
    rec = await store.get(rid)
    if rec is None or rec.role != "supplier":
        raise LookupError(f"{record_id} not in the supplier directory")
    return store, rec


def _store_file(record_id: str, filename: str, content: bytes, mime: str) -> dict[str, Any]:
    """Drive first; the OS temp folder only as the loud fallback."""
    try:
        from shared.connectors.google_drive import get_connector
        drive = get_connector()
        if drive.is_configured():
            return {"storage": "drive", **drive.upload_document(f"suppliers/{record_id}", filename, content, mime)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("SupplierOnboarding: Drive upload failed (%s) — local fallback", exc)
    import tempfile
    from pathlib import Path
    folder = Path(tempfile.gettempdir()) / "portless-docs" / "suppliers" / record_id.replace(":", "_")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(content)
    logger.warning("SupplierOnboarding: %s stored on local disk (%s) — set up Google Drive", filename, path)
    return {"storage": "local", "path": str(path)}


async def add_document(record_id: str, *, kind: str, filename: str, by: str, content_b64: str | None = None,
                       url: str | None = None, mime_type: str = "application/pdf", number: str | None = None,
                       valid_until: str | None = None, note: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not (content_b64 or url):
        raise ValueError("give the file (content_b64) or a link (url)")
    store, rec = await _supplier(record_id)
    doc: dict[str, Any] = {"doc_id": f"SDOC-{len(rec.documents) + 1:03d}", "kind": kind, "filename": filename,
                           "number": number, "valid_until": valid_until, "note": note, "by": by,
                           "at": datetime.now(timezone.utc).isoformat()}
    if content_b64:
        try:
            content = base64.b64decode(content_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("content_b64 is not valid base64") from exc
        doc["sha256"] = hashlib.sha256(content).hexdigest()
        doc.update(_store_file(rec.id, filename, content, mime_type))
    else:
        doc.update({"storage": "link", "url": url})
    await store.replace(rec.model_copy(update={"documents": [*rec.documents, doc]}))
    from shared.audit_log import record
    await record("supplier.document_added", by, rec.id, {"kind": kind, "doc_id": doc["doc_id"], "sha256": doc.get("sha256")})
    return doc


async def verify_bank(record_id: str, *, doc_id: str, by: str, note: str | None = None) -> dict[str, Any]:
    store, rec = await _supplier(record_id)
    doc = next((d for d in rec.documents if d.get("doc_id") == doc_id), None)
    if doc is None or doc.get("kind") not in BANK_PROOF:
        raise ValueError("name a cancelled cheque or bank letter on file (doc_id)")
    stamp = {"bank_verified_at": datetime.now(timezone.utc).isoformat(), "bank_verified_by": by,
             "bank_verified_doc": doc_id, "bank_verified_note": note}
    await store.replace(rec.model_copy(update=stamp))
    from shared.audit_log import record
    await record("supplier.bank_verified", by, rec.id, {"doc_id": doc_id, "note": note})
    return {"supplier_id": rec.id, **stamp}


async def bank_verified_for_deal(deal: Any) -> bool | None:
    """True / False for the deal's supplier; None when its directory record is unknown."""
    supplier = next((p for p in getattr(deal, "parties", None) or []
                     if str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower() == "supplier"), None)
    if supplier is None:
        return None
    from . import product_facts
    rec = await product_facts.find_record("supplier", name=getattr(supplier, "legal_name", None),
                                          website=getattr(supplier, "website", None),
                                          email=getattr(supplier, "email", None))
    if rec is None:
        return None
    return bool(rec.bank_verified_at)
