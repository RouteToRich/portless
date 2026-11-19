"""Destination requirements from the India Trade Portal (A.30) and the importing side's rule changes
(A.31) — todo 23.3.

**Monthly** (``monthly``): ``india-trade-portal-ai`` per catalogue product × market → MFN duty, the
preferential duty under each Indian trade agreement and its rules of origin, SPS / TBT measures,
licensing and border documents, with a hash per section. Stored in Mongo ``destination_requirements``
(one row per 6-digit HS × country, ``source=itp`` with the date read). A section whose hash changed
re-checks the open prospects / deals of that product × market (``destination_requirement_changed``
exception: a person reads what changed before the next step).

**Weekly** (``weekly``): ``destination-rule-watch-ai`` (WTO ePing SPS / TBT notifications, the main
regulators) → each new measure kept with ``source=eping`` / the regulator, and the same re-check.

**Used by**: ``requirements(hs, market)`` — the catalogue's staff entries first (staff always win),
then these rows, each with its source and date; ``preferential(hs, market)`` — the lowest
preferential duty and its agreement: the radar's net gap uses it (``trade_min`` phase 3) and the
shipment then needs the preferential certificate of origin (``export_certs.country_kinds``).
Both jobs are off while their actor is not built (``APIFY_ACTOR_<KEY>_BUILT``) and while
``DESTINATION_RULES_ENABLED`` is off (pinned off in tests).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_rows: dict[str, dict[str, Any]] = {}
_measures: dict[str, dict[str, Any]] = {}
_last: dict[str, datetime] = {}


def enabled() -> bool:
    return os.getenv("DESTINATION_RULES_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _key(hs: str, cc: str) -> str:
    return f"{''.join(ch for ch in str(hs) if ch.isdigit())[:6]}:{(cc or '').upper()}"


def _collection(name: str = "destination_requirements") -> Any:
    try:
        from shared.mongo import collection
        return collection(name)
    except Exception:  # noqa: BLE001
        return None


async def load() -> None:
    col = _collection()
    if col is None:
        return
    try:
        for d in [d async for d in col.find({}, {"_id": 0})]:
            _rows[d["key"]] = d
    except Exception as exc:  # noqa: BLE001
        logger.debug("DestinationRules: load failed: %s", exc)


def changed_sections(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Pure: the sections whose hash differs from the stored row (all of them for a first read)."""
    if old is None:
        return []
    a, b = old.get("hashes") or {}, new.get("hashes") or {}
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


async def apply_itp(row: dict[str, Any]) -> dict[str, Any]:
    key = _key(row.get("hs_code") or "", row.get("country") or "")
    old = _rows.get(key)
    changed = sorted(set(changed_sections(old, row)) | set(row.get("changed") or [])) if old else []
    stored = {**row, "key": key, "source": "itp", "stored_at": datetime.now(timezone.utc).isoformat()}
    _rows[key] = stored
    col = _collection()
    if col is not None:
        try:
            await col.replace_one({"key": key}, dict(stored), upsert=True)
        except Exception:  # noqa: BLE001
            pass
    held = await recheck(row.get("hs_code") or "", row.get("country") or "",
                         f"India Trade Portal: {', '.join(changed)} changed") if changed else []
    return {"key": key, "changed": changed, "deals_rechecked": held}


async def apply_measure(rule: dict[str, Any]) -> dict[str, Any] | None:
    mid = "|".join(str(rule.get(k) or "") for k in ("country", "regulator", "published_on", "measure"))[:300]
    if mid in _measures:
        return None
    row = {**rule, "measure_id": mid, "source": rule.get("regulator") or "eping",
           "stored_at": datetime.now(timezone.utc).isoformat()}
    _measures[mid] = row
    col = _collection("destination_measures")
    if col is not None:
        try:
            await col.replace_one({"measure_id": mid}, dict(row), upsert=True)
        except Exception:  # noqa: BLE001
            pass
    held: list[str] = []
    for hs in rule.get("hs_codes") or []:
        held += await recheck(hs, rule.get("country") or "", f"{row['source']}: {(rule.get('measure') or '')[:160]}")
    return {**row, "deals_rechecked": held}


