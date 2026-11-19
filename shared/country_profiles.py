"""One profile per market (todo 24) instead of lists spread over the code.

A ``CountryProfile`` holds, per ISO-2 country: the trading **tier** (``preferred`` / ``standard`` /
``review`` / ``blocked``), ECGC rating, FATF and sanctions status, currency and convertibility, the
escrow providers that accept a buyer there, the trade agreement with India and its certificate of
origin form, main ports with transit days, demurrage free days, the documents the country asks
for, the e-mail rule for cold outreach, language, time zone — each with its source and date.

Stored in Mongo ``country_profiles`` (memory without Mongo); ``GET / PUT /deals/compliance/countries``
(compliance officer, audited). A built-in **seed** covers the usual markets; a stored profile
replaces it field by field. Without either, the old settings decide: a country on
``COMPLIANCE_BLOCKED_COUNTRIES`` is ``blocked``, on ``COMPLIANCE_REVIEW_COUNTRIES`` ``review``,
otherwise ``standard``.

A **tier change** re-checks open prospects and deals in that country (``country_tier_changed``
exception per deal, Telegram + digest); ``blocked`` makes the trade check block every PI and money
step there. Risk inputs can be refreshed from a feed (``COUNTRY_RISK_FEED_URL``, JSON
``{"countries": {"AE": {"tier": ..., "fatf": ..., "ecgc_rating": ...}}}``) by the daily watcher.

Seed values are a starting point to confirm (ECGC ratings, provider coverage, rules change).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Tier = Literal["preferred", "standard", "review", "blocked"]
TIERS = ("preferred", "standard", "review", "blocked")
EmailRule = Literal["opt_out", "postal_address", "consent_required", "none"]


class Port(BaseModel):
    locode: str
    name: str | None = None
    transit_days: int | None = None


class CountryProfile(BaseModel):
    iso2: str
    name: str | None = None
    tier: Tier = "standard"
    ecgc_rating: str | None = None
    fatf: str | None = None
    """``clear`` / ``grey`` / ``black``."""
    sanctions: str | None = None
    currency: str | None = None
    convertible: bool | None = None
    escrow_providers: list[str] = Field(default_factory=list)
    """Escrow providers that accept a buyer in this country (empty = not known: assumed served)."""
    trade_agreement: str | None = None
    coo_form: str | None = None
    main_ports: list[Port] = Field(default_factory=list)
    demurrage_free_days: int | None = None
    documents: list[str] = Field(default_factory=list)
    """What the country asks for beyond the standard pack (CoO form, attestation, consular invoice …)."""
    email_rule: EmailRule = "opt_out"
    commercial_wing_email: str | None = None
    """Commercial wing of India's embassy / consulate there — asked for importer lists (gov_leads.py)."""
    tier_suggestion: str | None = None
    """Tier the ECGC rating suggests (``suggest_tier``) when it differs from ``tier`` — staff confirm."""
    language: str | None = None
    timezone: str | None = None
    sources: dict[str, str] = Field(default_factory=dict)
    updated_by: str | None = None
    updated_at: datetime | None = None


