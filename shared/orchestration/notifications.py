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


def _signature() -> str:
    return f"\n\nBest regards,\n{_entity_name()} — Trade Operations\n"


# ---------------------------------------------------------------------------
# Templates — (subject, body) per notification kind
# ---------------------------------------------------------------------------

def _tpl_pi_issued(ctx: dict[str, Any]) -> tuple[str, str]:
    pi = ctx["pi"]
    subject = f"Proforma Invoice {pi.pi_id} for your order — deal {ctx['deal_id']}"
    link = pi.storage_uri if str(pi.storage_uri or "").startswith("http") else None
    body = (
        f"Dear {pi.buyer_name or 'Buyer'},\n\n"
        f"Please find below the Proforma Invoice for the order we agreed on.\n\n"
        f"  Proforma Invoice : {pi.pi_id}\n"
        f"  Product (HS)     : {ctx.get('hs_code', '')}\n"
        f"  Quantity         : {pi.quantity_mt} MT\n"
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
        f"    • {t.tranche_id}: {int(round((t.pct or 0) * 100))}% released on {t.trigger}\n"
        for t in (getattr(esc, "tranches", None) or [])
    )
    body = (
        f"Dear {ctx.get('buyer_name') or 'Buyer'},\n\n"
        f"Thank you for accepting Proforma Invoice {ctx.get('pi_id', '')}. A neutral escrow "
        f"has been opened with {str(esc.provider).replace('_', '.').title()} for this order.\n\n"
        f"  Escrow reference : {esc.escrow_id}\n"
        f"  Amount to fund   : {_fmt_money(esc.amount, esc.currency)}\n"
        + (f"  Payment link     : {esc.checkout_url}\n" if esc.checkout_url else "")
        + ("\n  Staged release schedule:\n" + tranches if tranches else "")
        + "\nFunds are held by the escrow provider — never by us — and are released only "
        "on the schedule above: the production advance when the purchase order is placed, "
        "the rest against the independent QC report, the shipping documents and delivery. "
        "Production starts as soon as the escrow is funded."
        + _signature()
    )
    return subject, body


def _tpl_po_issued(ctx: dict[str, Any]) -> tuple[str, str]:
    po = ctx["po"]
    subject = f"Purchase Order {po.po_id} — deal {ctx['deal_id']}"
    link = po.storage_uri if str(po.storage_uri or "").startswith("http") else None
    body = (
        f"Dear {po.supplier_name or 'Supplier'},\n\n"
        f"We are pleased to confirm the order. Please find the Purchase Order details below.\n\n"
        f"  Purchase Order   : {po.po_id}\n"
        f"  Product (HS)     : {ctx.get('hs_code', '')}\n"
        f"  Quantity         : {po.quantity_mt} MT\n"
        f"  Unit price       : {_fmt_money(po.agreed_price, po.currency)} / MT ({po.incoterm})\n"
        f"  Total value      : {_fmt_money(po.total_value, po.currency)}\n"
        f"  Port of loading  : {po.port_of_loading}\n"
        f"  Delivery by      : {po.delivery_deadline}\n"
        f"  Payment terms    : {po.payment_terms}\n"
        + (f"  Quality spec     : {po.quality_spec}\n" if getattr(po, 'quality_spec', None) else "")
        + (f"  Document         : {link}\n" if link else "")
        + "\nPlease reply to confirm receipt and your production schedule. A third-party "
        "inspection will be booked before dispatch; we will share the inspector's details "
        "separately."
        + _signature()
    )
    return subject, body


