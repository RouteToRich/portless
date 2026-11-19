"""Buyer leads from public demand — buy requests, food tenders, government enquiry portals (todo 18.1,
15.4, 21.3: A.16 ``buy-leads-ai``, A.17 ``food-tenders-ai``, Trade Connect / Indian Business Portal).

Every lead is stored once (Mongo ``demand_leads``, key = source + buyer + product + posted date) and:

- becomes a **directory buyer** (source = the lead's source) with the quantity it stated carried as a
  *stated* quantity (``stated_quantity_mt`` + source + date — never an offer quantity);
- joins an **open prospect** of the same product (4-digit HS, else the product name) and market as a
  buyer candidate (``source``, ``quantity_mt``); a lead is ``unverified`` until the contact finder and
  the counterparty check pass, so outreach treats it like any unchecked buyer (``OUTREACH_VERIFIED_ONLY``);
- buy-request boards are full of brokers: those rows carry ``unverified_lead`` and never skip the check.

``daily`` (deal watcher, ``DEMAND_LEADS_ENABLED`` — pinned off in tests) runs the two actors for the
catalogue's products once a day; each actor runs only once it is built (``APIFY_ACTOR_<KEY>_BUILT``).
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_memory: dict[str, dict[str, Any]] = {}
_last: dict[str, str] = {}
_OPEN = ("DRAFT", "MATCHING")


def enabled() -> bool:
    return os.getenv("DEMAND_LEADS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("demand_leads")
    except Exception:  # noqa: BLE001
        return None


def lead_id(lead: dict[str, Any]) -> str:
    key = "|".join(str(lead.get(k) or "").lower() for k in ("source", "buyer_name", "product", "posted_on", "url"))
    return "LEAD-" + hashlib.sha1(key.encode()).hexdigest()[:12].upper()


async def store(lead: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """(lead, is_new) — a lead seen before is returned unchanged."""
    lid = lead.get("lead_id") or lead_id(lead)
    if lid in _memory:
        return _memory[lid], False
    col = _collection()
    if col is not None:
        try:
            old = await col.find_one({"_id": lid})
            if old:
                old.pop("_id", None)
                _memory[lid] = old
                return old, False
        except Exception:  # noqa: BLE001
            pass
    row = {**lead, "lead_id": lid, "seen_at": datetime.now(timezone.utc).isoformat()}
    _memory[lid] = row
    if col is not None:
        try:
            await col.replace_one({"_id": lid}, {"_id": lid, **row}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("DemandLeads: %s not stored: %s", lid, exc)
    return row, True


async def to_directory(lead: dict[str, Any]) -> Any:
    """The lead's buyer as a directory record with the stated quantity (None without a name)."""
    name = (lead.get("buyer_name") or "").strip()
    if not name:
        return None
    from services.trade_min.directory import get_directory, record_key  # type: ignore[import]
    from services.trade_min.schemas import CounterpartyRecord  # type: ignore[import]
    now = datetime.now(timezone.utc)
    rec = CounterpartyRecord(
        id=record_key("buyer", name, lead.get("website")), role="buyer", name=name, country=lead.get("country"),
        email=lead.get("email"), phone=lead.get("phone"), website=lead.get("website"), contact_url=lead.get("url"),
        source=lead.get("source") or "demand_lead", sources=[lead.get("source") or "demand_lead"],
        hs_codes=[lead["hs_code"]] if lead.get("hs_code") else [], markets=[lead["country"]] if lead.get("country") else [],
        stated_quantity_mt=lead.get("quantity_mt"), stated_quantity_source=lead.get("source") if lead.get("quantity_mt") else None,
        stated_at=(lead.get("posted_on") or now.date().isoformat()) if lead.get("quantity_mt") else None,
        flags=list(lead.get("flags") or []), first_seen_at=now, last_seen_at=now,
        last_outcome=f"{lead.get('kind') or 'lead'}: {lead.get('product') or ''}".strip(),
    )
    return await (await get_directory()).upsert(rec)


def _matches(deal: Any, lead: dict[str, Any]) -> bool:
    if (lead.get("country") or "").upper() != str(getattr(deal, "market", "") or "").upper():
        return False
    hs = str(getattr(deal, "hs_code", "") or "")
    if lead.get("hs_code"):
        return hs[:4] == str(lead["hs_code"])[:4]
    try:
        from shared.product_label import product_label
        name = (product_label(hs) or "").lower()
    except Exception:  # noqa: BLE001
        name = ""
    product = (lead.get("product") or "").lower()
    return bool(name) and any(w in product for w in name.split() if len(w) > 3)


async def attach(lead: dict[str, Any]) -> list[str]:
    """Add the lead's buyer to every open prospect of its product × market (never twice)."""
    if not (lead.get("buyer_name") and (lead.get("email") or lead.get("phone"))):
        return []
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        from services.deals.schemas import BuyerCandidate  # type: ignore[import]
        from services.deals.service import add_buyer_candidate  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    cid = "LEAD-" + lead_id(lead)[5:13]
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status not in _OPEN or not _matches(d, lead):
            continue
        if any(c.candidate_id == cid or (c.email and lead.get("email") and c.email.lower() == lead["email"].lower())
               for c in d.buyer_candidates):
            continue
        try:
            await add_buyer_candidate(d.deal_id, BuyerCandidate(
                candidate_id=cid, legal_name=lead["buyer_name"], country=lead.get("country") or d.market,
                email=lead.get("email"), phone=lead.get("phone"), website=lead.get("website"),
                source=lead.get("source"), quantity_mt=lead.get("quantity_mt"), validation_status="unverified",
                pitch_angle=(f"your {lead.get('kind', 'request').replace('_', ' ')} for {lead.get('product')}"
                             if lead.get("product") else None)))
            out.append(d.deal_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("DemandLeads: candidate not added to %s: %s", d.deal_id, exc)
    return out


async def ingest(leads: list[dict[str, Any]]) -> dict[str, Any]:
    """Store, add to the directory, attach to prospects. Never raises."""
    out = {"new": 0, "directory": 0, "attached": []}
    for lead in leads:
        try:
            row, new = await store(lead)
            if not new:
                continue
            out["new"] += 1
            if await to_directory(row) is not None:
                out["directory"] += 1
            out["attached"] += await attach(row)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DemandLeads: lead not ingested: %s", exc)
    return out


async def run_actors() -> dict[str, Any]:
    from shared import catalogue
    from shared.connectors.registry import get_connector
    lines = catalogue.active_lines()
    products = sorted({getattr(line, "name", "") for line in lines if getattr(line, "name", "")})
    countries = sorted({m for line in lines for m in (getattr(line, "markets", None) or [])})
    leads: list[dict[str, Any]] = []
    for name in ("apify_buy_leads", "apify_food_tenders"):
        try:
            res = await get_connector(name).quote({"products": products, "countries": countries})
            leads += res.get("leads", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("DemandLeads: %s failed: %s", name, exc)
    return await ingest(leads)


async def daily(now: datetime | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    day = (now or datetime.now(timezone.utc)).date().isoformat()
    if _last.get("day") == day:
        return None
    _last["day"] = day
    return await run_actors()


async def recent(limit: int = 100) -> list[dict[str, Any]]:
    rows = list(_memory.values())
    col = _collection()
    if col is not None:
        try:
            rows = [d async for d in col.find({}, {"_id": 0}).sort("seen_at", -1).limit(limit)]
        except Exception:  # noqa: BLE001
            pass
    return sorted(rows, key=lambda r: r.get("seen_at", ""), reverse=True)[:limit]


def reset_for_tests() -> None:
    _memory.clear()
    _last.clear()
