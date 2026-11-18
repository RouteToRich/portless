"""Bulk actions on selected directory companies, and the CSV export.

``POST /trade-min/directory/{buyers|suppliers}/bulk`` (staff):

- ``check`` — run the counterparty check on the selected companies. Each check is paid Apify
  runs, so without ``confirm=true`` it only answers what it would cost (``runs_estimate``).
- ``do_not_contact`` — put them on the do-not-contact list (reason required).
- ``add_to_prospect`` — add each to an open prospect (``deal_id``), with the same refusals as
  one by one (do-not-contact, failed, no e-mail …); a supplier also gets the RFQ gate.

Every action reports per company what happened. ``GET /trade-min/directory/{role}/export.csv``
writes the filtered, sorted directory as CSV (same filters as the page).
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord

logger = logging.getLogger(__name__)

MAX_SELECTED = 200
RUNS_PER_CHECK = 2
"""Actor runs one company's check can start: sanctions screening + e-mail check (GST / IEC
registries are not Apify runs)."""


async def bulk(role: str, ids: list[str], action: str, *, by: str, reason: str | None = None,
               deal_id: str | None = None, confirm: bool = False) -> dict[str, Any]:
    if not ids:
        raise ValueError("select at least one company")
    if len(ids) > MAX_SELECTED:
        raise ValueError(f"at most {MAX_SELECTED} companies at a time")
    store = await directory.get_directory()
    recs: list[CounterpartyRecord] = []
    missing: list[str] = []
    for rid in dict.fromkeys(ids):
        rec = await store.get(rid)
        if rec is None or rec.role != role:
            missing.append(rid)
        else:
            recs.append(rec)
    results: list[dict[str, Any]] = [{"id": rid, "ok": False, "detail": "not in the directory"} for rid in missing]

    if action == "check":
        estimate = len(recs) * RUNS_PER_CHECK
        if not confirm:
            return {"action": action, "confirm_needed": True, "companies": len(recs), "runs_estimate": estimate,
                    "note": f"up to {estimate} paid Apify runs — send again with confirm=true to run them"}
        from services.trade_min import validation
        for rec in recs:
            try:
                checked = await validation.validate_record(rec)
                await store.set_validation(checked)
                results.append({"id": rec.id, "ok": True, "detail": checked.validation.status if checked.validation else "unchecked"})
            except Exception as exc:  # noqa: BLE001
                results.append({"id": rec.id, "ok": False, "detail": f"check failed: {exc}"})
    elif action == "do_not_contact":
        if not (reason or "").strip():
            raise ValueError("a reason is required")
        now = datetime.now(timezone.utc)
        for rec in recs:
            await store.set_contact_preference(rec.id, {"do_not_contact": True, "dnc_at": now,
                                                       "dnc_reason": f"{reason.strip()} (by {by}, bulk)"})
            results.append({"id": rec.id, "ok": True, "detail": "do not contact"})
    elif action == "add_to_prospect":
        if not deal_id:
            raise ValueError("deal_id is required")
        from services.trade_min import counterparty_profile as cp
        for rec in recs:
            try:
                out = await cp.add_to_prospect(role, rec.id, deal_id)
                results.append({"id": rec.id, "ok": True, "detail": out.get("next", "added")})
            except Exception as exc:  # noqa: BLE001 - refusals are reported per company
                results.append({"id": rec.id, "ok": False, "detail": str(exc)})
    else:
        raise ValueError(f"unknown action {action!r}")
    done = sum(1 for r in results if r["ok"])
    logger.info("Directory bulk %s by %s: %d of %d done", action, by, done, len(results))
    return {"action": action, "done": done, "failed": len(results) - done, "results": results}


COLUMNS = ["id", "role", "name", "country", "email", "phone", "website", "validation", "stage", "score",
           "do_not_contact", "hs_codes", "markets", "source", "first_seen_at", "last_seen_at"]


def to_csv(rows: Iterable[CounterpartyRecord]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in rows:
        rel = r.relationship or {}
        w.writerow([
            r.id, r.role, r.name, r.country or "", r.email or "", r.phone or "", r.website or "",
            r.validation.status if r.validation else "", rel.get("stage", ""), "" if r.score is None else r.score,
            "yes" if r.do_not_contact else "", " ".join(r.hs_codes), " ".join(r.markets), r.source or "",
            r.first_seen_at.isoformat() if r.first_seen_at else "", r.last_seen_at.isoformat() if r.last_seen_at else "",
        ])
    return buf.getvalue()
