"""One buyer or supplier, everything needed to act on it — and the directory at a glance.

``profile(role, record_id)`` — the company (cleaned, scored), every prospect / deal it
appears in and how (candidate, quote, party), a dated timeline (found, contacted,
replied, offered, quoted, became a party, traded), the open prospects of its products
it could join, its open opportunities, and the next best actions.

``add_to_prospect(role, record_id, deal_id)`` — a buyer joins the prospect's outreach
funnel as a candidate; a supplier joins its match board as an offer with the quote
pending. Refused for a failed check, the do-not-contact list, or a "buyer" that looks
like an exporter.

``summary()`` — the book at a glance: companies per stage, new this week, verified,
contactable, the hottest companies (recent replies / quotes), the products and
countries the newest companies were found for.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from . import directory, relationships
from .schemas import CounterpartyRecord

logger = logging.getLogger(__name__)

_OPEN = ("DRAFT", "MATCHING")
_DONE = ("PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED")


class ProfileError(ValueError):
    pass


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _status(deal: Any) -> str:
    return str(getattr(getattr(deal, "status", None), "value", getattr(deal, "status", "")))


def _iso(dt: Any) -> str | None:
    a = _aware(dt)
    return a.isoformat() if a else None


async def _record(role: str, record_id: str) -> CounterpartyRecord:
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None or rec.role != role:
        raise ProfileError("no such company in the directory")
    return (await relationships.annotate([directory.clean_record(rec)]))[0]


def _engagements(rec: CounterpartyRecord, deals: list[Any]) -> tuple[list[dict], list[dict]]:
    """(per-deal engagement rows, timeline events) for this company."""
    keys = relationships.record_keys(rec)
    rows: list[dict] = []
    events: list[dict] = []

    def ev(at: Any, kind: str, text: str, deal_id: str | None = None) -> None:
        if _aware(at):
            events.append({"at": _iso(at), "kind": kind, "text": text, "deal_id": deal_id})

    for d in deals:
        st = _status(d)
        base = {"deal_id": d.deal_id, "hs_code": d.hs_code, "market": d.market, "deal_status": st,
                "prospect": st in _OPEN}
        if rec.role == "buyer":
            for c in getattr(d, "buyer_candidates", None) or []:
                if not (relationships.keys_for(c.email, c.website, c.candidate_id, name=c.legal_name) & keys):
                    continue
                req = getattr(c, "requirement", None)
                io = getattr(c, "indicative_offer", None)
                rows.append({**base, "as": "candidate", "status": c.status,
                             "last_at": _iso(c.replied_at or c.followed_up_at or c.contacted_at),
                             "reply": c.reply_preview,
                             "target_price_usd_per_mt": getattr(req, "target_price_usd_per_mt", None) if req else None,
                             "offer": (f"{io.price_low_usd_per_mt:,.0f}–{io.price_high_usd_per_mt:,.0f} "
                                       f"{io.incoterm}" + (f" → {io.response}" if io.response else "")) if io else None})
                ev(c.contacted_at, "contacted", f"Introduction sent ({d.hs_code} → {d.market})", d.deal_id)
                ev(c.followed_up_at, "followed_up", "Follow-up sent", d.deal_id)
                if c.replied_at:
                    ev(c.replied_at, "replied", f"Replied: {(c.reply_preview or '')[:120]}", d.deal_id)
                if io is not None:
                    ev(io.sent_at, "offer", f"Indicative offer {io.price_low_usd_per_mt:,.0f}–{io.price_high_usd_per_mt:,.0f} {io.incoterm}", d.deal_id)
                    if io.response:
                        ev(io.responded_at, "offer_response", f"Offer {io.response}", d.deal_id)
        else:
            for o in getattr(d, "supplier_offers", None) or []:
                if not (relationships.keys_for(o.email, o.supplier_id, name=o.company_name) & keys):
                    continue
                rows.append({**base, "as": "quote", "status": "quoted" if o.fob_price_usd_per_mt else "quote pending",
                             "last_at": _iso(o.received_at),
                             "price_usd_per_mt": o.fob_price_usd_per_mt, "moq_mt": o.moq_mt,
                             "lead_time_days": o.lead_time_days})
                if o.fob_price_usd_per_mt:
                    ev(o.received_at, "quoted", f"Quoted {o.fob_price_usd_per_mt:,.0f} FOB/MT ({d.hs_code})", d.deal_id)
        for p in getattr(d, "parties", None) or []:
            role = str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower()
            if role != rec.role:
                continue
            if not (relationships.keys_for(p.email, getattr(p, "website", None), getattr(p, "party_id", None),
                                           name=p.legal_name) & keys):
                continue
            rows.append({**base, "as": "party", "status": st, "last_at": _iso(getattr(d, "updated_at", None))})
            ev(getattr(d, "updated_at", None), "traded" if st == "PAID" else "party",
               ("Traded — deal paid" if st == "PAID" else f"Party of deal at {st.replace('_', ' ').lower()}"), d.deal_id)
    ev(rec.first_seen_at, "found", f"Found by {rec.source.replace('apify_', '').replace('_', ' ')}"
       + (f" for {', '.join(rec.markets)}" if rec.markets else ""))
    if rec.do_not_contact:
        ev(rec.dnc_at, "do_not_contact", f"Do not contact: {rec.dnc_reason or ''}")
    events.sort(key=lambda e: e["at"], reverse=True)
    rows.sort(key=lambda r: r.get("last_at") or "", reverse=True)
    return rows, events


def _open_prospects(rec: CounterpartyRecord, deals: list[Any], engaged: set[str]) -> list[dict]:
    """Open prospects of the company's products (same 6- or 4-digit code) it is not in yet."""
    out = []
    for d in deals:
        if _status(d) not in _OPEN or d.deal_id in engaged:
            continue
        if not any(h[:4] == d.hs_code[:4] for h in rec.hs_codes):
            continue
        if rec.role == "buyer" and rec.markets and d.market and d.market.upper() not in {m.upper() for m in rec.markets} \
                and (rec.country or "").upper() != d.market.upper():
            continue
        out.append({"deal_id": d.deal_id, "hs_code": d.hs_code, "market": d.market,
                    "buyers": len(getattr(d, "buyer_candidates", None) or []),
                    "quotes": len(getattr(d, "supplier_offers", None) or []),
                    "created_at": _iso(getattr(d, "created_at", None))})
    return out[:10]


