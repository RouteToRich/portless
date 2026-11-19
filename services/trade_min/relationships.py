"""Where PortLess stands with each buyer and supplier — the directory as a trading book.

The directory knows what a scan found about a company; the deals know what happened
with it: introduced, replied, said yes, quoted, became a party, traded. This module
joins the two (by website domain, e-mail domain, else name) and scores each company so
the trader works the best counterparties first.

Relationship stages, best first:
``traded`` (party of a PAID deal) · ``in_deal`` (party of a live deal) · ``quoted``
(supplier quote received) / ``interested`` (buyer said yes) · ``replied`` · ``contacted``
· ``new`` (never contacted) — and ``declined`` / ``do_not_contact`` which stop outreach.

Score (0–100): the scan-time check (verified 30, unverified 10, failed → 0), a usable
e-mail 10, engagement (replied 10 · interested / quoted 20 · in a deal 25 · traded 30),
past outcomes (signed / completed vs failed / disputed), the actor's fit score (≤ 10),
for suppliers a quote below the product's median (+10); data-quality flags −30;
do-not-contact → 0. Never raises: the directory lists without it.
"""
from __future__ import annotations

import logging
import re
import statistics
from datetime import datetime, timezone
from typing import Any

from .schemas import CounterpartyRecord

logger = logging.getLogger(__name__)

_LIVE = {"NEGOTIATING", "NEGOTIATION_AGREED", "PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING", "ESCROW_FUNDED",
         "PO_ISSUED", "SUPPLIER_DISPATCH_READY", "QC_PASSED", "IN_TRANSIT", "CLEARED", "DISPUTE"}
_STAGE_RANK = {"traded": 7, "in_deal": 6, "quoted": 5, "interested": 5, "replied": 4, "contacted": 3,
               "new": 1, "declined": 0, "do_not_contact": 0}
_FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "rediffmail.com", "icloud.com", "aol.com",
              "live.com", "yahoo.co.in", "protonmail.com"}


def _domain(value: Any) -> str:
    v = str(value or "").strip().lower()
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    v = re.sub(r"^https?://", "", v).split("/")[0].removeprefix("www.")
    return "" if v in _FREE_MAIL else v


