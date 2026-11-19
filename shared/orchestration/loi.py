"""Letter of intent (LOI) from a buyer — and the LOI / ICPO / FCO broker-chain pattern (todo 5.3).

**Reading a buyer's LOI.** Many buyers send a letter of intent before a PI. ``parse`` reads one from
the e-mail body (or the text of a PDF attachment) without a model: an LOI / letter-of-intent marker
plus quantity, delivery window, port and specification. ``record`` keeps it on the deal (``loi``) and
on the selected buyer candidate's requirement (``commitment=loi``); the match board raises the
chance of closing for an LOI (``CHANCE_FACTOR``), and the PI quotes the LOI reference.

**Sending one.** ``template`` is the LOI PortLess can send a new buyer on a large lot: product + spec,
quantity and tolerance, price range, Incoterms® 2020 rule + named place, delivery window, payment
through escrow, validity, non-binding except confidentiality.

**The scam chain.** Commodity fraud runs through intermediaries with a fixed vocabulary — "LOI →
ICPO → FCO → SPA → POF / BCL", "SGS at load port", "buyer's mandate", "procedures", "seller's
mandate". ``broker_chain_pattern`` spots it (two or more of those markers); an e-mail carrying it
is flagged ``broker_chain_pattern``, held at L0 and never answered with bank details, proof of funds
or company documents. An LOI counts only from a buyer that is screened (not ``failed`` / blocked)
and writes from its own company domain.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

CHANCE_FACTOR = 1.15
"""How much an LOI lifts the match board's chance of closing (capped at 0.95)."""

_LOI = re.compile(r"\b(?:LOI|letter\s+of\s+intent)\b", re.IGNORECASE)
_CHAIN = {
    "icpo": re.compile(r"\bI\.?C\.?P\.?O\.?\b|irrevocable\s+corporate\s+purchase\s+order", re.I),
    "fco": re.compile(r"\bF\.?C\.?O\.?\b|full\s+corporate\s+offer", re.I),
    "spa": re.compile(r"\bS\.?P\.?A\.?\b(?=.{0,40}(?:sign|draft|contract))|sales\s+and\s+purchase\s+agreement", re.I | re.S),
    "pof": re.compile(r"\bP\.?O\.?F\.?\b|proof\s+of\s+funds?", re.I),
    "bcl": re.compile(r"\bB\.?C\.?L\.?\b|bank\s+comfort\s+letter", re.I),
    "sgs_load_port": re.compile(r"\bSGS\s+(?:at|in)\s+(?:the\s+)?load(?:ing)?\s+port\b", re.I),
    "mandate": re.compile(r"\b(?:buyer'?s?|seller'?s?)\s+mandate\b|\bmandate\s+(?:holder|agreement)\b", re.I),
    "procedure": re.compile(r"\b(?:transaction|working|payment)\s+procedures?\b", re.I),
    "ncnda": re.compile(r"\bNCND(?:A)?\b|\bIMFPA\b", re.I),
}
_QTY = re.compile(r"\b(\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:MT|metric\s+tons?|tonnes?|tons)\b", re.I)
_MONTHLY = re.compile(r"\b(?:per|a|each|/)\s*month\b|\bmonthly\b", re.I)
_PORT = re.compile(r"\b(?:CIF|CFR|CNF|FOB|DAP|CIP)\s+([A-Z][A-Za-z .'-]{2,30}?)(?=[,.;\n]|\s+(?:port|by|within|in)\b|$)")
_WINDOW = re.compile(r"\b(?:shipment|delivery|ship)\s+(?:in|by|within|during|from)\s+([A-Za-z0-9 ,/-]{3,40}?)(?=[.;\n]|$)", re.I)
_SPEC = re.compile(r"\b(?:spec(?:ification)?s?|grade|quality)\s*[:\-]\s*([^\n.;]{3,120})", re.I)
_REF = re.compile(r"\b(?:LOI|ref(?:erence)?)\s*(?:no\.?|number|#)?\s*[:\-]\s*([A-Z0-9][A-Z0-9/\-]{2,30})", re.I)


def broker_chain_pattern(text: str | None) -> list[str]:
    """Pure: the scam-chain markers in a text — two or more mean the pattern (empty list otherwise)."""
    t = text or ""
    hits = [k for k, rx in _CHAIN.items() if rx.search(t)]
    return hits if len(hits) >= 2 else []


def _num(raw: str) -> float:
    return float(raw.replace(",", "").replace(" ", ""))


