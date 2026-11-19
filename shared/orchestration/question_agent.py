"""Inbound question agent — a buyer's or supplier's question answered, not parked (todo P1).

A verified negotiation reply with no price used to go to ``NEEDS_OPERATOR`` and wait for a
person, even when it only asked how payment works or which certificates come with the goods.
When the reply is a question (``is_question``) on a topic the deal's own facts answer
(``topics``: specification, certificates, packing, payment steps, inspection, lead time, samples),
the agent drafts the answer **from facts only** — the deal's agreed specification, the export
certificates and destination documents it will carry, the agreed packing, the escrow steps from
``company_profile``, the inspector PortLess books — and puts it behind the normal
``SEND_NEGOTIATION_EMAIL`` gate (policy level; draft checks such as leaks and stage order still
apply, flag ``question_answer``). A question nothing on file answers (price talk, a new term, a
complaint) still goes to a person. No model writes the facts, so no claim can be invented.

``QUESTION_AGENT_ENABLED`` (default on).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

TOPICS: dict[str, tuple[str, ...]] = {
    "specification": ("spec", "specification", "grade", "variety", "size", "moisture", "quality", "purity"),
    "certificates": ("certificate", "certification", "phyto", "origin", "coo", "fssai", "apeda", "document"),
    "packing": ("packing", "packaging", "bag", "bags", "pallet", "label", "marking"),
    "payment": ("payment", "pay ", "escrow", "advance", "deposit", "letter of credit", "l/c", "lc ", "terms"),
    "inspection": ("inspection", "inspect", "inspector", "qima", "sgs", "quality control", "qc "),
    "lead_time": ("lead time", "how long", "when can", "shipment date", "ship by", "delivery time", "ready"),
    "samples": ("sample",),
    # Payment chase (todo 23.2): a buyer asking how to fund the escrow — answered from the escrow record.
    "funding": ("bank charge", "charges", "which account", "account number", "iban", "swift", "partial",
                "in parts", "instalment", "installment", "how do i pay", "how to pay", "how can i pay", "payment link",
                "checkout", "wire", "transfer the", "remit"),
}
_QWORDS = re.compile(r"^\s*(?:can|could|would|will|do|does|is|are|what|which|how|when|where|who|please (?:confirm|advise|let us know))\b",
                     re.IGNORECASE | re.MULTILINE)
_NOT_FOR_AGENT = re.compile(r"\b(?:discount|reduce|lower|cheaper|best price|price|complain|claim|refund|cancel|dispute)\b",
                            re.IGNORECASE)


def enabled() -> bool:
    return os.getenv("QUESTION_AGENT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def is_question(text: str | None) -> bool:
    """Pure: the reply asks something (a '?' or a question opening a line)."""
    t = text or ""
    return "?" in t or bool(_QWORDS.search(t))


def topics(text: str | None) -> list[str]:
    """Pure: the topics a question touches, in TOPICS order."""
    t = " " + " ".join((text or "").lower().split()) + " "
    return [k for k, words in TOPICS.items() if any(w in t for w in words)]


def answerable(text: str | None) -> bool:
    """A question on known topics only — price talk, claims and cancellations go to a person."""
    return is_question(text) and bool(topics(text)) and not _NOT_FOR_AGENT.search(text or "")


def _status(x: Any) -> str:
    return str(getattr(getattr(x, "status", ""), "value", getattr(x, "status", "")))


def funding_facts(hold: Any) -> str | None:
    """Pure: how to fund the escrow, from the escrow record (None without one)."""
    if hold is None or not getattr(hold, "amount", None):
        return None
    cur = getattr(hold, "currency", None) or "USD"
    provider = str(getattr(hold, "provider", "") or "escrow").replace("_", ".").title()
    lines = [f"Amount to fund: {cur} {float(hold.amount):,.2f} into the {provider} escrow (reference "
             f"{getattr(hold, 'escrow_id', '')})."]
    if getattr(hold, "checkout_url", None):
        lines.append(f"Pay through the provider's secure page: {hold.checkout_url} — the account details are shown "
                     "there; we never ask you to pay to a bank account by e-mail.")
    lines.append("Bank charges: the escrow must receive the full amount, so please pay your bank's and any "
                 "intermediary bank's charges (charge option OUR); a short amount keeps the order waiting.")
    short = getattr(hold, "shortfall", None)
    if short:
        lines.append(f"Partial payment: {cur} {float(getattr(hold, 'funded_amount', 0) or 0):,.2f} has arrived and is held "
                     f"safely; {cur} {float(short):,.2f} is still to come before the order starts.")
    else:
        lines.append("Partial payment: a part payment is held safely in the escrow; the order starts once the full "
                     "amount has arrived.")
    lines.append("The money is released to us only in stages, against the independent inspection and the shipping "
                 "documents.")
    return " ".join(lines)


def facts_for(topic: str, deal: Any, session: Any, party: str, extra: dict[str, Any] | None = None) -> str | None:
    """One true paragraph per topic from the deal's own records, or None when nothing is on file."""
    from shared import company_profile
    if topic == "funding":
        return funding_facts((extra or {}).get("hold")) if party == "buyer" else None
    if topic == "specification":
        try:
            from shared.product_spec import spec_text
            spec = spec_text(deal)
        except Exception:  # noqa: BLE001
            spec = ""
        try:
            from shared.documents.facts import product_name
            name = product_name(getattr(deal, "hs_code", "") or "")
        except Exception:  # noqa: BLE001
            name = ""
        from shared.orchestration import samples
        ref = samples.approved_ref(deal)
        if not spec and not ref:
            return None
        return f"Specification: {name}{' — ' + spec if spec else ''}{' (' + ref + ')' if ref else ''}."
    if topic == "certificates":
        from shared.orchestration import export_certs
        labels = [export_certs.KINDS[k] for k in export_certs.required(getattr(deal, "hs_code", None),
                                                                        getattr(deal, "market", None))]
        if party == "supplier":
            return ("Documents for the shipment: " + "; ".join(labels) + ". We apply for the export certificates; "
                    "we need your invoice, packing list and GST details for them.")
        try:
            from shared import country_profiles
            dest = country_profiles.documents(getattr(deal, "market", None))
        except Exception:  # noqa: BLE001
            dest = []
        extra = f" For your country we also provide: {'; '.join(dest)}." if dest else ""
        return ("The shipment travels with the commercial invoice, packing list, bill of lading and the "
                "inspection report, plus: " + "; ".join(labels) + "." + extra)
    if topic == "packing":
        from shared.orchestration import packing
        agreed = packing.describe(getattr(session, "packing", None))
        if agreed:
            return f"Packing as agreed: {agreed}."
        return ("Standard export packing is included in our price; if you need a special bag size, liner, "
                "private label, pallets or fumigation, tell us and we confirm it with the price.")
    if topic == "payment":
        steps = company_profile.supplier_steps() if party == "supplier" else company_profile.buyer_steps()
        return "How payment works:\n" + company_profile.bullets(steps, numbered=True)
    if topic == "inspection":
        return company_profile._inspector_sentence() if party == "buyer" else (
            "An independent inspector checks the goods at your facility before dispatch and supervises "
            "the container loading and seal; the inspection is booked on your goods-ready date.")
    if topic == "lead_time":
        return ("The shipment date is fixed in the proforma invoice once the price is agreed; the goods are "
                "inspected on the supplier's ready date and shipped on the next sailing.") if party == "buyer" else (
            "Please tell us your goods-ready date for the quantity — the inspection is booked on it.")
    if topic == "samples":
        if party == "buyer":
            return ("We can arrange a sample by courier before the first order; the approved sample becomes the "
                    "quality reference in the proforma invoice and the inspection.")
        return "Please send a sample of the offered grade; we will share the courier address and reference."
    return None


