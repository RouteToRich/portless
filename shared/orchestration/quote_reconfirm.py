"""Ask a supplier to reconfirm its quote before the quote expires.

A quote is valid ``validity_days`` (the supplier's own, else ``MATCH_OFFER_VALIDITY_DAYS``)
from when it was received. While the prospect is open (DRAFT / MATCHING, not parked), the
deal watcher e-mails the supplier ``QUOTE_RECONFIRM_DAYS_BEFORE`` days (default 3) before
the end — on the original ``[RFQ:<id>]`` thread, so the reply maps back to sourcing:

- a reply with a price is a new quote (the normal RFQ path replaces the offer);
- a reply that only confirms ("we confirm the price stands") restarts the validity from now
  (``confirm_from_reply``), offer ``reconfirm_status=confirmed``;
- no reply: once the validity has run out the offer is marked ``reconfirm_status=expired``
  (the match board already leaves expired offers out).

One request per offer. ``QUOTE_RECONFIRM_ENABLED=false`` switches it off.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

PROSPECT_STATUSES = ("DRAFT", "MATCHING")
_CONFIRM = re.compile(
    r"\b(confirm(ed|s)?|still (valid|stands|good)|remains? (valid|unchanged|the same)|price stands|"
    r"same price|valid (until|till)|we (agree|accept)|no change)\b", re.IGNORECASE)
_NOT = re.compile(
    r"\b(not|no longer|cannot|can't|unable|revis(e|ed)|increase[ds]?|higher|new price|withdraw)\b", re.IGNORECASE)


def enabled() -> bool:
    return os.getenv("QUOTE_RECONFIRM_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def days_before() -> float:
    try:
        return max(0.0, float(os.getenv("QUOTE_RECONFIRM_DAYS_BEFORE", "3")))
    except ValueError:
        return 3.0


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expires_at(offer: Any) -> datetime | None:
    from shared.orchestration.matchmaking import offer_validity_days
    received = _aware(getattr(offer, "received_at", None))
    if received is None:
        return None
    return received + timedelta(days=getattr(offer, "validity_days", None) or offer_validity_days())


def reads_as_confirmation(text: str | None) -> bool:
    """A short reconfirmation with no change — never one that revises or withdraws."""
    if not text:
        return False
    from agents.negotiation_agent.guards import _new_reply_text  # quoted history is not their answer
    new = _new_reply_text(text)
    return bool(_CONFIRM.search(new)) and not _NOT.search(new)


def _status(deal: Any) -> str:
    s = getattr(deal, "status", "")
    return str(getattr(s, "value", s) or "")


async def _save_offer(deal_id: str, offer: Any) -> None:
    from services.deals.service import upsert_supplier_offer  # type: ignore[import]
    await upsert_supplier_offer(deal_id, offer)


def body(company: str | None, price: float | None, until: datetime, email: str | None = None) -> str:
    from shared import company_profile as cp
    from shared.orchestration.unsubscribe import supplier_opt_out_line
    greeting = f"Dear {company} team," if company else "Dear Sir / Madam,"
    figure = f"USD {price:,.2f} per MT FOB" if price else "your quoted price"
    return (
        f"{greeting}\n\n"
        f"Thank you again for your quotation. It is valid until {until:%d %B %Y}, and our buyer's decision is "
        "close, so we would like to keep it on the table.\n\n"
        f"Could you please confirm that {figure} still stands, and until which date? A one-line reply is enough. "
        "If anything has changed (price, quantity or shipment date), simply reply with the new figures.\n\n"
        + supplier_opt_out_line(email)
        + cp.signature()
    )


async def _ask(deal: Any, offer: Any, until: datetime) -> bool:
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    from services.sourcing.service import get_quote, rfq_subject  # type: ignore[import]
    rfq_id = None
    if getattr(offer, "quote_id", None):
        try:
            rfq_id = (await get_quote(offer.quote_id)).rfq_id
        except Exception:  # noqa: BLE001 - an offer entered by staff has no RFQ thread
            rfq_id = None
    if not rfq_id:
        return False
    from shared.orchestration.contact_guard import supplier_blocked
    if await supplier_blocked(offer.email):
        return False  # asked PortLess to stop writing
    subject = "Re: " + rfq_subject(rfq_id, deal.hs_code, getattr(deal, "quantity_mt", None)) + " — please reconfirm"
    text = body(offer.company_name, offer.fob_price_usd_per_mt, until, offer.email)
    result = await send_email(SendEmailRequest(
        to_address=offer.email, subject=subject, body=text, negotiation_id="", round_no=0,
        email_id=f"RECONF{deal.deal_id.replace('-', '')[-8:]}{re.sub(r'[^A-Z0-9]', '', offer.offer_id.upper())[:12]}",
    ))
    outcome = str(getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent")))
    from agents.negotiation_agent.email_history import record_deal_email  # type: ignore[import]
    await record_deal_email(deal_id=deal.deal_id, direction="outbound", party="supplier", to_address=offer.email,
                            from_address="PortLess", subject=subject, body=text, kind="quote_reconfirm",
                            delivered=outcome != "logged")
    return outcome != "logged"


async def sweep(now: datetime | None = None) -> dict[str, int]:
    """Ask for reconfirmation of quotes about to expire; mark unanswered ones expired."""
    counts = {"asked": 0, "expired": 0}
    if not enabled():
        return counts
    now = now or datetime.now(timezone.utc)
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("QuoteReconfirm: could not list deals: %s", exc)
        return counts
    from shared.orchestration.prospect_actions import is_parked
    window = timedelta(days=days_before())
    for deal in deals:
        if _status(deal) not in PROSPECT_STATUSES or is_parked(deal, now):
            continue
        for offer in getattr(deal, "supplier_offers", None) or []:
            end = expires_at(offer)
            if end is None or getattr(offer, "reconfirm_status", None) in ("confirmed", "expired"):
                continue
            try:
                if end <= now:
                    if getattr(offer, "reconfirm_asked_at", None):
                        await _save_offer(deal.deal_id, offer.model_copy(update={"reconfirm_status": "expired"}))
                        counts["expired"] += 1
                    continue
                if end - now <= window and not getattr(offer, "reconfirm_asked_at", None) and offer.email:
                    if await _ask(deal, offer, end):
                        await _save_offer(deal.deal_id, offer.model_copy(update={
                            "reconfirm_asked_at": now, "reconfirm_status": "asked"}))
                        counts["asked"] += 1
            except Exception as exc:  # noqa: BLE001 - one offer never stops the sweep
                logger.error("QuoteReconfirm: %s / %s failed: %s", deal.deal_id, offer.offer_id, exc)
    return counts


async def confirm_from_reply(rfq: Any, from_address: str, text: str) -> bool:
    """A supplier's reply on the RFQ thread that only reconfirms an asked quote: restart
    its validity from now. Returns True when it did."""
    if not reads_as_confirmation(text):
        return False
    from services.deals.service import get_deal  # type: ignore[import]
    from services.sourcing.service import rfq_sender_supplier  # type: ignore[import]
    supplier_id = rfq_sender_supplier(rfq, from_address)
    if supplier_id is None:
        return False
    deal = await get_deal(rfq.deal_id)
    offer = next((o for o in reversed(getattr(deal, "supplier_offers", None) or [])
                  if o.supplier_id == supplier_id and getattr(o, "reconfirm_asked_at", None)), None)
    if offer is None:
        return False
    now = datetime.now(timezone.utc)
    await _save_offer(deal.deal_id, offer.model_copy(update={
        "received_at": now, "reconfirmed_at": now, "reconfirm_status": "confirmed"}))
    logger.info("QuoteReconfirm: %s reconfirmed %.2f USD/MT on %s", offer.company_name or supplier_id,
                offer.fob_price_usd_per_mt or 0.0, deal.deal_id)
    return True
