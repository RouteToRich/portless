"""Buyer enquiries from government portals and Indian missions abroad (todo 18.1).

**Portals.** Trade Connect (trade.gov.in — passes on enquiries from Indian missions) and the Indian
Business Portal (indianbusinessportal.in, FIEO) e-mail their buyer enquiries to the PortLess trade
mailbox. ``is_portal_enquiry`` recognises them by sender domain / subject; ``parse_enquiry`` reads the
buyer's company, country, e-mail, phone, product, HS code and quantity without a model, and the lead
goes through ``demand_leads.ingest`` (directory buyer with the stated quantity, candidate on a matching
prospect — still checked before any outreach).

**Missions.** ``request_list`` e-mails the commercial wing of India's embassy / consulate in a market
(``commercial_wing_email`` on the country profile) for a list of importers of a product — at most one
request per market × HS a quarter, with a ``[MISSION:<id>]`` token; ``reminders`` sends one reminder
after ``MISSION_REMINDER_DAYS`` (14). A returned list (reply body, or the text of its Excel / PDF) is
read by ``parse_list`` into rows that wait for staff (``pending_rows``); ``import_list`` puts the
confirmed rows into the directory as buyers (source ``indian_mission:<cc>``), checked like any
scanned buyer. ``stats`` counts leads per source and how many reached a PI.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

PORTAL_DOMAINS = {"trade.gov.in": "trade_connect", "indianbusinessportal.in": "indian_business_portal"}
MISSION_TOKEN = re.compile(r"\[MISSION:(MSN-[A-Z0-9]{6,12})\]", re.IGNORECASE)
_requests: dict[str, dict[str, Any]] = {}

_FIELD = {
    "company": re.compile(r"^\s*(?:company(?:\s+name)?|buyer(?:\s+name)?|importer|organi[sz]ation)\s*[:\-]\s*(.+)$", re.I | re.M),
    "country": re.compile(r"^\s*(?:country|destination)\s*[:\-]\s*(.+)$", re.I | re.M),
    "email": re.compile(r"^\s*(?:e-?mail(?:\s+id)?|contact\s+e-?mail)\s*[:\-]\s*(\S+@\S+)", re.I | re.M),
    "phone": re.compile(r"^\s*(?:phone|mobile|tel(?:ephone)?|whatsapp)\s*[:\-]\s*([+\d][\d\s()\-]{6,20})", re.I | re.M),
    "product": re.compile(r"^\s*(?:product(?:\s+name)?|item|commodity|requirement)\s*[:\-]\s*(.+)$", re.I | re.M),
    "contact": re.compile(r"^\s*(?:contact\s+person|name)\s*[:\-]\s*(.+)$", re.I | re.M),
}
_HS = re.compile(r"\b(?:HS|ITC[\s-]?HS|HSN)\s*(?:code)?\s*[:\-]?\s*(\d{4,8})\b", re.I)
_QTY = re.compile(r"\b(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(MT|metric\s+tons?|tonnes?|tons|kg|kgs)\b", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _country_code(raw: str | None) -> str | None:
    t = (raw or "").strip().rstrip(".").strip()
    if not t:
        return None
    if len(t) == 2 and t.isalpha():
        return t.upper()
    from shared import country_profiles
    for p in country_profiles.all_profiles():
        if p.name and p.name.lower() == t.lower():
            return p.iso2
    extra = {"uae": "AE", "united arab emirates": "AE", "usa": "US", "united states": "US", "uk": "GB",
             "united kingdom": "GB", "germany": "DE", "netherlands": "NL", "saudi arabia": "SA", "ksa": "SA"}
    return extra.get(t.lower())


def is_portal_enquiry(from_address: str | None, subject: str | None) -> str | None:
    """The portal an e-mail comes from (``trade_connect`` / ``indian_business_portal``), else None."""
    domain = (from_address or "").lower().rsplit("@", 1)[-1]
    for d, source in PORTAL_DOMAINS.items():
        if domain == d or domain.endswith("." + d):
            return source
    s = (subject or "").lower()
    if "trade connect" in s and "enquir" in s:
        return "trade_connect"
    if "indian business portal" in s:
        return "indian_business_portal"
    return None


def parse_enquiry(body: str | None, source: str) -> dict[str, Any]:
    """Pure: a portal enquiry → a demand lead."""
    t = body or ""
    got = {k: (m.group(1).strip() if (m := rx.search(t)) else None) for k, rx in _FIELD.items()}
    qty = None
    if (q := _QTY.search(t)):
        qty = float(q.group(1).replace(",", ""))
        if q.group(2).lower().startswith("kg"):
            qty = qty / 1000.0
    hs = (_HS.search(t).group(1) if _HS.search(t) else None)
    email = got["email"] or next((e for e in _EMAIL.findall(t) if not is_portal_enquiry(e, "")), None)
    return {"source": source, "kind": "enquiry", "buyer_name": got["company"], "country": _country_code(got["country"]),
            "email": email.rstrip(".,;") if email else None, "phone": got["phone"], "product": got["product"], "hs_code": hs,
            "quantity_mt": qty, "contact_person": got["contact"], "posted_on": datetime.now(timezone.utc).date().isoformat(),
            "flags": []}


async def handle_inbound(from_address: str, subject: str | None, body: str | None) -> dict[str, Any] | None:
    """A portal enquiry → a demand lead (None when the e-mail is not one)."""
    source = is_portal_enquiry(from_address, subject)
    if source is None:
        return None
    lead = parse_enquiry(body, source)
    from shared.orchestration import demand_leads
    result = await demand_leads.ingest([lead])
    return {"source": source, "lead": lead, **result}


# ── Indian missions ─────────────────────────────────────────────────────────

def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("mission_requests")
    except Exception:  # noqa: BLE001
        return None


async def _save(row: dict[str, Any]) -> None:
    _requests[row["request_id"]] = row
    col = _collection()
    if col is not None:
        try:
            await col.replace_one({"request_id": row["request_id"]}, dict(row), upsert=True)
        except Exception:  # noqa: BLE001
            pass


async def all_requests() -> list[dict[str, Any]]:
    col = _collection()
    if col is not None:
        try:
            for d in [d async for d in col.find({}, {"_id": 0})]:
                _requests[d["request_id"]] = d
        except Exception:  # noqa: BLE001
            pass
    return sorted(_requests.values(), key=lambda r: r["sent_at"], reverse=True)


def _quarter(dt: datetime) -> str:
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def mission_body(product: str, hs_code: str, quantity_range: str | None, market_name: str) -> str:
    from shared import company_profile
    from shared.documents import issuer
    ids = []
    for key, label in (("iec", "IEC"), ("rcmc", "RCMC")):
        value = os.getenv(f"PORTLESS_{key.upper()}", "").strip()
        if value:
            ids.append(f"{label} {value}")
    _ = issuer  # exporter facts printed only when set (same rule as the documents)
    return "\n".join([
        "Dear Sir / Madam,",
        "",
        f"We are an Indian exporter of {product} (HS {hs_code}) and would be grateful for a list of importers / "
        f"wholesale buyers of this product in {market_name}"
        + (f" (typical lots {quantity_range})" if quantity_range else "") + ".",
        "",
        "We sell with payment held in a neutral escrow account and independent pre-shipment inspection, "
        "so buyers take no advance-payment risk." + (f" Registration: {', '.join(ids)}." if ids else ""),
        "",
        "Thank you for your support of Indian exports.",
        "",
        company_profile.signature(),
    ])


async def request_list(market: str, hs_code: str, *, product: str | None, quantity_range: str | None, by: str,
                       now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    from shared import country_profiles
    cc = (market or "").strip().upper()[:2]
    profile = country_profiles.get(cc)
    to = getattr(profile, "commercial_wing_email", None) if profile else None
    if not to:
        raise LookupError(f"no commercial-wing address on the country profile of {cc} — add commercial_wing_email")
    hs = "".join(ch for ch in hs_code if ch.isdigit())
    if any(r["market"] == cc and r["hs_code"][:6] == hs[:6] and r["quarter"] == _quarter(now) for r in await all_requests()):
        raise ValueError(f"an importer-list request for HS {hs} in {cc} was already sent this quarter")
    from shared.product_label import product_label
    name = product or product_label(hs) or f"HS {hs}"
    rid = f"MSN-{uuid.uuid4().hex[:8].upper()}"
    subject = f"Request for importers of {name} in {getattr(profile, 'name', None) or cc} [MISSION:{rid}]"
    body = mission_body(name, hs, quantity_range, getattr(profile, "name", None) or cc)
    delivered = await _send(to, subject, body, f"MSN{rid[4:]}")
    row = {"request_id": rid, "market": cc, "hs_code": hs, "product": name, "to": to, "subject": subject,
           "sent_at": now.isoformat(), "sent_by": by, "delivered": delivered, "quarter": _quarter(now),
           "status": "sent", "reminded_at": None, "pending_rows": [], "imported": 0}
    await _save(row)
    return row


async def _send(to: str, subject: str, body: str, email_id: str) -> bool:
    try:
        from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
        from services.email_gateway.sender import send_email  # type: ignore[import]
        result = await send_email(SendEmailRequest(to_address=to, subject=subject, body=body, email_id=email_id))
        return str(getattr(getattr(result, "outcome", None), "value", "")) not in ("logged", "failed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("GovLeads: mission e-mail to %s not sent: %s", to, exc)
        return False


async def reminders(now: datetime | None = None) -> list[str]:
    """One reminder per request still unanswered after MISSION_REMINDER_DAYS (14)."""
    now = now or datetime.now(timezone.utc)
    days = int(os.getenv("MISSION_REMINDER_DAYS", "14") or 14)
    out = []
    for r in await all_requests():
        if r["status"] != "sent" or r.get("reminded_at"):
            continue
        if now - datetime.fromisoformat(r["sent_at"]) >= timedelta(days=days):
            await _send(r["to"], "Reminder: " + r["subject"],
                        f"A gentle reminder of our request below for a list of importers of {r['product']}.\n\n"
                        + mission_body(r["product"], r["hs_code"], None, r["market"]), f"MSNR{r['request_id'][4:]}")
            r["reminded_at"] = now.isoformat()
            await _save(r)
            out.append(r["request_id"])
    return out


def parse_token(subject: str | None) -> str | None:
    m = MISSION_TOKEN.search(subject or "")
    return m.group(1).upper() if m else None


def parse_list(text: str | None) -> list[dict[str, Any]]:
    """Pure: importer rows from a mission's list — one company per line that carries an e-mail address
    (``Company, City, email, phone`` / tab or ``|`` separated)."""
    rows = []
    for line in (text or "").splitlines():
        m = _EMAIL.search(line)
        if not m:
            continue
        parts = [p.strip() for p in re.split(r"\t|\||;|,", line) if p.strip()]
        name = next((p for p in parts if "@" not in p and not re.fullmatch(r"[+\d\s()\-]{7,}", p) and len(p) > 2), None)
        if not name:
            continue
        phone = next((p for p in parts if re.fullmatch(r"[+\d][\d\s()\-]{6,20}", p)), None)
        rows.append({"company": name.strip(" -•*"), "email": m.group(0).lower(), "phone": phone})
    return rows


async def handle_mission_reply(request_id: str, from_address: str, body: str | None) -> dict[str, Any]:
    reqs = {r["request_id"]: r for r in await all_requests()}
    r = reqs.get(request_id)
    if r is None:
        return {"handled": False, "reason": "unknown request"}
    if (from_address or "").lower().rsplit("@", 1)[-1] != r["to"].lower().rsplit("@", 1)[-1]:
        return {"handled": False, "reason": "reply is not from the mission's domain"}
    rows = parse_list(body)
    r.update(status="replied", replied_at=datetime.now(timezone.utc).isoformat(), pending_rows=rows)
    await _save(r)
    return {"handled": True, "rows": len(rows)}


async def import_list(request_id: str, *, rows: list[dict[str, Any]] | None, by: str) -> dict[str, Any]:
    """Staff confirm the list: the rows (or the pending ones) become directory buyers."""
    reqs = {r["request_id"]: r for r in await all_requests()}
    r = reqs.get(request_id)
    if r is None:
        raise LookupError(f"mission request {request_id} not found")
    use = rows if rows is not None else r.get("pending_rows") or []
    if not use:
        raise ValueError("no rows to import")
    from shared.orchestration import demand_leads
    leads = [{"source": f"indian_mission:{r['market']}", "kind": "mission_list", "buyer_name": x.get("company"),
              "country": r["market"], "email": x.get("email"), "phone": x.get("phone"), "product": r["product"],
              "hs_code": r["hs_code"], "posted_on": datetime.now(timezone.utc).date().isoformat(), "flags": []}
             for x in use if x.get("company")]
    result = await demand_leads.ingest(leads)
    r.update(status="imported", imported=result["directory"], imported_by=by, pending_rows=[])
    await _save(r)
    return {"request_id": request_id, **result}


async def stats() -> dict[str, Any]:
    """Leads per source (portals, missions, buy requests, tenders) and how many reached a PI."""
    from shared.orchestration import demand_leads
    leads = await demand_leads.recent(limit=5000)
    per: dict[str, dict[str, int]] = {}
    for lead in leads:
        src = str(lead.get("source") or "other").split(":")[0]
        per.setdefault(src, {"leads": 0, "reached_pi": 0})["leads"] += 1
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        deals = []
    past_pi = {"PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING", "ESCROW_FUNDED", "PO_ISSUED", "SUPPLIER_DISPATCH_READY",
               "QC_PASSED", "IN_TRANSIT", "CLEARED", "PAID"}
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status not in past_pi:
            continue
        chosen = next((c for c in d.buyer_candidates if c.status == "selected"), None)
        src = str(getattr(chosen, "source", "") or "").split(":")[0] if chosen else ""
        if src in per:
            per[src]["reached_pi"] += 1
    return {"sources": per}


def reset_for_tests() -> None:
    _requests.clear()
