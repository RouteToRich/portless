"""Registration numbers checked at their source (A.34 ``india-exporter-registries-ai`` lookup mode) —
todo 23.3 / 14.8.

**PortLess's own** (``monthly``): every held registration the actor can look up (FSSAI on FoSCoS,
APEDA RCMC, Spices Board CRES, MPEDA, Tea / Coffee Board) is checked once a month; the source's
``validUntil`` replaces the stored expiry (``registrations.record(..., source="registry")``), and an
``expired`` / ``suspended`` / ``not_found`` answer pages Telegram — the trade check already blocks a PI
on a lapsed registration.

**A supplier's** (``check_supplier``): when a supplier is added to a prospect, the certificate numbers
staff entered (``manual.fssai`` / ``manual.apeda``) are looked up; the result is kept on the directory
record (``flags``: ``certificate_active:<kind>`` / ``certificate_problem:<kind>``) for the supplier's
trust and the shipment certificates.

Nothing runs while the actor is not built (``APIFY_ACTOR_INDIA_EXPORTER_REGISTRIES_BUILT``) or while
``REGISTRATION_CHECKS_ENABLED`` is off (pinned off in tests).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

LOOKUP_KIND = {"fssai": "fssai", "apeda_rcmc": "apeda_rcmc", "spices_board_cres": "spices_board_cres",
               "mpeda": "mpeda", "tea_board": "tea_board", "coffee_board": "coffee_board"}
"""PortLess registration kind → the actor's lookup kind."""
PROBLEM = ("expired", "suspended", "not_found")
_last: dict[str, datetime] = {}


def enabled() -> bool:
    return os.getenv("REGISTRATION_CHECKS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


async def _lookup(lookups: list[dict[str, str]]) -> list[dict[str, Any]] | None:
    from shared.connectors.registry import get_connector
    res = await get_connector("apify_registry_lookup").quote({"lookups": lookups})
    if res.get("stub"):
        return None
    return res.get("lookups", [])


async def check_own(now: datetime | None = None) -> dict[str, Any]:
    from shared import registrations
    held = await registrations.held()
    lookups = [{"kind": LOOKUP_KIND[k], "number": r["number"]} for k, r in held.items() if k in LOOKUP_KIND and r.get("number")]
    if not lookups:
        return {"checked": 0, "problems": []}
    rows = await _lookup(lookups)
    if rows is None:
        return {"checked": 0, "problems": [], "stub": True}
    by_kind = {LOOKUP_KIND[k]: k for k in held if k in LOOKUP_KIND}
    problems, updated = [], []
    for r in rows:
        kind = by_kind.get(r.get("kind") or "")
        if not kind:
            continue
        if r["status"] in PROBLEM:
            problems.append({"kind": kind, "number": r["number"], "status": r["status"]})
        elif r.get("valid_until") and r["valid_until"] != held[kind].get("expires_on"):
            await registrations.record(kind, r["number"], r["valid_until"], by="registry:A.34", source="registry")
            updated.append(kind)
    if problems:
        try:
            from shared import telegram
            await telegram.send_message("Registration check: " + "; ".join(
                f"{p['kind']} {p['number']} is {p['status']} at its source" for p in problems))
        except Exception:  # noqa: BLE001
            pass
    return {"checked": len(rows), "updated": updated, "problems": problems}


async def monthly(now: datetime | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    if _last.get("own") and now - _last["own"] < timedelta(days=30):
        return None
    _last["own"] = now
    return await check_own(now)


async def check_supplier(rec: Any) -> dict[str, Any]:
    """Look up the supplier's certificate numbers; flags on the directory record. Never raises."""
    if not enabled():
        return {"checked": 0}
    manual = getattr(rec, "manual", None) or {}
    lookups = [{"kind": kind, "number": manual[field]} for field, kind in (("fssai", "fssai"), ("apeda", "apeda_rcmc"))
               if manual.get(field)]
    if not lookups:
        return {"checked": 0}
    try:
        rows = await _lookup(lookups)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RegistrationChecks: supplier lookup failed: %s", exc)
        return {"checked": 0, "error": str(exc)[:200]}
    if rows is None:
        return {"checked": 0, "stub": True}
    flags = [f for f in (rec.flags or []) if not f.startswith(("certificate_active:", "certificate_problem:"))]
    for r in rows:
        flags.append(f"certificate_{'problem' if r['status'] in PROBLEM else 'active' if r['status'] == 'active' else 'unknown'}:{r['kind']}")
    try:
        from services.trade_min.directory import get_directory  # type: ignore[import]
        await (await get_directory()).upsert(rec.model_copy(update={"flags": flags}))
    except Exception as exc:  # noqa: BLE001
        logger.debug("RegistrationChecks: flags not stored for %s: %s", getattr(rec, "id", "?"), exc)
    return {"checked": len(rows), "results": rows, "flags": flags}


def reset_for_tests() -> None:
    _last.clear()