_EU = ("DE", "FR", "IT", "ES", "NL", "BE", "AT", "IE", "PT", "FI", "GR", "PL", "SE", "DK")
SEED: dict[str, dict[str, Any]] = {
    "AE": {"name": "United Arab Emirates", "tier": "preferred", "ecgc_rating": "A2", "fatf": "clear", "currency": "AED",
           "convertible": True, "trade_agreement": "India–UAE CEPA", "coo_form": "CEPA certificate of origin",
           "main_ports": [{"locode": "AEJEA", "name": "Jebel Ali", "transit_days": 5}], "demurrage_free_days": 5,
           "documents": ["Certificate of origin under India–UAE CEPA", "Invoice attested by the chamber of commerce"],
           "language": "en", "timezone": "Asia/Dubai"},
    "SA": {"name": "Saudi Arabia", "tier": "preferred", "ecgc_rating": "A2", "fatf": "clear", "currency": "SAR",
           "convertible": True, "main_ports": [{"locode": "SAJED", "name": "Jeddah", "transit_days": 9}],
           "documents": ["Invoice and certificate of origin attested by the chamber of commerce",
                         "SABER / SFDA conformity for food products"], "language": "ar", "timezone": "Asia/Riyadh"},
    "QA": {"name": "Qatar", "tier": "preferred", "ecgc_rating": "A2", "currency": "QAR", "convertible": True,
           "main_ports": [{"locode": "QAHMD", "name": "Hamad", "transit_days": 7}], "language": "ar"},
    "OM": {"name": "Oman", "tier": "preferred", "ecgc_rating": "A2", "currency": "OMR", "convertible": True,
           "main_ports": [{"locode": "OMSOH", "name": "Sohar", "transit_days": 5}], "language": "ar"},
    "SG": {"name": "Singapore", "tier": "preferred", "ecgc_rating": "A1", "fatf": "clear", "currency": "SGD",
           "convertible": True, "trade_agreement": "India–Singapore CECA",
           "main_ports": [{"locode": "SGSIN", "name": "Singapore", "transit_days": 8}], "language": "en"},
    "JP": {"name": "Japan", "tier": "preferred", "ecgc_rating": "A1", "currency": "JPY", "convertible": True,
           "trade_agreement": "India–Japan CEPA", "main_ports": [{"locode": "JPTYO", "name": "Tokyo", "transit_days": 18}],
           "language": "ja"},
    "AU": {"name": "Australia", "tier": "preferred", "ecgc_rating": "A1", "currency": "AUD", "convertible": True,
           "trade_agreement": "India–Australia ECTA", "coo_form": "ECTA certificate of origin",
           "main_ports": [{"locode": "AUSYD", "name": "Sydney", "transit_days": 22}], "email_rule": "opt_out",
           "language": "en"},
    "GB": {"name": "United Kingdom", "tier": "preferred", "ecgc_rating": "A1", "currency": "GBP", "convertible": True,
           "main_ports": [{"locode": "GBFXT", "name": "Felixstowe", "transit_days": 24}], "email_rule": "opt_out",
           "language": "en"},
    "US": {"name": "United States", "tier": "preferred", "ecgc_rating": "A1", "currency": "USD", "convertible": True,
           "main_ports": [{"locode": "USNYC", "name": "New York", "transit_days": 28}], "email_rule": "postal_address",
           "documents": ["FDA prior notice (filed by the importer)"], "language": "en"},
    "CA": {"name": "Canada", "tier": "standard", "ecgc_rating": "A1", "currency": "CAD", "convertible": True,
           "main_ports": [{"locode": "CAVAN", "name": "Vancouver", "transit_days": 30}], "email_rule": "consent_required",
           "language": "en"},
    **{cc: {"tier": "preferred", "ecgc_rating": "A1", "currency": "EUR", "convertible": True, "email_rule": "opt_out"}
       for cc in _EU},
}


def _f_list(name: str, default: tuple[str, ...]) -> set[str]:
    raw = os.getenv(name)
    if raw is None:
        return set(default)
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


def _settings_tier(iso2: str) -> Tier:
    from shared.orchestration import compliance
    if iso2 in _f_list("COMPLIANCE_BLOCKED_COUNTRIES", compliance.BLOCKED_COUNTRIES_DEFAULT):
        return "blocked"
    if iso2 in _f_list("COMPLIANCE_REVIEW_COUNTRIES", compliance.REVIEW_COUNTRIES_DEFAULT):
        return "review"
    return "standard"


_stored: dict[str, dict[str, Any]] = {}
_loaded = False


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("country_profiles")
    except Exception:  # noqa: BLE001
        return None


async def load() -> None:
    """Read the stored profiles into the cache (startup, and after a change elsewhere)."""
    global _loaded
    col = _collection()
    if col is not None:
        try:
            async for d in col.find({}):
                d.pop("_id", None)
                _stored[str(d.get("iso2", "")).upper()] = d
        except Exception as exc:  # noqa: BLE001
            logger.warning("CountryProfiles: unreadable (%s) — seed and settings only", exc)
    _loaded = True


def get(iso2: str | None) -> CountryProfile | None:
    """The profile for a country: stored fields over the seed over the settings (None when unknown)."""
    cc = (iso2 or "").strip().upper()[:2]
    if len(cc) != 2:
        return None
    seeded = SEED.get(cc)
    if not seeded and cc not in _stored and _settings_tier(cc) == "standard":
        return None  # nothing is known about this country (tier() still answers "standard")
    base: dict[str, Any] = {"iso2": cc, "tier": _settings_tier(cc)}
    if seeded:
        base.update(seeded)
        if _settings_tier(cc) in ("blocked", "review"):  # the settings' lists outrank a seed's tier
            base["tier"] = _settings_tier(cc)
    if cc in _stored:
        base.update({k: v for k, v in _stored[cc].items() if v not in (None, "", [])})
    return CountryProfile(**base)


