"""Counterparty notifications — the emails a merchant exporter's back office
would otherwise send by hand at every milestone.

Before this module the pipeline produced the artefacts (PI, escrow, PO, QC
booking, freight booking, customs clearance) but never told the buyer or the
supplier about any of them: the PI sat in Drive, the escrow checkout link sat
in Mongo, the PO was never mailed. A "fully automatic" trade therefore stalled
at every step that needed the counterparty to act (accept the PI, fund the
escrow, prepare the cargo for inspection).

Each notification is a **templated, deterministic** plain-text email (no LLM,
so no hallucination surface), recorded in the negotiation's email history
(so it shows in the Negotiation Inbox threads next to the LLM-drafted
negotiation emails) and routed through a ``SEND_DEAL_NOTIFICATION`` HITL
approval whose level comes from the autonomy policy — default L2, i.e. the
email goes out immediately and the operator sees an AUTO_APPROVED entry.
Set ``AUTONOMY_SEND_DEAL_NOTIFICATION=L0`` to review every one.

Delivery reuses the negotiation agent's ``send_approved_email`` (SMTP via the
Email Gateway; log-only without credentials) and the same ``[NEG:<id>]``
subject token, so a buyer's reply to "your Proforma Invoice" threads back
into the Email Processor, which recognises post-agreement replies (PI
acceptance) — see ``services/email_processor/service.py``.

Idempotent: the email id is derived from ``(kind, deal_id, suffix)``; a second
firing of the same event (the same topic arrives from both the owning service
and the Deals status emitter) finds the existing record and does nothing.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ACTION = "SEND_DEAL_NOTIFICATION"


def enabled() -> bool:
    return os.getenv("DEAL_NOTIFICATIONS_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _email_id(kind: str, deal_id: str, suffix: str) -> str:
    # Must match the Email Processor's Message-ID regex: EMAIL-[A-Z0-9]+ — so
    # hash the key rather than embedding the hyphenated deal id.
    digest = hashlib.sha1(f"{kind}:{deal_id}:{suffix}".encode()).hexdigest()[:12].upper()
    return f"EMAIL-N{digest}"


def _entity_name() -> str:
    try:
        from services.dealmaker.config import settings as dm_settings  # type: ignore[import]
        return dm_settings.portless_entity_name
    except Exception:  # noqa: BLE001
        return "PortLess"


def _fmt_money(value: Any, currency: str = "USD") -> str:
    try:
        return f"{currency} {float(value):,.2f}"
    except (TypeError, ValueError):
        return f"{currency} {value}"


def _dear(name: Any) -> str:
    """"Dear Gulf Staples LLC team" — or "Dear Sir or Madam" when the name is unknown
    (the template adds the comma)."""
    name = str(name or "").strip()
    return f"Dear {name} team" if name else "Dear Sir or Madam"


def _qty(value: Any) -> str:
    """20.0 → "20", 20.5 → "20.5" (never "20.0 MT" in a letter)."""
    try:
        return f"{float(value):,.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def _release_when(trigger: Any) -> str:
    """Escrow trigger code (PO_ISSUED…) in words, addressed to the buyer."""
    code = str(getattr(trigger, "value", trigger) or "")
    try:
        from agents.negotiation_agent.briefing import _MILESTONE  # type: ignore[import]
        text = _MILESTONE.get(code)
    except Exception:  # noqa: BLE001
        text = None
    if not text:
        return "at the " + code.replace("_", " ").lower() + " stage"
    return text.replace("the buyer accepts", "you accept")


def _signature() -> str:
    from shared import company_profile as cp
    return cp.signature()


# ---------------------------------------------------------------------------
# Templates — (subject, body) per notification kind
# ---------------------------------------------------------------------------

def _tpl_pi_issued(ctx: dict[str, Any]) -> tuple[str, str]:
    pi = ctx["pi"]
    subject = f"Proforma Invoice {pi.pi_id} for your order — deal {ctx['deal_id']}"
    link = pi.storage_uri if str(pi.storage_uri or "").startswith("http") else None
    body = (
        f"{_dear(pi.buyer_name)},\n\n"
        f"Please find below the Proforma Invoice for the order we agreed on.\n\n"
        f"  Proforma Invoice : {pi.pi_id}\n"
        f"  Product (HS)     : {ctx.get('hs_code', '')}\n"
        f"  Quantity         : {_qty(pi.quantity_mt)} MT\n"
        f"  Unit price       : {_fmt_money(pi.agreed_price, pi.currency)} / MT ({pi.incoterm})\n"
        f"  Total value      : {_fmt_money(pi.total_value, pi.currency)}\n"
        f"  Destination port : {pi.port_of_destination}\n"
        f"  Payment terms    : {pi.payment_terms}\n"
        f"  Valid until      : {pi.expires_at.strftime('%Y-%m-%d') if pi.expires_at else ''}\n"
        + (f"  Document         : {link}\n" if link else "")
        + "\nTo proceed, simply reply to this email with \"We accept the Proforma Invoice\". "
        "Once accepted we will open a neutral escrow account and send you the funding "
        "instructions; your payment stays in escrow and is released in stages — a "
        "production advance when the order is placed, the rest against independent "
        "inspection, shipping proof and your acceptance of the goods."
        + _signature()
    )
    return subject, body


def _tpl_escrow_opened(ctx: dict[str, Any]) -> tuple[str, str]:
    esc = ctx["escrow"]
    subject = f"Escrow funding instructions — deal {ctx['deal_id']}"
    tranches = "".join(
        f"    • {int(round((t.pct or 0) * 100))}% {_release_when(t.trigger)}\n"
        for t in (getattr(esc, "tranches", None) or [])
    )
    from shared.connectors.escrow import ESCROW_PROVIDER_CATALOG, display_name

    provider = display_name(esc.provider)
    meta = ESCROW_PROVIDER_CATALOG.get(str(esc.provider), {})
    if esc.checkout_url:
        pay_line = f"  Payment link     : {esc.checkout_url}\n"
    elif esc.provider == "escrow_com":
        pay_line = "  How to pay       : Escrow.com has e-mailed you an invitation to this transaction — fund it from there\n"
    else:
        pay_line = ""
    if meta.get("buyer_accepts_at_provider"):
        authority = (f"{provider} pays each stage out only after you accept it on {provider} — or when that "
                     f"stage's inspection period ends without an objection from you.")
    elif meta:
        authority = (f"{provider} pays each stage out only after it has checked our documents for that stage "
                     f"(purchase order, inspection report, bill of lading); the last stage waits for your "
                     f"acceptance of the goods.")
    else:
        authority = "The provider releases each stage only on the schedule above."
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"Thank you for accepting Proforma Invoice {ctx.get('pi_id', '')}. A neutral escrow "
        f"has been opened with {provider} for this order.\n\n"
        f"  Escrow provider  : {provider}\n"
        f"  Escrow reference : {esc.escrow_id}"
        + (f" ({provider} ref {esc.ext_ref})" if getattr(esc, "ext_ref", None) else "") + "\n"
        f"  Amount to fund   : {_fmt_money(esc.amount, esc.currency)}\n"
        + pay_line
        + (f"  Pay by           : {meta['buyer_pays_by']}\n" if meta.get("buyer_pays_by") else "")
        + ("\n  Staged release schedule:\n" + tranches if tranches else "")
        + f"\nFunds are held by {provider} — never by us. {authority} "
        "Production starts as soon as the escrow is funded."
        + _signature()
    )
    return subject, body



def _plan_lines(types: list[str] | None, agency: str | None) -> str:
    from shared.orchestration import inspection_plan
    who = f" by {agency}" if agency else ""
    return "".join(f"    • {inspection_plan.label(t)}{who} — {inspection_plan.META[t]['when']}\n"
                   for t in inspection_plan.normalise(types))


def _tpl_order_confirmed_buyer(ctx: dict[str, Any]) -> tuple[str, str]:
    """One e-mail with everything the buyer needs for the order (never the supplier)."""
    from shared.connectors.escrow import ESCROW_PROVIDER_CATALOG, display_name
    pi, esc = ctx["pi"], ctx.get("escrow")
    provider = display_name(getattr(esc, "provider", None) or getattr(pi, "escrow_provider", None) or "")
    meta = ESCROW_PROVIDER_CATALOG.get(str(getattr(esc, "provider", "") or ""), {})
    stages = "".join(f"    • {int(round((t.pct or 0) * 100))}% {_release_when(t.trigger)}\n"
                     for t in (getattr(esc, "tranches", None) or []))
    subject = f"Order confirmed — {ctx.get('product') or 'your order'} · PI {pi.pi_id} · deal {ctx['deal_id']}"
    body = (
        f"{_dear(pi.buyer_name)},\n\n"
        "Thank you — your order is confirmed. Here is everything in one place.\n\n"
        "ORDER\n"
        f"  Proforma Invoice : {pi.pi_id}\n"
        f"  Product          : {ctx.get('product') or ''} (HS {ctx.get('hs_code', '')})\n"
        f"  Quantity         : {_qty(pi.quantity_mt)} MT\n"
        f"  Price            : {_fmt_money(pi.agreed_price, pi.currency)} / MT {pi.incoterm} — total {_fmt_money(pi.total_value, pi.currency)}\n"
        f"  Destination port : {pi.port_of_destination}\n"
        + (f"  Packing          : {ctx['packing']}\n" if ctx.get("packing") else "")
        + "\nPAYMENT — NEUTRAL ESCROW\n"
        f"  Escrow provider  : {provider or 'to be confirmed'}\n"
        + (f"  Escrow reference : {esc.escrow_id}\n" if esc else "")
        + (f"  Payment link     : {esc.checkout_url}\n" if esc and getattr(esc, "checkout_url", None) else "")
        + (f"  Pay by           : {meta['buyer_pays_by']}\n" if meta.get("buyer_pays_by") else "")
        + (f"  Released in stages:\n{stages}" if stages else "")
        + (f"  Who releases     : {meta['release_authority']}\n" if meta.get("release_authority") else "")
        + "\nINDEPENDENT INSPECTION\n"
        + _plan_lines(ctx.get("inspection_types"), ctx.get("agency"))
        + "  Every report is shared with you before the next payment stage.\n"
        "\nSHIPPING\n"
        f"  Carrier          : {ctx.get('carrier') or 'Maersk'} — booked by PortLess once the goods pass inspection\n"
        f"  Route            : {ctx.get('port_of_loading') or 'Indian port of loading'} → {pi.port_of_destination}\n"
        "  Tracking         : container and vessel milestones are sent to you as they happen\n"
        "\nDOCUMENTS YOU WILL RECEIVE\n"
        "  Sales contract, commercial invoice, packing list, certificate of origin, inspection report(s), bill of lading"
        + (", marine insurance declaration" if str(pi.incoterm).upper() in ("CIF", "CIP") else "") + ".\n"
        "\nNEXT STEP\n"
        "  Fund the escrow using the details above. Production starts as soon as the funds are confirmed.\n"
        "  Reply to this e-mail for anything about the order — it reaches the trade desk handling it."
        + _signature()
    )
    return subject, body


def _tpl_order_confirmed_supplier(ctx: dict[str, Any]) -> tuple[str, str]:
    """One e-mail with everything the supplier needs (never the buyer or the sale price)."""
    subject = f"Order confirmed — {ctx.get('product') or 'order'} · {_qty(ctx.get('quantity_mt'))} MT · deal {ctx['deal_id']}"
    price = ctx.get("supplier_price")
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        "Thank you for confirming. The order is now confirmed on our side too — here is the full plan.\n\n"
        "ORDER\n"
        f"  Product          : {ctx.get('product') or ''} (HS {ctx.get('hs_code', '')})\n"
        f"  Quantity         : {_qty(ctx.get('quantity_mt'))} MT\n"
        + (f"  Price            : {_price(ctx, price)} / MT FOB {ctx.get('port_of_loading') or ''}\n" if price else "")
        + (f"  Packing          : {ctx['packing']}\n" if ctx.get("packing") else "")
        + "  Purchase order   : issued as soon as the buyer's payment is secured in escrow (within days)\n"
        "\nINSPECTION AT YOUR FACILITY\n"
        + _plan_lines(ctx.get("inspection_types"), ctx.get("agency"))
        + "  Please tell us your goods-ready date when you acknowledge the PO; the inspector also supervises\n"
        "  the container stuffing and fixes the seal.\n"
        "\nSHIPPING\n"
        f"  PortLess books the container with {ctx.get('carrier') or 'Maersk'}; the empty container is released to you\n"
        "  for stuffing under the inspector's supervision. You deliver FOB at the port of loading.\n"
        "\nPAYMENT TO YOU\n"
        "  Paid from the escrow in stages as the trade advances — production advance after the PO, then\n"
        "  after the inspection passes and the shipment clears; the last share once the buyer accepts the goods.\n"
        "\nDOCUMENTS WE NEED FROM YOU\n"
        "  Tax invoice (GST), packing list, mill / test certificate if any, and the goods-ready date.\n"
        "\nReply to this e-mail for anything about the order — it reaches the trade desk handling it."
        + _signature()
    )
    return subject, body


def _tpl_inspection_phase(ctx: dict[str, Any]) -> tuple[str, str]:
    from shared.orchestration import inspection_plan
    phase, result = ctx.get("inspection_type") or "", ctx.get("result") or ""
    label = inspection_plan.label(phase)
    ref = ctx.get("provider_order_number") or ctx.get("ext_ref") or ""
    subject = f"{label}: {'passed' if result == 'PASS' else 'needs attention'} — deal {ctx['deal_id']}"
    if ctx.get("party") == "supplier" and result == "FAIL":
        body = (f"{_dear(ctx.get('name'))},\n\n"
                f"The {label} ({str(ctx.get('provider') or '').upper()} {ref}) found issues: "
                f"{ctx.get('notes') or 'see the inspection report'}.\n\n"
                "Please correct them during production and reply with what was done and the expected "
                "goods-ready date; the pre-shipment inspection will check the full lot." + _signature())
    else:
        body = (f"{_dear(ctx.get('name'))},\n\n"
                f"The {label} ({str(ctx.get('provider') or '').upper()} {ref}) "
                + ("passed. " if result == "PASS" else f"reported issues: {ctx.get('notes') or 'see the report'}. ")
                + ("The order stays on track; the next step follows as planned." if result == "PASS"
                   else "We are handling it with the supplier and will keep you informed.")
                + _signature())
    return subject, body


def _tpl_bill_of_lading(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Bill of lading {ctx.get('bl_number')} issued — deal {ctx['deal_id']}"
    boxes = ", ".join(ctx.get("containers") or []) or "—"
    body = (f"{_dear(ctx.get('buyer_name'))},\n\n"
            "The carrier has issued the bill of lading for your order.\n\n"
            f"  Bill of lading   : {ctx.get('bl_number')}\n"
            f"  Vessel / voyage  : {ctx.get('vessel') or '—'} {ctx.get('voyage') or ''}\n"
            f"  Container(s)     : {boxes}\n"
            + (f"  Seal(s)          : {', '.join(ctx.get('seal_numbers') or [])}\n" if ctx.get("seal_numbers") else "")
            + "\nYou can follow the container on the carrier's tracking with the B/L number; we send you every "
            "milestone too." + _signature())
    return subject, body


def _tpl_po_issued(ctx: dict[str, Any]) -> tuple[str, str]:
    po = ctx["po"]
    subject = f"Purchase Order {po.po_id} — deal {ctx['deal_id']}"
    link = po.storage_uri if str(po.storage_uri or "").startswith("http") else None
    body = (
        f"{_dear(po.supplier_name)},\n\n"
        f"We are pleased to confirm the order. Please find the Purchase Order details below.\n\n"
        f"  Purchase Order   : {po.po_id}\n"
        f"  Product (HS)     : {ctx.get('hs_code', '')}\n"
        f"  Quantity         : {_qty(po.quantity_mt)} MT\n"
        f"  Unit price       : {_po_money(ctx, po.agreed_price, po.currency)} / MT ({po.incoterm})\n"
        f"  Total value      : {_po_money(ctx, po.total_value, po.currency)}\n"
        f"  Port of loading  : {po.port_of_loading}\n"
        f"  Delivery by      : {po.delivery_deadline}\n"
        f"  Payment terms    : {po.payment_terms}\n"
        + (f"  Quality spec     : {po.quality_spec}\n" if getattr(po, 'quality_spec', None) else "")
        + (f"  Packing          : {po.packing_spec} (checked by the inspector)\n" if getattr(po, 'packing_spec', None) else "")
        + (f"  Document         : {link}\n" if link else "")
        + "\n" + _rate_line(ctx)
        + "Please reply to:\n"
        "  1. acknowledge this Purchase Order,\n"
        f"  2. tell us the date the goods will be 100% produced and at least {_packed_pct()}% packed "
        "(for example \"ready for inspection on 15 December 2026\")"
        + (f", and\n  3. send us {_ADDRESS_ASK}.\n\n" if ctx.get("address_needed") else ".\n\n")
        + "We book the independent pre-shipment inspection for that date — the agency needs "
        f"{_lead_days()} working days' notice — and share the inspector's details with you. The "
        "inspector also supervises the container stuffing and seals the container."
        + _signature()
    )
    return subject, body


def _packed_pct() -> int:
    from shared.orchestration import qc_schedule
    return qc_schedule.min_packed_pct()


def _lead_days() -> int:
    from shared.orchestration import qc_schedule
    return qc_schedule.lead_business_days()


def _tpl_qc_readiness_request(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"When will the goods be ready for inspection? — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        "Thank you for acknowledging the Purchase Order. To book the independent pre-shipment "
        "inspection we need the date the goods will be ready:\n\n"
        "  - 100% of the order quantity produced, and\n"
        f"  - at least {ctx.get('packed_pct', 80)}% packed and labelled, with the rest available "
        "for sampling.\n\n"
        "Please reply with that date (for example \"ready for inspection on 15 December 2026\"). "
        f"The inspection agency needs {ctx.get('lead_days', 2)} working days' notice, so the earliest "
        f"possible inspection is {ctx.get('earliest', 'shortly after your reply')}. On the inspection day "
        "please keep the Purchase Order, the packing list and the product specification ready, "
        "give the inspector access to the whole lot, and plan the container stuffing right after "
        "the inspection — the inspector supervises the loading and seals the container."
        + _signature()
    )
    return subject, body


def _tpl_facility_address_request(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Address of your facility for the inspection — deal {ctx['deal_id']}"
    known = ctx.get("known")
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        "To book the independent inspection we need the address the inspector will come to: the full address "
        "of the facility where the goods are produced, packed and loaded — street, town and PIN code.\n\n"
        + (f"We currently have only \"{known}\", which is not enough for the inspection agency.\n\n" if known else "")
        + "Please reply with the address (for example \"Factory address: Plot 14, SIPCOT Industrial Estate, "
        "Thanjavur, Tamil Nadu 613005\"). The inspection is booked as soon as we have it."
        + _signature()
    )
    return subject, body


def _tpl_supplier_order_request(ctx: dict[str, Any]) -> tuple[str, str]:
    """Firm order request before the buyer gets a Proforma Invoice — the supplier
    commits first (shared/orchestration/supplier_commitment.py)."""
    from shared.product_label import product_phrase
    product = product_phrase(ctx.get("hs_code", "") or "")
    qty = _qty(ctx.get("quantity_mt"))
    price = _price(ctx, ctx.get("price"))
    hours = ctx.get("hours") or 24
    window = f"{hours:g} hours" if float(hours) != 24 else "24 hours"
    subject = f"Order confirmation requested — {qty} MT {product} at {price}/MT FOB (deal {ctx['deal_id']})"
    opening = (
        "Thank you for your offer during our discussions. We now have a confirmed overseas buyer for this "
        "order and would like to place it with you."
        if ctx.get("introduced") else
        f"Thank you for your quotation of {price} per MT FOB for {product}. We now have a confirmed overseas "
        "buyer for this order and would like to place it with you at your quoted price."
    )
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        f"{opening}\n\n"
        "Please confirm the following terms:\n\n"
        f"  Product          : {product} (HS {ctx.get('hs_code', '')})\n"
        f"  Quantity         : {qty} MT\n"
        f"  Price            : {price} per MT FOB Indian port of loading\n"
        "  Payment          : Purchase Order issued only after the buyer has paid into a neutral escrow\n"
        "                     account; production advance at the Purchase Order, balance from escrow as\n"
        "                     the shipment clears and is accepted\n"
        "  Inspection       : Independent pre-shipment inspection at your facility before dispatch\n"
        + (f"  Packing          : {ctx['packing']}\n" if ctx.get("packing") else "")
        + "\n"
        + ("The buyer requires the packing above and the inspector will check it. Your price must include this "
           "packing; if it costs extra, tell us the amount per MT in your reply instead of confirming.\n\n"
           if ctx.get("packing_special") else
           "Packing is your standard export packing, which the buyer has accepted.\n\n" if ctx.get("packing") else "")
        + _rate_line(ctx)
        + "To confirm, simply reply \"We confirm the order\" and, if you have not already done so, let us know "
        "your port of loading, your lead time from Purchase Order to goods at port"
        + (f" and {_ADDRESS_ASK}" if ctx.get("address_needed") else "")
        + ". As soon as we receive "
        "your confirmation we will finalise the contract with the buyer, and the Purchase Order follows once "
        "the buyer's payment is in escrow.\n\n"
        f"To hold this order for you, we kindly ask for your confirmation within {window}. If you are unable "
        "to supply on these terms, a short reply letting us know is appreciated."
        + _signature()
    )
    return subject, body


def _tpl_qc_booked(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Pre-shipment inspection booked — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        f"A pre-shipment quality inspection has been booked for the goods under "
        f"Purchase Order {ctx.get('po_id', '')}.\n\n"
        f"  Inspection agency : {str(ctx.get('provider', '')).replace('_', ' ').upper()}\n"
        f"  Booking reference : {ctx.get('provider_order_number') or ctx.get('ext_ref', '')}\n"
        + (f"  Inspection date   : {ctx['inspection_date']}\n" if ctx.get("inspection_date") else "")
        + "  Sampling          : ISO 2859-1, general inspection level II\n"
        + "\nOn the inspection date please have:\n"
        "  - 100% of the order quantity produced and at least "
        f"{ctx.get('packed_pct', 80)}% packed and labelled, the rest available for sampling;\n"
        "  - the Purchase Order, the packing list and the product specification at hand;\n"
        "  - a person who can open cartons / bags and move goods for the inspector.\n"
        + (f"\nThe inspector checks the buyer's packing: {ctx['packing']} — weight per unit, marking and "
           "condition, with photos.\n" if ctx.get("packing") else "")
        + ("\nThe inspector also supervises the container stuffing and seals the container, so please "
           "plan the loading right after the inspection.\n" if ctx.get("loading_supervision") else "")
        + "\nIf the goods will not be ready on that date, tell us at once (at least one working day "
        "before) so we can move the booking — a wasted visit may be charged. Dispatch proceeds only "
        "after a clean inspection report."
        + _signature()
    )
    return subject, body


def _tpl_freight_booked(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Shipment booked — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"The inspection has passed and your shipment has been booked.\n\n"
        f"  Forwarder        : {str(ctx.get('provider', '')).title()}\n"
        f"  Booking reference: {ctx.get('ext_ref', '')}\n"
        + (f"  Vessel / voyage  : {ctx.get('vessel', '')} / {ctx.get('voyage', '')}\n"
           if ctx.get("vessel") else "")
        + (f"  ETD              : {ctx['etd']}\n" if ctx.get("etd") else "")
        + (f"  ETA              : {ctx['eta']}\n" if ctx.get("eta") else "")
        + "\nThe commercial invoice, packing list, certificate of origin and shipping bill "
        "are being prepared and will be shared once customs clearance is complete."
        + _signature()
    )
    return subject, body


def _tpl_cargo_cleared(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Cargo cleared — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        "Your cargo has cleared customs at destination. The complete document pack for this "
        "shipment (commercial invoice, packing list, certificate of origin and shipping bill) "
        "is available on request.\n\n"
        "Please inspect the goods on receipt. Unless a dispute is raised within the "
        "inspection window, the final escrow tranche is released automatically."
        + _signature()
    )
    return subject, body


def _tpl_deal_completed(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Order completed — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('name'))},\n\n"
        f"The order under deal {ctx['deal_id']} is now complete and all payments have "
        f"been settled. Thank you for trading with us — we look forward to the next one."
        + _signature()
    )
    return subject, body


def _tpl_pi_reminder(ctx: dict[str, Any]) -> tuple[str, str]:
    pi = ctx["pi"]
    subject = f"Reminder: Proforma Invoice {pi.pi_id} awaiting your confirmation"
    body = (
        f"{_dear(pi.buyer_name)},\n\n"
        f"A gentle reminder that Proforma Invoice {pi.pi_id} "
        f"({_fmt_money(pi.total_value, pi.currency)}, {_qty(pi.quantity_mt)} MT) is awaiting your "
        f"confirmation and is valid until "
        f"{pi.expires_at.strftime('%Y-%m-%d') if pi.expires_at else 'the stated date'}.\n\n"
        "Reply \"We accept the Proforma Invoice\" to proceed, or let us know if anything "
        "needs to change."
        + _signature()
    )
    return subject, body


def _tpl_escrow_reminder(ctx: dict[str, Any]) -> tuple[str, str]:
    esc = ctx["escrow"]
    subject = f"Reminder: escrow funding pending — deal {ctx['deal_id']}"
    shortfall = getattr(esc, "shortfall", None)
    if shortfall:
        opening = (
            f"The escrow for your order (reference {esc.escrow_id}) is partially funded: "
            f"{_fmt_money(esc.funded_amount, esc.currency)} received of "
            f"{_fmt_money(esc.amount, esc.currency)} — a balance of "
            f"{_fmt_money(shortfall, esc.currency)} is still outstanding. Production starts "
            "as soon as the full amount is held.\n"
        )
    else:
        opening = (
            f"The escrow for your order ({_fmt_money(esc.amount, esc.currency)}, reference "
            f"{esc.escrow_id}) has not been funded yet. Production starts as soon as it is.\n"
        )
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        + opening
        + (f"\n  Payment link: {esc.checkout_url}\n" if esc.checkout_url else "")
        + "\nPlease let us know if you need any assistance with the payment."
        + _signature()
    )
    return subject, body


def _tpl_funding_shortfall(ctx: dict[str, Any]) -> tuple[str, str]:
    esc = ctx["escrow"]
    subject = f"Escrow partially funded — balance due for deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"Thank you — the escrow provider has confirmed receipt of "
        f"{_fmt_money(esc.funded_amount, esc.currency)} for escrow {esc.escrow_id}. "
        f"The Proforma Invoice total is {_fmt_money(esc.amount, esc.currency)}, so a balance of "
        f"{_fmt_money(esc.shortfall, esc.currency)} is still outstanding.\n"
        + (f"\n  Payment link: {esc.checkout_url}\n" if esc.checkout_url else "")
        + "\nProduction (and the Purchase Order to the supplier) starts as soon as the full "
        "amount is held in escrow. Please let us know if the shortfall is a bank charge or a "
        "partial payment so we can reconcile it."
        + _signature()
    )
    return subject, body


def _tpl_inspection_window_open(ctx: dict[str, Any]) -> tuple[str, str]:
    esc = ctx["escrow"]
    deadline = ctx.get("deadline")
    subject = f"Goods delivered — please confirm acceptance (deal {ctx['deal_id']})"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"The shipment under deal {ctx['deal_id']} has cleared and the final escrow tranche "
        f"({_fmt_money(ctx.get('final_amount'), esc.currency)}) is now waiting on your acceptance "
        f"of the goods.\n\n"
        f"  Escrow reference : {esc.escrow_id}\n"
        + (f"  Inspection window: until {deadline}\n" if deadline else "")
        + "\nPlease inspect the goods on receipt. Reply \"We accept the goods\" to release the "
        "final tranche now, or tell us about any issue before the window closes — a dispute "
        "raised in time holds the tranche until it is resolved. If we hear nothing by the "
        "deadline the tranche is released automatically, as agreed in the Proforma Invoice."
        + _signature()
    )
    return subject, body


def _tpl_deal_settled(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Escrow settled — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('name'))},\n\n"
        f"Every escrow tranche for deal {ctx['deal_id']} has now been released and the "
        f"money side of the trade is closed"
        + (f" (supplier balance of {_fmt_money(ctx['balance_due'], ctx.get('currency', 'USD'))} "
           "is being paid out)" if ctx.get("balance_due") else "")
        + ". Thank you for trading with us."
        + _signature()
    )
    return subject, body


def _tpl_po_reminder(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Reminder: please confirm Purchase Order — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        f"We have not yet received your confirmation of our Purchase Order"
        + (f" {ctx['po_id']}" if ctx.get("po_id") else "")
        + f" for deal {ctx['deal_id']}. The escrow for this order is funded, so production can "
        "start immediately.\n\nPlease reply with your confirmation and the expected ready-for-"
        "inspection date so we can schedule the pre-shipment inspection."
        + _signature()
    )
    return subject, body


def _tpl_qc_readiness_reminder(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Reminder: pre-shipment inspection pending — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        f"The pre-shipment inspection for deal {ctx['deal_id']} has not been completed yet. "
        "Please confirm the lot is packed, labelled and ready for sampling, or tell us the date "
        "it will be, so the inspector can attend. The next escrow release depends on a clean "
        "inspection report."
        + _signature()
    )
    return subject, body


def _tpl_qc_rework_notice(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Inspection failed — rework and re-inspection for deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        f"The pre-shipment inspection for deal {ctx['deal_id']} did not pass"
        + (f": {ctx['notes']}" if ctx.get("notes") else "")
        + ".\n\nPlease rework the lot to the agreed specification. A re-inspection has been booked"
        + (f" (reference {ctx['ext_ref']})" if ctx.get("ext_ref") else "")
        + "; confirm the date the reworked lot will be ready. No escrow tranche is released "
        "until a clean report is issued."
        + _signature()
    )
    return subject, body


def _tpl_deal_cancelled(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Order cancelled — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('name'))},\n\n"
        f"The order under deal {ctx['deal_id']} has been cancelled"
        + (f": {ctx['reason']}" if ctx.get("reason") else "")
        + "."
        + (f" The {_fmt_money(ctx['refunded'], ctx.get('currency', 'USD'))} held in escrow is being refunded "
           "to your account." if ctx.get("refunded") else "")
        + "\n\nWe would be glad to work with you on a future order."
        + _signature()
    )
    return subject, body


def _tpl_escrow_funded(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Payment received in escrow — deal {ctx['deal_id']}"
    cur = ctx.get("currency") or "USD"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"Thank you — the escrow provider has confirmed your payment of "
        f"{_fmt_money(ctx.get('funded_amount') or ctx.get('amount'), cur)}"
        + (f" (escrow {ctx['escrow_id']})" if ctx.get("escrow_id") else "")
        + ". The full order value is now held in escrow.\n\n"
        "Next we place the Purchase Order with the supplier. Your money stays in escrow and "
        "is released in stages only against the agreed proof — order placed, a clean "
        "independent inspection, shipping, and your acceptance of the goods. We will write "
        "to you at each step."
        + _signature()
    )
    return subject, body


def _tpl_qc_passed(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Pre-shipment inspection passed — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"The independent pre-shipment inspection of your goods has passed.\n\n"
        + (f"  Inspection agency : {str(ctx['provider']).replace('_', ' ').upper()}\n" if ctx.get("provider") else "")
        + (f"  Report reference  : {ctx['ext_ref']}\n" if ctx.get("ext_ref") else "")
        + (f"  Container / seal  : {ctx['containers']} / {ctx['seals']}\n" if ctx.get("seals") else "")
        + "\nWe are now booking the shipment and preparing the export documents; you will "
        "receive the booking details shortly. The inspection report is available on request."
        + _signature()
    )
    return subject, body


def _tpl_qc_failed(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Pre-shipment inspection did not pass — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"The independent pre-shipment inspection of the goods for deal {ctx['deal_id']} did not pass"
        + (f": {ctx['notes']}" if ctx.get("notes") else "")
        + ".\n\nNothing ships until a clean inspection report is issued, and no further escrow "
        "money is released meanwhile. We are working with the supplier on rework and a "
        "re-inspection; if the goods cannot be brought to the agreed specification, the money "
        "still held in escrow is refunded to you. We will update you as soon as the next "
        "step is confirmed."
        + _signature()
    )
    return subject, body


_TRANCHE_STAGE = {
    "PO_ISSUED": "the production advance (order placed with the supplier)",
    "QC_PASSED": "the stage released on a clean independent inspection",
    "CLEARED": "the stage released on shipping and customs clearance",
    "ACCEPTED": "the final stage (acceptance of the goods)",
}


def _tpl_tranche_released(ctx: dict[str, Any]) -> tuple[str, str]:
    cur = ctx.get("currency") or "USD"
    stage = _TRANCHE_STAGE.get(str(ctx.get("trigger") or ""), "a scheduled stage")
    subject = f"Escrow release: {stage.split(' (')[0]} — deal {ctx['deal_id']}"
    lapsed = ctx.get("event") == "auto_release_window_lapsed"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"For your records: {_fmt_money(ctx.get('amount'), cur)} has been released from escrow "
        f"for {stage}"
        + (", as the inspection window closed without a dispute" if lapsed else "")
        + ".\n\n"
        + (f"  Escrow reference : {ctx['escrow_id']}\n" if ctx.get("escrow_id") else "")
        + f"  Released so far  : {_fmt_money(ctx.get('released_total'), cur)}\n"
        + (f"  Still in escrow  : {_fmt_money(ctx['remaining'], cur)}\n" if ctx.get("remaining") is not None else "")
        + "\nThe remaining balance stays in escrow until the next agreed milestone."
        + _signature()
    )
    return subject, body


def _tpl_payout_sent(ctx: dict[str, Any]) -> tuple[str, str]:
    cur = ctx.get("currency") or "USD"
    subject = f"Payment sent for deal {ctx['deal_id']}"
    balance = ctx.get("balance_due")
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        f"We have sent you a payment of {_fmt_money(ctx.get('amount'), cur)} for the goods under "
        f"deal {ctx['deal_id']}.\n\n"
        + (f"  Payment reference : {ctx['ext_ref']}\n" if ctx.get("ext_ref") else "")
        + (f"  Paid so far       : {_fmt_money(ctx['completed_total'], cur)}\n" if ctx.get("completed_total") else "")
        + (f"  Order value       : {_fmt_money(ctx['supplier_owed'], cur)}\n" if ctx.get("supplier_owed") else "")
        + ("\nThis completes payment for the order." if not balance else
           f"\nThe balance of {_fmt_money(balance, cur)} follows at the next milestone "
           "(the last share after the buyer accepts the goods).")
        + "\nPlease allow the usual bank processing time and let us know once it is credited."
        + _signature()
    )
    return subject, body


def _tpl_goods_delivered(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Goods delivered to the buyer — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('supplier_name'))},\n\n"
        f"The carrier reports the shipment for deal {ctx['deal_id']} as delivered"
        + (f" ({ctx['location']})" if ctx.get("location") else "")
        + ". The buyer now has the agreed inspection window to accept the goods.\n\n"
        "The last share of your payment is released once the buyer accepts, or automatically "
        "when the window closes without a complaint. Thank you for a smooth shipment."
        + _signature()
    )
    return subject, body


def _tpl_escrow_refunded(ctx: dict[str, Any]) -> tuple[str, str]:
    cur = ctx.get("currency") or "USD"
    subject = f"Escrow refund issued — deal {ctx['deal_id']}"
    body = (
        f"{_dear(ctx.get('buyer_name'))},\n\n"
        f"We have instructed the escrow provider to refund {_fmt_money(ctx.get('amount'), cur)} "
        f"— the balance still held in escrow for deal {ctx['deal_id']} — to the account it came from"
        + (f". Reason: {ctx['reason']}" if ctx.get("reason") else "")
        + ".\n\n"
        + (f"  Escrow reference : {ctx['escrow_id']}\n\n" if ctx.get("escrow_id") else "")
        + "Please allow the provider's usual processing time. We are sorry this order could "
        "not be completed and would be glad to work with you again."
        + _signature()
    )
    return subject, body


_TEMPLATES = {
    "escrow_funded": _tpl_escrow_funded,
    "qc_passed": _tpl_qc_passed,
    "qc_failed": _tpl_qc_failed,
    "tranche_released": _tpl_tranche_released,
    "payout_sent": _tpl_payout_sent,
    "goods_delivered": _tpl_goods_delivered,
    "escrow_refunded": _tpl_escrow_refunded,
    "pi_issued": _tpl_pi_issued,
    "qc_rework_notice": _tpl_qc_rework_notice,
    "deal_cancelled": _tpl_deal_cancelled,
    "po_reminder": _tpl_po_reminder,
    "qc_readiness_reminder": _tpl_qc_readiness_reminder,
    "funding_shortfall": _tpl_funding_shortfall,
    "inspection_window_open": _tpl_inspection_window_open,
    "deal_settled": _tpl_deal_settled,
    "escrow_opened": _tpl_escrow_opened,
    "order_confirmed_buyer": _tpl_order_confirmed_buyer,
    "order_confirmed_supplier": _tpl_order_confirmed_supplier,
    "inspection_phase": _tpl_inspection_phase,
    "bill_of_lading": _tpl_bill_of_lading,
    "po_issued": _tpl_po_issued,
    "qc_readiness_request": _tpl_qc_readiness_request,
    "facility_address_request": _tpl_facility_address_request,
    "qc_booked": _tpl_qc_booked,
    "freight_booked": _tpl_freight_booked,
    "cargo_cleared": _tpl_cargo_cleared,
    "supplier_order_request": _tpl_supplier_order_request,
    "deal_completed": _tpl_deal_completed,
    "pi_reminder": _tpl_pi_reminder,
    "escrow_reminder": _tpl_escrow_reminder,
}


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

async def _session_for(deal_id: str):
    from agents.negotiation_agent.repository import get_repository  # type: ignore[import]
    return await get_repository().get_session_by_deal(deal_id)


async def _record_skipped(
    email_id: str, deal_id: str, party: str, kind: str, context: dict[str, Any], *,
    negotiation_id: str, round_no: int, reason: str,
) -> None:
    """Keep a SKIPPED record of a notice that could not be addressed, so the
    console shows the gap instead of a silent server log line. Best-effort;
    never overwrites a notice that was drafted or sent."""
    try:
        from agents.negotiation_agent.email_history import (  # type: ignore[import]
            EmailDirection, EmailHistoryRecord, EmailStatus, get_email_repository,
        )
        from agents.negotiation_agent.service import _SMTP_FROM  # type: ignore[import]

        repo = get_email_repository()
        existing = await repo.get(email_id)
        if existing is not None and existing.status != EmailStatus.SKIPPED:
            return
        try:
            subject, body = _TEMPLATES[kind]({"deal_id": deal_id, **context})
        except Exception:  # noqa: BLE001 — a template short of context still leaves a trace
            subject, body = f"{kind.replace('_', ' ').title()} — deal {deal_id}", ""
        now = datetime.now(timezone.utc)
        await repo.save(EmailHistoryRecord(
            email_id=email_id, negotiation_id=negotiation_id, deal_id=deal_id, round_no=round_no,
            direction=EmailDirection.OUTBOUND, party=party, to_address="", from_address=_SMTP_FROM,
            subject=subject, body=body, status=EmailStatus.SKIPPED, kind="notification",
            notice_kind=kind, delivery_note=reason,
            created_at=existing.created_at if existing else now, updated_at=now,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Notifications: could not record skipped %s for deal %s: %s", kind, deal_id, exc)


async def _local_terms(ctx: dict[str, Any], deal_id: str, party: str, deal: Any | None) -> None:
    """What every template may use about this party: the currency its prices are
    put in (its country's, next to the USD contract figure — shared/currency.py)
    and, for the supplier, whether its facility address is still missing.
    Separate keys: ``currency`` in a context is the currency of an amount."""
    from shared import currency
    try:
        if deal is None:
            from services.deals.service import get_deal  # type: ignore[import]
            deal = await get_deal(deal_id)
        p = next((x for x in getattr(deal, "parties", None) or []
                  if str(getattr(x.role, "value", x.role)).lower() == party), None)
    except Exception:  # noqa: BLE001
        p = None
    country = getattr(p, "country", None) or ("IN" if party == "supplier" else None)
    code = currency.party_currency(country)
    ctx.setdefault("party_currency", code)
    if "fx_rate" not in ctx:
        ctx["fx_rate"] = await currency.usd_rate(code) if code != "USD" else None
    if party == "supplier" and "address_needed" not in ctx:
        from shared.orchestration import supplier_address
        ctx["address_needed"] = not supplier_address.is_complete(getattr(p, "address", None), country)


def _price(ctx: dict[str, Any], usd: Any) -> str:
    """A USD price as this party reads it: ``INR 87,360 (USD 910.00)`` or ``USD 910.00``."""
    from shared import currency
    return currency.money(usd, ctx.get("party_currency") or "USD", ctx.get("fx_rate"))


def _rate_line(ctx: dict[str, Any]) -> str:
    from shared import currency
    note = currency.rate_note(ctx.get("party_currency") or "USD", ctx.get("fx_rate"))
    return f"{note}\n\n" if note else ""


def _po_money(ctx: dict[str, Any], value: Any, code: str | None) -> str:
    return _price(ctx, value) if (code or "USD") == "USD" else _fmt_money(value, code)


_ADDRESS_ASK = ("the full address of the facility where the goods are produced and will be inspected "
                "(street, town and PIN code) — the inspector is booked for that address")


async def notify_party(
    deal_id: str,
    party: str,
    kind: str,
    context: dict[str, Any],
    *,
    suffix: str = "",
    deal: Any | None = None,
) -> str | None:
    """Draft, record, gate and (per policy) send one notification.

    Returns the email id, or None when nothing could be sent (notifications
    disabled, no negotiation session for the deal, no address on file for
    that party). Never raises — a notification failure must not break the
    pipeline leg that triggered it.
    """
    if not enabled():
        return None
    if kind not in _TEMPLATES:
        logger.error("Notifications: unknown kind %r for deal %s", kind, deal_id)
        return None
    try:
        session = await _session_for(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Notifications: session lookup failed for %s: %s", deal_id, exc)
        session = None
    email_id = _email_id(kind, deal_id, suffix)
    if session is None:
        logger.warning(
            "Notifications: deal %s has no negotiation session — cannot address the %s; "
            "%s not sent", deal_id, party, kind,
        )
        await _record_skipped(email_id, deal_id, party, kind, context, negotiation_id="", round_no=0,
                              reason=f"No negotiation thread for this deal — the {party} has no address on file.")
        return None
    to_address = session.buyer_email if party == "buyer" else session.seller_email
    if not to_address:
        logger.warning(
            "Notifications: no %s email on negotiation %s — %s not sent",
            party, session.negotiation_id, kind,
        )
        await _record_skipped(email_id, deal_id, party, kind, context, negotiation_id=session.negotiation_id,
                              round_no=session.round_no,
                              reason=f"No e-mail address on file for the {party}.")
        return None

    try:
        from agents.negotiation_agent.email_history import (  # type: ignore[import]
            EmailDirection, EmailHistoryRecord, EmailStatus, get_email_repository,
        )
        from agents.negotiation_agent.service import _SMTP_FROM  # type: ignore[import]

        repo = get_email_repository()
        existing = await repo.get(email_id)
        if existing is not None and existing.status != EmailStatus.SKIPPED:
            return email_id  # already drafted/sent — idempotent (a SKIPPED one is retried)

        ctx = {"deal_id": deal_id, **context}
        await _local_terms(ctx, deal_id, party, deal)
        subject, body = _TEMPLATES[kind](ctx)
        now = datetime.now(timezone.utc)
        record = EmailHistoryRecord(
            email_id=email_id,
            negotiation_id=session.negotiation_id,
            deal_id=deal_id,
            round_no=session.round_no,
            direction=EmailDirection.OUTBOUND,
            party=party,
            to_address=to_address,
            from_address=_SMTP_FROM,
            subject=subject,
            body=body,
            status=EmailStatus.DRAFT,
            kind="notification",
            notice_kind=kind,
            created_at=now,
            updated_at=now,
        )
        await repo.save(record)

        from shared.orchestration.autonomy_policy import resolve_level
        from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
        from services.hitl_approval.service import create_approval  # type: ignore[import]

        approval = await create_approval(CreateApprovalRequest(
            deal_id=deal_id,
            action=ACTION,
            summary=f"{kind.replace('_', ' ').title()} → {party} <{to_address}>: {subject}",
            risk_flags=["templated_notification"],
            autonomy_level=resolve_level(ACTION, deal),
            callback_payload={
                "email_id": email_id,
                "negotiation_id": session.negotiation_id,
                "party": party,
                "kind": kind,
            },
            idempotency_key=email_id,
        ))
        await repo.save(record.model_copy(update={
            "approval_id": approval.approval_id,
            "status": EmailStatus.PENDING_APPROVAL,
            "updated_at": datetime.now(timezone.utc),
        }))
        logger.info(
            "Notifications: %s to %s for deal %s gated as %s (%s)",
            kind, party, deal_id, approval.approval_id, approval.autonomy_level,
        )
        return email_id
    except Exception as exc:  # noqa: BLE001
        logger.error("Notifications: %s to %s for deal %s failed: %s", kind, party, deal_id, exc)
        return None


def _party_name(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        raw = getattr(p, "role", "")
        if (getattr(raw, "value", raw) or "").lower() == role:
            return getattr(p, "legal_name", None)
    return None


# ---------------------------------------------------------------------------
# Convenience wrappers used by the continuations and the deal watcher
# ---------------------------------------------------------------------------

async def notify_pi_issued(deal: Any, pi: Any) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "pi_issued",
        {"pi": pi, "hs_code": getattr(deal, "hs_code", "")}, deal=deal,
    )


async def notify_escrow_opened(deal: Any, escrow: Any, pi_id: str | None = None) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "escrow_opened",
        {"escrow": escrow, "pi_id": pi_id or "", "buyer_name": _party_name(deal, "buyer")},
        deal=deal,
    )


async def notify_order_confirmed(deal: Any, pi: Any, escrow: Any) -> list[str | None]:
    """The consolidated order summary — one e-mail to the buyer, one to the supplier,
    each with only its own side of the trade."""
    from shared.orchestration import inspection_plan, packing, supplier_commitment, trade_brief
    b = await trade_brief.brief(deal.deal_id)
    plan = await inspection_plan.plan_for(deal.deal_id)
    agency = None
    try:
        from services.shield_link.service import _buyer_opted_qc_provider, _buyer_packing  # type: ignore[import]
        agency = await _buyer_opted_qc_provider(deal.deal_id)
        packed = packing.describe(await _buyer_packing(deal.deal_id))
    except Exception:  # noqa: BLE001
        packed = ""
    from shared.connectors.qc_providers import QC_PROVIDER_CATALOG
    agency_name = QC_PROVIDER_CATALOG.get(agency or "", {}).get("display_name") if agency else None
    common = {"product": b.get("product"), "hs_code": getattr(deal, "hs_code", ""), "inspection_types": plan,
              "agency": agency_name, "carrier": "Maersk", "packing": packed}
    commitment = await supplier_commitment.get(deal.deal_id)
    supplier_price = getattr(commitment, "confirmed_price", None) if commitment else None
    from shared.orchestration.continuations import _supplier_port
    port = await _supplier_port(deal.deal_id)
    buyer = await notify_party(deal.deal_id, "buyer", "order_confirmed_buyer",
                               {**common, "pi": pi, "escrow": escrow, "port_of_loading": port}, deal=deal)
    supplier = await notify_party(deal.deal_id, "supplier", "order_confirmed_supplier",
                                  {**common, "quantity_mt": getattr(pi, "quantity_mt", None),
                                   "supplier_price": supplier_price, "port_of_loading": port,
                                   "supplier_name": _party_name(deal, "supplier")}, deal=deal)
    return [buyer, supplier]


async def notify_inspection_phase(deal: Any, payload: dict[str, Any]) -> list[str | None]:
    """DUPRO / PEO result: the supplier hears a DUPRO failure (rework); the buyer hears every result."""
    sent = []
    suffix = f"{payload.get('inspection_type')}-{payload.get('tpo_id')}"
    if payload.get("result") == "FAIL" and payload.get("inspection_type") == "DUPRO":
        sent.append(await notify_party(deal.deal_id, "supplier", "inspection_phase",
                                       {**payload, "party": "supplier", "name": _party_name(deal, "supplier")},
                                       suffix=suffix + "-s", deal=deal))
    sent.append(await notify_party(deal.deal_id, "buyer", "inspection_phase",
                                   {**payload, "party": "buyer", "name": _party_name(deal, "buyer")},
                                   suffix=suffix + "-b", deal=deal))
    return sent


async def notify_bill_of_lading(deal: Any, payload: dict[str, Any]) -> str | None:
    return await notify_party(deal.deal_id, "buyer", "bill_of_lading",
                              {**payload, "buyer_name": _party_name(deal, "buyer")}, deal=deal)


async def notify_po_issued(deal: Any, po: Any) -> str | None:
    return await notify_party(
        deal.deal_id, "supplier", "po_issued",
        {"po": po, "hs_code": getattr(deal, "hs_code", "")}, deal=deal,
    )


async def notify_qc_booked(deal: Any, payload: dict[str, Any], po_id: str | None) -> str | None:
    from shared.orchestration import packing
    try:
        from services.shield_link.service import _buyer_packing  # type: ignore[import]
        buyer_packing = packing.describe(await _buyer_packing(deal.deal_id))
    except Exception:  # noqa: BLE001
        buyer_packing = ""
    return await notify_party(
        deal.deal_id, "supplier", "qc_booked",
        {
            "provider": payload.get("provider"),
            "ext_ref": payload.get("ext_ref"),
            "provider_order_number": payload.get("provider_order_number"),
            "inspection_date": payload.get("inspection_date"),
            "loading_supervision": payload.get("loading_supervision"),
            "packed_pct": _packed_pct(),
            "packing": buyer_packing,
            "po_id": po_id or "",
            "supplier_name": _party_name(deal, "supplier"),
        },
        # a rescheduled booking is a new notice with the new date
        suffix=str(payload.get("tpo_id") or "") if payload.get("rescheduled_from") else "",
        deal=deal,
    )


async def notify_freight_booked(deal: Any, payload: dict[str, Any]) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "freight_booked",
        {
            "provider": payload.get("provider"),
            "ext_ref": payload.get("ext_ref"),
            "vessel": payload.get("vessel"),
            "voyage": payload.get("voyage"),
            "etd": payload.get("etd"),
            "eta": payload.get("eta"),
            "buyer_name": _party_name(deal, "buyer"),
        },
        deal=deal,
    )


async def notify_cargo_cleared(deal: Any, payload: dict[str, Any]) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "cargo_cleared",
        {"milestone": payload.get("milestone"), "buyer_name": _party_name(deal, "buyer")},
        deal=deal,
    )


async def notify_deal_completed(deal: Any) -> list[str]:
    sent: list[str] = []
    for party in ("buyer", "supplier"):
        eid = await notify_party(
            deal.deal_id, party, "deal_completed",
            {"name": _party_name(deal, party)}, suffix=party, deal=deal,
        )
        if eid:
            sent.append(eid)
    return sent


async def notify_funding_shortfall(deal: Any, escrow: Any) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "funding_shortfall",
        {"escrow": escrow, "buyer_name": _party_name(deal, "buyer")},
        suffix=f"{getattr(escrow, 'funded_amount', 0) or 0:.2f}", deal=deal,
    )


async def notify_inspection_window_open(
    deal: Any, escrow: Any, final_amount: float | None, deadline: str | None,
) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "inspection_window_open",
        {"escrow": escrow, "final_amount": final_amount, "deadline": deadline,
         "buyer_name": _party_name(deal, "buyer")},
        deal=deal,
    )


async def notify_deal_settled(deal: Any, balance_due: float | None, currency: str = "USD") -> list[str]:
    sent: list[str] = []
    for party in ("buyer", "supplier"):
        eid = await notify_party(
            deal.deal_id, party, "deal_settled",
            {"name": _party_name(deal, party),
             "balance_due": balance_due if party == "supplier" else None, "currency": currency},
            suffix=party, deal=deal,
        )
        if eid:
            sent.append(eid)
    return sent


async def notify_qc_rework(deal: Any, notes: str | None, ext_ref: str | None, attempt: int) -> str | None:
    return await notify_party(
        deal.deal_id, "supplier", "qc_rework_notice",
        {"notes": notes, "ext_ref": ext_ref, "supplier_name": _party_name(deal, "supplier")},
        suffix=f"a{attempt}", deal=deal,
    )


async def notify_deal_cancelled(deal: Any, reason: str, refunded: float | None, currency: str = "USD") -> list[str]:
    sent: list[str] = []
    for party in ("buyer", "supplier"):
        eid = await notify_party(
            deal.deal_id, party, "deal_cancelled",
            {"name": _party_name(deal, party), "reason": reason,
             "refunded": refunded if party == "buyer" else None, "currency": currency},
            suffix=party, deal=deal,
        )
        if eid:
            sent.append(eid)
    return sent


async def notify_po_reminder(deal: Any, reminder_no: int) -> str | None:
    po_id = None
    try:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        po = await _dm_repo().get_po_by_deal(deal.deal_id)
        po_id = po.po_id if po else None
    except Exception:  # noqa: BLE001
        pass
    return await notify_party(
        deal.deal_id, "supplier", "po_reminder",
        {"po_id": po_id, "supplier_name": _party_name(deal, "supplier")},
        suffix=f"r{reminder_no}", deal=deal,
    )


async def notify_qc_readiness_reminder(deal: Any, reminder_no: int) -> str | None:
    return await notify_party(
        deal.deal_id, "supplier", "qc_readiness_reminder",
        {"supplier_name": _party_name(deal, "supplier")},
        suffix=f"r{reminder_no}", deal=deal,
    )


async def notify_pi_reminder(deal: Any, pi: Any, reminder_no: int) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "pi_reminder", {"pi": pi},
        suffix=f"r{reminder_no}", deal=deal,
    )


async def notify_escrow_funded(deal: Any, payload: dict[str, Any]) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "escrow_funded",
        {"escrow_id": payload.get("escrow_id"), "amount": payload.get("amount"),
         "funded_amount": payload.get("funded_amount"), "currency": payload.get("currency"),
         "buyer_name": _party_name(deal, "buyer")},
        deal=deal,
    )


async def notify_qc_result(deal: Any, payload: dict[str, Any], passed: bool) -> str | None:
    """Inspection outcome to the buyer (the supplier hears through the QC booking /
    rework notices). One notice per inspection report (``ext_ref``)."""
    return await notify_party(
        deal.deal_id, "buyer", "qc_passed" if passed else "qc_failed",
        {"provider": payload.get("provider"), "ext_ref": payload.get("ext_ref"),
         "notes": payload.get("notes"),
         "containers": ", ".join(payload.get("container_numbers") or []),
         "seals": ", ".join(payload.get("seal_numbers") or []),
         "buyer_name": _party_name(deal, "buyer")},
        suffix=str(payload.get("ext_ref") or payload.get("tpo_id") or ""), deal=deal,
    )


async def notify_tranche_released(deal: Any, payload: dict[str, Any], principal: float | None = None) -> str | None:
    released = payload.get("released_total")
    remaining = None
    try:
        if principal is not None and released is not None:
            remaining = round(max(float(principal) - float(released), 0.0), 2)
    except (TypeError, ValueError):
        remaining = None
    return await notify_party(
        deal.deal_id, "buyer", "tranche_released",
        {"trigger": payload.get("trigger"), "amount": payload.get("amount"),
         "released_total": released, "remaining": remaining,
         "escrow_id": payload.get("escrow_id"), "currency": payload.get("currency"),
         "event": payload.get("event"), "buyer_name": _party_name(deal, "buyer")},
        suffix=str(payload.get("tranche_id") or payload.get("trigger") or ""), deal=deal,
    )


async def notify_payout_sent(deal: Any, payload: dict[str, Any]) -> str | None:
    return await notify_party(
        deal.deal_id, "supplier", "payout_sent",
        {"amount": payload.get("amount"), "currency": payload.get("currency"),
         "ext_ref": payload.get("ext_ref"), "completed_total": payload.get("completed_total"),
         "supplier_owed": payload.get("supplier_owed"), "balance_due": payload.get("balance_due"),
         "supplier_name": _party_name(deal, "supplier")},
        suffix=str(payload.get("payout_id") or payload.get("ext_ref") or ""), deal=deal,
    )


async def notify_goods_delivered(deal: Any, payload: dict[str, Any]) -> str | None:
    return await notify_party(
        deal.deal_id, "supplier", "goods_delivered",
        {"location": payload.get("location"), "supplier_name": _party_name(deal, "supplier")},
        deal=deal,
    )


async def notify_escrow_refunded(deal: Any, escrow_id: str, amount: float, currency: str,
                                 reason: str | None) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "escrow_refunded",
        {"escrow_id": escrow_id, "amount": amount, "currency": currency, "reason": reason,
         "buyer_name": _party_name(deal, "buyer")},
        deal=deal,
    )


async def notify_escrow_reminder(deal: Any, escrow: Any, reminder_no: int) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "escrow_reminder",
        {"escrow": escrow, "buyer_name": _party_name(deal, "buyer")},
        suffix=f"r{reminder_no}", deal=deal,
    )


# ---------------------------------------------------------------------------
# Correspondence log — what each counterparty was told, and whether it arrived
# ---------------------------------------------------------------------------

NOTICE_LABELS: dict[str, tuple[str, str]] = {
    # kind: (label, deal stage it belongs to)
    "pi_issued": ("Proforma Invoice issued", "PI_SENT"),
    "pi_reminder": ("PI reminder", "PI_SENT"),
    "escrow_opened": ("Escrow funding instructions", "ESCROW_PENDING"),
    "order_confirmed_buyer": ("Order confirmed — full summary", "PI_ACCEPTED"),
    "order_confirmed_supplier": ("Order confirmed — full plan", "PI_ACCEPTED"),
    "inspection_phase": ("Inspection phase result", "—"),
    "bill_of_lading": ("Bill of lading issued", "IN_TRANSIT"),
    "escrow_reminder": ("Escrow funding reminder", "ESCROW_PENDING"),
    "funding_shortfall": ("Escrow partially funded", "ESCROW_PENDING"),
    "escrow_funded": ("Payment received in escrow", "ESCROW_FUNDED"),
    "supplier_order_request": ("Order request to supplier", "ESCROW_FUNDED"),
    "po_issued": ("Purchase Order issued", "PO_ISSUED"),
    "po_reminder": ("PO confirmation reminder", "PO_ISSUED"),
    "qc_readiness_request": ("Goods-ready date requested", "PO_ISSUED"),
    "facility_address_request": ("Facility address requested", "PO_ISSUED"),
    "qc_booked": ("Inspection booked", "SUPPLIER_DISPATCH_READY"),
    "qc_readiness_reminder": ("Inspection readiness reminder", "SUPPLIER_DISPATCH_READY"),
    "qc_passed": ("Inspection passed", "QC_PASSED"),
    "qc_failed": ("Inspection did not pass", "QC_FAILED"),
    "qc_rework_notice": ("Rework + re-inspection", "DISPUTE"),
    "freight_booked": ("Shipment booked", "IN_TRANSIT"),
    "cargo_cleared": ("Cargo cleared customs", "CLEARED"),
    "goods_delivered": ("Goods delivered", "CLEARED"),
    "inspection_window_open": ("Acceptance window open", "CLEARED"),
    "tranche_released": ("Escrow stage released", "—"),
    "payout_sent": ("Payment sent to supplier", "CLEARED"),
    "deal_settled": ("Escrow settled", "PAID"),
    "deal_completed": ("Order completed", "PAID"),
    "escrow_refunded": ("Escrow refunded", "REFUNDED"),
    "deal_cancelled": ("Order cancelled", "CANCELLED"),
}

_KIND_LABELS = {
    "negotiation": "Negotiation e-mail",
    "followup": "Follow-up",
    "outreach": "Buyer introduction",
    "outreach_intro": "Buyer introduction",
    "outreach_followup": "Buyer introduction follow-up",
    "rfq": "Request for quotation",
    "indicative_offer": "Indicative offer",
}


def mailbox_configured() -> bool:
    try:
        from services.email_gateway.config import settings as gw  # type: ignore[import]
        return bool(gw.gmail_sender_address and gw.gmail_app_password)
    except Exception:  # noqa: BLE001
        return False


def _delivery(status: str) -> str:
    """One word for the console: delivered / logged_only / pending / skipped / failed / blocked / rejected / received."""
    return {
        "sent": "delivered", "modified_sent": "delivered", "logged_only": "logged_only",
        "draft": "pending", "pending_approval": "pending", "skipped": "skipped",
        "failed": "failed", "blocked": "blocked", "rejected": "rejected", "received": "received",
    }.get(status, status)


async def deal_correspondence_log(deal_id: str) -> dict[str, Any]:
    """Every e-mail of the deal to or from the buyer and the supplier — status
    notices, negotiation, outreach, RFQ — with the delivery outcome in one word,
    newest first, plus per-party counts. Backs ``GET /deals/{id}/notifications``."""
    from agents.negotiation_agent.email_history import get_email_repository  # type: ignore[import]

    records = await get_email_repository().list_by_deal(deal_id)
    entries: list[dict[str, Any]] = []
    summary: dict[str, dict[str, int]] = {}
    for r in records:
        status = str(getattr(r.status, "value", r.status))
        direction = str(getattr(r.direction, "value", r.direction))
        kind = r.kind or "negotiation"
        notice = getattr(r, "notice_kind", None)
        if kind == "notification":
            label, stage = NOTICE_LABELS.get(notice or "", ((notice or "Deal update").replace("_", " ").capitalize(), None))
        else:
            base = kind[:-len("_reply")] if kind.endswith("_reply") else kind
            label, stage = _KIND_LABELS.get(base, base.replace("_", " ").capitalize()), None
        if direction == "inbound":
            label = "Reply received" if kind in ("negotiation", "notification") else f"Reply to: {label}"
        delivery = _delivery(status)
        party = r.party or "unknown"
        summary.setdefault(party, {}).setdefault(delivery, 0)
        summary[party][delivery] += 1
        entries.append({
            "email_id": r.email_id,
            "party": party,
            "direction": direction,
            "kind": kind,
            "notice_kind": notice,
            "label": label,
            "stage": stage,
            "status": status,
            "delivery": delivery,
            "delivery_note": getattr(r, "delivery_note", None) or r.block_reason,
            "to_address": r.to_address,
            "from_address": r.from_address,
            "subject": r.subject,
            "body": r.body,
            "approval_id": r.approval_id,
            "at": (r.sent_at or r.updated_at or r.created_at).isoformat(),
            "created_at": r.created_at.isoformat(),
        })
    entries.sort(key=lambda e: e["at"], reverse=True)
    return {
        "deal_id": deal_id,
        "mailbox_configured": mailbox_configured(),
        "notifications_enabled": enabled(),
        "summary": summary,
        "entries": entries,
    }
