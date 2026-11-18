"""Best-and-final round with the suppliers of a prospect, by e-mail.

When several suppliers have quoted, the lowest valid FOB is leverage on the others. This
asks every other quoted supplier — on its own ``[RFQ:<id>]`` thread, so the reply comes back
through the normal quote intake — whether it can match or beat that price for the quantity
by a date. The e-mail never names the competitor nor says how many quotes there are.

``POST /deals/{id}/match/best-and-final`` opens one ``SEND_BEST_AND_FINAL`` gate (default L0)
listing the suppliers and the price; APPROVE sends the e-mails, REJECT sends nothing.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

ACTION = "SEND_BEST_AND_FINAL"
RESPOND_DAYS = 3


class NothingToAsk(ValueError):
    """Fewer than two valid quotes, or no other supplier that can be written to."""


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _rfq_id(offer: Any) -> str | None:
    if not getattr(offer, "quote_id", None):
        return None
    try:
        from services.sourcing.service import get_quote  # type: ignore[import]
        return (await get_quote(offer.quote_id)).rfq_id
    except Exception:  # noqa: BLE001 - an offer entered by staff has no RFQ thread
        return None


async def plan(deal: Any, now: datetime | None = None) -> dict[str, Any]:
    """The best price and the suppliers to ask (this prospect's own valid quotes)."""
    from shared.orchestration.matchmaking import offer_validity_days
    now = now or datetime.now(timezone.utc)
    valid = []
    for o in getattr(deal, "supplier_offers", None) or []:
        received = _aware(getattr(o, "received_at", None))
        validity = getattr(o, "validity_days", None) or offer_validity_days()
        if o.fob_price_usd_per_mt and received and received + timedelta(days=validity) >= now \
                and not getattr(o, "verification_failed", False):
            valid.append(o)
    if len(valid) < 2:
        raise NothingToAsk("a best-and-final round needs at least two valid supplier quotes")
    best = min(valid, key=lambda o: o.fob_price_usd_per_mt)
    others = []
    for o in sorted(valid, key=lambda o: o.fob_price_usd_per_mt):
        if o.supplier_id == best.supplier_id or o.fob_price_usd_per_mt <= best.fob_price_usd_per_mt or not o.email:
            continue
        rfq_id = await _rfq_id(o)
        if rfq_id:
            others.append({"supplier_id": o.supplier_id, "company_name": o.company_name, "email": o.email,
                           "rfq_id": rfq_id, "their_price_usd_per_mt": o.fob_price_usd_per_mt})
    if not others:
        raise NothingToAsk("no other quoted supplier with an e-mail and an RFQ thread")
    return {"best_price_usd_per_mt": best.fob_price_usd_per_mt, "best_supplier_id": best.supplier_id,
            "respond_by": (now + timedelta(days=RESPOND_DAYS)).date().isoformat(), "suppliers": others}


async def open_gate(deal_id: str) -> str:
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.orchestration.continuations import _create_gate, _deal_quantity
    deal = await get_deal(deal_id)
    p = await plan(deal)
    names = ", ".join(s["company_name"] or s["supplier_id"] for s in p["suppliers"])
    approval = await _create_gate(
        deal_id, ACTION,
        summary=(f"Best and final on {deal_id}: ask {names} to match or beat USD "
                 f"{p['best_price_usd_per_mt']:,.2f}/MT FOB by {p['respond_by']} (the competitor is never named)"),
        financial_impact_usd=None,
        callback_payload={"deal_id": deal_id, "hs_code": deal.hs_code, "quantity_mt": _deal_quantity(deal), **p},
        idempotency_key=f"bafo-{deal_id}-{int(round(p['best_price_usd_per_mt'] * 100))}",
        risk_flags=["supplier_price_leverage"],
        deal=deal,
    )
    return getattr(approval, "approval_id", "")


def body(company: str | None, price: float, quantity: float, product: str, respond_by: str) -> str:
    from shared import company_profile as cp
    greeting = f"Dear {company} team," if company else "Dear Sir / Madam,"
    return (
        f"{greeting}\n\n"
        f"Thank you for your quotation for {quantity:g} MT of {product}. We are about to place this order, and "
        f"the best price we hold for it is USD {price:,.2f} per MT FOB.\n\n"
        f"Can you match or beat this price for the same quantity and specification? If so, please reply by "
        f"{respond_by} with your best and final price and the shipment date. Payment is secured in escrow "
        "before production, as in our first request."
        + cp.signature()
    )


async def send(deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """APPROVE callback: one e-mail per supplier on its RFQ thread."""
    from agents.negotiation_agent.email_history import record_deal_email  # type: ignore[import]
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    from services.sourcing.service import rfq_subject  # type: ignore[import]
    from shared.product_label import product_label

    product = product_label(payload.get("hs_code") or "") or f"HS {payload.get('hs_code')}"
    sent, failed = [], []
    for s in payload.get("suppliers") or []:
        subject = "Re: " + rfq_subject(s["rfq_id"], payload.get("hs_code") or "", payload.get("quantity_mt")) \
                  + " — best and final"
        text = body(s.get("company_name"), float(payload["best_price_usd_per_mt"]), float(payload.get("quantity_mt") or 0),
                    product, payload.get("respond_by") or "")
        try:
            result = await send_email(SendEmailRequest(
                to_address=s["email"], subject=subject, body=text, negotiation_id="", round_no=0,
                email_id=f"BAFO{deal_id.replace('-', '')[-8:]}{re.sub(r'[^A-Z0-9]', '', s['supplier_id'].upper())[:12]}",
            ))
            outcome = str(getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent")))
            await record_deal_email(deal_id=deal_id, direction="outbound", party="supplier", to_address=s["email"],
                                    from_address="PortLess", subject=subject, body=text, kind="best_and_final",
                                    delivered=outcome != "logged")
            (sent if outcome != "logged" else failed).append(s["supplier_id"])
        except Exception as exc:  # noqa: BLE001 - one bad address never stops the others
            logger.warning("BestAndFinal: %s / %s not sent: %s", deal_id, s.get("supplier_id"), exc)
            failed.append(s["supplier_id"])
    logger.info("BestAndFinal: deal %s — sent to %s, not delivered %s", deal_id, sent, failed)
    return {"sent": sent, "not_delivered": failed}
