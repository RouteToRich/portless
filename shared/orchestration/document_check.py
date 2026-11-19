"""Document checking (todo 23.2): what suppliers, inspectors and forwarders send — a
phytosanitary or origin certificate, a commercial invoice, a packing list, a draft B/L, an
inspection report — compared field by field with the deal's own facts
(``shared/documents/facts.deal_facts``) before anyone relies on it.

``POST /deals/{id}/documents/check`` with the document's kind and its text (PDF / photo text is
extracted upstream — OCR is not part of this module). Deterministic checks, each ``ok`` /
``mismatch`` / ``not_found``:
- HS code (6 digits of the deal), product name;
- quantity in MT (within ``COMPLIANCE_QTY_TOLERANCE_PCT``, 5 %) of the loaded / PO quantity;
- every container number and seal number the inspector recorded appears (and no other ISO 6346
  container number does);
- the buyer (consignee / notify) on buyer-side documents, and the supplier never named on them;
- port of loading / discharge;
- invoice value (quantity × PI price) when the document states a USD total.
A mismatch on a document that money or customs depends on (B/L, invoice, certificates) raises
``deal.exception`` ``DOCUMENT_ERROR`` with the findings (``raise_on_mismatch``). The LLM decides
nothing here.
"""
from __future__ import annotations

import os
import re
from typing import Any

KINDS = ("draft_bl", "commercial_invoice", "packing_list", "phytosanitary", "certificate_of_origin",
         "inspection_report", "other")
BUYER_SIDE = ("draft_bl", "commercial_invoice", "packing_list", "phytosanitary", "certificate_of_origin")
_CONTAINER = re.compile(r"\b([A-Z]{3}[UJZ]\s?\d{6}\s?\d)\b")
_MT = re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:MT|M/T|metric tons?|tonnes?|tons?)\b", re.IGNORECASE)
_KG = re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:KGS?|kilograms?)\b", re.IGNORECASE)
_USD = re.compile(r"(?:USD|US\$)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _norm(s: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).split())


def _tolerance() -> float:
    try:
        return float(os.getenv("COMPLIANCE_QTY_TOLERANCE_PCT", "5") or 5)
    except ValueError:
        return 5.0


def check_text(kind: str, text: str, facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: findings for one document against the deal facts."""
    t = text or ""
    tn = _norm(t)
    out: list[dict[str, Any]] = []

    def add(field: str, status: str, expected: Any = None, found: Any = None, note: str = "") -> None:
        out.append({"field": field, "status": status, "expected": expected, "found": found, "note": note})

    hs = str(facts.get("hs_code") or "")[:6]
    if hs:
        digits = re.sub(r"[^0-9]", "", t)
        codes = {m.replace(".", "").replace(" ", "")[:6] for m in re.findall(r"\b\d{4}[.\s]?\d{2}(?:[.\s]?\d{2})?\b", t)}
        if hs in codes:
            add("hs_code", "ok", hs, hs)
        elif codes:
            add("hs_code", "mismatch", hs, sorted(codes)[0])
        else:
            add("hs_code", "not_found", hs, None, "no HS code on the document" if hs not in digits else "")
    product = facts.get("product")
    if product:
        add("product", "ok" if _norm(product).split()[0] in tn else "not_found", product)

    qty = facts.get("loaded_quantity_mt") or facts.get("quantity_mt")
    if qty:
        stated = [_num(m) for m in _MT.findall(t)] + [(_num(m) or 0) / 1000.0 for m in _KG.findall(t)]
        stated = [s for s in stated if s]
        if not stated:
            add("quantity_mt", "not_found", qty)
        else:
            close = [s for s in stated if abs(s - float(qty)) / float(qty) * 100.0 <= _tolerance()]
            add("quantity_mt", "ok" if close else "mismatch", qty, close[0] if close else max(stated))

    found_boxes = {c.replace(" ", "") for c in _CONTAINER.findall(t.upper())}
    want_boxes = {str(c).replace(" ", "").upper() for c in facts.get("container_numbers") or []}
    if want_boxes:
        missing = sorted(want_boxes - found_boxes)
        extra = sorted(found_boxes - want_boxes)
        add("containers", "ok" if not missing and not extra else "mismatch", sorted(want_boxes), sorted(found_boxes),
            (f"missing {', '.join(missing)}" if missing else "") + (f"; unknown {', '.join(extra)}" if extra else ""))
    seals = [str(s).upper() for s in facts.get("seal_numbers") or []]
    if seals:
        missing = [s for s in seals if s not in t.upper()]
        add("seals", "ok" if not missing else "mismatch", seals, None, f"missing {', '.join(missing)}" if missing else "")

    buyer = facts.get("buyer")
    supplier = facts.get("supplier")
    if kind in BUYER_SIDE and buyer is not None:
        name = getattr(buyer, "legal_name", None) or (buyer.get("legal_name") if isinstance(buyer, dict) else None)
        if name:
            core = [w for w in _norm(name).split() if len(w) > 3][:2]
            add("buyer", "ok" if core and all(w in tn for w in core) else "not_found", name)
    if kind in BUYER_SIDE and kind != "inspection_report" and supplier is not None:
        sname = getattr(supplier, "legal_name", None) or (supplier.get("legal_name") if isinstance(supplier, dict) else None)
        core = [w for w in _norm(sname).split() if len(w) > 3][:2]
        if core and all(w in tn for w in core):
            add("supplier_named", "mismatch", "not named on a buyer document", sname,
                "the buyer must not learn who the supplier is")

    for field in ("port_of_loading", "port_of_destination"):
        port = facts.get(field)
        if port:
            word = _norm(port).split()[0] if _norm(port) else ""
            add(field, "ok" if word and word in tn else "not_found", port)

    if kind == "commercial_invoice" and facts.get("unit_price") and qty:
        expected = round(float(facts["unit_price"]) * float(qty), 2)
        totals = [_num(m) for m in _USD.findall(t)]
        totals = [x for x in totals if x]
        if totals:
            ok = any(abs(x - expected) / expected <= 0.01 for x in totals)
            add("invoice_value_usd", "ok" if ok else "mismatch", expected, max(totals))
        else:
            add("invoice_value_usd", "not_found", expected)
    return out


async def check(deal_id: str, kind: str, text: str, *, raise_on_mismatch: bool = True) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    from shared.documents.facts import deal_facts
    facts = await deal_facts(deal_id)
    findings = check_text(kind, text, facts)
    mismatches = [f for f in findings if f["status"] == "mismatch"]
    raised = False
    if mismatches and raise_on_mismatch and kind != "other":
        from shared.orchestration.continuations import _publish_exception
        await _publish_exception(deal_id, f"DOCUMENT:{kind}",
                                      f"{kind.replace('_', ' ')} does not match the deal: "
                                      + "; ".join(f"{m['field']} ({m['note'] or m['found']})" for m in mismatches),
                                      {"category": "DOCUMENT_ERROR", "findings": mismatches,
                                       "action_required": "ask_sender_to_correct_the_document"})
        raised = True
    return {"deal_id": deal_id, "kind": kind, "ok": not mismatches, "findings": findings, "exception_raised": raised}