def parse(text: str | None) -> dict[str, Any] | None:
    """Pure: an LOI's facts, or None when the text is not a letter of intent."""
    t = text or ""
    if not _LOI.search(t):
        return None
    out: dict[str, Any] = {"is_loi": True}
    m = _QTY.search(t)
    if m:
        out["quantity_mt"] = _num(m.group(1))
        out["monthly"] = bool(_MONTHLY.search(t[m.end(): m.end() + 40]))
    if (p := _PORT.search(t)):
        out["port"] = p.group(1).strip()
    if (w := _WINDOW.search(t)):
        out["delivery_window"] = w.group(1).strip()
    if (s := _SPEC.search(t)):
        out["spec"] = s.group(1).strip()
    if (r := _REF.search(t)):
        out["reference"] = r.group(1).strip()
    return out


def counts(deal: Any, from_address: str | None, candidate: Any = None) -> tuple[bool, str]:
    """An LOI counts only from a screened buyer writing from its own company domain."""
    from shared.orchestration.outreach import sender_matches
    status = str(getattr(getattr(candidate, "validation_status", None), "value",
                         getattr(candidate, "validation_status", None)) or "")
    if status == "failed":
        return False, "the buyer failed the counterparty check"
    if candidate is not None and not sender_matches(candidate, from_address or ""):
        return False, "the LOI did not come from the buyer's own address / domain"
    return True, "ok"


async def record(deal_id: str, facts: dict[str, Any], *, source: str, by: str, candidate_id: str | None = None) -> dict[str, Any]:
    """Keep the LOI on the deal (and the candidate's requirement: ``commitment=loi``)."""
    from services.deals.repository import get_repository  # type: ignore[import]
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise LookupError(f"deal {deal_id} not found")
    row = {k: v for k, v in facts.items() if k != "is_loi"}
    row.update(source=source, recorded_by=by, recorded_at=datetime.now(timezone.utc).isoformat(),
               candidate_id=candidate_id)
    fields: dict[str, Any] = {"loi": row}
    if candidate_id:
        cands = []
        for c in getattr(deal, "buyer_candidates", None) or []:
            data = c.model_dump() if hasattr(c, "model_dump") else dict(c)
            if data.get("candidate_id") == candidate_id:
                req = dict(data.get("requirement") or {})
                req["commitment"] = "loi"
                if row.get("quantity_mt") and not req.get("quantity_mt"):
                    req["quantity_mt"] = row["quantity_mt"]
                data["requirement"] = req
            cands.append(data)
        from services.deals.schemas import BuyerCandidate  # type: ignore[import]
        fields["buyer_candidates"] = [BuyerCandidate.model_validate(c) for c in cands]
    await repo.set_fields(deal_id, fields)
    return row


async def capture_from_reply(deal_id: str, candidate: Any, from_address: str, body: str | None) -> dict[str, Any] | None:
    """An outreach / negotiation reply carrying an LOI records it (never raises)."""
    facts = parse(body)
    if facts is None or broker_chain_pattern(body):
        return None
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deal = await get_repository().get(deal_id)
        ok, _ = counts(deal, from_address, candidate)
        if not ok:
            return None
        return await record(deal_id, facts, source="email", by="reply",
                            candidate_id=getattr(candidate, "candidate_id", None))
    except Exception:  # noqa: BLE001
        return None


def chance_factor(deal: Any) -> float:
    return CHANCE_FACTOR if getattr(deal, "loi", None) else 1.0


def template(*, product: str, spec: str | None, quantity_mt: float, tolerance_pct: float = 5.0,
             price_low: float | None, price_high: float | None, incoterm: str, place: str,
             delivery_window: str, validity_days: int = 14) -> str:
    """The LOI PortLess sends a new buyer on a large lot (filled in by the buyer and returned)."""
    from shared import company_profile
    from shared.orchestration.landed_cost import term
    price = (f"USD {price_low:,.0f} – {price_high:,.0f} per MT" if price_low and price_high
             else "to be confirmed in the proforma invoice")
    lines = [
        "LETTER OF INTENT",
        "",
        "To: PortLess Trade Desk",
        "",
        f"We, [buyer's legal name and address], intend to buy from PortLess:",
        f"  Product          : {product}{' — ' + spec if spec else ''}",
        f"  Quantity         : {quantity_mt:,.0f} MT (± {tolerance_pct:g} %)",
        f"  Indicative price : {price}",
        f"  Delivery terms   : {term(incoterm)} {place}, Incoterms® 2020",
        f"  Delivery window  : {delivery_window}",
        "  Payment          : into a neutral escrow account before production; released in stages "
        "against independent inspection and shipping documents",
        f"  Valid until      : {validity_days} days from the date below",
        "",
        "This letter is not binding on either party except for confidentiality. The firm terms are those of "
        "PortLess's proforma invoice once accepted.",
        "",
        "Name, title, signature, company stamp, date:",
        "",
        company_profile.signature(),
    ]
    return "\n".join(lines)
