"""Who PortLess is, in one place — every e-mail to a buyer or a supplier uses it.

The introduction, the RFQ, the indicative offer, the negotiation agent's
drafts and the milestone notices all take the company description, the "how a
deal works" steps and the signature from here, so a counterparty reads one
consistent, professional company.

Only true statements live in this module: what the platform does by design
(verified manufacturers, an inspector the buyer chooses, a neutral escrow with
staged release, freight and export documents handled). Company facts that
cannot be known from the code — address, phone, website, IEC, GSTIN, the
signing person — come from the environment and appear only once set. Never add
a track record, client count, volume or award here unless it is real.

Environment (all optional):
  PORTLESS_BRAND_NAME        default "PortLess"
  PORTLESS_LEGAL_NAME        default services.dealmaker portless_entity_name
  PORTLESS_SENDER_NAME       person who signs, e.g. "Priya Raman"
  PORTLESS_SENDER_TITLE      e.g. "Head of Trade"
  PORTLESS_DESK_NAME         default "Trade Desk" (signs when no sender name is set)
  PORTLESS_WEBSITE / PORTLESS_CONTACT_EMAIL / PORTLESS_PHONE / PORTLESS_ADDRESS
  PORTLESS_IEC / PORTLESS_GSTIN   registration numbers, printed in the signature
"""
from __future__ import annotations

import os
import re
from typing import Any


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def brand() -> str:
    return _env("PORTLESS_BRAND_NAME", "PortLess")


def legal_name() -> str:
    explicit = _env("PORTLESS_LEGAL_NAME")
    if explicit:
        return explicit
    try:
        from services.dealmaker.config import settings as dm  # type: ignore[import]
        return dm.portless_entity_name
    except Exception:  # noqa: BLE001
        return "PortLess Trade Solutions Pvt Ltd"


def _escrow_schedule() -> str:
    try:
        from agents.negotiation_agent.briefing import escrow_schedule_text  # type: ignore[import]
        return escrow_schedule_text()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# What we say about ourselves
# ---------------------------------------------------------------------------

def one_line() -> str:
    """One sentence that says what PortLess is."""
    return (
        f"{legal_name()} is an Indian merchant exporter: we supply products from verified Indian "
        "manufacturers to importers worldwide and manage the whole export for both sides."
    )


def buyer_reasons() -> list[str]:
    """Why an importer can buy from PortLess with confidence — all true by design."""
    return [
        "Your payment is protected. You pay into a neutral escrow account, never to us or to "
        "the manufacturer, and the money is released in stages only as the order progresses.",
        "You choose the inspector. SGS, QIMA, Bureau Veritas, Intertek or TÜV SÜD inspects the goods at "
        "the factory before shipment and supervises the container loading and seal; you receive the "
        "photo-documented report. The inspection cost is included in our price.",
        "Verified manufacturers only. Every supplier's GST registration and Import-Export Code are "
        "checked, and every party is screened against sanctions lists before a contract is issued.",
        "One accountable partner. We handle sourcing, ocean freight, export documentation and customs, "
        "and quote one clear CIF price to your port.",
        "A window to accept the goods. After delivery you have time to inspect the shipment before the "
        "final payment is released.",
    ]


def supplier_reasons() -> list[str]:
    """Why an Indian manufacturer should take a PortLess order — all true by design."""
    return [
        "Payment secured before you produce. We issue the purchase order only after the overseas buyer "
        "has paid into a neutral escrow account, and you are paid from those funds in stages, starting "
        "with a production advance when the order is placed.",
        "No credit risk and no letter-of-credit paperwork. You never chase an overseas buyer for money.",
        "We bring the export buyer. We find and manage the overseas customer, book the ocean freight and "
        "prepare the export documents; you deliver FOB Indian port.",
        "Quality settled at your gate. An independent inspection at your facility before dispatch means "
        "no disputes after arrival.",
        "Repeat business. Buyers who receive a good first shipment reorder through us.",
    ]


def buyer_steps() -> list[str]:
    schedule = _escrow_schedule()
    return [
        "You tell us the grade, quantity and destination port; we send a firm CIF offer.",
        "You accept our proforma invoice and pay into the neutral escrow account.",
        "We place the order with the verified manufacturer; the inspector you chose checks the goods "
        "and supervises the loading.",
        "We ship and handle export clearance; you receive the documents for import.",
        ("Your payment is released in stages: " + schedule.replace("the buyer accepts", "you accept") + "."
         if schedule else "Your payment is released in stages, the final part only after you accept the goods."),
    ]


def supplier_steps() -> list[str]:
    return [
        "You send your FOB quotation.",
        "We confirm the order with the overseas buyer, who pays into escrow.",
        "We issue the purchase order and you receive the production advance from escrow.",
        "The independent inspector checks the goods and supervises loading at your facility.",
        "You deliver FOB; the balance is paid from escrow as the shipment clears and is accepted.",
    ]


def contact_lines() -> list[str]:
    """Registration and contact lines that are set; empty when none are."""
    lines: list[str] = []
    reg = " | ".join(x for x in (
        f"IEC {_env('PORTLESS_IEC')}" if _env("PORTLESS_IEC") else "",
        f"GSTIN {_env('PORTLESS_GSTIN')}" if _env("PORTLESS_GSTIN") else "",
    ) if x)
    if reg:
        lines.append(reg)
    reach = " | ".join(x for x in (_env("PORTLESS_PHONE"), _env("PORTLESS_CONTACT_EMAIL"), _env("PORTLESS_WEBSITE")) if x)
    if reach:
        lines.append(reach)
    if _env("PORTLESS_ADDRESS"):
        lines.append(_env("PORTLESS_ADDRESS"))
    return lines


def signature() -> str:
    """The sign-off block every e-mail ends with (starts with a blank line)."""
    who = _env("PORTLESS_SENDER_NAME")
    lines = ["Kind regards,", ""]
    if who:
        lines.append(who)
        if _env("PORTLESS_SENDER_TITLE"):
            lines.append(_env("PORTLESS_SENDER_TITLE"))
        lines.append(legal_name())
    else:
        lines.append(f"{_env('PORTLESS_DESK_NAME', 'Trade Desk')} | {legal_name()}")
    lines.append("Merchant exporter, India")
    lines.extend(contact_lines())
    return "\n\n" + "\n".join(lines) + "\n"


def bullets(items: list[str], numbered: bool = False) -> str:
    return "\n".join(f"  {i}. {t}" if numbered else f"  - {t}" for i, t in enumerate(items, 1))


def for_llm(audience: str) -> dict[str, Any]:
    """The facts a drafting agent may state about PortLess (and nothing more)."""
    buyer = audience == "buyer"
    return {
        "name": brand(),
        "legal_name": legal_name(),
        "who_we_are": one_line(),
        "why_work_with_us": buyer_reasons() if buyer else supplier_reasons(),
        "how_a_deal_works": buyer_steps() if buyer else supplier_steps(),
        "registration_and_contact": contact_lines(),
    }


# ---------------------------------------------------------------------------
# Sign-off handling for LLM drafts
# ---------------------------------------------------------------------------

_SIGNOFF = re.compile(
    r"\n\s*(best regards|kind regards|warm regards|regards|sincerely|yours sincerely|yours faithfully|"
    r"thanks and regards|thank you and regards|best wishes|with regards)\s*,?\s*(\n.*)?$",
    re.IGNORECASE | re.DOTALL,
)


def with_signature(body: str) -> str:
    """Drop any sign-off the model wrote and end with the real signature block."""
    text = (body or "").rstrip()
    if not text:
        return text
    text = _SIGNOFF.sub("", text).rstrip()
    return text + signature()