def _slug(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def keys_for(*values: Any, name: Any = None) -> set[str]:
    """Join keys for a company: its domains (website / e-mail / id) and, last, its name."""
    out = {f"d:{d}" for d in (_domain(v) for v in values) if d and "." in d}
    if name and _slug(name):
        out.add(f"n:{_slug(name)}")
    return out


def record_keys(r: CounterpartyRecord) -> set[str]:
    rid = r.id.split(":", 1)[1] if ":" in r.id else r.id
    return keys_for(r.website, r.email, rid, name=r.name)


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _status(deal: Any) -> str:
    return str(getattr(getattr(deal, "status", None), "value", getattr(deal, "status", "")))


def _new_entry() -> dict[str, Any]:
    return {"stage": "new", "prospects": set(), "deals_active": set(), "deals_won": set(), "contacted": 0,
            "replied": 0, "interested": 0, "declined": 0, "quotes": 0, "last_contact_at": None,
            "last_reply_at": None, "last_quote_usd_per_mt": None, "last_quote_at": None,
            "target_price_usd_per_mt": None, "requirement": None, "keys": set()}


def build_index(deals: list[Any]) -> dict[str, dict[str, Any]]:
    """Pure: every deal → per-company activity, keyed by ``role|key`` (one entry shared by its keys)."""
    entries: dict[str, dict[str, Any]] = {}

    def entry(role: str, keys: set[str]) -> dict[str, Any]:
        for k in keys:
            hit = entries.get(f"{role}|{k}")
            if hit is not None:
                for k2 in keys:
                    entries[f"{role}|{k2}"] = hit
                hit["keys"] |= keys
                return hit
        e = _new_entry()
        e["keys"] = set(keys)
        for k in keys:
            entries[f"{role}|{k}"] = e
        return e

    def later(a: Any, b: Any) -> Any:
        a, b = _aware(a), _aware(b)
        return b if a is None or (b is not None and b > a) else a

    for d in deals:
        status = _status(d)
        for c in getattr(d, "buyer_candidates", None) or []:
            keys = keys_for(getattr(c, "website", None), getattr(c, "email", None), getattr(c, "candidate_id", None),
                            name=getattr(c, "legal_name", None))
            if not keys:
                continue
            e = entry("buyer", keys)
            e["prospects"].add(d.deal_id)
            st = getattr(c, "status", "pending")
            if st in ("contacted", "followed_up", "interested", "selected", "declined", "no_reply"):
                e["contacted"] += 1
            if getattr(c, "replied_at", None):
                e["replied"] += 1
            if st in ("interested", "selected"):
                e["interested"] += 1
            if st == "declined":
                e["declined"] += 1
            e["last_contact_at"] = later(e["last_contact_at"], getattr(c, "followed_up_at", None) or getattr(c, "contacted_at", None))
            e["last_reply_at"] = later(e["last_reply_at"], getattr(c, "replied_at", None))
            req = getattr(c, "requirement", None)
            if req is not None:
                if getattr(req, "target_price_usd_per_mt", None):
                    e["target_price_usd_per_mt"] = req.target_price_usd_per_mt
                bits = [str(x) for x in (getattr(req, "grade", None),
                                         f"{req.quantity_mt:g} MT" if getattr(req, "quantity_mt", None) else None,
                                         getattr(req, "packaging", None), getattr(req, "destination_port", None)) if x]
                if bits:
                    e["requirement"] = " · ".join(bits)
        for o in getattr(d, "supplier_offers", None) or []:
            keys = keys_for(getattr(o, "email", None), getattr(o, "supplier_id", None), name=getattr(o, "company_name", None))
            if not keys:
                continue
            e = entry("supplier", keys)
            e["prospects"].add(d.deal_id)
            e["quotes"] += 1
            when = _aware(getattr(o, "received_at", None))
            if getattr(o, "fob_price_usd_per_mt", None) and (e["last_quote_at"] is None or (when and when >= e["last_quote_at"])):
                e["last_quote_usd_per_mt"] = float(o.fob_price_usd_per_mt)
                e["last_quote_at"] = when
        for p in getattr(d, "parties", None) or []:
            raw_role = getattr(p, "role", "") or ""
            role = str(getattr(raw_role, "value", raw_role)).lower()
            if role not in ("buyer", "supplier"):
                continue
            keys = keys_for(getattr(p, "website", None), getattr(p, "email", None), getattr(p, "party_id", None),
                            name=getattr(p, "legal_name", None))
            if not keys:
                continue
            e = entry(role, keys)
            if status == "PAID":
                e["deals_won"].add(d.deal_id)
            elif status in _LIVE:
                e["deals_active"].add(d.deal_id)
    # stages
    for e in {id(v): v for v in entries.values()}.values():
        if e["deals_won"]:
            e["stage"] = "traded"
        elif e["deals_active"]:
            e["stage"] = "in_deal"
        elif e["quotes"]:
            e["stage"] = "quoted"
        elif e["interested"]:
            e["stage"] = "interested"
        elif e["declined"]:
            e["stage"] = "declined"
        elif e["replied"]:
            e["stage"] = "replied"
        elif e["contacted"]:
            e["stage"] = "contacted"
    return entries


def relationship_of(r: CounterpartyRecord, index: dict[str, dict[str, Any]],
                    median_quote: dict[str, float]) -> dict[str, Any]:
    e = next((index[f"{r.role}|{k}"] for k in record_keys(r) if f"{r.role}|{k}" in index), None)
    rel: dict[str, Any] = {"stage": "new", "prospects": 0, "deals_active": 0, "deals_won": 0}
    if e is not None:
        rel = {
            "stage": e["stage"], "prospects": len(e["prospects"]), "deals_active": len(e["deals_active"]),
            "deals_won": len(e["deals_won"]), "contacted": e["contacted"], "replied": e["replied"],
            "reply_rate": round(e["replied"] / e["contacted"], 2) if e["contacted"] else None,
            "interested": e["interested"], "declined": e["declined"], "quotes": e["quotes"],
            "last_contact_at": e["last_contact_at"].isoformat() if e["last_contact_at"] else None,
            "last_reply_at": e["last_reply_at"].isoformat() if e["last_reply_at"] else None,
            "last_quote_usd_per_mt": e["last_quote_usd_per_mt"],
            "last_quote_at": e["last_quote_at"].isoformat() if e["last_quote_at"] else None,
            "target_price_usd_per_mt": e["target_price_usd_per_mt"], "requirement": e["requirement"],
        }
    quote = rel.get("last_quote_usd_per_mt") or r.estimated_fob_price_usd
    med = next((median_quote[h] for h in r.hs_codes if h in median_quote), None)
    if r.role == "supplier" and quote and med:
        rel["quote_vs_median_pct"] = round((quote - med) / med * 100.0, 1)
    if r.do_not_contact:
        rel["stage"] = "do_not_contact"
    return rel


def score_of(r: CounterpartyRecord, rel: dict[str, Any], track: dict[str, Any] | None) -> tuple[float, list[str]]:
    basis: list[str] = []
    if r.do_not_contact:
        return 0.0, ["do not contact" + (f": {r.dnc_reason}" if r.dnc_reason else "")]
    status = r.validation.status if r.validation else "unverified"
    if status == "failed":
        return 0.0, ["failed the counterparty check"]
    pts = 30.0 if status == "verified" else 10.0
    basis.append(f"check {status} (+{pts:.0f})")
    if r.email and (r.validation is None or (r.validation.email or "ok") not in ("invalid", "bounced")):
        pts += 10
        basis.append("usable e-mail (+10)")
    engagement = {"traded": 30, "in_deal": 25, "quoted": 20, "interested": 20, "replied": 10}.get(rel.get("stage", ""), 0)
    if engagement:
        pts += engagement
        basis.append(f"{rel['stage'].replace('_', ' ')} (+{engagement})")
    if rel.get("stage") == "declined":
        pts -= 10
        basis.append("declined before (−10)")
    if track:
        good = track.get("signed", 0) + track.get("completed", 0)
        bad = track.get("failed", 0) + track.get("bad", 0)
        if good or bad:
            delta = 10.0 * (good - bad) / max(1, good + bad)
            pts += delta
            basis.append(f"past deals {good} good / {bad} bad ({delta:+.0f})")
    perf = rel.get("performance") or {}
    if perf.get("qc_inspections"):
        rate = perf.get("qc_pass_rate") or 0.0
        delta = round(20.0 * rate - 10.0)  # 100 % pass → +10, 50 % → 0, 0 % → −10
        pts += delta
        basis.append(f"QC passed {perf.get('qc_passed', 0)} of {perf['qc_inspections']} ({delta:+.0f})")
    if perf.get("late_ready"):
        pts -= 5 * min(2, perf["late_ready"])
        basis.append(f"goods ready late {perf['late_ready']}× (−{5 * min(2, perf['late_ready'])})")
    if perf.get("disputes"):
        pts -= 10
        basis.append(f"{perf['disputes']} dispute(s) (−10)")
    if perf.get("requoted_up"):
        pts -= 5
        basis.append("raised its price after quoting (−5)")
    if r.fit_score is not None:
        pts += min(10.0, r.fit_score / 10.0)
        basis.append(f"fit {r.fit_score} (+{min(10.0, r.fit_score / 10.0):.0f})")
    vs = rel.get("quote_vs_median_pct")
    if vs is not None and vs < 0:
        pts += 10
        basis.append(f"quote {abs(vs):.0f}% below the median (+10)")
    # Found by more than one source (scan, directory list, staff, another actor): more likely
    # real and active (todo 21.3 "one de-duplication across sources").
    n_sources = len({s for s in (r.sources or []) if s})
    if n_sources > 1:
        bonus = min(9, 3 * (n_sources - 1))
        pts += bonus
        basis.append(f"found by {n_sources} sources (+{bonus})")
    # Exists beyond its website: LEI, a valid EU VAT number, an Indian CIN / Udyam (badge).
    evidence = [e for e in ((r.validation.existence if r.validation else None) or []) if "NOT valid" not in e]
    if evidence or r.cin or r.lei:
        pts += 5
        basis.append("company register evidence (+5)")
    # Certificates the supplier shows (its site, its papers): supplier trust (todo 13.6).
    if r.role == "supplier":
        certs = {c for pf in (r.product_facts or {}).values()
                 for c in ((((pf or {}).get("facts") or {}).get("certificates") or {}).get("value") or [])}
        certs |= {d.get("kind") for d in (r.documents or []) if d.get("kind") in ("fssai", "apeda_rcmc", "iso", "organic")}
        if certs:
            bonus = min(6, 2 * len(certs))
            pts += bonus
            basis.append(f"{len(certs)} certificate(s) (+{bonus})")
    bad_flags = {"role_suspect", "product_mismatch"} & set(r.flags or [])
    if bad_flags:
        pts -= 30
        basis.append(f"{', '.join(sorted(bad_flags)).replace('_', ' ')} (−30)")
    return round(max(0.0, min(100.0, pts)), 1), basis


async def _deals() -> list[Any]:
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        return await deals_repo().list_active(limit=2000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Relationships: deals unavailable: %s", exc)
        return []


async def _tracks() -> dict[str, Any]:
    try:
        from shared.orchestration import outcomes
        return outcomes.track_records(await outcomes.all_outcomes())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Relationships: outcomes unavailable: %s", exc)
        return {}


def _track_for(r: CounterpartyRecord, tracks: dict[str, Any]) -> dict[str, Any] | None:
    rid = r.id.split(":", 1)[1] if ":" in r.id else r.id
    return tracks.get(f"{r.role}:{rid}")


async def annotate(rows: list[CounterpartyRecord]) -> list[CounterpartyRecord]:
    """Copies of ``rows`` with ``relationship``, ``score`` and ``score_basis`` (never raises)."""
    try:
        from shared import view_cache

        async def _index():
            return build_index(await _deals())

        index = await view_cache.get_or_compute("relationships.index", _index)
        tracks = await view_cache.get_or_compute("relationships.tracks", _tracks, deps=("deals", "outcomes"))
        try:
            from shared.orchestration import supplier_performance
            performance = await supplier_performance.cached()
        except Exception:  # noqa: BLE001
            performance = {}
        quotes: dict[str, list[float]] = {}
        for r in rows:
            if r.role != "supplier":
                continue
            rel = relationship_of(r, index, {})
            q = rel.get("last_quote_usd_per_mt") or r.estimated_fob_price_usd
            if q:
                for h in r.hs_codes:
                    quotes.setdefault(h, []).append(float(q))
        median = {h: statistics.median(v) for h, v in quotes.items() if len(v) >= 2}
        out = []
        for r in rows:
            rel = relationship_of(r, index, median)
            perf = None
            if r.role == "supplier" and performance:
                perf = next((performance[k] for k in record_keys(r) if k in performance), None)
                if perf:
                    rel["performance"] = perf
            score, basis = score_of(r, rel, _track_for(r, tracks))
            out.append(r.model_copy(update={"relationship": rel, "score": score, "score_basis": basis}))
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Relationships: annotate failed: %s", exc)
        return rows


async def blocked_contacts() -> set[str]:
    """Domains / addresses outreach must never write to: do-not-contact directory records
    and every buyer that declined in any prospect (the introduction promises it)."""
    out: set[str] = set()
    try:
        from .directory import get_directory
        store = await get_directory()
        for r in await store.list("buyer", limit=5000):
            if r.do_not_contact:
                out |= {k[2:] for k in record_keys(r) if k.startswith("d:")}
                if r.email:
                    out.add(r.email.lower())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Relationships: directory unavailable for the do-not-contact list: %s", exc)
    for d in await _deals():
        for c in getattr(d, "buyer_candidates", None) or []:
            # A decline blocks that exact address everywhere; the whole company (domain) is
            # blocked through its directory record (set at the decline, or by staff).
            if getattr(c, "status", None) == "declined" and str(getattr(c, "reply_preview", "") or "") \
                    and not str(c.reply_preview).startswith(("declined by operator", "skipped")) and c.email:
                out.add(c.email.lower())
    return out


async def suspect_contacts() -> set[str]:
    """Domains / addresses of directory "buyers" flagged ``role_suspect`` (they look like
    Indian exporters, not importers): outreach must not pitch them as buyers."""
    out: set[str] = set()
    try:
        from .directory import clean_record, get_directory
        store = await get_directory()
        for r in await store.list("buyer", limit=5000):
            r = clean_record(r)
            if "role_suspect" in (r.flags or []):
                out |= {k[2:] for k in record_keys(r) if k.startswith("d:")}
                if r.email:
                    out.add(r.email.lower())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Relationships: directory unavailable for suspect buyers: %s", exc)
    return out
