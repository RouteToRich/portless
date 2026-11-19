"""Supplier steps relayed by staff, with evidence (todo 5.5: local suppliers answer by phone or
WhatsApp, not e-mail).

Every supplier-side step has a staff relay, and each relay can carry its evidence — a call note,
a WhatsApp screenshot, a photo of the signed PO, a document reference:

| step | e-mail path | staff relay |
|---|---|---|
| quote | RFQ reply | ``POST /deals/{id}/supplier-offers`` |
| order confirmation | reply to the firm order request | ``POST /deals/{id}/supplier-commitment/confirm`` |
| PO acknowledgement | reply to the PO | ``POST /deals/{id}/supplier-commitment/po-ack`` |
| goods-ready date | reply | ``POST /shield-link/deals/{id}/qc-readiness`` |
| facility address | reply | ``PATCH /deals/{id}/parties/supplier`` |
| bank details | onboarding papers | ``POST /trade-min/suppliers/{id}/bank-verified`` |

``GET /deals/{id}/supplier-steps`` shows each step: done or not, by e-mail or by a staff relay,
and the evidence on the relay (from the hash-chained audit log). With
``RELAY_EVIDENCE_REQUIRED=true`` the money-relevant relays (order confirmation, PO
acknowledgement) refuse to run without evidence beyond the reason text.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    kind: Literal["call_note", "whatsapp", "photo", "document", "email", "meeting"] = "call_note"
    ref: str | None = None
    """A link / file id / message id that shows it (screenshot, photo, document)."""
    note: str | None = None
    at: datetime | None = None
    """When the supplier said it (else when it was relayed)."""


STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quote", ("supplier.quote_relayed",)),
    ("order_confirmation", ("supplier.order_confirmation_relayed",)),
    ("po_acknowledgement", ("supplier.po_ack_relayed",)),
    ("goods_ready_date", ("supplier.ready_date_relayed",)),
    ("facility_address", ("party.supplier_updated",)),
    ("bank_details", ("supplier.bank_verified",)),
)


def required() -> bool:
    return os.getenv("RELAY_EVIDENCE_REQUIRED", "false").strip().lower() in ("1", "true", "yes", "on")


def check(evidence: Evidence | None) -> None:
    """Raise ValueError when evidence is required and missing."""
    if required() and (evidence is None or not (evidence.ref or (evidence.note and len(evidence.note) >= 10))):
        raise ValueError("RELAY_EVIDENCE_REQUIRED: attach the evidence (a link / file reference, or a call note)")


def stamp(evidence: Evidence | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    data = evidence.model_dump(mode="json")
    data["at"] = data.get("at") or datetime.now(timezone.utc).isoformat()
    return data


async def supplier_steps(deal: Any) -> dict[str, Any]:
    """Each supplier step: done (by e-mail / staff relay), with the relay's evidence."""
    from shared import audit_log
    entries = await audit_log.entries(limit=500, subject=deal.deal_id)
    offers = list(getattr(deal, "supplier_offers", None) or [])
    commitment = None
    try:
        from shared.orchestration import supplier_commitment
        commitment = await supplier_commitment.get(deal.deal_id)
    except Exception:  # noqa: BLE001
        commitment = None
    supplier = next((p for p in getattr(deal, "parties", None) or []
                     if str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower() == "supplier"), None)
    by_email = {
        "quote": any(getattr(o, "source", "") == "quote" for o in offers),
        "order_confirmation": str(getattr(commitment, "status", "") or "") == "confirmed",
        "po_acknowledgement": str(getattr(commitment, "po_ack", "") or "") == "acknowledged",
        "goods_ready_date": bool(getattr(commitment, "qc_ready_date", None)),
        "facility_address": bool(getattr(supplier, "address", None)),
        "bank_details": False,
    }
    rows = []
    for step, actions in STEPS:
        relays = [e for e in entries if e.get("action") in actions]
        last = relays[0] if relays else None
        detail = (last or {}).get("detail") or {}
        evidence = detail.get("evidence")
        rows.append({
            "step": step, "done": bool(by_email[step] or last),
            "how": "staff relay" if last else ("e-mail" if by_email[step] else None),
            "relayed_by": (last or {}).get("actor"), "relayed_at": (last or {}).get("at"),
            "evidence": evidence, "evidence_missing": bool(last) and not evidence,
        })
    try:
        from services.trade_min import supplier_onboarding  # type: ignore[import]
        verified = await supplier_onboarding.bank_verified_for_deal(deal)
        if verified:
            next(r for r in rows if r["step"] == "bank_details").update(done=True, how=rows[-1]["how"] or "documents")
    except Exception:  # noqa: BLE001
        pass
    return {"deal_id": deal.deal_id, "steps": rows,
            "evidence_required": required()}


def evidence_field() -> Any:
    return Field(default=None, description="Call note, WhatsApp screenshot, photo of the signed PO, document")
