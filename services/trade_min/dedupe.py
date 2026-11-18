"""Duplicate companies in the directory: suggest merges, staff confirm.

The same company is found under two domains (group site and local site), with and without
its legal form ("Gulf Foods LLC" / "Gulf Foods Trading"), or through two sources. Suggestions
(``GET /trade-min/directory/{role}/duplicates``) group records of one role that share

- the same company e-mail domain (never a free-mail one), or
- the same phone number (last 9 digits), or
- the same name core in the same country (legal forms and trade words removed).

Merging (``POST /trade-min/directory/{role}/merge``, staff) keeps one record and folds the
others into it: sources, HS codes, markets, opportunities, flags, notes and screening reviews
are joined; empty contact fields are filled; do-not-contact wins; the merged ids and names
stay on the kept record (``merged_from``, ``aliases``) and the others are deleted.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord

logger = logging.getLogger(__name__)

_LEGAL = re.compile(
    r"\b(llc|ltd|limited|pvt|private|gmbh|ag|sa|sas|sarl|srl|spa|bv|nv|inc|corp|corporation|co|company|"
    r"plc|fze|fzco|fzc|est|establishment|trading|traders|international|intl|group|enterprises?|exports?|imports?|"
    r"foods?|general|the|and|&)\b", re.IGNORECASE)


def name_core(name: str | None) -> str:
    text = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower().replace(".", ""))
    return " ".join(_LEGAL.sub(" ", text).split())


def _email_domain(rec: CounterpartyRecord) -> str | None:
    from agents.negotiation_agent.email_safety import FREEMAIL
    email = (rec.email or "").lower()
    if "@" not in email:
        return None
    d = email.split("@", 1)[1]
    return None if d in FREEMAIL else d


def _phone(rec: CounterpartyRecord) -> str | None:
    digits = re.sub(r"\D", "", rec.phone or "")
    return digits[-9:] if len(digits) >= 9 else None


def suggestions(rows: list[CounterpartyRecord]) -> list[dict[str, Any]]:
    """Groups of probable duplicates (union of the three keys), biggest first."""
    parent = {r.id: r.id for r in rows}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    reasons: dict[frozenset, set[str]] = {}
    keys: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        for kind, value in (("e-mail domain", _email_domain(r)), ("phone", _phone(r)),
                            ("name", f"{name_core(r.name)}|{(r.country or '').upper()}" if len(name_core(r.name)) >= 4 else None)):
            if value:
                keys.setdefault((kind, value), []).append(r.id)
    for (kind, _value), ids in keys.items():
        if len(ids) < 2:
            continue
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
        reasons.setdefault(frozenset(ids), set()).add(kind)
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(find(r.id), []).append(r.id)
    by_id = {r.id: r for r in rows}
    out = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        why = sorted({k for s, ks in reasons.items() if s & set(ids) for k in ks})
        members = sorted((by_id[i] for i in ids), key=lambda r: (-(r.score or 0), -r.times_seen, r.first_seen_at))
        out.append({"ids": [m.id for m in members], "names": [m.name for m in members], "reasons": why,
                    "keep_suggested": members[0].id})
    return sorted(out, key=lambda g: -len(g["ids"]))


async def duplicates(role: str) -> list[dict[str, Any]]:
    store = await directory.get_directory()
    return suggestions([r for r in await store.list(role, limit=100_000)])


def _join(a: list[str], b: list[str]) -> list[str]:
    return list(dict.fromkeys([*a, *b]))


async def merge(role: str, keep_id: str, merge_ids: list[str], by: str) -> CounterpartyRecord:
    store = await directory.get_directory()
    keep = await store.get(keep_id)
    if keep is None or keep.role != role:
        raise LookupError(f"{keep_id} not in the {role} directory")
    others = []
    for mid in dict.fromkeys(merge_ids):
        if mid == keep_id:
            continue
        rec = await store.get(mid)
        if rec is None or rec.role != role:
            raise LookupError(f"{mid} not in the {role} directory")
        others.append(rec)
    if not others:
        raise ValueError("nothing to merge")
    data = keep.model_dump()
    for o in others:
        for f in ("sources", "hs_codes", "markets", "opportunity_ids", "flags"):
            data[f] = _join(data.get(f) or [], getattr(o, f) or [])
        for f in ("email", "phone", "website", "address", "contact_url", "gstin", "iec", "country"):
            if not data.get(f) and getattr(o, f, None):
                data[f] = getattr(o, f)
        data["notes"] = [*(data.get("notes") or []), *[n.model_dump() for n in o.notes]]
        data["screening_reviews"] = [*(data.get("screening_reviews") or []),
                                     *[s.model_dump() for s in o.screening_reviews]]
        data["times_seen"] = (data.get("times_seen") or 0) + o.times_seen
        if o.first_seen_at < keep.first_seen_at:
            data["first_seen_at"] = o.first_seen_at
        if o.last_seen_at > data["last_seen_at"]:
            data["last_seen_at"] = o.last_seen_at
        if o.do_not_contact and not data.get("do_not_contact"):
            data.update(do_not_contact=True, dnc_reason=o.dnc_reason, dnc_at=o.dnc_at)
        if data.get("validation") is None and o.validation is not None:
            data["validation"] = o.validation.model_dump()
    data["merged_from"] = _join(data.get("merged_from") or [], [o.id for o in others])
    data["aliases"] = _join(data.get("aliases") or [], [o.name for o in others if o.name != keep.name])
    data["merged_at"] = datetime.now(timezone.utc)
    data["merged_by"] = by
    merged = CounterpartyRecord.model_validate(data)
    await store.replace(merged)
    for o in others:
        await store.delete(o.id)
    from shared import audit_log
    await audit_log.record("directory.merge", by, keep_id, {"merged": [o.id for o in others]})
    logger.warning("Directory merge by %s: %s ← %s", by, keep_id, [o.id for o in others])
    return merged
