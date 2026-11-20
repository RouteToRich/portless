"""Firm offers — where every negotiation thread ends when deal composition is on (todo 13.2).

With ``DEAL_COMPOSITION_ENABLED`` nobody is made "the buyer" or "the supplier" while the
prospect is negotiated. Each counterparty keeps its own thread — a buyer its outreach
``[BUY:<deal>:<candidate>]`` thread, a supplier its ``[RFQ:<id>]`` thread — and each thread
ends at a **firm offer**, not an agreement:

- buyer: "Q MT of grade G at P <incoterm> port X, shipment by D, valid until V";
- supplier: "up to Q MT of grade G at P FOB port Y, ready by D, valid until V".

The e-mails say "subject to final order confirmation": there is no agreement gate, no PI and
no firm order request until the composition agent (composition.py) has chosen the deal.

Sources of a firm offer, newest wins per counterparty:

1. ``stored`` — read from the counterparty's own reply (``capture_from_reply``) or entered by
   staff; kept on ``DealRecord.firm_offers`` with its validity;
2. ``derived`` — the board already holds it: a buyer whose requirement carries a stated /
   countered / accepted price and a quantity, a supplier's written quote (``price_basis``
   quoted). Its validity is the quote's own (``validity_days``), else
   ``FIRM_OFFER_VALIDITY_DAYS``.

Validity is asked for and refreshed: an offer without one gets a short request on its thread
(``request_firm_offer``); ``FIRM_OFFER_REMIND_HOURS_BEFORE`` (24) hours before a stored offer
expires the counterparty is reminded once; after that it is ``expired`` and never enters a
composition without a fresh price. Written quotes derived from the board are refreshed by
``quote_reconfirm`` (the same rule, already on the RFQ thread).

Pricing per thread (``thread_bounds``): the buyer's band comes from the best supply mix
currently available, and a supplier's ceiling from the best buyer price currently available —
not from one fixed counterpart. The margin floor is checked on the combination (composition)
as well as on each leg.

Every e-mail here passes the N-party privacy check (``email_safety.prospect_leak_findings``):
a buyer never learns how many suppliers there are or who they are, a supplier never learns
about the buyers or the other suppliers.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_FIRM_BUYER = ("stated", "counter", "accepted_offer")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE = r"(\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?(?:,?\s+\d{4})?|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)"
_VALID_UNTIL = re.compile(r"\bvalid(?:ity)?\s*(?:is\s+)?(?:until|till|through|up to|upto|to|:)\s*" + _DATE, re.IGNORECASE)
_VALID_FOR = re.compile(r"\bvalid(?:ity)?\s*(?:is\s+)?(?:for\s+|of\s+|:\s*)?(\d{1,3}|one|two|three|four|five|seven|ten|fourteen|thirty)\s+"
                        r"(day|days|week|weeks|month|months)\b", re.IGNORECASE)
_READY_BY = re.compile(r"\b(?:ready|available|dispatch(?:ed)?|ship(?:ment|ped)?|deliver(?:y|ed)?|load(?:ing|ed)?)\s+"
                       r"(?:by|before|on|latest|not later than)\s+" + _DATE, re.IGNORECASE)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "seven": 7, "ten": 10, "fourteen": 14, "thirty": 30}


def default_validity_days() -> int:
    try:
        return max(1, int(os.getenv("FIRM_OFFER_VALIDITY_DAYS", "7")))
    except ValueError:
        return 7


def remind_hours_before() -> float:
    try:
        return max(0.0, float(os.getenv("FIRM_OFFER_REMIND_HOURS_BEFORE", "24")))
    except ValueError:
        return 24.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Reading dates and validity from a reply (no model)
# ---------------------------------------------------------------------------

def parse_date(text: str | None, now: datetime | None = None) -> datetime | None:
    """'2026-12-05', '5 December', 'Dec 5, 2026' → an aware date (the next one when no year)."""
    raw = (text or "").strip().rstrip(".,")
    if not raw:
        return None
    now = now or _now()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    m = (re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?(?:,?\s+(\d{4}))?", raw)
         or None)
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
    else:
        m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", raw)
        if not m:
            return None
        mon, day, year = m.group(1), m.group(2), m.group(3)
    month = _MONTHS.get(mon[:3].lower())
    if not month:
        return None
    try:
        out = datetime(int(year) if year else now.year, month, int(day), tzinfo=timezone.utc)
    except ValueError:
        return None
    if not year and out < now - timedelta(days=1):
        out = out.replace(year=now.year + 1)
    return out


def parse_validity(text: str | None, now: datetime | None = None) -> datetime | None:
    """'valid until 5 December' / 'validity: 10 days' / 'valid for one week' → end of validity."""
    now = now or _now()
    m = _VALID_UNTIL.search(text or "")
    if m:
        d = parse_date(m.group(1), now)
        if d is not None:
            return d.replace(hour=23, minute=59)
    m = _VALID_FOR.search(text or "")
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _WORDS.get(m.group(1).lower(), 0)
        unit = m.group(2).lower()
        days = n * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
        if days > 0:
            return now + timedelta(days=days)
    return None


def parse_date_by(text: str | None, now: datetime | None = None) -> datetime | None:
    """'ready by 15 December' / 'shipment by Jan 10' → the date."""
    m = _READY_BY.search(text or "")
    return parse_date(m.group(1), now) if m else None


# ---------------------------------------------------------------------------
# Live offers
# ---------------------------------------------------------------------------

def _status(c: Any) -> str:
    return str(getattr(c, "status", "") or "")


def _key(o: Any) -> tuple[str, str]:
    return (o.side, o.counterparty_id)


def derived_offers(deal: Any, supplier_offers: list[Any] | None = None, now: datetime | None = None) -> list[Any]:
    """Firm offers the board already holds (see module doc, source 2)."""
    from services.deals.schemas import FirmOffer  # type: ignore[import]
    now = now or _now()
    out: list[Any] = []
    for c in getattr(deal, "buyer_candidates", None) or []:
        req = getattr(c, "requirement", None)
        if _status(c) not in ("interested", "selected") or req is None:
            continue
        if not req.target_price_usd_per_mt or req.target_price_basis not in _FIRM_BUYER or not req.quantity_mt:
            continue
        received = _aware(req.updated_at) or _aware(getattr(c, "replied_at", None)) or now
        by = (received + timedelta(days=req.delivery_by_days)) if req.delivery_by_days else None
        out.append(FirmOffer(
            offer_id=f"FO-B-{c.candidate_id}", side="buyer", counterparty_id=c.candidate_id,
            company_name=c.legal_name, email=getattr(c, "reply_address", None) or c.email, grade=req.grade,
            quantity_mt=req.quantity_mt, price_usd_per_mt=req.target_price_usd_per_mt,
            incoterm=req.target_price_incoterm or req.incoterm or "CIF", port=req.destination_port,
            date_by=by, valid_until=received + timedelta(days=default_validity_days()),
            verified=(getattr(c, "validation_status", None) == "verified") if getattr(c, "validation_status", None) else None,
            source="derived", origin_deal_id=deal.deal_id, received_at=received, spec=dict(req.spec or {}),
        ))
    from shared.orchestration.matchmaking import offer_validity_days
    for o in supplier_offers if supplier_offers is not None else (getattr(deal, "supplier_offers", None) or []):
        if getattr(o, "price_basis", "quoted") != "quoted" or not o.fob_price_usd_per_mt:
            continue
        if getattr(o, "verification_failed", False) or getattr(o, "reconfirm_status", None) == "expired":
            continue
        received = _aware(o.received_at) or now
        valid = received + timedelta(days=o.validity_days or offer_validity_days())
        if getattr(o, "reconfirmed_at", None):
            valid = _aware(o.reconfirmed_at) + timedelta(days=o.validity_days or offer_validity_days())
        ready = received + timedelta(days=o.lead_time_days) if o.lead_time_days else None
        verified = None
        if o.gst_verified is not None or o.iec_active is not None:
            verified = bool(o.gst_verified) and bool(o.iec_active)
        out.append(FirmOffer(
            offer_id=f"FO-S-{o.supplier_id}", side="supplier", counterparty_id=o.supplier_id,
            company_name=o.company_name, email=o.email, grade=o.grade,
            quantity_mt=o.capacity_mt_per_month, min_quantity_mt=o.moq_mt, price_usd_per_mt=o.fob_price_usd_per_mt,
            incoterm=o.incoterm or "FOB", port=o.port_of_loading, port_km=o.port_km, date_by=ready,
            valid_until=valid, verified=verified, source="derived",
            origin_deal_id=getattr(o, "origin_deal_id", None) or deal.deal_id, received_at=received,
            spec=dict(o.spec or {}),
        ))
    return out


def is_live(offer: Any, now: datetime | None = None) -> bool:
    now = now or _now()
    if getattr(offer, "status", "live") != "live" or not getattr(offer, "price_usd_per_mt", None):
        return False
    end = _aware(getattr(offer, "valid_until", None))
    return end is None or end > now


async def live_offers(deal: Any, now: datetime | None = None, *, cross_prospect: bool = True) -> list[Any]:
    """Every live firm offer of the prospect, one per counterparty (newest wins), plus still-valid
    supplier offers of other live prospects of the same HS code (matchmaking.offers_for)."""
    now = now or _now()
    supplier_pool = None
    if cross_prospect:
        try:
            from shared.orchestration import matchmaking
            supplier_pool = await matchmaking.offers_for(deal)
        except Exception as exc:  # noqa: BLE001
            logger.debug("FirmOffers: cross-prospect offers unavailable for %s: %s", deal.deal_id, exc)
    best: dict[tuple[str, str], Any] = {}
    stored_dead: set[tuple[str, str]] = set()
    for o in getattr(deal, "firm_offers", None) or []:
        if o.status in ("withdrawn", "used", "expired") or not is_live(o, now):
            # an expired / withdrawn stored offer hides the derived one of the same counterparty
            # until it sends a fresh price
            if o.status != "used":
                stored_dead.add(_key(o))
            continue
        cur = best.get(_key(o))
        if cur is None or (_aware(cur.received_at) or now) <= (_aware(o.received_at) or now):
            best[_key(o)] = o
    for o in derived_offers(deal, supplier_pool, now):
        k = _key(o)
        if k in stored_dead and not _newer_than_dead(deal, o):
            continue
        if not is_live(o, now):
            continue
        cur = best.get(k)
        if cur is None or (cur.source == "derived" and (_aware(cur.received_at) or now) < (_aware(o.received_at) or now)):
            best[k] = o
    return list(best.values())


def _newer_than_dead(deal: Any, derived: Any) -> bool:
    """A derived offer still counts after its stored one expired when it is newer (a fresh price)."""
    dead = [o for o in getattr(deal, "firm_offers", None) or []
            if _key(o) == _key(derived) and o.status in ("expired", "withdrawn")]
    newest = max((_aware(o.expired_at) or _aware(o.received_at) for o in dead if (o.expired_at or o.received_at)), default=None)
    return newest is None or (_aware(derived.received_at) or _now()) > newest


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

async def _save_offers(deal_id: str, mutate) -> Any:
    from services.deals.repository import get_repository  # type: ignore[import]
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise ValueError(f"deal {deal_id} not found")
    offers = mutate(list(deal.firm_offers))
    updated = deal.model_copy(update={"firm_offers": offers, "updated_at": _now()})
    await repo.save(updated)
    return updated


async def record(deal_id: str, offer: Any, *, rerun: bool = True) -> Any:
    """Add or replace (same side + counterparty) a stored firm offer; re-run the composition."""
    from services.deals.schemas import FirmOffer  # type: ignore[import]
    offer = offer if isinstance(offer, FirmOffer) else FirmOffer(**offer)
    if offer.received_at is None:
        offer = offer.model_copy(update={"received_at": _now()})
    if offer.valid_until is None:
        offer = offer.model_copy(update={"valid_until": offer.received_at + timedelta(days=default_validity_days())})
    deal = await _save_offers(deal_id, lambda offers: [o for o in offers if _key(o) != _key(offer)] + [offer])
    logger.info("FirmOffers: %s %s on %s — %s MT at %s, valid until %s", offer.side, offer.counterparty_id,
                deal_id, offer.quantity_mt, offer.price_usd_per_mt, offer.valid_until)
    if rerun:
        await _rerun(deal_id, f"firm offer from {offer.side} {offer.counterparty_id}")
    return deal


async def withdraw(deal_id: str, side: str, counterparty_id: str, reason: str) -> Any:
    def _w(offers):
        return [o.model_copy(update={"status": "withdrawn", "expired_at": _now()})
                if (o.side, o.counterparty_id) == (side, counterparty_id) and o.status == "live" else o for o in offers]
    deal = await _save_offers(deal_id, _w)
    await _rerun(deal_id, f"{side} {counterparty_id} withdrew: {reason}")
    return deal


async def mark_used(deal_id: str, keys: set[tuple[str, str]]) -> None:
    """Offers taken into an approved composition are ``used`` (never offered twice)."""
    def _u(offers):
        return [o.model_copy(update={"status": "used"}) if _key(o) in keys and o.status == "live" else o for o in offers]
    await _save_offers(deal_id, _u)


async def _rerun(deal_id: str, why: str) -> None:
    try:
        from shared.orchestration import composition
        if composition.enabled():
            await composition.run(deal_id, reason=why)
    except Exception as exc:  # noqa: BLE001 — recording an offer never fails on the re-run
        logger.warning("FirmOffers: composition re-run for %s failed: %s", deal_id, exc)


async def capture_from_reply(deal: Any, side: str, counterparty_id: str, body: str, *,
                             base: dict[str, Any] | None = None, now: datetime | None = None) -> Any | None:
    """A counterparty's reply on its own thread with a price → its stored firm offer (validity,
    date and spec read without a model; ``base`` carries what the board already knows)."""
    from shared.orchestration.outreach import composition_enabled
    if not composition_enabled():
        return None
    now = now or _now()
    base = dict(base or {})
    if not base.get("price_usd_per_mt"):
        return None
    valid = parse_validity(body, now)
    date_by = parse_date_by(body, now) or base.get("date_by")
    from services.deals.schemas import FirmOffer  # type: ignore[import]
    offer = FirmOffer(
        offer_id=f"FO-{uuid4().hex[:10].upper()}", side=side, counterparty_id=counterparty_id,
        source="reply", received_at=now, valid_until=valid, date_by=date_by,
        origin_deal_id=deal.deal_id, **{k: v for k, v in base.items() if k in FirmOffer.model_fields
                                        and k not in ("offer_id", "side", "counterparty_id", "source",
                                                      "received_at", "valid_until", "date_by", "origin_deal_id")},
    )
    await record(deal.deal_id, offer)
    if valid is None:
        await request_firm_offer(deal, side, counterparty_id, reason="validity")
    return offer


# ---------------------------------------------------------------------------
# Asking for a firm offer / its validity on the counterparty's own thread
# ---------------------------------------------------------------------------

def firm_offer_body(side: str, company: str | None, *, product: str, quantity_mt: float | None,
                    reason: str, email: str | None = None) -> str:
    from shared import company_profile as cp
    greeting = f"Dear {company} team," if company else "Dear Sir / Madam,"
    qty = f"{quantity_mt:g} MT" if quantity_mt else "the quantity you need"
    if side == "buyer":
        ask = (f"To prepare a firm order for {product}, could you please confirm your firm offer: {qty}, "
               "the grade and packing, your price per MT and incoterm, the destination port, the latest "
               "shipment date, and until which date the offer is valid?")
        tail = ""
    else:
        ask = (f"To prepare a firm order for {product}, could you please confirm your firm offer: the most "
               "you can supply (MT) and your minimum order, the grade, your FOB price per MT and port of "
               "loading, the date the goods are ready, and until which date the offer is valid?")
        from shared.orchestration.unsubscribe import supplier_opt_out_line
        tail = supplier_opt_out_line(email)
    if reason == "validity":
        ask = "Thank you for your offer. Could you please tell us until which date it is valid? A one-line reply is enough."
    return (f"{greeting}\n\n{ask}\n\nThis is subject to final order confirmation; nothing is binding until "
            "the written order.\n\n" + tail + cp.signature())


async def _thread_subject(deal: Any, side: str, counterparty_id: str) -> str | None:
    if side == "buyer":
        from shared.orchestration.outreach import _product_label, subject_token
        return f"Re: {subject_token(deal.deal_id, counterparty_id)} {_product_label(deal)} — firm offer"
    offer = next((o for o in getattr(deal, "supplier_offers", None) or [] if o.supplier_id == counterparty_id), None)
    if offer is None or not getattr(offer, "quote_id", None):
        return None
    try:
        from services.sourcing.service import get_quote, rfq_subject  # type: ignore[import]
        rfq_id = (await get_quote(offer.quote_id)).rfq_id
        return "Re: " + rfq_subject(rfq_id, deal.hs_code, getattr(deal, "quantity_mt", None)) + " — firm offer"
    except Exception:  # noqa: BLE001 — an offer entered by staff has no RFQ thread
        return None


def _requested(deal: Any) -> dict[str, Any]:
    return dict(((getattr(deal, "composition", None) or {}).get("firm_offer_requests")) or {})


async def request_firm_offer(deal: Any, side: str, counterparty_id: str, *, reason: str = "firm_offer") -> bool:
    """One short e-mail on the counterparty's own thread asking for its firm offer (or only its
    validity). Once per counterparty and reason; refused when it would leak another party."""
    key = f"{side}:{counterparty_id}:{reason}"
    if key in _requested(deal):
        return False
    if side == "buyer":
        cand = next((c for c in deal.buyer_candidates if c.candidate_id == counterparty_id), None)
        to = (getattr(cand, "reply_address", None) or getattr(cand, "email", None)) if cand else None
        company = getattr(cand, "legal_name", None)
        qty = getattr(getattr(cand, "requirement", None), "quantity_mt", None) if cand else None
    else:
        offer = next((o for o in deal.supplier_offers if o.supplier_id == counterparty_id), None)
        to, company, qty = (offer.email, offer.company_name, offer.capacity_mt_per_month) if offer else (None, None, None)
        from shared.orchestration.contact_guard import supplier_blocked
        if to and await supplier_blocked(to):
            return False
    subject = await _thread_subject(deal, side, counterparty_id)
    if not to or not subject:
        return False
    from shared.orchestration.outreach import _product_label
    body = firm_offer_body(side, company, product=_product_label(deal), quantity_mt=qty, reason=reason, email=to)
    from agents.negotiation_agent.email_safety import prospect_leak_findings  # type: ignore[import]
    leaks = prospect_leak_findings(body, to, side, deal)
    if leaks:
        logger.warning("FirmOffers: request to %s on %s not sent — %s", to, deal.deal_id, leaks)
        return False
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    result = await send_email(SendEmailRequest(
        to_address=to, subject=subject, body=body, negotiation_id="", round_no=0,
        email_id=f"FIRM{deal.deal_id.replace('-', '')[-8:]}{re.sub(r'[^A-Z0-9]', '', counterparty_id.upper())[:10]}{reason[:1].upper()}",
    ))
    outcome = str(getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent")))
    from agents.negotiation_agent.email_history import record_deal_email  # type: ignore[import]
    await record_deal_email(deal_id=deal.deal_id, direction="outbound", party=side, to_address=to, from_address="PortLess",
                            subject=subject, body=body, kind="firm_offer_request", delivered=outcome != "logged")
    from shared.orchestration import composition
    await composition.note(deal.deal_id, {"firm_offer_requests": {**_requested(deal), key: _now().isoformat()}})
    return outcome != "logged"


# ---------------------------------------------------------------------------
# Validity sweep (deal watcher)
# ---------------------------------------------------------------------------

def reminder_body(company: str | None, until: datetime, side: str, email: str | None = None) -> str:
    from shared import company_profile as cp
    greeting = f"Dear {company} team," if company else "Dear Sir / Madam,"
    tail = ""
    if side == "supplier":
        from shared.orchestration.unsubscribe import supplier_opt_out_line
        tail = supplier_opt_out_line(email)
    return (f"{greeting}\n\nYour offer is valid until {until:%d %B %Y}. Our decision is close — could you "
            "please confirm it still stands, and until which date? If anything changed (price, quantity or "
            "date), simply reply with the new figures.\n\nThis remains subject to final order confirmation.\n\n"
            + tail + cp.signature())


async def _remind(deal: Any, offer: Any) -> bool:
    subject = await _thread_subject(deal, offer.side, offer.counterparty_id)
    if not subject or not offer.email:
        return False
    body = reminder_body(offer.company_name, _aware(offer.valid_until), offer.side, offer.email)
    from agents.negotiation_agent.email_safety import prospect_leak_findings  # type: ignore[import]
    if prospect_leak_findings(body, offer.email, offer.side, deal):
        return False
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    result = await send_email(SendEmailRequest(
        to_address=offer.email, subject=subject.replace("firm offer", "please reconfirm"), body=body,
        negotiation_id="", round_no=0,
        email_id=f"FIRMR{deal.deal_id.replace('-', '')[-8:]}{re.sub(r'[^A-Z0-9]', '', offer.offer_id.upper())[-10:]}",
    ))
    outcome = str(getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent")))
    from agents.negotiation_agent.email_history import record_deal_email  # type: ignore[import]
    await record_deal_email(deal_id=deal.deal_id, direction="outbound", party=offer.side, to_address=offer.email,
                            from_address="PortLess", subject=subject, body=body, kind="firm_offer_reminder",
                            delivered=outcome != "logged")
    return True


async def sweep(now: datetime | None = None) -> dict[str, int]:
    """Stored firm offers: one reminder before they expire, then ``expired`` (re-runs the composition)."""
    counts = {"reminded": 0, "expired": 0}
    from shared.orchestration.outreach import composition_enabled
    if not composition_enabled():
        return counts
    now = now or _now()
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("FirmOffers: could not list deals: %s", exc)
        return counts
    window = timedelta(hours=remind_hours_before())
    for deal in deals:
        if str(getattr(deal.status, "value", deal.status)) not in ("DRAFT", "MATCHING"):
            continue
        expired: list[str] = []
        reminded: list[str] = []
        for o in getattr(deal, "firm_offers", None) or []:
            end = _aware(o.valid_until)
            if o.status != "live" or end is None:
                continue
            try:
                if end <= now:
                    expired.append(o.offer_id)
                elif end - now <= window and o.reminder_sent_at is None and await _remind(deal, o):
                    reminded.append(o.offer_id)
            except Exception as exc:  # noqa: BLE001 — one offer never stops the sweep
                logger.error("FirmOffers: %s / %s failed: %s", deal.deal_id, o.offer_id, exc)
        if not expired and not reminded:
            continue

        def _apply(offers, expired=expired, reminded=reminded):
            out = []
            for o in offers:
                if o.offer_id in expired:
                    o = o.model_copy(update={"status": "expired", "expired_at": now})
                elif o.offer_id in reminded:
                    o = o.model_copy(update={"reminder_sent_at": now})
                out.append(o)
            return out
        await _save_offers(deal.deal_id, _apply)
        counts["expired"] += len(expired)
        counts["reminded"] += len(reminded)
        if expired:
            await _rerun(deal.deal_id, f"{len(expired)} firm offer(s) expired")
    return counts


# ---------------------------------------------------------------------------
# Pricing per thread
# ---------------------------------------------------------------------------

def supply_mix_fob(supplier_offers: list[Any], quantity_mt: float) -> float | None:
    """Weighted FOB of the cheapest live supplier offers that together cover ``quantity_mt``
    (each up to its stated quantity; a supplier without one is assumed to cover the rest)."""
    rows = sorted((o for o in supplier_offers if o.price_usd_per_mt), key=lambda o: o.price_usd_per_mt)
    if not rows or quantity_mt <= 0:
        return None
    left, cost = quantity_mt, 0.0
    for o in rows:
        take = min(left, o.quantity_mt or left)
        cost += take * float(o.price_usd_per_mt)
        left -= take
        if left <= 1e-9:
            break
    if left > 1e-9:
        return None
    return round(cost / quantity_mt, 2)


async def thread_bounds(deal: Any, side: str, counterparty_id: str | None = None, *,
                        offers: list[Any] | None = None) -> dict[str, Any]:
    """Per-thread price bounds (todo 13.2):

    - ``buyer`` → ``min_offer_usd_per_mt``: the lowest price this buyer can be offered — the
      best supply mix for its quantity, through the landed-cost bridge at the margin floor;
    - ``supplier`` → ``max_fob_usd_per_mt``: the highest FOB this supplier can be paid — the
      best live buyer price back through the bridge at the margin floor.
    """
    from shared.orchestration import landed_cost as lc
    offers = offers if offers is not None else await live_offers(deal)
    floor = getattr(deal, "margin_floor_pct", None) or lc.default_margin_floor_pct()
    market = getattr(deal, "market", None)
    buyers = [o for o in offers if o.side == "buyer"]
    sellers = [o for o in offers if o.side == "supplier"]
    if side == "buyer":
        me = next((o for o in buyers if o.counterparty_id == counterparty_id), None)
        qty = (me.quantity_mt if me else None) or getattr(deal, "quantity_mt", None) or 25.0
        fob = supply_mix_fob(sellers, qty)
        if fob is None:
            return {"side": side, "min_offer_usd_per_mt": None, "basis": "no supply mix covers the quantity"}
        inc = (me.incoterm if me else None) or "CIF"
        landed = lc.landed(fob, market, qty, incoterm=inc, margin_floor_pct=floor)
        return {"side": side, "quantity_mt": qty, "supply_mix_fob_usd_per_mt": fob, "incoterm": inc,
                "min_offer_usd_per_mt": landed.min_offer_usd_per_mt,
                "basis": f"best supply mix of {len(sellers)} live supplier offer(s)"}
    best_buyer = None
    for b in buyers:
        cif = lc.to_cif(float(b.price_usd_per_mt), b.incoterm, market)
        if best_buyer is None or cif > best_buyer[0]:
            best_buyer = (cif, b)
    if best_buyer is None:
        return {"side": side, "max_fob_usd_per_mt": None, "basis": "no live buyer price"}
    cif, b = best_buyer
    qty = b.quantity_mt or 25.0
    # invert the bridge: the FOB whose minimum CIF offer equals the buyer's CIF price
    zero = lc.landed(0.0, market, qty, incoterm="CIF", margin_floor_pct=floor)
    one = lc.landed(1000.0, market, qty, incoterm="CIF", margin_floor_pct=floor)
    slope = (one.min_offer_usd_per_mt - zero.min_offer_usd_per_mt) / 1000.0
    max_fob = round((cif - zero.min_offer_usd_per_mt) / slope, 2) if slope > 0 else None
    return {"side": side, "best_buyer_cif_usd_per_mt": round(cif, 2), "quantity_mt": qty,
            "max_fob_usd_per_mt": max_fob, "basis": f"best of {len(buyers)} live buyer price(s)"}
