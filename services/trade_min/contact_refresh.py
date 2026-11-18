"""Refresh a directory company's contacts when it enters a new prospect.

A company found months ago may have changed its mailbox, or its e-mail bounced. Re-running
the contact finder on every scan costs money, so it runs only when the company is added to
a prospect (``counterparty_profile.add_to_prospect``) and only when the contacts are stale:

- the e-mail was found undeliverable (``validation.email == "invalid"``), or
- nothing refreshed them for ``CONTACT_REFRESH_DAYS`` (default 90): the newer of the last
  scan that found the company (``last_seen_at``) and the last refresh (``contacts_refreshed_at``).

A new e-mail replaces the old one and is marked ``unverified`` for the check; phone, website
and address are filled when found. Never raises: without an Apify token (stub) or on any
failure the record is used as it is.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord

logger = logging.getLogger(__name__)


def refresh_days() -> int:
    try:
        return max(1, int(os.getenv("CONTACT_REFRESH_DAYS", "90")))
    except ValueError:
        return 90


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def needs_refresh(rec: CounterpartyRecord, now: datetime | None = None) -> str | None:
    """Why the contacts should be refreshed, or None."""
    now = now or datetime.now(timezone.utc)
    if rec.validation is not None and rec.validation.email == "invalid":
        return "e-mail undeliverable"
    if not rec.email:
        return "no e-mail"
    times = [t for t in (_aware(rec.last_seen_at), _aware(rec.contacts_refreshed_at)) if t]
    if not times or now - max(times) > timedelta(days=refresh_days()):
        return f"contacts older than {refresh_days()} days"
    return None


async def ensure_fresh(rec: CounterpartyRecord, now: datetime | None = None) -> CounterpartyRecord:
    """The record with refreshed contacts when they were stale (stored), else unchanged."""
    reason = needs_refresh(rec, now)
    if reason is None:
        return rec
    try:
        from shared.connectors.registry import get_connector
        conn = get_connector("apify_contact_finder")
        result = await conn.quote({"companies": [{"name": rec.name, "country": rec.country, "website": rec.website}]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("ContactRefresh: %s not refreshed (%s): %s", rec.name, reason, exc)
        return rec
    if result.get("stub") or not result.get("contacts"):
        return rec
    row: dict[str, Any] = result["contacts"][0]
    now = now or datetime.now(timezone.utc)
    fields: dict[str, Any] = {"contacts_refreshed_at": now.isoformat()}
    new_email = (row.get("email") or "").strip() or None
    if new_email and new_email.lower() != (rec.email or "").lower():
        fields["email"] = new_email
        if rec.validation is not None:
            from services.trade_min.validation import decide
            v = rec.validation.model_copy(update={"email": row.get("email_status") or "unverified"})
            fields["validation"] = decide(v, role=rec.role, country=rec.country,
                                          has_phone=bool(row.get("phone") or rec.phone)).model_dump(mode="json")
    for key in ("phone", "website", "address"):
        if row.get(key) and not getattr(rec, key, None):
            fields[key] = row[key]
    store = await directory.get_directory()
    saved = await store.set_contact_preference(rec.id, fields)
    logger.info("ContactRefresh: %s %s refreshed (%s)%s", rec.role, rec.name, reason,
                f" — new e-mail {new_email}" if "email" in fields else "")
    return saved or rec
