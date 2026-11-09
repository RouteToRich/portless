"""What the negotiation agent knows about a counterparty BEFORE its first e-mail.

The negotiation thread is not where the relationship starts: the buyer already
got PortLess's introduction (outreach funnel) and said yes, usually telling us
grade, quantity, port and target price; the supplier already got the RFQ and
quoted. Those e-mails are sent outside the negotiation (``negotiation_id=""``),
so the agent used to see an empty history, re-introduce PortLess to someone who
had just replied, and quote a CIF price "to your destination port" without
knowing the port.

``build_brief`` reads the deal (selected buyer candidate + its requirement,
the supplier's offer) and returns:

* ``stage`` — ``cold_introduction`` (nothing ever exchanged),
  ``first_offer_after_introduction`` (they know us, the negotiation thread is
  new) or ``continuing_thread``;
* ``buyer_terms()`` / ``supplier_terms()`` — the PortLess facts (price basis,
  escrow schedule from ``escrow_guard.DEFAULT_TRANCHES``, inspection) an e-mail
  may state; anything else about payment is off-limits to the model;
* ``what_they_told_us`` — the facts they gave, so the agent never asks again;
* ``our_earlier_messages`` — what we already sent them;
* ``missing_information`` — the few facts still needed to move the trade to a
  proforma invoice / purchase order, most important first, so every e-mail
  makes progress instead of only talking price.

Secrecy: the buyer brief never carries a supplier price and the supplier brief
never carries a buyer price — each side only sees what it told us itself.
Reply text is untrusted input: trimmed, and the prompts tell the model to
ignore instructions inside it.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

COLD = "cold_introduction"
AFTER_INTRO = "first_offer_after_introduction"
CONTINUING = "continuing_thread"

_MAX_EXCERPT = 500
_MAX_MISSING = 4


def _role(p: Any) -> str:
    r = getattr(p, "role", "")
    return str(getattr(r, "value", r) or "").lower()


def _party(deal: Any, role: str) -> Any | None:
    return next((p for p in (getattr(deal, "parties", None) or []) if _role(p) == role), None)


def _excerpt(text: str | None) -> str:
    t = " ".join((text or "").split())
    return t[:_MAX_EXCERPT] + ("…" if len(t) > _MAX_EXCERPT else "")


def _date(dt: Any) -> str:
    try:
        return dt.date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _selected_candidate(deal: Any, buyer_email: str | None) -> Any | None:
    cands = list(getattr(deal, "buyer_candidates", None) or [])
    email = (buyer_email or "").lower()
    if email:
        for c in cands:
            if email in {(c.email or "").lower(), (getattr(c, "reply_address", None) or "").lower()}:
                return c
    for status in ("selected", "interested"):
        for c in cands:
            if c.status == status:
                return c
    return None


def _supplier_offer(deal: Any, seller_id: str | None, seller_email: str | None) -> Any | None:
    offers = list(getattr(deal, "supplier_offers", None) or [])
    email = (seller_email or "").lower()
    for o in offers:
        if seller_id and o.supplier_id == seller_id:
            return o
        if email and (o.email or "").lower() == email:
            return o
    return None


def _stage(history: str, inbound: str, prior_contact: bool) -> str:
    if (history or "").strip() or (inbound or "").strip():
        return CONTINUING
    return AFTER_INTRO if prior_contact else COLD


# ---------------------------------------------------------------------------
# Buyer
# ---------------------------------------------------------------------------

def _buyer_facts(req: Any) -> dict[str, Any]:
    if req is None:
        return {}
    facts = {
        "grade": req.grade,
        "packaging": req.packaging,
        "quantity_mt": req.quantity_mt,
        "delivery_window": req.delivery_window,
        "incoterm": req.incoterm,
        "destination_port": req.destination_port,
        "their_target_price_usd_per_mt": req.target_price_usd_per_mt,
        "certifications_required": list(req.certifications or []) or None,
        "payment_terms_requested": req.payment_terms,
        "notes": req.notes,
    }
    return {k: v for k, v in facts.items() if v not in (None, "", [])}


def buyer_missing(facts: dict[str, Any], *, qc_selected: bool, quantity_known: bool) -> list[str]:
    """Facts still needed before a proforma invoice can be issued, by priority."""
    out: list[str] = []
    if not facts.get("destination_port"):
        out.append("destination port (our price is CIF to that port)")
    if not (facts.get("quantity_mt") or quantity_known):
        out.append("quantity in MT for the first shipment")
    if not facts.get("grade"):
        out.append("grade / specification and packaging")
    if not facts.get("delivery_window"):
        out.append("when the goods must arrive")
    if not qc_selected:
        out.append("which inspection agency they trust (SGS, QIMA, Bureau Veritas, Intertek or TÜV SÜD)")
    if not facts.get("certifications_required"):
        out.append("documents and certificates required at destination")
    return out[:_MAX_MISSING]


async def buyer_brief(session: Any, history: str = "", inbound: str = "") -> dict[str, Any]:
    deal = await _load_deal(getattr(session, "deal_id", ""))
    party = _party(deal, "buyer") if deal else None
    cand = _selected_candidate(deal, getattr(session, "buyer_email", None)) if deal else None
    req = getattr(cand, "requirement", None) if cand else None
    facts = _buyer_facts(req)

    earlier: list[str] = []
    if cand is not None and getattr(cand, "contacted_at", None):
        earlier.append(
            f"{_date(cand.contacted_at)}: our introduction e-mail — PortLess as Indian merchant exporter, "
            "verified manufacturers, third-party inspection of their choice, neutral escrow; asked for "
            "grade, quantity, delivery date, destination port and target price"
        )
    offer = getattr(cand, "indicative_offer", None) if cand else None
    if offer is not None and getattr(offer, "sent_at", None):
        earlier.append(
            f"{_date(offer.sent_at)}: indicative price range USD {offer.price_low_usd_per_mt:,.0f}–"
            f"{offer.price_high_usd_per_mt:,.0f} per MT {offer.incoterm}"
            + (f" (their response: {offer.response})" if offer.response else "")
        )
    reply = _excerpt(getattr(cand, "reply_preview", None)) if cand else ""
    prior = bool(earlier or reply or facts)

    company = (getattr(party, "legal_name", None) or getattr(cand, "legal_name", None) or "").strip()
    country = getattr(party, "country", None) or getattr(cand, "country", None) or getattr(session, "buyer_country_code", "")
    return {
        "stage": _stage(history, inbound, prior),
        "company_name": company or None,
        "country": country or None,
        "what_they_told_us": facts,
        "their_reply_to_our_introduction": reply or None,
        "our_earlier_messages": earlier,
        "missing_information": buyer_missing(
            facts,
            qc_selected=bool(getattr(session, "qc_provider", None)),
            quantity_known=bool(getattr(session, "quantity_mt", None)),
        ),
    }


# ---------------------------------------------------------------------------
# Supplier
# ---------------------------------------------------------------------------

def _supplier_facts(offer: Any) -> dict[str, Any]:
    if offer is None:
        return {}
    facts = {
        "grade": offer.grade,
        "their_fob_quote_usd_per_mt": offer.fob_price_usd_per_mt,
        "moq_mt": offer.moq_mt,
        "capacity_mt_per_month": offer.capacity_mt_per_month,
        "lead_time_days": offer.lead_time_days,
        "port_of_loading": offer.port_of_loading,
        "certifications": list(offer.certifications or []) or None,
        "payment_terms_requested": offer.payment_terms,
        "quote_validity_days": offer.validity_days,
        "gst_verified": offer.gst_verified,
        "iec_active": offer.iec_active,
    }
    return {k: v for k, v in facts.items() if v not in (None, "", [])}


def supplier_missing(facts: dict[str, Any], *, verified: bool) -> list[str]:
    out: list[str] = []
    if not verified:
        out.append("GSTIN and IEC (we verify every supplier before a purchase order)")
    if not facts.get("grade"):
        out.append("grade / specification and packaging")
    if not facts.get("lead_time_days"):
        out.append("lead time from purchase order to goods ready at the port")
    if not (facts.get("moq_mt") or facts.get("capacity_mt_per_month")):
        out.append("minimum order and monthly capacity in MT")
    if not facts.get("port_of_loading"):
        out.append("port of loading")
    if not facts.get("certifications"):
        out.append("certificates available (COA and any others)")
    if not facts.get("quote_validity_days"):
        out.append("how long the price is valid")
    return out[:_MAX_MISSING]


async def supplier_brief(session: Any, history: str = "", inbound: str = "") -> dict[str, Any]:
    deal = await _load_deal(getattr(session, "deal_id", ""))
    party = _party(deal, "supplier") if deal else None
    offer = _supplier_offer(deal, getattr(session, "seller_id", None), getattr(session, "seller_email", None)) if deal else None
    facts = _supplier_facts(offer)

    earlier: list[str] = []
    if offer is not None:
        when = _date(getattr(offer, "received_at", None))
        earlier.append(
            "our request for quotation e-mail, and their quotation"
            + (f" received {when}" if when else "")
        )
    verified = bool(
        (offer is not None and offer.gst_verified and offer.iec_active)
        or (party is not None and getattr(party, "gstin", None) and getattr(party, "iec", None))
    )
    company = (getattr(party, "legal_name", None) or getattr(offer, "company_name", None) or "").strip()
    return {
        "stage": _stage(history, inbound, offer is not None),
        "company_name": company or None,
        "what_they_told_us": facts,
        "our_earlier_messages": earlier,
        "missing_information": supplier_missing(facts, verified=verified),
    }


async def _load_deal(deal_id: str) -> Any | None:
    if not deal_id:
        return None
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        return await get_deal(deal_id)
    except Exception as exc:  # noqa: BLE001 — no deal (tests / standalone) → empty brief
        logger.debug("briefing: deal %s unavailable (%s)", deal_id, exc)
        return None


# ---------------------------------------------------------------------------
# PortLess terms the agents may state as fact
# ---------------------------------------------------------------------------

_MILESTONE = {
    "PO_ISSUED": "when the purchase order is placed with the manufacturer (production advance)",
    "QC_PASSED": "when the independent pre-shipment inspection passes",
    "CLEARED": "when the goods clear customs at destination",
    "ACCEPTED": "when the buyer accepts the goods after delivery",
}


def escrow_schedule_text() -> str:
    """The real staged-release schedule (escrow_guard.DEFAULT_TRANCHES), in words —
    so no e-mail ever promises a release rule the escrow does not follow."""
    try:
        from services.escrow_guard.service import DEFAULT_TRANCHES  # type: ignore[import]
        tranches = list(DEFAULT_TRANCHES)
    except Exception:  # noqa: BLE001
        tranches = [("T1", "PO_ISSUED", 0.30), ("T2", "QC_PASSED", 0.40), ("T3", "CLEARED", 0.20), ("T4", "ACCEPTED", 0.10)]
    parts = [f"{round(pct * 100):d}% {_MILESTONE.get(trg, trg.lower())}" for _, trg, pct in tranches]
    return "; ".join(parts)


def buyer_terms() -> str:
    return (
        "Price basis: USD per MT, CIF the buyer's destination port; the third-party pre-shipment inspection is "
        "included in the price. Payment: after accepting our proforma invoice the buyer pays into a neutral "
        f"escrow account (not to PortLess, not to the manufacturer); it is released in stages — {escrow_schedule_text()}. "
        "The buyer chooses the inspection agency (SGS, QIMA, Bureau Veritas, Intertek or TÜV SÜD) and gets its "
        "photo-documented report; after delivery the buyer has an acceptance window to raise a complaint before "
        "the final release. A failed inspection stops the shipment (re-inspection or refund). PortLess sources "
        "from GST- and IEC-verified Indian manufacturers and manages freight and export documentation."
    )


def supplier_terms() -> str:
    return (
        "Price basis: FOB Indian port, per MT (INR or USD). Payment security: we place the purchase order only "
        "after the overseas buyer has paid into a neutral escrow account, and the supplier is "
        "paid from those funds in stages as they are released — a production advance when the purchase order is "
        "placed, then after the inspection passes and as the shipment clears (exact supplier split agreed on the "
        "purchase order; never quote percentages). No open credit, no letter-of-credit "
        "paperwork, no chasing payments. Independent inspection at the supplier's facility before dispatch. "
        "PortLess finds and manages the overseas buyer, books the ocean freight and coordinates the export "
        "documents. Repeat orders follow when a first shipment goes well."
    )
