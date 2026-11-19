"""Country risk from direct feeds + actor A.32 → a tier proposal per country (todo 23.3 / 24).

Plain connectors, no Apify run, each read from its official file (URL settings; empty → skipped):

- **UN consolidated list** (``UN_SANCTIONS_XML_URL``, default the UN XML): the country regimes from the
  ``REFERENCE_NUMBER`` prefixes (``KPi.``, ``IRi.``, ``LYi.`` …) — company screening stays in
  ``counterparty-check-ai``;
- **OFAC** SDN list (``OFAC_SDN_CSV_URL``): the programme column → country-wide programmes
  (``CUBA``, ``IRAN``, ``DPRK``, ``SYRIA`` …);
- **UK OFSI** consolidated list (``UK_SANCTIONS_CSV_URL``): the ``Regime`` column (names → ISO-2);
- **FATF** "high-risk" (``FATF_BLACK_URL``) and "increased monitoring" (``FATF_GREY_URL``) pages: the
  countries named on each (``shared.country_names``);
- **OECD** country risk classification (``OECD_RISK_CSV_URL``: country, category 0–7);
- **A.32** ``country-risk-feed-ai`` (once built): ECGC rating, escrow providers' supported countries,
  attestation rules — written straight to the profile (ECGC through its own suggestion, ``country_profiles``).

``propose`` turns them into a tier per country: a comprehensive sanctions programme or the FATF
black list → ``blocked`` (applied **at once**, audited, open deals re-checked); FATF grey or OECD 6–7 →
``review``; nothing else is ever raised automatically — a proposal above the current tier is stored as
``tier_suggestion`` for a compliance officer. ``daily`` (deal watcher, ``COUNTRY_FEEDS_ENABLED`` —
pinned off in tests) runs it once a day.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

UN_REGIMES = {"KP": "KP", "IR": "IR", "IQ": "IQ", "LY": "LY", "SO": "SO", "SS": "SS", "SD": "SD", "CF": "CF",
              "CD": "CD", "YE": "YE", "ML": "ML", "HT": "HT", "GB": None, "QD": None, "TA": None}
"""UN REFERENCE_NUMBER prefix → country (ISIL / Al-Qaida ``QD`` and the Taliban ``TA`` are not countries)."""
COMPREHENSIVE = {"KP", "IR", "SY", "CU"}
"""Countries under country-wide (comprehensive) programmes — blocked; others are targeted (review)."""
OFAC_PROGRAMMES = {"CUBA": "CU", "IRAN": "IR", "IFSR": "IR", "DPRK": "KP", "DPRK2": "KP", "DPRK3": "KP", "SYRIA": "SY",
                   "RUSSIA-EO14024": "RU", "BELARUS": "BY", "VENEZUELA": "VE", "MYANMAR": "MM", "BURMA": "MM",
                   "SOMALIA": "SO", "SUDAN": "SD", "SOUTH SUDAN": "SS", "LIBYA": "LY", "YEMEN": "YE", "CAR": "CF",
                   "DRC": "CD", "IRAQ": "IQ", "LEBANON": "LB", "MALI": "ML", "NICARAGUA": "NI", "ZIMBABWE": "ZW"}
_last: dict[str, str] = {}


def enabled() -> bool:
    return os.getenv("COUNTRY_FEEDS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def parse_un_xml(text: str) -> set[str]:
    """Pure: the countries with a UN sanctions regime (from the entries' reference numbers)."""
    out = set()
    for prefix in re.findall(r"<REFERENCE_NUMBER>\s*([A-Z]{2})[a-z]\.", text or ""):
        cc = UN_REGIMES.get(prefix)
        if cc:
            out.add(cc)
    return out


def parse_ofac_csv(text: str) -> set[str]:
    """Pure: countries with an OFAC programme in the SDN list (programme column, e.g. ``CUBA] [IRAN``)."""
    out = set()
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) < 4:
            continue
        for tag in re.findall(r"[A-Z][A-Z0-9 \-]+", row[3].upper()):
            cc = OFAC_PROGRAMMES.get(tag.strip())
            if cc:
                out.add(cc)
    return out


def parse_uk_csv(text: str) -> set[str]:
    """Pure: countries named in the UK consolidated list's ``Regime`` column."""
    from shared.country_names import find_all
    reader = csv.DictReader(io.StringIO(text or ""))
    col = next((c for c in reader.fieldnames or [] if c and c.strip().lower() in ("regime", "regime name")), None)
    if col is None:
        return set()
    out = set()
    for row in reader:
        out.update(find_all(row.get(col) or ""))
    return out


def parse_fatf(html: str) -> set[str]:
    """Pure: the countries a FATF list page names (tags stripped)."""
    from shared.country_names import find_all
    return set(find_all(re.sub(r"<[^>]+>", " ", html or "")))


def parse_oecd_csv(text: str) -> dict[str, int]:
    """Pure: ISO-2 → OECD country risk category (0–7) from a two-column CSV (country, category)."""
    from shared.country_names import iso2
    out = {}
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) < 2:
            continue
        cc = iso2(row[0])
        try:
            cat = int(str(row[1]).strip())
        except ValueError:
            continue
        if cc and 0 <= cat <= 7:
            out[cc] = cat
    return out