def blocked_reason(rec: CounterpartyRecord) -> str | None:
    if rec.do_not_contact:
        return "on the do-not-contact list"
    if rec.validation is not None and rec.validation.status == "failed":
        return "failed the counterparty check"
    if rec.role == "buyer" and "role_suspect" in (rec.flags or []):
        return "looks like an exporter, not an importer"
    return None


def next_actions(rec: CounterpartyRecord, rows: list[dict], open_prospects: list[dict]) -> list[dict]:
    """The moves that take this company further, best first."""
    acts: list[dict] = []
    blocked = blocked_reason(rec)
    if blocked:
        acts.append({"kind": "blocked", "label": f"Not contactable: {blocked}"})
    rel = rec.relationship or {}
    live = [r for r in rows if r["deal_status"] not in _DONE]
    if live:
        r = live[0]
        acts.append({"kind": "open_deal", "label": f"Open {r['deal_id']} ({r['hs_code']} → {r['market']}, "
                     f"{r['status'].replace('_', ' ')})", "deal_id": r["deal_id"]})
    if not blocked and open_prospects:
        p = open_prospects[0]
        acts.append({"kind": "add_to_prospect", "label": (f"Add to prospect {p['deal_id']} ({p['hs_code']} → {p['market']})"
                     if rec.role == "buyer" else f"Add to prospect {p['deal_id']} for a quote"), "deal_id": p["deal_id"]})
    if rec.open_opportunity_ids and not live:
        acts.append({"kind": "open_opportunity", "label": "On an open opportunity — start a prospect from the radar",
                     "opportunity_id": rec.open_opportunity_ids[0]})
    if not blocked and (rec.validation is None or rec.validation.status == "unverified"):
        acts.append({"kind": "check", "label": "Run the counterparty check (sanctions, e-mail, GST / IEC)"})
    if not rec.email and not rec.phone:
        acts.append({"kind": "find_contact", "label": "No contact yet — find one (contact finder) or add it by hand"})
    if rel.get("stage") == "replied" and rec.role == "buyer":
        acts.append({"kind": "follow_up", "label": "Replied but not yet interested — read the reply and answer it"})
    return acts