def compose(question: str, deal: Any, session: Any, party: str,
            extra: dict[str, Any] | None = None) -> tuple[str | None, list[str]]:
    """Pure-ish: the answer body (signature appended) and the topics answered; None when a topic
    has nothing on file (a person answers instead)."""
    asked = topics(question)
    parts: list[str] = []
    for t in asked:
        para = facts_for(t, deal, session, party, extra)
        if para is None:
            return None, asked
        parts.append(para)
    if not parts:
        return None, asked
    from shared import company_profile
    body = "Thank you for your question.\n\n" + "\n\n".join(parts) + "\n\nPlease let us know if anything else is open."
    return company_profile.with_signature(body), asked


async def answer(negotiation_id: str, party: str, from_address: str, subject: str, question: str) -> str | None:
    """Draft and gate an answer; the email id, or None when the agent leaves it to a person."""
    if not enabled() or not answerable(question):
        return None
    try:
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
        from agents.negotiation_agent.service import _create_and_gate_email  # type: ignore[import]
        from services.deals.repository import get_repository  # type: ignore[import]
        session = await neg_repo().get_session(negotiation_id)
        if session is None or _status(session) in ("FAILED", "ESCALATED"):
            return None
        deal = await get_repository().get(session.deal_id)
        if deal is None:
            return None
        extra: dict[str, Any] = {}
        if "funding" in topics(question):
            try:
                from services.escrow_guard.repository import get_repository as eg_repo  # type: ignore[import]
                extra["hold"] = await eg_repo().get_hold_by_deal(deal.deal_id)
            except Exception:  # noqa: BLE001
                extra["hold"] = None
        body, asked = compose(question, deal, session, party, extra)
        if body is None:
            return None
        subj = subject if (subject or "").lower().startswith("re:") else f"Re: {subject or 'your question'}"
        return await _create_and_gate_email(session, party, from_address, subj, body,
                                            extra_flags=["question_answer", *[f"topic_{t}" for t in asked]],
                                            kind="question_answer")
    except Exception as exc:  # noqa: BLE001
        logger.warning("QuestionAgent: no answer drafted for %s: %s", negotiation_id, exc)
        return None