def _tpl_qc_booked(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Pre-shipment inspection booked — deal {ctx['deal_id']}"
    body = (
        f"Dear {ctx.get('supplier_name') or 'Supplier'},\n\n"
        f"A pre-shipment quality inspection has been booked for the goods under "
        f"Purchase Order {ctx.get('po_id', '')}.\n\n"
        f"  Inspection agency : {str(ctx.get('provider', '')).replace('_', ' ').upper()}\n"
        f"  Booking reference : {ctx.get('ext_ref', '')}\n"
        + (f"  Inspection date   : {ctx['inspection_date']}\n" if ctx.get("inspection_date") else "")
        + "\nPlease have the full lot packed, labelled and accessible for sampling on the "
        "inspection date, and keep the inspector's sampling plan unobstructed. Dispatch "
        "proceeds only after a clean inspection report."
        + _signature()
    )
    return subject, body


def _tpl_freight_booked(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Shipment booked — deal {ctx['deal_id']}"
    body = (
        f"Dear {ctx.get('buyer_name') or 'Buyer'},\n\n"
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
        f"Dear {ctx.get('buyer_name') or 'Buyer'},\n\n"
        f"Your cargo has cleared ({ctx.get('milestone', 'CLEARED')}). The document pack "
        f"for this shipment is available on request.\n\n"
        "Please inspect the goods on receipt. Unless a dispute is raised within the "
        "inspection window, the final escrow tranche is released automatically."
        + _signature()
    )
    return subject, body


def _tpl_deal_completed(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Order completed — deal {ctx['deal_id']}"
    body = (
        f"Dear {ctx.get('name') or 'Partner'},\n\n"
        f"The order under deal {ctx['deal_id']} is now complete and all payments have "
        f"been settled. Thank you for trading with us — we look forward to the next one."
        + _signature()
    )
    return subject, body


def _tpl_pi_reminder(ctx: dict[str, Any]) -> tuple[str, str]:
    pi = ctx["pi"]
    subject = f"Reminder: Proforma Invoice {pi.pi_id} awaiting your confirmation"
    body = (
        f"Dear {pi.buyer_name or 'Buyer'},\n\n"
        f"A gentle reminder that Proforma Invoice {pi.pi_id} "
        f"({_fmt_money(pi.total_value, pi.currency)}, {pi.quantity_mt} MT) is awaiting your "
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
        f"Dear {ctx.get('buyer_name') or 'Buyer'},\n\n"
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
        f"Dear {ctx.get('buyer_name') or 'Buyer'},\n\n"
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
        f"Dear {ctx.get('buyer_name') or 'Buyer'},\n\n"
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
        f"Dear {ctx.get('name') or 'Partner'},\n\n"
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
        f"Dear {ctx.get('supplier_name') or 'Supplier'},\n\n"
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
        f"Dear {ctx.get('supplier_name') or 'Supplier'},\n\n"
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
        f"Dear {ctx.get('supplier_name') or 'Supplier'},\n\n"
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
        f"Dear {ctx.get('name') or 'Partner'},\n\n"
        f"The order under deal {ctx['deal_id']} has been cancelled"
        + (f": {ctx['reason']}" if ctx.get("reason") else "")
        + "."
        + (f" The {_fmt_money(ctx['refunded'], ctx.get('currency', 'USD'))} held in escrow is being refunded "
           "to your account." if ctx.get("refunded") else "")
        + "\n\nWe would be glad to work with you on a future order."
        + _signature()
    )
    return subject, body


_TEMPLATES = {
    "pi_issued": _tpl_pi_issued,
    "qc_rework_notice": _tpl_qc_rework_notice,
    "deal_cancelled": _tpl_deal_cancelled,
    "po_reminder": _tpl_po_reminder,
    "qc_readiness_reminder": _tpl_qc_readiness_reminder,
    "funding_shortfall": _tpl_funding_shortfall,
    "inspection_window_open": _tpl_inspection_window_open,
    "deal_settled": _tpl_deal_settled,
    "escrow_opened": _tpl_escrow_opened,
    "po_issued": _tpl_po_issued,
    "qc_booked": _tpl_qc_booked,
    "freight_booked": _tpl_freight_booked,
    "cargo_cleared": _tpl_cargo_cleared,
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
    if session is None:
        logger.warning(
            "Notifications: deal %s has no negotiation session — cannot address the %s; "
            "%s not sent", deal_id, party, kind,
        )
        return None
    to_address = session.buyer_email if party == "buyer" else session.seller_email
    if not to_address:
        logger.warning(
            "Notifications: no %s email on negotiation %s — %s not sent",
            party, session.negotiation_id, kind,
        )
        return None

    email_id = _email_id(kind, deal_id, suffix)
    try:
        from agents.negotiation_agent.email_history import (  # type: ignore[import]
            EmailDirection, EmailHistoryRecord, EmailStatus, get_email_repository,
        )
        from agents.negotiation_agent.service import _SMTP_FROM  # type: ignore[import]

        repo = get_email_repository()
        if await repo.get(email_id) is not None:
            return email_id  # already drafted/sent — idempotent

        ctx = {"deal_id": deal_id, **context}
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


async def notify_po_issued(deal: Any, po: Any) -> str | None:
    return await notify_party(
        deal.deal_id, "supplier", "po_issued",
        {"po": po, "hs_code": getattr(deal, "hs_code", "")}, deal=deal,
    )


async def notify_qc_booked(deal: Any, payload: dict[str, Any], po_id: str | None) -> str | None:
    return await notify_party(
        deal.deal_id, "supplier", "qc_booked",
        {
            "provider": payload.get("provider"),
            "ext_ref": payload.get("ext_ref"),
            "inspection_date": payload.get("inspection_date"),
            "po_id": po_id or "",
            "supplier_name": _party_name(deal, "supplier"),
        },
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


async def notify_escrow_reminder(deal: Any, escrow: Any, reminder_no: int) -> str | None:
    return await notify_party(
        deal.deal_id, "buyer", "escrow_reminder",
        {"escrow": escrow, "buyer_name": _party_name(deal, "buyer")},
        suffix=f"r{reminder_no}", deal=deal,
    )
