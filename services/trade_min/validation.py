"""Scan-time buyer / supplier validation — the Opportunity Radar shows only
counterparties PortLess has checked, not every company a web search returned.

For the top ``TRADE_MIN_VALIDATION_TOP_N`` buyers and suppliers of an
opportunity (best fit with a contact first) it runs, in one batch each:

  1. sanctions / LEI / VAT screening  ``counterparty-check-ai``   → clear / review / block
  2. e-mail deliverability             ``email-list-qualifier-ai`` → only for addresses the
                                       company search could not check itself
  3. Indian registries (suppliers)     GSTN + DGFT IEC (API Setu) when the number is known;
                                       the DGFT denied list is a stub today → ``unchecked``

and writes a ``CounterpartyValidation`` on the ``BuyerSignal`` / ``SellerCandidate``
(the directory record keeps it). A result younger than
``TRADE_MIN_VALIDATION_MAX_AGE_DAYS`` on the directory record is reused, so a
company is not paid for twice in a month.

The verdict (``decide``) is deliberately strict about what it calls verified and
lenient about what it calls failed: only a positive answer verifies, only a
negative answer fails, and anything not checked stays ``unverified``. Without an
``APIFY_TOKEN`` every counterparty is ``unverified`` — never a made-up pass.

This is the first line of checks, not the last: the deal still re-screens the
buyer before the PI and verifies the supplier's GST / IEC at quote intake.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .schemas import BuyerSignal, CounterpartyRecord, CounterpartyValidation, SellerCandidate

logger = logging.getLogger(__name__)

_EMAIL_CHECKED = ("ok", "risky", "invalid")
_CONTACTABLE = ("ok", "risky")


def _norm(value: str | None) -> str:
    return " ".join((value or "").lower().replace(",", " ").split())


@dataclass
class _Subject:
    """One counterparty to check, whatever record it came from."""

    role: str
    name: str
    country: str | None
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    website: str | None = None
    gstin: str | None = None
    iec: str | None = None
    target: Any = None
    """The BuyerSignal / SellerCandidate / CounterpartyRecord the result is written to."""
    v: CounterpartyValidation = field(default_factory=CounterpartyValidation)

    @property
    def indian_supplier(self) -> bool:
        return self.role == "supplier" and (self.country or "IN").upper() == "IN"


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def decide(v: CounterpartyValidation, *, role: str, country: str | None,
           has_phone: bool = False) -> CounterpartyValidation:
    """Status + reasons from the individual checks (pure)."""
    reasons: list[str] = []
    failed = False
    if v.sanctions == "block":
        failed = True
        reasons.append("sanctions screening: blocked" + (f" ({v.sanctions_reason})" if v.sanctions_reason else ""))
    elif v.sanctions == "review":
        reasons.append("sanctions screening needs a person (possible match or a list was unavailable)")
    elif v.sanctions != "clear":
        reasons.append("sanctions screening not run")

    if v.email in _CONTACTABLE:
        if v.email == "risky":
            reasons.append("e-mail domain accepts every address (catch-all) — delivery not guaranteed")
    elif v.email == "invalid":
        reasons.append("e-mail undeliverable" + (" — phone only" if has_phone else ""))
    elif v.email == "missing":
        reasons.append("no e-mail address found")
    else:
        reasons.append("e-mail not checked")

    if role == "supplier" and (country or "IN").upper() == "IN":
        if v.gst == "inactive":
            failed = True
            reasons.append("GST registration is not active")
        elif v.gst == "missing":
            reasons.append("GSTIN not known yet — asked in the RFQ and verified at quote")
        elif v.gst == "unchecked":
            reasons.append("GST registry not reachable — verified again at quote")
        if v.iec == "inactive":
            failed = True
            reasons.append("IEC (exporter code) is not active")
        elif v.iec == "unchecked":
            reasons.append("IEC registry not reachable — verified again at quote")
        if v.dgft_denied == "denied":
            failed = True
            reasons.append("on the DGFT denied-entity list")

    if failed:
        status = "failed"
    elif v.sanctions == "clear" and v.email in _CONTACTABLE:
        status = "verified"
    else:
        status = "unverified"
    return v.model_copy(update={"status": status, "reasons": reasons})


def is_fresh(v: CounterpartyValidation | None, now: datetime | None = None) -> bool:
    """A stored result that actually checked something and is young enough to reuse."""
    if v is None or v.checked_at is None or v.sanctions == "unchecked":
        return False
    now = now or datetime.now(timezone.utc)
    checked = v.checked_at if v.checked_at.tzinfo else v.checked_at.replace(tzinfo=timezone.utc)
    return now - checked <= timedelta(days=max(0, settings.trade_min_validation_max_age_days))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _connector(name: str):
    try:
        from shared.connectors.registry import get_connector
        return get_connector(name)
    except Exception as exc:  # noqa: BLE001 — missing key / unknown connector → not checked
        logger.debug("Validation: connector %s unavailable: %s", name, exc)
        return None


async def _screen(subjects: list[_Subject]) -> None:
    conn = _connector("apify_counterparty_check")
    if conn is None or not subjects:
        return
    try:
        result = await conn.quote({"counterparties": [{"name": s.name, "country": s.country} for s in subjects]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Validation: screening failed: %s", exc)
        return
    if result.get("stub"):
        return
    rows = {_norm(r.get("name")): r for r in result.get("results", []) if r.get("name")}
    for s in subjects:
        row = rows.get(_norm(s.name))
        if row is None:
            continue
        s.v.sanctions = row.get("verdict") or "review"
        s.v.sanctions_reason = (row.get("reason") or "")[:300] or None
        s.v.evidence_url = row.get("evidence_url")


async def _qualify(subjects: list[_Subject]) -> None:
    for s in subjects:
        if not s.email:
            s.v.email = "missing"
        elif (s.email_status or "") in _EMAIL_CHECKED:
            s.v.email = s.email_status
        else:
            s.v.email = "unverified"
    todo = [s for s in subjects if s.v.email == "unverified"]
    conn = _connector("apify_email_qualifier") if todo else None
    if conn is None:
        return
    try:
        result = await conn.quote({"emails": [s.email for s in todo]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Validation: e-mail check failed: %s", exc)
        return
    if result.get("stub"):
        return
    rows = {str(r.get("email") or "").lower(): r for r in result.get("emails", [])}
    for s in todo:
        row = rows.get((s.email or "").lower())
        if row and row.get("status") in _EMAIL_CHECKED:
            s.v.email = row["status"]


async def _registry_status(name: str, method: str, request: dict) -> dict | None:
    conn = _connector(name)
    if conn is None:
        return None
    try:
        return await getattr(conn, method)(request)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Validation: %s.%s failed: %s", name, method, exc)
        return None


async def _registries(s: _Subject) -> None:
    if not s.indian_supplier:
        return
    gst_r, iec_r, denied_r = await asyncio.gather(
        _registry_status("gstn", "quote", {"gstin": s.gstin}) if s.gstin else _none(),
        _registry_status("dgft", "quote", {"iec": s.iec}) if s.iec else _none(),
        _registry_status("dgft", "book", {"supplier_id": s.name, "iec": s.iec}) if s.iec else _none(),
    )
    s.v.gst = _registry_word(gst_r) if s.gstin else "missing"
    if s.iec:
        s.v.iec = _registry_word(iec_r)
        # The DGFT denied list is a stub that always says "not denied" — that is no answer.
        if denied_r is None or "stub" in str(denied_r.get("note", "")).lower():
            s.v.dgft_denied = "unchecked"
        else:
            s.v.dgft_denied = "denied" if denied_r.get("on_denied_list") else "clear"


async def _none() -> None:
    return None


def _registry_word(result: dict | None) -> str:
    if not result:
        return "unchecked"
    status = str(result.get("status") or "").upper()
    if status == "ACTIVE":
        return "active"
    if status in ("", "UNKNOWN"):
        return "unchecked"
    return "inactive"


async def _run(subjects: list[_Subject], now: datetime) -> None:
    await asyncio.gather(_screen(subjects), _qualify(subjects), *(_registries(s) for s in subjects))
    for s in subjects:
        s.v.checked_at = now
        s.target.validation = decide(s.v, role=s.role, country=s.country, has_phone=bool(s.phone))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _buyer_subject(b: BuyerSignal) -> _Subject:
    return _Subject(role="buyer", name=b.buyer_name, country=b.buyer_country, email=b.email,
                    email_status=b.email_status, phone=b.phone, website=b.website, target=b)


def _seller_subject(s: SellerCandidate) -> _Subject:
    return _Subject(role="supplier", name=s.company_name, country=s.country, email=s.email,
                    email_status=s.email_status, phone=s.phone, website=s.website,
                    gstin=s.gstin, iec=s.iec, target=s)


def _rank(item: Any) -> tuple:
    has_contact = bool(getattr(item, "email", None) or getattr(item, "phone", None))
    return (not has_contact, -(getattr(item, "fit_score", None) or 0))


async def validate_scan(buyers: list[BuyerSignal], sellers: list[SellerCandidate],
                        *, now: datetime | None = None) -> dict[str, int]:
    """Check the top N buyers and suppliers of one opportunity in place.

    Returns counts per status (``verified`` / ``unverified`` / ``failed`` /
    ``reused``). Never raises — a failure leaves the counterparties unchecked.
    """
    counts = {"verified": 0, "unverified": 0, "failed": 0, "reused": 0}
    if not settings.trade_min_validation_enabled:
        return counts
    now = now or datetime.now(timezone.utc)
    top_n = max(0, settings.trade_min_validation_top_n)
    try:
        from . import directory
        store = await directory.get_directory()
        picked: list[_Subject] = []
        for items, build, role, name_of in (
            (buyers, _buyer_subject, "buyer", lambda x: x.buyer_name),
            (sellers, _seller_subject, "supplier", lambda x: x.company_name),
        ):
            ranked = [x for x in sorted(items, key=_rank) if (name_of(x) or "").strip()][:top_n]
            for item in ranked:
                old = await store.get(directory.record_key(role, name_of(item), item.website))
                if old is not None and is_fresh(old.validation, now):
                    item.validation = old.validation
                    counts["reused"] += 1
                else:
                    picked.append(build(item))
        if picked:
            await _run(picked, now)
        for item in [*buyers, *sellers]:
            if item.validation is not None:
                counts[item.validation.status] += 1
    except Exception as exc:  # noqa: BLE001 — validation must never break a scan
        logger.error("Validation: scan check failed: %s", exc)
    return counts


async def validate_record(rec: CounterpartyRecord) -> CounterpartyRecord:
    """Check one directory record now (staff, ``POST /trade-min/{buyers|suppliers}/{id}/validate``)."""
    subject = _Subject(role=rec.role, name=rec.name, country=rec.country, email=rec.email,
                       email_status=(rec.validation.email if rec.validation else None),
                       phone=rec.phone, website=rec.website, gstin=rec.gstin, iec=rec.iec, target=rec)
    await _run([subject], datetime.now(timezone.utc))
    return rec


def status_of(item: Any) -> str:
    """``verified`` / ``unverified`` / ``failed`` for any record with an optional ``validation``."""
    v = getattr(item, "validation", None)
    if v is None and isinstance(item, dict):
        v = item.get("validation")
        return (v or {}).get("status", "unverified") if isinstance(v, dict) else "unverified"
    return getattr(v, "status", None) or "unverified"
