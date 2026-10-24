"""Counterparty enrichment — turn a buyer/supplier *name* into a party PortLess
can actually write to and is allowed to trade with.

Runs on the PortLess-owned Apify actors (``shared/connectors/apify.py``):

  1. contact finder    company name + country → website, checked e-mail,
                       phone, address, VAT id            (company-contact-finder-ai)
  2. e-mail qualifier  deliverability of the chosen address, when the contact
                       finder could not verify it         (email-list-qualifier-ai)
  3. screening         OFAC / UN / EU / UK sanctions, GLEIF LEI, EU VIES →
                       ``clear`` / ``review`` / ``block``  (counterparty-check-ai)

The results land on the deal's ``Party`` records (``email``, ``website``,
``phone``, ``email_status``, ``sanctions_status`` …) so every later leg — the
negotiation agent's first outbound e-mail, the PI, the escrow funding
instructions — addresses a real mailbox, and the pipeline can refuse to issue
a Proforma Invoice to a sanctioned buyer.

Where it runs
-------------
  • ``continuations.on_deal_created`` — before the RFQ gate, best-effort
  • ``continuations.on_negotiation_agreed`` — screening (re)check before the PI
  • ``POST /deals/{id}/enrich`` — operator-triggered from the deal Overview tab

Every step degrades gracefully: without an ``APIFY_TOKEN`` the actors return
empty results and the party is marked ``sanctions_status="unchecked"`` /
``email_status="unverified"`` so the UI shows exactly what is missing.
Operator-entered contacts (``contact_source == "operator"``) are never
overwritten by an actor result.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_ROLES = ("buyer", "supplier")


def _role_of(party: Any) -> str:
    raw = getattr(party, "role", "")
    return (getattr(raw, "value", raw) or "").lower()


def _party(deal: Any, role: str) -> Any | None:
    for p in getattr(deal, "parties", None) or []:
        if _role_of(p) == role:
            return p
    return None


def _connector(name: str):
    try:
        from shared.connectors.registry import get_connector
        return get_connector(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Enrichment: connector %s unavailable: %s", name, exc)
        return None


def _norm_name(value: str | None) -> str:
    return " ".join((value or "").lower().replace(",", " ").split())


async def _find_contacts(parties: list[Any]) -> tuple[dict[str, dict[str, Any]], list[str], str | None]:
    """→ ({normalised legal_name: contact}, warnings, actor ref)."""
    conn = _connector("apify_contact_finder")
    if conn is None:
        return {}, ["contact finder connector unavailable"], None
    companies = [
        {"name": p.legal_name, "country": p.country, "website": getattr(p, "website", None)}
        for p in parties if getattr(p, "legal_name", None)
    ]
    if not companies:
        return {}, [], getattr(conn, "actor_ref", None)
    try:
        result = await conn.quote({"companies": companies})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Enrichment: contact finder failed: %s", exc)
        return {}, [f"contact finder failed: {exc}"], getattr(conn, "actor_ref", None)
    warnings = ["contact finder ran in stub mode (no APIFY_TOKEN)"] if result.get("stub") else []
    out: dict[str, dict[str, Any]] = {}
    for c in result.get("contacts", []):
        key = _norm_name(c.get("input_name") or c.get("name"))
        if key:
            out[key] = c
    return out, warnings, (None if result.get("stub") else result.get("actor"))


async def _qualify_emails(emails: list[str]) -> dict[str, dict[str, Any]]:
    conn = _connector("apify_email_qualifier")
    if conn is None or not emails:
        return {}
    try:
        result = await conn.quote({"emails": emails})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Enrichment: e-mail qualifier failed: %s", exc)
        return {}
    if result.get("stub"):
        return {}
    return {row["email"].lower(): row for row in result.get("emails", []) if row.get("email")}


async def _screen(parties: list[Any]) -> tuple[dict[str, dict[str, Any]], list[str], str | None]:
    conn = _connector("apify_counterparty_check")
    if conn is None:
        return {}, ["counterparty screening connector unavailable"], None
    counterparties = [
        {"name": p.legal_name, "country": p.country,
         "vat_number": getattr(p, "vat_number", None), "lei": getattr(p, "lei", None)}
        for p in parties if getattr(p, "legal_name", None)
    ]
    if not counterparties:
        return {}, [], getattr(conn, "actor_ref", None)
    try:
        result = await conn.quote({"counterparties": counterparties})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Enrichment: counterparty screening failed: %s", exc)
        return {}, [f"counterparty screening failed: {exc}"], getattr(conn, "actor_ref", None)
    warnings = ["counterparty screening ran in stub mode (no APIFY_TOKEN)"] if result.get("stub") else []
    out: dict[str, dict[str, Any]] = {}
    for r in result.get("results", []):
        key = _norm_name(r.get("name"))
        if key:
            out[key] = r
    return out, warnings, (None if result.get("stub") else result.get("actor"))


def _apply_contact(party: Any, contact: dict[str, Any] | None, actor: str | None) -> dict[str, Any]:
    """Field updates for one party from a contact-finder row (never overwrites operator data)."""
    updates: dict[str, Any] = {}
    if contact is None or contact.get("not_found"):
        if not getattr(party, "email", None):
            updates["email_status"] = getattr(party, "email_status", None) or "unverified"
        return updates
    source = f"apify:{(actor or 'company-contact-finder-ai').split('~')[-1]}"
    operator_owned = getattr(party, "contact_source", None) == "operator"
    if not operator_owned:
        if contact.get("email"):
            updates["email"] = contact["email"]
            updates["email_status"] = contact.get("email_status") or "unverified"
        if contact.get("website") and not getattr(party, "website", None):
            updates["website"] = contact["website"]
        if contact.get("phone") and not getattr(party, "phone", None):
            updates["phone"] = contact["phone"]
        if contact.get("address") and not getattr(party, "address", None):
            updates["address"] = contact["address"]
        if contact.get("vat_id") and not getattr(party, "vat_number", None):
            updates["vat_number"] = contact["vat_id"]
        if contact.get("email") or contact.get("website"):
            updates["contact_source"] = source
            updates["contact_confidence"] = contact.get("confidence")
    elif contact.get("website") and not getattr(party, "website", None):
        updates["website"] = contact["website"]
    return updates


def _apply_screening(party: Any, row: dict[str, Any] | None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if row is None:
        return {"sanctions_status": getattr(party, "sanctions_status", None) or "unchecked"}
    updates: dict[str, Any] = {
        "sanctions_status": row.get("verdict") or "review",
        "sanctions_reason": (row.get("reason") or "")[:500] or None,
        "sanctions_checked_at": now,
    }
    if row.get("lei"):
        updates["lei"] = row["lei"]
    if row.get("vat_valid") is not None:
        updates["vat_valid"] = bool(row["vat_valid"])
    if row.get("vat_number") and not getattr(party, "vat_number", None):
        updates["vat_number"] = row["vat_number"]
    if row.get("legal_name") and (getattr(party, "verification_status", None) in (None, "", "unverified")):
        updates["verification_status"] = "registry_match"
    return updates


async def enrich_deal_parties(
    deal_id: str,
    *,
    roles: tuple[str, ...] = _ROLES,
    screen: bool = True,
    contacts: bool = True,
    verify_emails: bool = True,
) -> dict[str, Any]:
    """Enrich the deal's parties in place. Returns an ``EnrichmentResult``-shaped dict.

    Never raises for connector failures (they become ``warnings``); raises
    only when the deal itself cannot be loaded.
    """
    from services.deals.service import get_deal, update_party  # type: ignore[import]

    deal = await get_deal(deal_id)
    parties = [p for p in (getattr(deal, "parties", None) or []) if _role_of(p) in roles]
    actors_used: list[str] = []
    warnings: list[str] = []
    per_role: dict[str, dict[str, Any]] = {}

    if not parties:
        warnings.append("deal has no buyer/supplier parties to enrich")

    contact_rows: dict[str, dict[str, Any]] = {}
    if contacts and parties:
        contact_rows, w, actor = await _find_contacts(parties)
        warnings.extend(w)
        if actor:
            actors_used.append(actor)

    email_rows: dict[str, dict[str, Any]] = {}
    if verify_emails and parties:
        unverified = []
        for p in parties:
            row = contact_rows.get(_norm_name(p.legal_name))
            email = (row or {}).get("email") or getattr(p, "email", None)
            status = (row or {}).get("email_status") or getattr(p, "email_status", None)
            if email and status in (None, "unverified", "risky"):
                unverified.append(email)
        if unverified:
            email_rows = await _qualify_emails(unverified)
            if email_rows:
                conn = _connector("apify_email_qualifier")
                if conn is not None:
                    actors_used.append(getattr(conn, "actor_ref", "apify_email_qualifier"))

    screen_rows: dict[str, dict[str, Any]] = {}
    if screen and parties:
        screen_rows, w, actor = await _screen(parties)
        warnings.extend(w)
        if actor:
            actors_used.append(actor)

    blocked = False
    now = datetime.now(timezone.utc)
    for p in parties:
        role = _role_of(p)
        key = _norm_name(p.legal_name)
        updates: dict[str, Any] = {}
        if contacts:
            updates.update(_apply_contact(p, contact_rows.get(key), actors_used[0] if actors_used else None))
        email = updates.get("email") or getattr(p, "email", None)
        if email and email.lower() in email_rows:
            updates["email_status"] = email_rows[email.lower()].get("status") or updates.get("email_status")
        if screen:
            updates.update(_apply_screening(p, screen_rows.get(key)))
        updates["enriched_at"] = now
        deal = await update_party(deal_id, role, updates, source=None)
        refreshed = _party(deal, role)
        snapshot = {
            "legal_name": getattr(refreshed, "legal_name", None),
            "country": getattr(refreshed, "country", None),
            "email": getattr(refreshed, "email", None),
            "email_status": getattr(refreshed, "email_status", None),
            "website": getattr(refreshed, "website", None),
            "phone": getattr(refreshed, "phone", None),
            "address": getattr(refreshed, "address", None),
            "vat_number": getattr(refreshed, "vat_number", None),
            "vat_valid": getattr(refreshed, "vat_valid", None),
            "lei": getattr(refreshed, "lei", None),
            "contact_source": getattr(refreshed, "contact_source", None),
            "contact_confidence": getattr(refreshed, "contact_confidence", None),
            "sanctions_status": getattr(refreshed, "sanctions_status", None),
            "sanctions_reason": getattr(refreshed, "sanctions_reason", None),
            "screening": screen_rows.get(key),
        }
        per_role[role] = snapshot
        if snapshot["sanctions_status"] == "block":
            blocked = True

    if blocked:
        try:
            from shared.bus import get_publisher
            from shared.domain.events import DomainEvent, EventTopic
            await get_publisher().publish(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION, deal_id=deal_id,
                payload={
                    "source": "enrichment", "stage": "COUNTERPARTY_SCREENING", "category": "APPROVAL_REJECTED",
                    "error": "counterparty screening returned BLOCK — do not trade",
                    "parties": {r: s.get("sanctions_reason") for r, s in per_role.items() if s.get("sanctions_status") == "block"},
                    "action_required": "sanctions_block_review",
                },
            ))
        except Exception:  # noqa: BLE001
            logger.exception("Enrichment: could not emit sanctions block exception for %s", deal_id)

    if per_role:  # nothing screened → nothing to announce
        try:
            from shared.bus import get_publisher
            from shared.domain.events import DomainEvent, EventTopic
            await get_publisher().publish(DomainEvent(
                topic=EventTopic.COUNTERPARTY_SCREENED, deal_id=deal_id,
                payload={
                    "verdicts": {r: s.get("sanctions_status") for r, s in per_role.items()},
                    "actors_used": actors_used, "blocked": blocked,
                },
            ))
        except Exception:  # noqa: BLE001
            logger.debug("Enrichment: screened event not emitted for %s", deal_id)

    logger.info(
        "Enrichment: deal %s → %s (actors=%s, warnings=%d, blocked=%s)",
        deal_id,
        {r: (s.get("email"), s.get("sanctions_status")) for r, s in per_role.items()},
        actors_used, len(warnings), blocked,
    )
    return {
        "deal_id": deal_id,
        "buyer": per_role.get("buyer"),
        "supplier": per_role.get("supplier"),
        "actors_used": sorted(set(actors_used)),
        "warnings": warnings,
        "blocked": blocked,
        "deal": deal,
    }


def screening_verdict(deal: Any, role: str = "buyer") -> str:
    """``clear`` / ``review`` / ``block`` / ``unchecked`` for a deal party."""
    p = _party(deal, role)
    return (getattr(p, "sanctions_status", None) or "unchecked") if p is not None else "unchecked"


def screening_is_fresh(deal: Any, role: str = "buyer", max_age_days: int = 30) -> bool:
    p = _party(deal, role)
    checked = getattr(p, "sanctions_checked_at", None) if p is not None else None
    if checked is None:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - checked).days <= max_age_days
