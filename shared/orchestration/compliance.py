"""Trade compliance — what may be traded, with whom, and what actually ships.

PortLess is the exporter of record (its IEC / GSTIN on the shipping bill) even
though it never touches the cargo, so it carries the legal risk of whatever is
in the container. Counterparty sanctions screening (``enrichment``) answers
"who"; this module answers the rest, in two checks:

**1. Trade check** (``check_trade``) — at ``deal.created`` and again right
before the Proforma Invoice:

- ``hs_policy``     India's export policy for the HS code (ITC-HS Schedule 2 +
                    the SCOMET dual-use list): prohibited / SCOMET → block,
                    restricted / state-trading / possible dual-use → review,
                    never verified → review once (APPROVE records it Free).
- ``destination``   embargoed destination → block, high-risk → review.
- ``screening``     a party whose sanctions screening never ran is not
                    "clear" (review, ``COMPLIANCE_UNCHECKED_SCREENING``).
- ``supplier``      DGFT denied-entity list → block; company younger than
                    ``COMPLIANCE_MIN_COMPANY_AGE_YEARS`` → review.
- ``related``       buyer and supplier share a company domain / phone /
                    address / name → review (round-tripping).
- ``price``         agreed buyer price or supplier price far from the market
                    (price book, this deal's own prices excluded), or a buyer /
                    supplier spread above ``COMPLIANCE_MAX_SPREAD_RATIO`` →
                    review (over / under-invoicing).

**2. Shipment check** (``check_shipment``) — after QC passes: the inspector
supervises the container stuffing and seals it; container + seal numbers,
loaded quantity and the declared HS code are compared with the PI / PO. Only
then are the QC-stage tranche and the freight booking gated. A bill-of-lading
seal number reported later (``check_seal``) must match the loading seal.

Plus ``check_payer`` at escrow funding — the money must come from the buyer.

Findings hold the deal through ``hold_flags``: every money / commitment gate
of a deal with an open hold is created at L0 with the flags (``_create_gate``
in continuations), whatever the autonomy policy says. A block never reaches a
gate — it raises ``deal.exception``.

The HS and country lists below are a SEED, not the law: DGFT notifications
change (rice, wheat, sugar, onions move in and out of restriction), so every
HS code needs an operator's one-time confirmation against the current ITC-HS
Schedule 2 and SCOMET list (``PUT /deals/compliance/hs-policy/{hs}``).

Storage: Mongo ``compliance_checks`` / ``compliance_hs_policy`` when
``MONGO_URI`` is set, else in memory.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Severity = Literal["block", "review", "info"]

_CHECKS = "compliance_checks"
_HS_POLICY = "compliance_hs_policy"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def enabled() -> bool:
    """Trade check on (default). Off only for dev/test — readiness fails it in production."""
    return _flag("COMPLIANCE_ENABLED", True)


def loading_check_required() -> bool:
    """Supervised loading + seal before freight / the QC tranche (default on)."""
    return _flag("COMPLIANCE_LOADING_CHECK_REQUIRED", True)


def _codes(name: str, default: set[str]) -> set[str]:
    raw = os.getenv(name)
    if raw is None:
        return set(default)
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


# ---------------------------------------------------------------------------
# Seed lists (see module docstring — confirm against DGFT before trading)
# ---------------------------------------------------------------------------

# HS prefix → (status, note). Longest prefix wins.
HS_POLICY_SEED: dict[str, tuple[str, str]] = {
    # Prohibited for export
    "0106": ("prohibited", "live wild animals — Wildlife (Protection) Act / CITES"),
    "0507": ("prohibited", "ivory, tortoise-shell, horns — Wildlife Act / CITES"),
    "4403": ("prohibited", "wood in the rough / logs — export prohibited"),
    # SCOMET / munitions list — needs a SCOMET authorisation per shipment
    "2844": ("scomet", "radioactive elements and isotopes — SCOMET Category 0"),
    "2845": ("scomet", "isotopes / heavy water — SCOMET Category 0"),
    "3601": ("scomet", "propellent powders — munitions list"),
    "3602": ("scomet", "prepared explosives — munitions list"),
    "3603": ("scomet", "detonators / fuses — munitions list"),
    "8401": ("scomet", "nuclear reactors and parts — SCOMET Category 0"),
    "8710": ("scomet", "tanks and armoured fighting vehicles — munitions list"),
    "8806": ("scomet", "unmanned aircraft — SCOMET Category 5B / munitions list"),
    "93": ("scomet", "arms and ammunition — munitions list"),
    # Possible dual use — depends on the exact item / grade
    "2812": ("dual_use", "halides of non-metals — possible CWC / SCOMET precursor"),
    "2920": ("dual_use", "esters of inorganic acids — possible CWC precursor"),
    "2921": ("dual_use", "amine-function compounds — possible CWC precursor"),
    "2930": ("dual_use", "organo-sulphur compounds — possible CWC precursor"),
    "8456": ("dual_use", "laser / plasma machine tools — possible SCOMET Category 3"),
    "8526": ("dual_use", "radar / radio navigation — possible SCOMET item"),
    "9013": ("dual_use", "lasers — possible SCOMET item"),
    # Narcotic / psychotropic — NDPS export authorisation
    "121130": ("restricted", "coca leaf — NDPS Act"),
    "130211": ("restricted", "opium — NDPS Act"),
    "2939": ("restricted", "alkaloids — NDPS / precursor licensing"),
    # Commodities frequently under export restriction — check the current notification
    "0201": ("restricted", "bovine meat — beef prohibited, buffalo meat needs APEDA"),
    "0202": ("restricted", "frozen bovine meat — beef prohibited, buffalo meat needs APEDA"),
    "0703": ("restricted", "onions — export restrictions / MEP notified from time to time"),
    "1001": ("restricted", "wheat — export restricted by DGFT notification"),
    "1006": ("restricted", "rice — non-basmati export restrictions / duty notified from time to time"),
    "1101": ("restricted", "wheat flour — export restricted by DGFT notification"),
    "1701": ("restricted", "sugar — export restricted / quota notified from time to time"),
    "2601": ("state_trading", "iron ore — high-grade ore canalised through state trading"),
    "7102": ("restricted", "diamonds — Kimberley Process certificate required"),
}

_STATUS_SEVERITY: dict[str, Severity] = {
    "prohibited": "block", "scomet": "block",
    "restricted": "review", "state_trading": "review", "dual_use": "review",
    "unverified": "review", "free": "info", "licensed": "info",
}

BLOCKED_COUNTRIES_DEFAULT = {"KP", "IR", "SY", "CU"}
"""Comprehensive UN / US / EU embargoes."""
REVIEW_COUNTRIES_DEFAULT = {
    "RU", "BY", "MM", "VE", "AF", "SD", "SS", "YE", "LY", "SO", "CF", "CD", "IQ",
    "LB", "ML", "NI", "HT", "ZW", "PK",
}
"""Sectoral sanctions, FATF high-risk / increased monitoring, India-specific
trade restrictions (PK). Trade is possible but needs a human look."""

_COUNTRY_NAMES = {
    "NORTH KOREA": "KP", "IRAN": "IR", "SYRIA": "SY", "CUBA": "CU", "RUSSIA": "RU",
    "BELARUS": "BY", "MYANMAR": "MM", "VENEZUELA": "VE", "AFGHANISTAN": "AF",
    "PAKISTAN": "PK", "SUDAN": "SD", "YEMEN": "YE", "LIBYA": "LY",
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class CheckResult(BaseModel):
    stage: str
    status: Literal["clear", "review", "block"]
    findings: list[Finding] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def reviews(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "review"]

    def summary(self) -> str:
        parts = [f"{f.code}: {f.message}" for f in self.findings if f.severity != "info"]
        return "; ".join(parts) or "no findings"


class LoadingRecord(BaseModel):
    """What the inspector saw going into the container (stuffing supervision)."""
    container_numbers: list[str] = Field(default_factory=list)
    seal_numbers: list[str] = Field(default_factory=list)
    loaded_quantity_mt: float | None = None
    hs_code: str | None = None
    goods_description: str | None = None
    inspector_ref: str | None = None
    photos: list[str] = Field(default_factory=list)
    source: str = "operator"
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HoldClearance(BaseModel):
    code: str
    cleared_by: str
    reason: str
    evidence_ref: str | None = None
    seal_numbers: list[str] = Field(default_factory=list)
    cleared_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ComplianceRecord(BaseModel):
    deal_id: str
    trade: CheckResult | None = None
    shipment: CheckResult | None = None
    payer: CheckResult | None = None
    loading: LoadingRecord | None = None
    bl_seal_numbers: list[str] = Field(default_factory=list)
    holds: list[str] = Field(default_factory=list)
    """Open flags that force every money / commitment gate of the deal to L0."""
    cleared_holds: list[str] = Field(default_factory=list)
    """Flags an operator explicitly accepted (the gate's APPROVE)."""
    accepted_seal_numbers: list[str] = Field(default_factory=list)
    """Carrier-reported seals an operator confirmed as a false alarm."""
    hold_clearances: list[HoldClearance] = Field(default_factory=list)
    """Audit trail of block-level holds an operator cleared as false alarms."""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HsPolicyEntry(BaseModel):
    hs_code: str
    status: Literal["free", "licensed", "restricted", "prohibited", "scomet", "state_trading", "dual_use"]
    note: str | None = None
    licence_ref: str | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_mem_checks: dict[str, ComplianceRecord] = {}
_mem_policy: dict[str, HsPolicyEntry] = {}


def reset_for_tests() -> None:
    _mem_checks.clear()
    _mem_policy.clear()


def _collection(name: str):
    try:
        from shared.config import settings
        if not settings.mongo_uri:
            return None
        import motor.motor_asyncio as motor  # type: ignore[import]
        client: Any = motor.AsyncIOMotorClient(settings.mongo_uri)
        return client[settings.mongo_db_name][name]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Compliance: Mongo unavailable (%s) — in-memory only", exc)
        return None


async def get_record(deal_id: str, *, strict: bool = False) -> ComplianceRecord:
    """The deal's compliance record. ``strict`` raises when the store cannot be
    read instead of falling back to this process's memory."""
    col = _collection(_CHECKS)
    if col is not None:
        try:
            doc = await col.find_one({"deal_id": deal_id}, {"_id": 0})
            if doc:
                return ComplianceRecord(**doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Compliance: read failed for %s: %s", deal_id, exc)
            if strict:
                raise
    return _mem_checks.get(deal_id) or ComplianceRecord(deal_id=deal_id)


async def save_record(rec: ComplianceRecord) -> ComplianceRecord:
    rec.updated_at = datetime.now(timezone.utc)
    _mem_checks[rec.deal_id] = rec
    col = _collection(_CHECKS)
    if col is not None:
        try:
            await col.replace_one({"deal_id": rec.deal_id}, rec.model_dump(mode="python"), upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Compliance: write failed for %s: %s", rec.deal_id, exc)
    return rec


async def get_hs_policy(hs_code: str) -> HsPolicyEntry | None:
    hs = _hs(hs_code)
    col = _collection(_HS_POLICY)
    if col is not None:
        try:
            doc = await col.find_one({"hs_code": hs}, {"_id": 0})
            if doc:
                return HsPolicyEntry(**doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Compliance: HS policy read failed for %s: %s", hs, exc)
    return _mem_policy.get(hs)


async def set_hs_policy(entry: HsPolicyEntry) -> HsPolicyEntry:
    entry.hs_code = _hs(entry.hs_code)
    _mem_policy[entry.hs_code] = entry
    col = _collection(_HS_POLICY)
    if col is not None:
        try:
            await col.replace_one({"hs_code": entry.hs_code}, entry.model_dump(mode="python"), upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Compliance: HS policy write failed for %s: %s", entry.hs_code, exc)
    logger.info("Compliance: HS %s confirmed as %s by %s", entry.hs_code, entry.status, entry.confirmed_by)
    return entry


async def list_hs_policy() -> list[HsPolicyEntry]:
    col = _collection(_HS_POLICY)
    if col is not None:
        try:
            return [HsPolicyEntry(**d) async for d in col.find({}, {"_id": 0})]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Compliance: HS policy list failed: %s", exc)
    return list(_mem_policy.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hs(code: Any) -> str:
    return re.sub(r"\D", "", str(code or ""))


def _country(value: Any) -> str:
    v = str(value or "").strip().upper()
    if len(v) == 2:
        return v
    return _COUNTRY_NAMES.get(v, v)


def _role(p) -> str:
    r = getattr(p, "role", "")
    return str(getattr(r, "value", r) or "").lower()


def _party(deal, role: str):
    return next((p for p in (getattr(deal, "parties", None) or []) if _role(p) == role), None)


def _status(findings: list[Finding]) -> Literal["clear", "review", "block"]:
    if any(f.severity == "block" for f in findings):
        return "block"
    if any(f.severity == "review" for f in findings):
        return "review"
    return "clear"


def _digits(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))


_NAME_NOISE = {
    "pvt", "private", "ltd", "limited", "llc", "inc", "gmbh", "co", "company", "corp",
    "corporation", "the", "and", "group", "international", "intl", "trading", "traders",
    "exports", "export", "imports", "import", "industries", "india", "sa", "srl", "bv", "ag",
}


def _name_core(name: Any) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    return {w for w in words if len(w) > 2 and w not in _NAME_NOISE}


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

async def hs_policy_status(hs_code: str) -> tuple[str, str]:
    """(status, note) — operator confirmation first, then the seed, else ``unverified``."""
    hs = _hs(hs_code)
    confirmed = await get_hs_policy(hs)
    if confirmed is None:
        # An operator confirmation of a 4/6-digit parent covers the longer code.
        for n in (6, 4):
            if len(hs) > n:
                confirmed = await get_hs_policy(hs[:n])
                if confirmed:
                    break
    if confirmed is not None:
        return confirmed.status, (confirmed.note or f"confirmed by {confirmed.confirmed_by or 'operator'}")
    for n in range(len(hs), 1, -1):
        hit = HS_POLICY_SEED.get(hs[:n])
        if hit:
            return hit
    return "unverified", "export policy for this HS code not confirmed against ITC-HS Schedule 2 / SCOMET"


async def check_hs(hs_code: str) -> Finding:
    status, note = await hs_policy_status(hs_code)
    return Finding(code=f"hs_{status}", severity=_STATUS_SEVERITY.get(status, "review"),
                   message=f"HS {_hs(hs_code)}: {note}", data={"hs_code": _hs(hs_code), "status": status})


def check_destination(country: Any) -> Finding | None:
    cc = _country(country)
    if not cc:
        return Finding(code="destination_unknown", severity="review", message="destination country unknown")
    if cc in _codes("COMPLIANCE_BLOCKED_COUNTRIES", BLOCKED_COUNTRIES_DEFAULT):
        return Finding(code="destination_embargoed", severity="block",
                       message=f"destination {cc} is under a comprehensive embargo", data={"country": cc})
    if cc in _codes("COMPLIANCE_REVIEW_COUNTRIES", REVIEW_COUNTRIES_DEFAULT):
        return Finding(code="destination_high_risk", severity="review",
                       message=f"destination {cc} is sanctioned / high-risk — check end use and payment route",
                       data={"country": cc})
    return None


def check_party_countries(deal) -> list[Finding]:
    out = []
    for role in ("buyer", "supplier"):
        p = _party(deal, role)
        if p is None:
            continue
        f = check_destination(getattr(p, "country", None))
        if f and f.code != "destination_unknown":
            out.append(f.model_copy(update={
                "code": f"{role}_country_{f.code.split('_', 1)[1]}",
                "message": f"{role} {getattr(p, 'legal_name', '?')} is in {f.data.get('country')}",
            }))
    return out


def check_screening(deal) -> list[Finding]:
    mode = (os.getenv("COMPLIANCE_UNCHECKED_SCREENING") or "review").strip().lower()
    out = []
    for role in ("buyer", "supplier"):
        p = _party(deal, role)
        if p is None:
            continue
        verdict = (getattr(p, "sanctions_status", None) or "unchecked").lower()
        if verdict == "unchecked" and mode in ("review", "block"):
            out.append(Finding(code=f"{role}_screening_unchecked", severity=mode,  # type: ignore[arg-type]
                               message=f"{role} {getattr(p, 'legal_name', '?')} was never sanctions-screened"))
    return out


def check_related_parties(deal) -> Finding | None:
    from agents.negotiation_agent.email_safety import FREEMAIL, domain
    b, s = _party(deal, "buyer"), _party(deal, "supplier")
    if b is None or s is None:
        return None
    reasons = []

    def _dom(p) -> set[str]:
        out = set()
        d = domain(getattr(p, "email", None) or "")
        if d and d not in FREEMAIL:
            out.add(d)
        site = re.sub(r"^https?://", "", str(getattr(p, "website", None) or "").lower()).split("/")[0]
        site = site[4:] if site.startswith("www.") else site
        if site:
            out.add(site)
        return out

    shared_dom = _dom(b) & _dom(s)
    if shared_dom:
        reasons.append(f"same company domain {', '.join(sorted(shared_dom))}")
    pb, ps = _digits(getattr(b, "phone", None))[-9:], _digits(getattr(s, "phone", None))[-9:]
    if len(pb) >= 7 and pb == ps:
        reasons.append("same phone number")
    ab = re.sub(r"\W+", " ", str(getattr(b, "address", None) or "").lower()).strip()
    if ab and len(ab) > 10 and ab == re.sub(r"\W+", " ", str(getattr(s, "address", None) or "").lower()).strip():
        reasons.append("same address")
    nb, ns = _name_core(getattr(b, "legal_name", "")), _name_core(getattr(s, "legal_name", ""))
    if nb and ns and (nb <= ns or ns <= nb):
        reasons.append("same company name")
    if not reasons:
        return None
    return Finding(code="related_parties", severity="review",
                   message="buyer and supplier look related (" + "; ".join(reasons) + ") — possible round-tripping",
                   data={"reasons": reasons})


async def check_supplier_record(deal) -> list[Finding]:
    """DGFT denied-entity list and company age from the winning supplier's verified quote."""
    out: list[Finding] = []
    sup_id = getattr(deal, "winning_supplier_id", None)
    if not sup_id:
        return out
    try:
        from services.sourcing.service import list_quotes  # type: ignore[import]
        quotes = [q for q in await list_quotes(deal_id=deal.deal_id) if q.supplier_id == sup_id and q.verification]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Compliance: supplier quotes unavailable for %s: %s", deal.deal_id, exc)
        return out
    if not quotes:
        return out
    v = quotes[-1].verification
    if getattr(v, "dgft_denied", False):
        out.append(Finding(code="supplier_dgft_denied", severity="block",
                           message=f"supplier {sup_id} is on DGFT's denied-entity list"))
    age = float(getattr(v, "business_age_years", 0.0) or 0.0)
    min_age = _float("COMPLIANCE_MIN_COMPANY_AGE_YEARS", 1.0)
    if 0.0 < age < min_age:
        out.append(Finding(code="supplier_new_company", severity="review",
                           message=f"supplier {sup_id} incorporated {age:.1f} years ago (< {min_age:g})",
                           data={"age_years": age}))
    return out


async def check_prices(deal, *, buyer_price: float | None, supplier_price: float | None) -> list[Finding]:
    """Over / under-invoicing signals: prices far from what the market pays, or
    a spread no real trade earns."""
    out: list[Finding] = []
    max_dev = _float("COMPLIANCE_PRICE_DEVIATION_PCT", 40.0) / 100.0
    max_ratio = _float("COMPLIANCE_MAX_SPREAD_RATIO", 1.8)
    try:
        from shared import pricebook
        priors = await pricebook.public_priors(deal.hs_code, deal.market)
        for side, price in (("buyer", buyer_price), ("supplier", supplier_price)):
            if not price or price <= 0:
                continue
            obs = [o for o in await pricebook.observations(deal.hs_code, side) if o.deal_id != deal.deal_id]
            est = pricebook.estimate_from(obs, hs_code=deal.hs_code, side=side, market=deal.market,
                                          prior=priors.get(side), prior_source=priors.get("source"))
            if not est.median or est.confidence == "none":
                continue
            dev = (price - est.median) / est.median
            if abs(dev) > max_dev:
                out.append(Finding(
                    code=f"{side}_price_off_market", severity="review",
                    message=(f"{side} price ${price:,.2f}/MT is {dev:+.0%} from the market estimate "
                             f"${est.median:,.2f}/MT ({est.confidence}) — check for over / under-invoicing"),
                    data={"price": price, "market_median": est.median, "deviation": round(dev, 4),
                          "confidence": est.confidence},
                ))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Compliance: price check skipped for %s: %s", getattr(deal, "deal_id", "?"), exc)
    if buyer_price and supplier_price and supplier_price > 0 and buyer_price / supplier_price > max_ratio:
        out.append(Finding(
            code="spread_too_wide", severity="review",
            message=(f"buyer pays {buyer_price / supplier_price:.2f}× the supplier price "
                     f"(limit {max_ratio:g}×) — a spread that wide is a money-laundering signal"),
            data={"ratio": round(buyer_price / supplier_price, 3)},
        ))
    return out


# ---------------------------------------------------------------------------
# Check 1 — trade
# ---------------------------------------------------------------------------

async def check_trade(
    deal, *, stage: str, buyer_price: float | None = None, supplier_price: float | None = None,
    persist: bool = True,
) -> CheckResult:
    """Product, destination, parties, prices. Accepted holds stay accepted."""
    findings: list[Finding] = [await check_hs(deal.hs_code)]
    dest = check_destination(getattr(deal, "market", None))
    if dest:
        findings.append(dest)
    findings += check_party_countries(deal)
    if stage != "deal_created":
        findings += check_screening(deal)
        rel = check_related_parties(deal)
        if rel:
            findings.append(rel)
        findings += await check_supplier_record(deal)
        findings += await check_prices(deal, buyer_price=buyer_price, supplier_price=supplier_price)
    rec = await get_record(deal.deal_id)
    for f in findings:  # what an operator already accepted is not asked again
        if f.severity == "review" and f.code in rec.cleared_holds:
            f.severity = "info"
            f.message += " (accepted by operator)"
    result = CheckResult(stage=stage, status=_status(findings), findings=findings)
    if persist:
        rec.trade = result
        open_codes = {f.code for f in result.findings if f.severity in ("review", "block")}
        rec.holds = sorted((set(rec.holds) - _trade_holds(rec.holds)) | open_codes)
        await save_record(rec)
    logger.info("Compliance[%s]: deal %s → %s (%s)", stage, deal.deal_id, result.status, result.summary())
    return result


def _trade_holds(holds: list[str]) -> set[str]:
    """Trade-check holds are recomputed on every trade check; others stay."""
    keep_prefixes = ("shipment_", "loading_", "seal_", "payer_")
    return {h for h in holds if not h.startswith(keep_prefixes)}


_BLOCK_CODES = frozenset({
    "loading_not_verified", "seal_mismatch", "shipment_hs_mismatch", "supplier_dgft_denied",
    *(f"hs_{status}" for status, severity in _STATUS_SEVERITY.items() if severity == "block"),
})


def _is_block_code(code: str, rec: ComplianceRecord) -> bool:
    """True for a block-level finding. The checks' own stored severity also
    counts, because some are configurable (``COMPLIANCE_UNCHECKED_SCREENING``)."""
    if code in _BLOCK_CODES or code.endswith("_embargoed"):
        return True
    return any(f.code == code and f.severity == "block"
               for check in (rec.trade, rec.shipment, rec.payer) if check is not None
               for f in check.findings)


async def accept_findings(deal_id: str, codes: list[str], *, by: str | None = None, hs_code: str | None = None) -> None:
    """Operator APPROVE of a compliance gate: the review findings are accepted
    (not asked again for this deal) and a never-verified HS code is recorded Free.

    Block-level findings are never accepted here. An approval covers one gate,
    so the hold stays open and the deal's later gates keep stopping for a human.
    """
    rec = await get_record(deal_id)
    blocked = {c for c in codes if _is_block_code(c, rec)}
    if blocked:
        logger.warning("Compliance: deal %s block-level holds stay open after approval: %s",
                       deal_id, ", ".join(sorted(blocked)))
    codes = [c for c in codes if c not in blocked]
    rec.cleared_holds = sorted(set(rec.cleared_holds) | set(codes))
    rec.holds = [h for h in rec.holds if h not in codes]
    await save_record(rec)
    if hs_code and "hs_unverified" in codes:
        await set_hs_policy(HsPolicyEntry(hs_code=hs_code, status="free", confirmed_by=by or "operator",
                                          note="confirmed Free at a trade-compliance gate"))


MONEY_RELEASE_ACTIONS = frozenset({
    "RELEASE_ESCROW_TRANCHE", "RELEASE_SUPPLIER_PAYOUT", "CONFIRM_BUYER_ACCEPTANCE",
})
"""Gates that cannot be approved while a block-level hold is open."""

STORE_UNAVAILABLE_BLOCK = "compliance_store_unavailable"

_CLEARABLE_BLOCK_CODES = frozenset({"seal_mismatch"})
"""Blocks an operator may clear as a false alarm. The others clear themselves
when the facts are corrected (a new loading record, a re-run trade check)."""


async def open_blocks(deal_id: str) -> list[str]:
    """Block-level holds that stop money leaving. Fails closed: if the record
    cannot be read, the release is refused rather than assumed safe."""
    try:
        rec = await get_record(deal_id, strict=True)
    except Exception:  # noqa: BLE001
        logger.exception("Compliance: cannot read holds for %s — refusing money release", deal_id)
        return [STORE_UNAVAILABLE_BLOCK]
    return [h for h in rec.holds if _is_block_code(h, rec)]


class HoldNotOpenError(Exception):
    """The hold to clear is not open on this deal."""


class HoldNotClearableError(Exception):
    """This block-level hold cannot be cleared by an operator."""


async def clear_block(
    deal_id: str, code: str, *, by: str, reason: str, evidence_ref: str | None = None,
) -> ComplianceRecord:
    """Clear a block-level hold an operator has confirmed is a false alarm,
    keeping who, why and on what evidence in ``hold_clearances``.

    For a seal mismatch the carrier's seal numbers seen so far become accepted,
    so a repeat of the same report does not re-raise it. A new unknown seal does.
    """
    if code not in _CLEARABLE_BLOCK_CODES:
        raise HoldNotClearableError(code)
    rec = await get_record(deal_id, strict=True)
    if code not in rec.holds:
        raise HoldNotOpenError(code)
    loaded = set(rec.loading.seal_numbers) if rec.loading else set()
    newly_accepted = sorted(set(rec.bl_seal_numbers) - loaded)
    rec.accepted_seal_numbers = sorted(set(rec.accepted_seal_numbers) | set(newly_accepted))
    rec.holds = [h for h in rec.holds if h != code]
    rec.hold_clearances.append(HoldClearance(
        code=code, cleared_by=by, reason=reason, evidence_ref=evidence_ref, seal_numbers=newly_accepted,
    ))
    await save_record(rec)
    logger.warning("Compliance: deal %s hold %s cleared by %s: %s", deal_id, code, by, reason)
    return rec


async def hold_flags(deal_id: str | None) -> list[str]:
    """Open compliance flags for the deal (empty when none / unavailable)."""
    if not deal_id:
        return []
    try:
        rec = await get_record(deal_id)
        return [f"compliance:{h}" for h in rec.holds]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Compliance: hold lookup failed for %s: %s", deal_id, exc)
        return []


# ---------------------------------------------------------------------------
# Payer — the money must come from the buyer
# ---------------------------------------------------------------------------

async def check_payer(deal, payer: dict[str, Any]) -> CheckResult:
    findings: list[Finding] = []
    name = (payer.get("payer_name") or "").strip()
    country = payer.get("payer_country")
    buyer = _party(deal, "buyer")
    if not name:
        findings.append(Finding(code="payer_unknown", severity="review",
                                message="escrow provider did not report who paid — confirm the buyer paid"))
    elif buyer is not None:
        nb, np_ = _name_core(getattr(buyer, "legal_name", "")), _name_core(name)
        if not (nb and np_ and (nb & np_)):
            findings.append(Finding(code="payer_not_buyer", severity="review",
                                    message=f"escrow funded by '{name}', not the buyer "
                                            f"'{getattr(buyer, 'legal_name', '?')}' — third-party payment",
                                    data={"payer_name": name}))
    if country:
        f = check_destination(country)
        if f and f.code != "destination_unknown":
            findings.append(f.model_copy(update={"code": f"payer_country_{f.code.split('_', 1)[1]}",
                                                 "message": f"payment came from {_country(country)}"}))
        if buyer is not None and getattr(buyer, "country", None) and _country(country) != _country(buyer.country):
            findings.append(Finding(code="payer_country_mismatch", severity="review",
                                    message=f"payment from {_country(country)}, buyer is in {_country(buyer.country)}"))
    rec = await get_record(deal.deal_id)
    for f in findings:
        if f.severity == "review" and f.code in rec.cleared_holds:
            f.severity = "info"
    result = CheckResult(stage="escrow_funded", status=_status(findings), findings=findings)
    rec.payer = result
    rec.holds = sorted({h for h in rec.holds if not h.startswith("payer_")}
                       | {f.code for f in findings if f.severity in ("review", "block")})
    await save_record(rec)
    return result


# ---------------------------------------------------------------------------
# Check 2 — shipment (supervised loading + seal + document consistency)
# ---------------------------------------------------------------------------

async def record_loading(deal_id: str, loading: LoadingRecord) -> ComplianceRecord:
    rec = await get_record(deal_id)
    loading.container_numbers = [c.strip().upper() for c in loading.container_numbers if c and c.strip()]
    loading.seal_numbers = [s.strip().upper() for s in loading.seal_numbers if s and s.strip()]
    rec.loading = loading
    return await save_record(rec)


async def _pi_po(deal_id: str):
    pi = po = None
    try:
        from services.dealmaker.repository import get_repository as _dm  # type: ignore[import]
        repo = _dm()
        pi = await repo.get_pi_by_deal(deal_id) if hasattr(repo, "get_pi_by_deal") else None
        po = await repo.get_po_by_deal(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Compliance: PI/PO lookup failed for %s: %s", deal_id, exc)
    return pi, po


async def check_shipment(deal) -> CheckResult:
    """Supervised loading present, and the container matches the paperwork."""
    rec = await get_record(deal.deal_id)
    loading = rec.loading
    findings: list[Finding] = []
    tol = _float("COMPLIANCE_QTY_TOLERANCE_PCT", 5.0) / 100.0
    if loading is None or not loading.seal_numbers or not loading.container_numbers:
        findings.append(Finding(code="loading_not_verified", severity="block",
                                message="no supervised loading on record (container + seal numbers) — "
                                        "freight and the QC tranche wait for it"))
    else:
        if loading.hs_code and _hs(loading.hs_code)[:4] != _hs(deal.hs_code)[:4]:
            findings.append(Finding(code="shipment_hs_mismatch", severity="block",
                                    message=f"inspector recorded HS {loading.hs_code}, the deal is HS {deal.hs_code}",
                                    data={"loaded_hs": loading.hs_code}))
        pi, po = await _pi_po(deal.deal_id)
        ordered = getattr(po, "quantity_mt", None) or getattr(pi, "quantity_mt", None)
        if loading.loaded_quantity_mt is None:
            findings.append(Finding(code="shipment_qty_unknown", severity="review",
                                    message="inspector did not record the loaded quantity"))
        elif ordered and abs(loading.loaded_quantity_mt - ordered) > ordered * tol:
            findings.append(Finding(code="shipment_qty_mismatch", severity="review",
                                    message=(f"{loading.loaded_quantity_mt:g} MT loaded, "
                                             f"{ordered:g} MT ordered (tolerance {tol:.0%})"),
                                    data={"loaded": loading.loaded_quantity_mt, "ordered": ordered}))
        if pi is not None and po is not None and pi.quantity_mt and po.quantity_mt \
                and abs(pi.quantity_mt - po.quantity_mt) > pi.quantity_mt * tol:
            findings.append(Finding(code="shipment_pi_po_mismatch", severity="review",
                                    message=f"PI says {pi.quantity_mt:g} MT, PO says {po.quantity_mt:g} MT"))
        if not loading.photos:
            findings.append(Finding(code="shipment_no_photos", severity="review",
                                    message="no stuffing / seal photos from the inspector"))
    for f in findings:
        if f.severity == "review" and f.code in rec.cleared_holds:
            f.severity = "info"
    result = CheckResult(stage="shipment", status=_status(findings), findings=findings)
    rec.shipment = result
    rec.holds = sorted({h for h in rec.holds if not h.startswith(("shipment_", "loading_"))}
                       | {f.code for f in findings if f.severity in ("review", "block")})
    await save_record(rec)
    logger.info("Compliance[shipment]: deal %s → %s (%s)", deal.deal_id, result.status, result.summary())
    return result


async def check_seal(deal_id: str, bl_seal_numbers: list[str]) -> Finding | None:
    """Bill-of-lading seal(s) reported by the carrier must be the seal(s) the
    inspector applied. A mismatch means the container was opened."""
    seals = {s.strip().upper() for s in bl_seal_numbers if s and s.strip()}
    if not seals:
        return None
    rec = await get_record(deal_id)
    rec.bl_seal_numbers = sorted(set(rec.bl_seal_numbers) | seals)
    loaded = set(rec.loading.seal_numbers) if rec.loading else set()
    finding = None
    if loaded and not seals <= loaded | set(rec.accepted_seal_numbers):
        finding = Finding(code="seal_mismatch", severity="block",
                          message=(f"carrier reports seal(s) {', '.join(sorted(seals))}, inspector applied "
                                   f"{', '.join(sorted(loaded))} — container may have been opened"),
                          data={"bl": sorted(seals), "loaded": sorted(loaded)})
        rec.holds = sorted(set(rec.holds) | {"seal_mismatch"})
    await save_record(rec)
    return finding
