"""Freight procurement agent — forwarders quote by e-mail, not API (todo 23.2).

Most forwarders and NVOCCs answer an e-mail, not an API call. For each deal after the PO:

1. **Ask** (``request_quotes``; the deal watcher's ``sweep`` once a deal is PO_ISSUED or later, or staff):
   3–5 forwarders on the lane (Mongo ``freight_forwarders``: name, e-mail, destination countries /
   regions served, ``PUT /deals/freight/forwarders``) get one e-mail each on a ``[FRT:<deal>]`` thread
   with the container plan, port of loading, destination port and the goods-ready date.
2. **Read** (``handle_reply``): a reply from a forwarder that was asked → rate per box (20' / 40' / 40' HC
   or a lump sum), free days, validity, transit days, sailing date (``parse_quote``, no model). Each
   quote is kept on the deal (``freight_rfq``) and saved as a lane rate (``freight_lanes``, provider
   ``email:<forwarder>``), so the radar's freight improves with every quote.
3. **Counter** (``counter``): the best quote is countered once toward the lane index / the second-best
   quote, never more than ``FREIGHT_COUNTER_MAX_PCT`` (8) under it — the price is decided by code.
4. **Propose** (``best``): the cheapest quote whose transit fits ``FREIGHT_MAX_TRANSIT_DAYS`` and whose
   validity covers the ready date is added to the ``BOOK_FREIGHT`` gate's comparison
   (``email_quote_best``; risk flag ``email_quote_cheaper`` when it beats every API quote — staff
   book that forwarder).

``FREIGHT_PROCUREMENT_ENABLED`` pins the sweep off in tests.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

TOKEN = re.compile(r"\[FRT:(DEAL-[A-Z0-9]{6,20})\]", re.IGNORECASE)
_forwarders: dict[str, dict[str, Any]] = {}
_STARTED = ("PO_ISSUED", "SUPPLIER_DISPATCH_READY", "QC_PASSED")


def enabled() -> bool:
    return os.getenv("FREIGHT_PROCUREMENT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("freight_forwarders")
    except Exception:  # noqa: BLE001
        return None


def parse_token(subject: str | None) -> str | None:
    m = TOKEN.search(subject or "")
    return m.group(1).upper() if m else None


async def put_forwarder(fields: dict[str, Any], *, by: str) -> dict[str, Any]:
    name = (fields.get("name") or "").strip()
    email = (fields.get("email") or "").strip().lower()
    if not name or "@" not in email:
        raise ValueError("a name and a real e-mail address are required")
    lanes = [str(x).strip().upper() for x in (fields.get("lanes") or []) if str(x).strip()]
    if not lanes:
        raise ValueError("list the destination countries (ISO-2) or ALL the forwarder serves")
    fid = fields.get("forwarder_id") or f"FWD-{uuid.uuid4().hex[:8].upper()}"
    row = {"forwarder_id": fid, "name": name, "email": email, "lanes": lanes, "phone": fields.get("phone"),
           "updated_by": by, "updated_at": datetime.now(timezone.utc).isoformat()}
    _forwarders[fid] = row
    col = _collection()
    if col is not None:
        try:
            await col.replace_one({"forwarder_id": fid}, dict(row), upsert=True)
        except Exception:  # noqa: BLE001
            pass
    return row


async def forwarders(market: str | None = None) -> list[dict[str, Any]]:
    col = _collection()
    if col is not None:
        try:
            for d in [d async for d in col.find({}, {"_id": 0})]:
                _forwarders[d["forwarder_id"]] = d
        except Exception:  # noqa: BLE001
            pass
    rows = sorted(_forwarders.values(), key=lambda r: r["name"])
    if market:
        rows = [r for r in rows if "ALL" in r["lanes"] or market.upper() in r["lanes"]]
    return rows


_BOX = re.compile(r"(20\s?['’]?\s?(?:ft|gp|dv)?|40\s?['’]?\s?(?:ft|gp|dv)?\s?(?:hc|hq|high\s?cube)?)\D{0,20}?(?:usd|us\$|\$)\s?([\d,]+(?:\.\d+)?)", re.I)
_BOX_AFTER = re.compile(r"(?:usd|us\$|\$)\s?([\d,]+(?:\.\d+)?)\s*(?:/|per)\s*(20\s?['’]?\s?(?:ft|gp)?|40\s?['’]?\s?(?:ft|gp)?\s?(?:hc|hq)?)", re.I)
_LUMP = re.compile(r"\b(?:all\s?in|total|lump\s?sum)\D{0,15}(?:usd|us\$|\$)\s?([\d,]+(?:\.\d+)?)", re.I)
_FREE = re.compile(r"(\d{1,2})\s*(?:days?)?\s*free\s*(?:days|time)?|free\s*(?:days|time)\D{0,10}(\d{1,2})", re.I)
_TRANSIT = re.compile(r"transit\D{0,15}(\d{1,2})\s*days?|(\d{1,2})\s*days?\s*transit", re.I)
_VALID = re.compile(r"valid\w*\s*(?:till|until|to|up\s?to)?\s*[:\-]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{4})", re.I)
_SAIL = re.compile(r"(?:sailing|etd|vessel\s+sails?)\D{0,10}(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{4})", re.I)


def _iso(raw: str | None) -> str | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _box_code(raw: str) -> str:
    t = raw.lower().replace(" ", "")
    if t.startswith("20"):
        return "20GP"
    return "40HC" if any(x in t for x in ("hc", "hq", "highcube")) else "40GP"


def parse_quote(text: str | None) -> dict[str, Any] | None:
    """Pure: rates per box (or a lump sum), free days, transit, validity, sailing — None without a rate."""
    t = text or ""
    rates: dict[str, float] = {}
    for box, amount in _BOX.findall(t):
        rates.setdefault(_box_code(box), float(amount.replace(",", "")))
    for amount, box in _BOX_AFTER.findall(t):
        rates.setdefault(_box_code(box), float(amount.replace(",", "")))
    lump = _LUMP.search(t)
    if not rates and not lump:
        return None
    free = _FREE.search(t)
    transit = _TRANSIT.search(t)
    return {"rates": rates, "lump_sum_usd": float(lump.group(1).replace(",", "")) if lump else None,
            "free_days": int(free.group(1) or free.group(2)) if free else None,
            "transit_days": int(transit.group(1) or transit.group(2)) if transit else None,
            "valid_until": _iso(_VALID.search(t).group(1)) if _VALID.search(t) else None,
            "sailing": _iso(_SAIL.search(t).group(1)) if _SAIL.search(t) else None}


def total_usd(quote: dict[str, Any], boxes: dict[str, int]) -> float | None:
    """Pure: the quote's price for the planned boxes (lump sum, else rate × boxes; 40GP rate for a 40HC
    the forwarder did not price separately)."""
    if quote.get("lump_sum_usd"):
        return quote["lump_sum_usd"]
    rates = quote.get("rates") or {}
    total = 0.0
    for code, n in (boxes or {"40HC": 1}).items():
        rate = rates.get(code) or (rates.get("40GP") if code == "40HC" else None) or (rates.get("40HC") if code == "40GP" else None)
        if rate is None:
            return None
        total += rate * n
    return round(total, 2)


async def _deal(deal_id: str) -> Any:
    from services.deals.repository import get_repository  # type: ignore[import]
    deal = await get_repository().get(deal_id)
    if deal is None:
        raise LookupError(f"deal {deal_id} not found")
    return deal


async def _save(deal_id: str, rfq: dict[str, Any]) -> None:
    from services.deals.repository import get_repository  # type: ignore[import]
    await get_repository().set_fields(deal_id, {"freight_rfq": rfq})


def _plan(deal: Any) -> dict[str, int]:
    sheet = getattr(deal, "cost_sheet", None) or {}
    choice = sheet.get("container_choice") if isinstance(sheet, dict) else None
    boxes = (choice or {}).get("boxes") if isinstance(choice, dict) else None
    if boxes:
        return dict(boxes)
    qty = float(getattr(deal, "quantity_mt", 0) or 0)
    return {"40HC": max(1, -(-int(qty) // 26))} if qty > 20 else {"20GP": 1}


async def request_quotes(deal_id: str, *, by: str, ready_date: str | None = None, pol: str | None = None,
                         pod: str | None = None) -> dict[str, Any]:
    deal = await _deal(deal_id)
    rfq = dict(getattr(deal, "freight_rfq", None) or {})
    if rfq.get("requests"):
        return rfq
    fwd = (await forwarders(getattr(deal, "market", None)))[: int(os.getenv("FREIGHT_RFQ_MAX_FORWARDERS", "5") or 5)]
    if not fwd:
        raise LookupError(f"no forwarder on the list serves {getattr(deal, 'market', None)} — add one (PUT /deals/freight/forwarders)")
    boxes = _plan(deal)
    from shared import company_profile
    from shared.product_label import product_label
    product = product_label(getattr(deal, "hs_code", "") or "") or f"HS {getattr(deal, 'hs_code', '')}"
    lines = [f"Please quote ocean freight for {', '.join(f'{n} x {c}' for c, n in boxes.items())} of {product} "
             f"({getattr(deal, 'quantity_mt', '')} MT), {pol or 'Nhava Sheva / Mundra'} to {pod or getattr(deal, 'market', '')}.",
             f"Cargo ready: {ready_date or 'to be confirmed'}.",
             "Please state: rate per container (all-in, USD), free days at destination, transit time, the next "
             "sailings, and how long the rate is valid.", "", company_profile.signature()]
    body = "\n".join(lines)
    subject = f"Freight quote request — {deal_id} [FRT:{deal_id}]"
    requests = []
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    for f in fwd:
        delivered = False
        try:
            res = await send_email(SendEmailRequest(to_address=f["email"], subject=subject, body=body,
                                                    email_id=f"FRT{deal_id.replace('-', '')[-8:]}{f['forwarder_id'][-6:]}"))
            delivered = str(getattr(getattr(res, "outcome", None), "value", "")) not in ("logged", "failed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("FreightProcurement: request to %s not sent: %s", f["email"], exc)
        requests.append({"forwarder_id": f["forwarder_id"], "name": f["name"], "email": f["email"],
                         "sent_at": datetime.now(timezone.utc).isoformat(), "delivered": delivered})
    rfq.update(requests=requests, boxes=boxes, ready_date=ready_date, quotes=rfq.get("quotes") or [], requested_by=by)
    await _save(deal_id, rfq)
    return rfq


async def handle_reply(deal_id: str, from_address: str, body: str | None) -> dict[str, Any]:
    deal = await _deal(deal_id)
    rfq = dict(getattr(deal, "freight_rfq", None) or {})
    sender = (from_address or "").strip().lower()
    asked = next((r for r in rfq.get("requests") or [] if r["email"].split("@")[-1] == sender.split("@")[-1]), None)
    if asked is None:
        return {"handled": False, "reason": "sender was not asked for a freight quote on this deal"}
    q = parse_quote(body)
    if q is None:
        return {"handled": False, "reason": "no rate in the reply"}
    q.update(forwarder_id=asked["forwarder_id"], forwarder=asked["name"], email=asked["email"],
             received_at=datetime.now(timezone.utc).isoformat())
    q["total_usd"] = total_usd(q, rfq.get("boxes") or {})
    rfq["quotes"] = [x for x in rfq.get("quotes") or [] if x["forwarder_id"] != asked["forwarder_id"]] + [q]
    await _save(deal_id, rfq)
    await _lane_rate(deal, q)
    return {"handled": True, "quote": q}


async def _lane_rate(deal: Any, q: dict[str, Any]) -> None:
    """Every e-mailed quote also improves the radar's lane rate (freight_lanes)."""
    try:
        from services.trade_min import freight_lanes  # type: ignore[import]
        rate = (q.get("rates") or {}).get("20GP")
        if rate:
            await freight_lanes.save(freight_lanes.LaneRate(market=str(getattr(deal, "market", "") or ""), container_type="dry",
                                                            usd_per_container=rate, provider=f"email:{q['forwarder']}",
                                                            quoted_at=datetime.now(timezone.utc), note=f"deal {deal.deal_id}"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("FreightProcurement: lane rate not saved: %s", exc)


def best(rfq: dict[str, Any], *, ready: date | None = None) -> dict[str, Any] | None:
    """Pure: the cheapest priced quote whose transit fits and whose validity covers the ready date."""
    try:
        max_transit = int(os.getenv("FREIGHT_MAX_TRANSIT_DAYS", "") or 0) or None
    except ValueError:
        max_transit = None
    ok = []
    for q in rfq.get("quotes") or []:
        if not q.get("total_usd"):
            continue
        if max_transit and q.get("transit_days") and q["transit_days"] > max_transit:
            continue
        if ready and q.get("valid_until") and date.fromisoformat(q["valid_until"]) < ready:
            continue
        ok.append(q)
    return min(ok, key=lambda q: q["total_usd"]) if ok else None


def counter_target(best_usd: float, second_usd: float | None, lane_usd: float | None) -> float | None:
    """Pure: where to counter the best quote — toward the lane index / second best, within the band."""
    floor = best_usd * (1 - float(os.getenv("FREIGHT_COUNTER_MAX_PCT", "8") or 8) / 100.0)
    refs = [x for x in (second_usd, lane_usd) if x and x < best_usd]
    target = max(floor, min(refs)) if refs else max(floor, best_usd * 0.97)
    return round(target, 2) if target < best_usd else None


async def counter(deal_id: str, *, by: str) -> dict[str, Any]:
    """Counter the best quote once (never twice to the same forwarder)."""
    deal = await _deal(deal_id)
    rfq = dict(getattr(deal, "freight_rfq", None) or {})
    top = best(rfq)
    if top is None:
        raise LookupError("no priced freight quote to counter yet")
    if any(c["forwarder_id"] == top["forwarder_id"] for c in rfq.get("counters") or []):
        raise ValueError(f"{top['forwarder']} was already countered")
    others = sorted(q["total_usd"] for q in rfq.get("quotes") or [] if q.get("total_usd") and q is not top
                    and q["forwarder_id"] != top["forwarder_id"])
    lane = None
    try:
        from services.trade_min import freight_lanes  # type: ignore[import]
        lr = await freight_lanes.lane(str(getattr(deal, "market", "") or ""), "dry")
        if lr is not None:
            lane = lr.usd_per_container * sum((rfq.get("boxes") or {"20GP": 1}).values())
    except Exception:  # noqa: BLE001
        lane = None
    target = counter_target(top["total_usd"], others[0] if others else None, lane)
    if target is None:
        raise ValueError("the best quote is already at the lane index — nothing to counter")
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    from shared import company_profile
    await send_email(SendEmailRequest(
        to_address=top["email"], subject=f"Re: Freight quote request — {deal_id} [FRT:{deal_id}]",
        body=(f"Thank you for your quote of USD {top['total_usd']:,.0f}. We have a lower offer on this lane — can you do "
              f"USD {target:,.0f} all-in for the same boxes and sailing? We can confirm the booking quickly.\n\n"
              + company_profile.signature()), email_id=f"FRTC{deal_id.replace('-', '')[-8:]}{top['forwarder_id'][-6:]}"))
    row = {"forwarder_id": top["forwarder_id"], "target_usd": target, "from_usd": top["total_usd"], "by": by,
           "at": datetime.now(timezone.utc).isoformat()}
    rfq["counters"] = [*(rfq.get("counters") or []), row]
    await _save(deal_id, rfq)
    return row


async def for_gate(deal_id: str, api_best_usd: float | None) -> dict[str, Any]:
    """The e-mailed quotes for the BOOK_FREIGHT gate's comparison (never raises)."""
    try:
        deal = await _deal(deal_id)
        rfq = dict(getattr(deal, "freight_rfq", None) or {})
        ready = date.fromisoformat(rfq["ready_date"]) if rfq.get("ready_date") else None
        top = best(rfq, ready=ready)
    except Exception:  # noqa: BLE001
        return {}
    if top is None:
        return {}
    return {"email_quote_best": top, "email_quotes": rfq.get("quotes") or [],
            "email_quote_cheaper": api_best_usd is None or top["total_usd"] < api_best_usd}


async def sweep(now: datetime | None = None) -> dict[str, Any] | None:
    """Deal watcher: ask forwarders for every deal past the PO that has not asked yet."""
    if not enabled():
        return None
    out: dict[str, Any] = {"asked": []}
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        return out
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status not in _STARTED or (getattr(d, "freight_rfq", None) or {}).get("requests"):
            continue
        try:
            await request_quotes(d.deal_id, by="system:freight_procurement")
            out["asked"].append(d.deal_id)
        except LookupError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("FreightProcurement: %s not asked: %s", d.deal_id, exc)
    return out


def reset_for_tests() -> None:
    _forwarders.clear()