def known(iso2: str | None) -> bool:
    cc = (iso2 or "").strip().upper()[:2]
    return cc in SEED or cc in _stored


def tier(iso2: str | None) -> Tier:
    p = get(iso2)
    return p.tier if p else "standard"


def all_profiles() -> list[CountryProfile]:
    codes = sorted(set(SEED) | set(_stored))
    return [p for p in (get(c) for c in codes) if p]


async def put(iso2: str, fields: dict[str, Any], *, by: str, source: str | None = None) -> dict[str, Any]:
    """Store a profile change (audited). A tier change re-checks open deals in that country."""
    cc = iso2.strip().upper()[:2]
    before = get(cc)
    merged = {**(_stored.get(cc) or {}), **fields, "iso2": cc, "updated_by": by,
              "updated_at": datetime.now(timezone.utc)}
    if source:
        merged["sources"] = {**(merged.get("sources") or {}), **{k: source for k in fields}}
    profile = CountryProfile(**{**(get(cc).model_dump() if get(cc) else {}), **merged})
    _stored[cc] = profile.model_dump(mode="json", exclude_none=True)
    col = _collection()
    if col is not None:
        try:
            await col.replace_one({"iso2": cc}, {**_stored[cc]}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("CountryProfiles: %s not stored (kept in memory): %s", cc, exc)
    try:
        from shared import audit_log
        await audit_log.record("country_profile_updated", by, cc, {"fields": sorted(fields), "source": source})
    except Exception:  # noqa: BLE001
        pass
    changed = before is not None and before.tier != profile.tier
    rechecked = await tier_changed(cc, before.tier if before else None, profile.tier) if changed else []
    ecgc_moved = (before is not None and "ecgc_rating" in fields
                  and (before.ecgc_rating or "") != (profile.ecgc_rating or ""))
    if ecgc_moved or ("ecgc_rating" in fields and before is None):
        suggestion = suggest_tier(profile.ecgc_rating)
        update = {"tier_suggestion": suggestion if suggestion and suggestion != profile.tier else None}
        _stored[cc] = {**_stored[cc], **{k: v for k, v in update.items() if v}} if update["tier_suggestion"] else {
            k: v for k, v in _stored[cc].items() if k != "tier_suggestion"}
        if col is not None:
            try:
                await col.replace_one({"iso2": cc}, {**_stored[cc]}, upsert=True)
            except Exception:  # noqa: BLE001
                pass
        if ecgc_moved:
            rechecked += await ecgc_changed(cc, before.ecgc_rating, profile.ecgc_rating, suggestion, profile.tier)
        profile = get(cc) or profile
    return {"profile": profile.model_dump(mode="json"), "tier_changed": changed, "deals_rechecked": rechecked}


# ECGC country risk categories (A1 lowest risk … D highest) → the tier they allow at most.
ECGC_TIER: dict[str, str] = {"A1": "preferred", "A2": "preferred", "B1": "standard", "B2": "standard",
                             "C1": "review", "C2": "review", "D": "blocked"}


def suggest_tier(rating: str | None) -> str | None:
    """Pure: the tier an ECGC rating suggests (A1 / A2 preferred allowed, B standard, C review, D blocked)."""
    return ECGC_TIER.get(str(rating or "").strip().upper())


async def ecgc_changed(iso2: str, old: str | None, new: str | None, suggestion: str | None, tier: str) -> list[str]:
    """ECGC re-rated a country: ``COUNTRY:<cc>:ecgc_changed`` on its open deals and a Telegram page.
    The tier itself changes only when staff confirm it (the audited ``put``)."""
    out: list[str] = []
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        deals = []
    from shared.orchestration.continuations import _publish_exception
    note = f"ECGC rating of {iso2} moved {old or 'none'} → {new or 'none'}"
    if suggestion and suggestion != tier:
        note += f"; it suggests tier {suggestion} (now {tier}) — confirm on the country profile"
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status in ("PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED"):
            continue
        countries = {str(getattr(d, "market", "") or "").upper()} | {
            str(getattr(p, "country", "") or "").upper() for p in getattr(d, "parties", None) or []}
        if iso2 in countries:
            await _publish_exception(d.deal_id, f"COUNTRY:{iso2}:ecgc_changed", note,
                                     {"category": "UNKNOWN", "country": iso2, "old_rating": old, "new_rating": new,
                                      "tier_suggestion": suggestion, "action_required": "review_country_change"})
            out.append(d.deal_id)
    try:
        from shared import telegram
        await telegram.send_message(f"{note}; {len(out)} open deal(s) flagged")
    except Exception:  # noqa: BLE001
        pass
    return out


async def tier_changed(iso2: str, old: str | None, new: str) -> list[str]:
    """Every open prospect / deal whose market or party is in the country gets a
    ``country_tier_changed`` exception (the trade check then re-runs at its next step)."""
    out: list[str] = []
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        return out
    from shared.orchestration.continuations import _publish_exception
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status in ("PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED"):
            continue
        countries = {str(getattr(d, "market", "") or "").upper()} | {
            str(getattr(p, "country", "") or "").upper() for p in getattr(d, "parties", None) or []}
        if iso2 in countries:
            await _publish_exception(d.deal_id, f"COUNTRY:{iso2}:tier_changed",
                                     f"{iso2} moved from {old} to {new} — the trade check re-runs before the next step",
                                     {"category": "UNKNOWN", "country": iso2, "old_tier": old, "new_tier": new,
                                      "action_required": "review_country_change"})
            out.append(d.deal_id)
    try:
        from shared import telegram
        await telegram.send_message(f"Country {iso2}: tier {old} → {new}; {len(out)} open deal(s) re-checked")
    except Exception:  # noqa: BLE001
        pass
    return out


async def refresh_from_feed(*, transport: Any = None, by: str = "country_risk_feed") -> dict[str, Any]:
    """Apply a published country-risk feed (``COUNTRY_RISK_FEED_URL``): tier, FATF, ECGC rating,
    sanctions. Only listed fields change; every change goes through ``put`` (audited, re-checks)."""
    url = os.getenv("COUNTRY_RISK_FEED_URL", "").strip()
    if not url:
        return {"ok": False, "reason": "COUNTRY_RISK_FEED_URL is not set", "changed": []}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("CountryProfiles: risk feed unreachable: %s", exc)
        return {"ok": False, "reason": f"feed unreachable: {exc}", "changed": []}
    rows = body.get("countries") if isinstance(body, dict) else None
    if not isinstance(rows, dict):
        return {"ok": False, "reason": "feed has no countries", "changed": []}
    changed = []
    for cc, data in rows.items():
        if not isinstance(data, dict):
            continue
        fields = {k: v for k, v in data.items() if k in ("tier", "fatf", "ecgc_rating", "sanctions") and v}
        if fields.get("tier") not in (None, *TIERS):
            fields.pop("tier")
        current = get(cc)
        diff = {k: v for k, v in fields.items() if current is None or getattr(current, k) != v}
        if diff:
            await put(cc, diff, by=by, source=f"feed {body.get('date') or ''}".strip())
            changed.append(str(cc).upper())
    return {"ok": True, "changed": changed}


# ── what other modules ask ─────────────────────────────────────────────────

def escrow_serves(iso2: str | None, providers: list[str] | None = None) -> bool:
    """Does at least one enabled escrow provider accept a buyer from this country? An unknown
    list counts as served (the provider says so when the buyer pays)."""
    p = get(iso2)
    if p is None or not p.escrow_providers:
        return True
    enabled = providers
    if enabled is None:
        enabled = [x.strip() for x in os.getenv("ESCROW_PROVIDERS_ENABLED", "tazapay,escrow_com").split(",") if x.strip()]
    return any(x in p.escrow_providers for x in enabled)


def transit_days(iso2: str | None) -> int | None:
    p = get(iso2)
    days = [port.transit_days for port in (p.main_ports if p else []) if port.transit_days]
    return min(days) if days else None


def documents(iso2: str | None) -> list[str]:
    p = get(iso2)
    out = list(p.documents) if p else []
    if p and p.coo_form and not any("certificate of origin" in d.lower() for d in out):
        out.insert(0, p.coo_form)
    return out


def reset_for_tests() -> None:
    global _loaded
    _stored.clear()
    _loaded = False