async def profile(role: str, record_id: str) -> dict[str, Any]:
    rec = await _record(role, record_id)
    rec = (await directory.with_open_opportunities([rec]))[0]
    deals = await relationships._deals()
    rows, events = _engagements(rec, deals)
    engaged = {r["deal_id"] for r in rows}
    opens = _open_prospects(rec, deals, engaged)
    return {
        "company": rec.model_dump(mode="json"),
        "engagements": rows,
        "timeline": events[:50],
        "open_prospects": opens,
        "next_actions": next_actions(rec, rows, opens),
        "blocked_reason": blocked_reason(rec),
    }


def _candidate_id(rec: CounterpartyRecord) -> str:
    rid = rec.id.split(":", 1)[1] if ":" in rec.id else rec.id
    return re.sub(r"[^a-z0-9.\-]", "", rid.lower())[:60] or "company"


async def add_to_prospect(role: str, record_id: str, deal_id: str) -> dict[str, Any]:
    """A buyer → the prospect's outreach funnel; a supplier → its match board, quote pending."""
    rec = await _record(role, record_id)
    reason = blocked_reason(rec)
    if reason:
        raise ProfileError(f"{rec.name} cannot be added: {reason}")
    from services.deals import service as deals  # type: ignore[import]
    from services.deals.schemas import BuyerCandidate, SupplierOffer  # type: ignore[import]
    deal = await deals.get_deal(deal_id)
    if _status(deal) not in _OPEN:
        raise ProfileError(f"{deal_id} is no longer a prospect ({_status(deal)})")
    cid = _candidate_id(rec)
    if role == "buyer":
        if not rec.email:
            raise ProfileError(f"{rec.name} has no e-mail — the introduction cannot be sent")
        await deals.add_buyer_candidate(deal_id, BuyerCandidate(
            candidate_id=cid, legal_name=rec.name, country=(rec.country or deal.market or ""), email=rec.email,
            website=rec.website, phone=rec.phone, source=f"directory:{rec.source}", fit_score=rec.fit_score,
            validation_status=rec.validation.status if rec.validation else None,
        ))
        return {"deal_id": deal_id, "added": "buyer_candidate", "candidate_id": cid,
                "next": "Use Contact next buyers on the prospect to send the introduction"}
    await deals.upsert_supplier_offer(deal_id, SupplierOffer(
        offer_id=f"OFR-DIR-{cid[:24]}", supplier_id=cid, company_name=rec.name, email=rec.email,
        source=f"directory:{rec.source}", fob_price_usd_per_mt=None,
    ))
    return {"deal_id": deal_id, "added": "supplier_offer", "supplier_id": cid,
            "next": "Quote pending — ask the supplier for its FOB price (RFQ) or record a phone quote"}


async def summary() -> dict[str, Any]:
    store = await directory.get_directory()
    now = datetime.now(timezone.utc)
    week = now - timedelta(days=7)
    fortnight = now - timedelta(days=14)
    out: dict[str, Any] = {}
    for role in ("buyer", "supplier"):
        rows = await relationships.annotate([directory.clean_record(r) for r in await store.list(role, limit=5000)])
        stages = Counter((r.relationship or {}).get("stage", "new") for r in rows)
        fresh = [r for r in rows if _aware(r.first_seen_at) and _aware(r.first_seen_at) >= week]
        hot = []
        for r in rows:
            rel = r.relationship or {}
            last = max(filter(None, [rel.get("last_reply_at"), rel.get("last_quote_at")]), default=None)
            if last and last >= fortnight.isoformat():
                hot.append({"id": r.id, "name": r.name, "country": r.country, "stage": rel.get("stage"),
                            "at": last, "score": r.score})
        hot.sort(key=lambda h: h["at"], reverse=True)
        top = sorted(rows, key=lambda r: r.score or 0, reverse=True)[:5]
        out[role] = {
            "total": len(rows),
            "stages": dict(stages),
            "verified": sum(1 for r in rows if r.validation is not None and r.validation.status == "verified"),
            "contactable": sum(1 for r in rows if not blocked_reason(r) and (r.email or r.phone)),
            "do_not_contact": sum(1 for r in rows if r.do_not_contact),
            "new_this_week": len(fresh),
            "new_by_product": Counter(h for r in fresh for h in r.hs_codes).most_common(5),
            "new_by_country": Counter((r.country or "?") for r in fresh).most_common(5),
            "hot": hot[:6],
            "top": [{"id": r.id, "name": r.name, "country": r.country, "score": r.score,
                     "stage": (r.relationship or {}).get("stage")} for r in top],
        }
    return out
