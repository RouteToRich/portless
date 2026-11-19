"""Personal data of counterparties (India DPDP Act 2023) and statutory record retention.

**Erase on request** (``POST /trade-min/{buyers|suppliers}/{id}/erase``, staff, reason required):
the person's contact data is removed from the directory record — e-mail, phone, contact persons,
note texts — and the company is set do-not-contact (so no scan or outreach writes to it again);
open prospects drop the same address from their buyer candidates. The company name, its country
and its trade facts stay (business data, not personal data). Audited.

**Statutory retention wins**: GST, customs (Shipping Bill) and FEMA records must be kept for
years (``RECORD_RETENTION_YEARS``, 8). A company that is a party to a deal past the PI — a
signed contract — keeps the contact data that deal's records need until the retention period
ends; the erase then removes the directory copy and reports what is held and until when.

**Retention sweep** (``GET / POST /trade-min/personal-data/retention``): directory companies
never contacted, in no prospect or deal, and not seen for ``PERSONAL_DATA_RETENTION_DAYS`` (730)
lose their personal contact data the same way (the company row stays for de-duplication).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from . import directory

logger = logging.getLogger(__name__)

_SIGNED = {"PI_ACCEPTED", "ESCROW_PENDING", "ESCROW_FUNDED", "PO_ISSUED", "SUPPLIER_DISPATCH_READY", "QC_PASSED",
           "QC_FAILED", "IN_TRANSIT", "CLEARED", "PAID", "DISPUTE", "REFUNDED"}


def retention_years() -> int:
    try:
        return max(1, int(os.getenv("RECORD_RETENTION_YEARS", "8")))
    except ValueError:
        return 8


def personal_retention_days() -> int:
    try:
        return max(30, int(os.getenv("PERSONAL_DATA_RETENTION_DAYS", "730")))
    except ValueError:
        return 730


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _scrubbed(rec: Any) -> dict[str, Any]:
    return {"email": None, "phone": None, "people": [], "contact_url": None,
            "notes": [n.model_copy(update={"text": "[erased]"}) for n in (rec.notes or [])]}


async def _deals() -> list[Any]:
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        return await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        return []


def _addresses(rec: Any) -> set[str]:
    out = {(rec.email or "").lower()} | {(p.get("email") or "").lower() for p in rec.people or []}
    return {a for a in out if a}


def held_by_deals(rec: Any, deals: list[Any], now: datetime) -> list[dict[str, Any]]:
    """Signed deals whose statutory records name this company, with the date they may go."""
    from .relationships import keys_for, record_keys
    keys = record_keys(rec)
    out = []
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status not in _SIGNED:
            continue
        for p in getattr(d, "parties", None) or []:
            if keys & keys_for(getattr(p, "email", None), getattr(p, "website", None), name=getattr(p, "legal_name", None)):
                ref = _aware(getattr(d, "updated_at", None)) or now
                until = ref + timedelta(days=365 * retention_years())
                if until > now:
                    out.append({"deal_id": d.deal_id, "status": status, "keep_until": until.date().isoformat()})
    return out


async def erase(role: str, record_id: str, *, by: str, reason: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    store = await directory.get_directory()
    rec = await store.get(record_id if record_id.startswith(f"{role}:") else f"{role}:{record_id}")
    if rec is None or rec.role != role:
        raise LookupError(f"{record_id} not in the {role} directory")
    deals = await _deals()
    held = held_by_deals(rec, deals, now)
    addresses = _addresses(rec)
    await store.replace(rec.model_copy(update={**_scrubbed(rec), "do_not_contact": True,
                                               "dnc_reason": "personal data erased on request", "dnc_at": now}))
    cleaned = 0
    if addresses:
        from services.deals.service import update_buyer_candidate  # type: ignore[import]
        for d in deals:
            status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
            if status not in ("DRAFT", "MATCHING"):
                continue
            for c in getattr(d, "buyer_candidates", None) or []:
                if (getattr(c, "email", None) or "").lower() in addresses:
                    await update_buyer_candidate(d.deal_id, c.candidate_id, {"email": None, "phone": None,
                                                                             "status": "declined"})
                    cleaned += 1
    from shared.audit_log import record
    await record("personal_data.erased", by, rec.id, {"reason": reason, "held_by_deals": held,
                                                      "prospect_candidates_cleaned": cleaned})
    return {"id": rec.id, "erased": ["email", "phone", "people", "notes"], "do_not_contact": True,
            "prospect_candidates_cleaned": cleaned, "held_by_deals": held,
            "note": ("signed deals keep the contact data their GST / customs / FEMA records need until the dates shown"
                     if held else "")}


async def retention_candidates(now: datetime | None = None) -> list[Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=personal_retention_days())
    from .relationships import annotate
    store = await directory.get_directory()
    out = []
    for role in ("buyer", "supplier"):
        for r in await annotate(await store.list(role, limit=100_000)):
            stage = str(((r.relationship or {}).get("stage")) or "new")
            if stage != "new" or not (_addresses(r) or r.phone):
                continue
            if (_aware(r.last_seen_at) or now) > cutoff or r.manual:
                continue
            out.append(r)
    return out


async def run_retention(*, by: str, now: datetime | None = None) -> dict[str, Any]:
    store = await directory.get_directory()
    done = []
    for r in await retention_candidates(now):
        await store.replace(r.model_copy(update={**_scrubbed(r), "relationship": None, "score": None,
                                                 "score_basis": []}))
        done.append(r.id)
    if done:
        from shared.audit_log import record
        await record("personal_data.retention_sweep", by, "directory", {"records": len(done)})
    return {"scrubbed": len(done), "ids": done[:100], "retention_days": personal_retention_days()}