async def recheck(hs: str, cc: str, note: str) -> list[str]:
    """Open prospects / deals of the product × market get ``destination_requirement_changed``."""
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        return []
    from shared.orchestration.continuations import _publish_exception
    out = []
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status in ("PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED"):
            continue
        if str(getattr(d, "market", "") or "").upper() == cc.upper() and str(getattr(d, "hs_code", ""))[:4] == hs[:4]:
            await _publish_exception(d.deal_id, f"DESTINATION:{cc.upper()}:{hs[:6]}",
                                     f"destination requirement changed — {note}",
                                     {"category": "UNKNOWN", "code": "destination_requirement_changed",
                                      "action_required": "review_destination_requirements"})
            out.append(d.deal_id)
    return out


def requirements(hs: str, market: str) -> list[dict[str, Any]]:
    """Staff entries (catalogue ``import_requirements``) first, then the portal / regulator rows."""
    out: list[dict[str, Any]] = []
    try:
        from shared import catalogue
        line = catalogue.line_for(hs)
        for req in (getattr(line, "import_requirements", None) or []) if line else []:
            out.append({"requirement": str(req), "source": "staff"})
    except Exception:  # noqa: BLE001
        pass
    row = _rows.get(_key(hs, market))
    if row:
        for section in ("sps", "tbt", "other_ntm", "documents"):
            for item in row.get(section) or []:
                out.append({"requirement": str(item), "source": "itp", "section": section, "date": row.get("read_on")})
        if row.get("licensing"):
            out.append({"requirement": str(row["licensing"]), "source": "itp", "section": "licensing", "date": row.get("read_on")})
    for m in _measures.values():
        if (m.get("country") or "").upper() == (market or "").upper() and any(
                str(h)[:4] == str(hs)[:4] for h in m.get("hs_codes") or []):
            out.append({"requirement": m.get("measure"), "source": m["source"], "section": m.get("measure_type"),
                        "date": m.get("published_on")})
    return out


def preferential(hs: str, market: str) -> dict[str, Any] | None:
    """The lowest preferential duty India pays there, with its agreement (None when unknown)."""
    row = _rows.get(_key(hs, market))
    prefs = [p for p in (row or {}).get("preferential") or [] if p.get("rate_pct") is not None]
    if not prefs:
        return None
    best = min(prefs, key=lambda p: p["rate_pct"])
    return {**best, "mfn_pct": (row or {}).get("mfn_pct"), "source": "itp", "read_on": (row or {}).get("read_on")}


async def monthly(now: datetime | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    if _last.get("itp") and now - _last["itp"] < timedelta(days=30):
        return None
    _last["itp"] = now
    from shared import catalogue
    from shared.connectors.registry import get_connector
    lines = catalogue.active_lines()
    hs_codes = sorted({str(getattr(line, "hs_code", ""))[:6] for line in lines if getattr(line, "hs_code", "")})
    countries = sorted({m for line in lines for m in (getattr(line, "markets", None) or [])})
    res = await get_connector("apify_india_trade_portal").quote({"hs_codes": hs_codes, "countries": countries})
    if res.get("stub"):
        return {"stub": True}
    return {"applied": [await apply_itp(r) for r in res.get("requirements", [])]}


async def weekly(now: datetime | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    if _last.get("eping") and now - _last["eping"] < timedelta(days=7):
        return None
    since = (_last.get("eping") or now - timedelta(days=7)).date().isoformat()
    _last["eping"] = now
    from shared import catalogue
    from shared.connectors.registry import get_connector
    lines = catalogue.active_lines()
    res = await get_connector("apify_destination_rules").quote({
        "countries": sorted({m for line in lines for m in (getattr(line, "markets", None) or [])}),
        "hs_codes": sorted({str(getattr(line, "hs_code", ""))[:6] for line in lines}), "since": since})
    if res.get("stub"):
        return {"stub": True}
    return {"new": [m for m in [await apply_measure(r) for r in res.get("rules", [])] if m]}


def reset_for_tests() -> None:
    _rows.clear()
    _measures.clear()
    _last.clear()