def propose(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pure: ISO-2 → {tier, reasons} from the feeds' facts (only countries with a finding)."""
    rank = {"standard": 1, "review": 2, "blocked": 3}
    out: dict[str, dict[str, Any]] = {}

    def add(cc: str, tier: str, why: str) -> None:
        cur = out.setdefault(cc, {"tier": tier, "reasons": []})
        if rank[tier] > rank[cur["tier"]]:
            cur["tier"] = tier
        cur["reasons"].append(why)

    for source in ("un", "ofac", "uk"):
        for cc in facts.get(source) or set():
            add(cc, "blocked" if cc in COMPREHENSIVE else "review", f"{source.upper()} sanctions programme")
    for cc in facts.get("fatf_black") or set():
        add(cc, "blocked", "FATF call for action (black list)")
    for cc in facts.get("fatf_grey") or set():
        add(cc, "review", "FATF increased monitoring (grey list)")
    for cc, cat in (facts.get("oecd") or {}).items():
        if cat >= 6:
            add(cc, "review", f"OECD country risk category {cat}")
    return out


async def _get(client: Any, env: str, default: str = "") -> str | None:
    url = os.getenv(env, default).strip()
    if not url:
        return None
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("CountryFeeds: %s unreadable: %s", env, exc)
        return None


async def read_feeds(*, transport: Any = None) -> dict[str, Any]:
    import httpx
    facts: dict[str, Any] = {"read": [], "failed": []}
    async with httpx.AsyncClient(timeout=60.0, transport=transport, follow_redirects=True) as client:
        for key, env, parser, default in (
                ("un", "UN_SANCTIONS_XML_URL", parse_un_xml, "https://scsanctions.un.org/resources/xml/en/consolidated.xml"),
                ("ofac", "OFAC_SDN_CSV_URL", parse_ofac_csv, ""), ("uk", "UK_SANCTIONS_CSV_URL", parse_uk_csv, ""),
                ("fatf_black", "FATF_BLACK_URL", parse_fatf, ""), ("fatf_grey", "FATF_GREY_URL", parse_fatf, ""),
                ("oecd", "OECD_RISK_CSV_URL", parse_oecd_csv, "")):
            text = await _get(client, env, default)
            if text is None:
                if os.getenv(env, default).strip():
                    facts["failed"].append(key)
                continue
            facts[key] = parser(text)
            facts["read"].append(key)
    return facts


async def apply(proposals: dict[str, dict[str, Any]], *, by: str = "country_feeds") -> dict[str, Any]:
    """A move to ``blocked`` applies at once; anything else above the current tier is a suggestion."""
    from shared import country_profiles
    rank = {"preferred": 0, "standard": 1, "review": 2, "blocked": 3}
    applied, suggested = [], []
    for cc, p in proposals.items():
        current = country_profiles.tier(cc)
        if p["tier"] not in ("review", "blocked") or rank[p["tier"]] <= rank.get(current, 1):
            continue
        if p["tier"] == "blocked":
            await country_profiles.put(cc, {"tier": "blocked"}, by=by, source="; ".join(p["reasons"])[:200])
            applied.append(cc)
        else:
            await country_profiles.put(cc, {"tier_suggestion": p["tier"]}, by=by, source="; ".join(p["reasons"])[:200])
            suggested.append(cc)
    return {"blocked": applied, "suggested": suggested}


async def apply_a32(*, by: str = "country_risk_feed_ai") -> dict[str, Any]:
    """A.32 rows → ECGC rating, escrow providers, attestation documents on the profiles."""
    from shared import country_profiles
    from shared.connectors.registry import get_connector
    res = await get_connector("apify_country_risk").quote({})
    if res.get("stub"):
        return {"stub": True}
    changed = []
    for r in res.get("countries", []):
        cc = r.get("country")
        if not cc:
            continue
        fields: dict[str, Any] = {}
        if r.get("ecgc_rating"):
            fields["ecgc_rating"] = r["ecgc_rating"]
        if r.get("escrow_providers"):
            fields["escrow_providers"] = r["escrow_providers"]
        if r.get("attestation_documents"):
            p = country_profiles.get(cc)
            fields["documents"] = sorted(set((p.documents if p else []) + r["attestation_documents"]))
        if fields:
            await country_profiles.put(cc, fields, by=by, source=f"A.32 {r.get('fetched_at') or ''}".strip())
            changed.append(cc)
    return {"changed": changed}


async def run(*, transport: Any = None) -> dict[str, Any]:
    facts = await read_feeds(transport=transport)
    proposals = propose(facts)
    out = {"read": facts["read"], "failed": facts["failed"], "proposals": proposals, **(await apply(proposals))}
    out["a32"] = await apply_a32()
    return out


async def daily(now: datetime | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    day = (now or datetime.now(timezone.utc)).date().isoformat()
    if _last.get("day") == day:
        return None
    _last["day"] = day
    return await run()


def reset_for_tests() -> None:
    _last.clear()
