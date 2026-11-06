"""PortLess-owned Apify actors — buyer / seller identification + enrichment.

PortLess generates and publishes its own Apify actors (the ``rich_minds``
account, built with the actor pipeline in ``C:/workArea/pandian/apify-actors``).
This module is the ONE place the backend talks to them. Every actor is reached
through the same ``ApifyActorConnector`` base (run → wait → read dataset) and a
thin subclass that maps the actor's dataset rows onto the shapes the pipeline
already consumes (``BuyerSignal``, ``SellerCandidate``, deal ``Party`` …), so a
paid API could replace any actor later without touching the pipeline.

Actors and where they plug in
-----------------------------
  us-importer-finder-ai       Trade-Min Phase 2 — named US buyers from customs
                              records (shipments, lanes, website/phone, fit)
  alibaba-rfq-leads-ai        Trade-Min Phase 2 — live buying requests (Level 3
                              intent) with buyer country, quantity, target price
  export-market-finder-ai     Trade-Min Phase 1 — UN Comtrade demand + the
                              market's real unit value per kg (buyer price
                              benchmark that replaces the 1.25× FOB heuristic)
  india-supplier-finder-ai    Trade-Min Phase 4 — Indian suppliers with price,
                              MOQ, GSTIN, contact (generate with /actor-pipeline;
                              the expected row schema is documented on the class)
  company-contact-finder-ai   Deal enrichment — buyer/supplier website, checked
                              e-mail, phone, VAT id (E-5 → E-2 → E-3 chain)
  counterparty-check-ai       Deal enrichment — OFAC/UN/EU/UK sanctions, GLEIF
                              LEI, EU VIES before the PI goes out (E-9 / E-10)
  email-list-qualifier-ai     Deal enrichment — deliverability check of a
                              counterparty address before the first outbound mail

Configuration (all optional — the connector degrades to an empty result)
------------------------------------------------------------------------
  APIFY_TOKEN                   API token. When unset the connector also looks
                                in ~/.apify/auth.json (the Apify CLI login) unless
                                APIFY_TOKEN_AUTODISCOVER=false.
  APIFY_ACTOR_OWNER             Apify username that owns the actors (rich_minds)
  APIFY_ACTOR_<KEY>             Per-actor override — a slug, ``owner~slug`` or an
                                actor id. Keys: US_IMPORTER_FINDER,
                                ALIBABA_RFQ_LEADS, EXPORT_MARKET_FINDER,
                                INDIA_SUPPLIER_FINDER, COMPANY_CONTACT_FINDER,
                                COUNTERPARTY_CHECK, EMAIL_LIST_QUALIFIER
  APIFY_ENABLE_AI               true (default) → the actors' AI tier (fit score,
                                reason, pitch angle); false → rule tier (cheaper)
  APIFY_MAX_ITEMS               cap per call (default 25 — each actor's free units)
  APIFY_RUN_TIMEOUT_SECONDS     wall-clock budget per actor run (default 240)
  APIFY_MAX_DISCOVERY_CHARGE_USD cap on what a source Store actor may charge (0.5)
  APIFY_WEBHOOK_SECRET          HMAC secret for actor → PortLess webhooks

Graceful degradation: no token → ``quote()`` returns an empty, well-formed
result (``{"buyers": [], "total": 0, "stub": True}``) and logs once. A run that
fails or times out raises ``ApifyError`` (callers wrap it like any connector).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .base import Connector

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.apify.com/v2"
_DEFAULT_OWNER = "rich_minds"
_TERMINAL_RUN_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT", "ABORTING"}
_WAIT_CHUNK_SECONDS = 60  # Apify caps waitForFinish at 60s per poll (300s on run start)

_warned_no_token = False

# ── Daily run budget (APIFY_MAX_RUNS_PER_DAY; 0 = unlimited) ──────────────
# Every actor run costs money. A scan over a large catalogue, the enrichment
# of every new deal and the carrier-tracking poll all start runs; the cap is a
# process-wide circuit breaker so a mis-configured schedule cannot burn the
# account in a night. Counted per UTC day in this process (reset at midnight).
_runs_by_day: dict[str, int] = {}
_warned_run_cap: str | None = None


def max_runs_per_day() -> int:
    return max(0, _int("APIFY_MAX_RUNS_PER_DAY", 0))


def runs_today() -> int:
    from datetime import datetime, timezone
    return _runs_by_day.get(datetime.now(timezone.utc).date().isoformat(), 0)


def run_budget_left() -> bool:
    cap = max_runs_per_day()
    return cap <= 0 or runs_today() < cap


def _count_run() -> None:
    from datetime import datetime, timezone
    key = datetime.now(timezone.utc).date().isoformat()
    for stale in [k for k in _runs_by_day if k != key]:
        _runs_by_day.pop(stale, None)
    _runs_by_day[key] = _runs_by_day.get(key, 0) + 1


def reset_run_budget_for_tests() -> None:
    global _warned_run_cap
    _runs_by_day.clear()
    _warned_run_cap = None


class ApifyError(Exception):
    """Raised when an actor run cannot be started, fails, or its dataset cannot be read."""


# ---------------------------------------------------------------------------
# Credentials / configuration helpers
# ---------------------------------------------------------------------------

def _autodiscover_token() -> str:
    """Token from the Apify CLI login (~/.apify/auth.json) — dev convenience,
    mirrors the NATS *.creds auto-discovery. Never logged."""
    if os.getenv("APIFY_TOKEN_AUTODISCOVER", "true").strip().lower() in ("0", "false", "no", "off"):
        return ""
    for base in (Path.home(), Path(os.getenv("USERPROFILE", "") or Path.home())):
        path = base / ".apify" / "auth.json"
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                token = str(data.get("token") or "")
                if token:
                    return token
        except Exception as exc:  # noqa: BLE001
            logger.debug("Apify: could not read %s: %s", path, exc)
    return ""


def apify_token() -> str:
    return os.getenv("APIFY_TOKEN", "").strip() or _autodiscover_token()


def apify_owner() -> str:
    return os.getenv("APIFY_ACTOR_OWNER", _DEFAULT_OWNER).strip() or _DEFAULT_OWNER


def apify_enabled() -> bool:
    return bool(apify_token())


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def resolve_actor_ref(key: str, default_slug: str) -> str:
    """``APIFY_ACTOR_<KEY>`` → slug / ``owner~slug`` / actor id; default ``owner~slug``."""
    raw = os.getenv(f"APIFY_ACTOR_{key}", "").strip()
    if not raw:
        return f"{apify_owner()}~{default_slug}"
    if "~" in raw or "/" in raw:
        return raw.replace("/", "~")
    # A bare 17-char id (e.g. Fn26zPbcUPRREJ45V) is an actor id; a slug needs the owner.
    if re.fullmatch(r"[A-Za-z0-9]{17}", raw):
        return raw
    return f"{apify_owner()}~{raw}"


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _safe_int(value: Any) -> int | None:
    f = _safe_float(value)
    return int(f) if f is not None else None


def _first(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


# ISO-3166 alpha-3 → alpha-2 for the markets PortLess trades with (the
# export-market actor reports ISO-3; the rest of the pipeline speaks ISO-2).
_ISO3_TO_ISO2: dict[str, str] = {
    "USA": "US", "DEU": "DE", "ESP": "ES", "ARE": "AE", "GBR": "GB", "FRA": "FR", "ITA": "IT",
    "NLD": "NL", "POL": "PL", "AUS": "AU", "CAN": "CA", "JPN": "JP", "SGP": "SG", "CHN": "CN",
    "BEL": "BE", "CHE": "CH", "SAU": "SA", "SWE": "SE", "DNK": "DK", "NOR": "NO", "AUT": "AT",
    "PRT": "PT", "IRL": "IE", "FIN": "FI", "TUR": "TR", "ZAF": "ZA", "BRA": "BR", "MEX": "MX",
    "KOR": "KR", "THA": "TH", "MYS": "MY", "EGY": "EG", "NGA": "NG", "ARG": "AR", "IND": "IN",
    "VNM": "VN", "IDN": "ID", "PHL": "PH", "BGD": "BD", "LKA": "LK", "PAK": "PK", "NPL": "NP",
    "QAT": "QA", "KWT": "KW", "OMN": "OM", "BHR": "BH", "JOR": "JO", "ISR": "IL", "GRC": "GR",
    "CZE": "CZ", "HUN": "HU", "ROU": "RO", "NZL": "NZ", "CHL": "CL", "COL": "CO", "PER": "PE",
    "KEN": "KE", "TZA": "TZ", "ETH": "ET", "MAR": "MA", "DZA": "DZ", "TUN": "TN", "RUS": "RU",
    "UKR": "UA", "KAZ": "KZ", "HKG": "HK", "TWN": "TW", "LTU": "LT", "LVA": "LV", "EST": "EE",
    "SVK": "SK", "SVN": "SI", "HRV": "HR", "BGR": "BG", "SRB": "RS", "LUX": "LU", "MLT": "MT",
    "CYP": "CY", "ISL": "IS",
}
_ISO2_TO_ISO3 = {v: k for k, v in _ISO3_TO_ISO2.items()}


def iso2(code: Any) -> str | None:
    if not code:
        return None
    c = str(code).strip().upper()
    if len(c) == 2:
        return c
    return _ISO3_TO_ISO2.get(c, c)


def iso3(code: Any) -> str | None:
    if not code:
        return None
    c = str(code).strip().upper()
    if len(c) == 3:
        return c
    return _ISO2_TO_ISO3.get(c, c)


# ---------------------------------------------------------------------------
# Base connector — run an actor, wait, read its dataset
# ---------------------------------------------------------------------------

class ApifyActorConnector(Connector):
    """Run one PortLess-owned actor and return its dataset rows.

    Subclasses set ``actor_key`` / ``default_slug`` and implement ``quote()``
    to map rows onto PortLess shapes. ``book()`` starts a run without waiting
    (returns the run id), ``status()`` polls a run, ``webhook_verify`` checks
    the ``X-Signature: sha256=<hmac>`` header the actors send to ``webhookUrl``.
    """

    name = "apify"
    actor_key = "GENERIC"
    default_slug = ""
    empty_key = "items"
    """Top-level list key of an empty ``quote()`` result (``buyers``, ``sellers`` …)."""

    def __init__(self) -> None:
        self._token = apify_token()
        self._base_url = os.getenv("APIFY_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = _float("APIFY_RUN_TIMEOUT_SECONDS", 240.0)
        self._max_items = max(1, _int("APIFY_MAX_ITEMS", 25))
        self._enable_ai = _flag("APIFY_ENABLE_AI", True)
        self._max_discovery_charge = _float("APIFY_MAX_DISCOVERY_CHARGE_USD", 0.5)
        self._memory_mbytes = _int("APIFY_MEMORY_MBYTES", 0)
        self._webhook_secret = os.getenv("APIFY_WEBHOOK_SECRET", "")

    # -- identity -----------------------------------------------------------

    @property
    def actor_ref(self) -> str:
        return resolve_actor_ref(self.actor_key, self.default_slug)

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def describe(self) -> dict[str, Any]:
        return {
            "connector": self.name,
            "actor": self.actor_ref,
            "live": self.configured,
            "ai_tier": self._enable_ai,
            "max_items": self._max_items,
            "external": self.external,
        }

    external = False
    """True for the slots whose actor is built outside /actor-pipeline (not run
    automatically by the pool); the connector stubs until it exists."""

    def _empty(self, **extra: Any) -> dict[str, Any]:
        return {self.empty_key: [], "total": 0, "actor": self.actor_ref, "stub": True, **extra}

    def _common_input(self, max_items: int | None = None) -> dict[str, Any]:
        """Input keys every PortLess actor accepts (see each .actor/input_schema.json)."""
        return {
            "enableAi": self._enable_ai,
            "maxQualified": min(self._max_items, max_items or self._max_items),
            "maxDiscoveryChargeUsd": self._max_discovery_charge,
            # PortLess keeps its own memory of what it saw — the actors' cross-run
            # dedupe would otherwise hide a buyer/supplier from the next scan.
            "dedupeAcrossRuns": False,
            "llmProvider": "apify",
        }

    # -- HTTP ---------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def run(self, actor_input: dict[str, Any], *, max_items: int | None = None) -> list[dict[str, Any]]:
        """Start the actor, wait (bounded by APIFY_RUN_TIMEOUT_SECONDS), return dataset rows.

        Returns ``[]`` without any network call when no token is configured.
        Partial results are returned when the budget runs out (the run is
        aborted so it stops charging); a FAILED run raises ``ApifyError``.
        """
        global _warned_no_token
        if not self.configured:
            if not _warned_no_token:
                logger.warning(
                    "Apify: APIFY_TOKEN not set (and no ~/.apify/auth.json) — actor %s "
                    "skipped; buyer/seller identification runs on the remaining sources",
                    self.actor_ref,
                )
                _warned_no_token = True
            return []

        if not run_budget_left():
            global _warned_run_cap
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).date().isoformat()
            if _warned_run_cap != today:
                logger.warning(
                    "Apify: daily run budget exhausted (%d/%d runs today, APIFY_MAX_RUNS_PER_DAY) — "
                    "actor %s skipped until tomorrow", runs_today(), max_runs_per_day(), self.actor_ref,
                )
                _warned_run_cap = today
            return []

        limit = min(self._max_items, max_items or self._max_items)
        first_wait = int(min(self._timeout, 300))
        params: dict[str, Any] = {"waitForFinish": first_wait}
        if self._memory_mbytes:
            params["memory"] = self._memory_mbytes
        deadline = asyncio.get_event_loop().time() + self._timeout

        async with httpx.AsyncClient(timeout=first_wait + 30) as client:
            resp = await client.post(
                f"{self._base_url}/acts/{self.actor_ref}/runs",
                params=params, json=actor_input, headers=self._headers(),
            )
            if resp.status_code not in range(200, 300):
                raise ApifyError(
                    f"Apify run start failed for {self.actor_ref}: HTTP {resp.status_code} — {resp.text[:300]}"
                )
            _count_run()
            run = resp.json().get("data") or {}
            run_id = run.get("id")
            status = run.get("status")

            while status not in _TERMINAL_RUN_STATUSES and run_id:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    logger.warning(
                        "Apify: run %s of %s exceeded %.0fs — aborting, using partial results",
                        run_id, self.actor_ref, self._timeout,
                    )
                    try:
                        await client.post(f"{self._base_url}/actor-runs/{run_id}/abort",
                                          headers=self._headers(), params={"gracefully": "true"})
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Apify: abort failed for %s: %s", run_id, exc)
                    break
                wait = int(min(_WAIT_CHUNK_SECONDS, max(1, remaining)))
                poll = await client.get(
                    f"{self._base_url}/actor-runs/{run_id}",
                    params={"waitForFinish": wait}, headers=self._headers(),
                )
                if poll.status_code not in range(200, 300):
                    raise ApifyError(f"Apify run poll failed for {run_id}: HTTP {poll.status_code}")
                run = poll.json().get("data") or {}
                status = run.get("status")

            if status == "FAILED":
                raise ApifyError(f"Apify run {run_id} of {self.actor_ref} FAILED: {run.get('statusMessage')}")

            dataset_id = run.get("defaultDatasetId")
            if not dataset_id:
                return []
            items = await client.get(
                f"{self._base_url}/datasets/{dataset_id}/items",
                params={"clean": "true", "limit": limit, "format": "json"},
                headers=self._headers(),
            )
            if items.status_code not in range(200, 300):
                raise ApifyError(f"Apify dataset read failed for {dataset_id}: HTTP {items.status_code}")
            rows = items.json()
            if not isinstance(rows, list):
                return []
            logger.info("Apify: %s run %s → %d row(s) (status %s)", self.actor_ref, run_id, len(rows), status)
            return [r for r in rows if isinstance(r, dict)]

    # -- Connector interface -------------------------------------------------

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start a run without waiting (long discovery jobs). Returns the run id."""
        if not self.configured:
            return {"run_id": None, "actor": self.actor_ref, "stub": True}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/acts/{self.actor_ref}/runs",
                json=request.get("input", {}), headers=self._headers(),
            )
            if resp.status_code not in range(200, 300):
                raise ApifyError(f"Apify run start failed: HTTP {resp.status_code}")
            data = resp.json().get("data") or {}
            return {"run_id": data.get("id"), "status": data.get("status"), "actor": self.actor_ref}

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if not self.configured:
            return {"run_id": ext_ref, "status": "UNKNOWN", "stub": True}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{self._base_url}/actor-runs/{ext_ref}", headers=self._headers())
            if resp.status_code not in range(200, 300):
                raise ApifyError(f"Apify run status failed: HTTP {resp.status_code}")
            data = resp.json().get("data") or {}
            return {"run_id": ext_ref, "status": data.get("status"), "dataset_id": data.get("defaultDatasetId")}

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """The actors sign every ``webhookUrl`` POST: ``X-Signature: sha256=<HMAC-SHA256(body)>``."""
        if not self._webhook_secret:
            logger.warning("Apify: APIFY_WEBHOOK_SECRET not set — accepting all actor webhooks")
            return True
        sig = headers.get("x-signature") or headers.get("X-Signature") or ""
        sig = sig.split("=", 1)[1] if sig.startswith("sha256=") else sig
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# Buyer identification
# ---------------------------------------------------------------------------

class ApifyUsImporterConnector(ApifyActorConnector):
    """``us-importer-finder-ai`` — named US importers from customs records.

    quote(request): ``hs_code`` (6-digit), ``products`` (list of bill-of-lading
    words, optional), ``origin_country`` (default IN), ``unit_value_usd_per_kg``
    (benchmark so the actor estimates each importer's annual value),
    ``goal`` (plain-words buyer description), ``max_items``.

    Returns ``{"buyers": [...], "total": n}`` — each buyer carries the fields
    ``BuyerSignal`` needs plus website/phone/contact_route and the actor's fit
    score / pitch angle. ``unit_price_usd`` is the benchmark expressed per MT
    (``price_unit = "USD/MT"``) so the Match Engine corridor is unit-consistent.
    """

    name = "apify_us_importers"
    actor_key = "US_IMPORTER_FINDER"
    default_slug = "us-importer-finder-ai"
    empty_key = "buyers"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        hs_code = str(request.get("hs_code") or "").strip()
        products = [p for p in (request.get("products") or []) if p]
        if not hs_code and not products:
            raise ValueError("apify_us_importers: hs_code or products required")
        actor_input: dict[str, Any] = {
            **self._common_input(request.get("max_items")),
            "sourceMode": "live",  # the actor's enum is live | dataset | list
            "originCountry": (request.get("origin_country") or "IN").upper(),
            "hsCodes": [hs_code] if hs_code else [],
            "products": products,
            "goalDescription": request.get("goal") or (
                f"Regular US importers of HS {hs_code or products[0]} who already buy from Asia "
                f"and could switch to or add an Indian merchant exporter with escrow + third-party QC."
            ),
            "minScore": int(request.get("min_score", 50)),
            "mostRecentWithinMonths": int(request.get("recent_months", 6)),
            "minShipments12m": int(request.get("min_shipments_12m", 3)),
            "excludeForwarders": True,
            "maxImporters": int(request.get("max_importers", 30)),
        }
        bench = _safe_float(request.get("unit_value_usd_per_kg"))
        if bench:
            actor_input["unitValueUsdPerKg"] = bench
            actor_input["unitValueSource"] = request.get("unit_value_source") or "PortLess export-market benchmark"
        rows = await self.run(actor_input, max_items=request.get("max_items"))
        if not rows and not self.configured:
            return self._empty()
        buyers = [self._buyer(r, bench) for r in rows if r.get("name")]
        return {"buyers": buyers, "total": len(buyers), "actor": self.actor_ref}

    @staticmethod
    def _buyer(r: dict[str, Any], bench_per_kg: float | None) -> dict[str, Any]:
        weight_kg = _safe_float(r.get("weightKg12m"))
        phones = r.get("phone")
        return {
            "buyer_name": r.get("name"),
            "buyer_country": "US",
            "company_id": r.get("itemId") or r.get("dedupeKey"),
            "signal_type": "manifest",
            "source": "apify_us_importers",
            "last_import_date": r.get("lastArrival"),
            "annual_volume_mt": round(weight_kg / 1000.0, 3) if weight_kg else None,
            "estimated_order_value_usd": _safe_float(r.get("estValueUsd12m")),
            "unit_price_usd": round(bench_per_kg * 1000.0, 2) if bench_per_kg else None,
            "price_unit": "USD/MT" if bench_per_kg else None,
            "shipments_12m": _safe_int(r.get("shipments12m")),
            "growth_pct": _safe_float(r.get("growthPct")),
            "website": r.get("website"),
            "phone": phones if isinstance(phones, str) else None,
            "contact_url": _first(r.get("contactRoute"), r.get("website"), r.get("url")),
            "address": ", ".join(str(x) for x in (r.get("address"), r.get("city"), r.get("state")) if x) or None,
            "hs_codes": [str(h) for h in (r.get("hsCodes") or [])][:10],
            "buys_from_origin": r.get("buysFromOrigin"),
            "origin_share_pct": _safe_float(r.get("originSharePct")),
            "fit_score": _safe_int(r.get("score")),
            "fit_reason": r.get("fitReason") or "",
            "pitch_angle": r.get("pitchAngle") or r.get("generatedText") or "",
            "company_type": r.get("companyType"),
            "flags": list(r.get("flags") or []),
            "source_actor": r.get("sourceActor"),
        }


class ApifyAlibabaRfqConnector(ApifyActorConnector):
    """``alibaba-rfq-leads-ai`` — live buying requests (Level-3 intent) matched to products.

    quote(request): ``products`` / ``query`` (keyword), ``country`` (ISO-2 buyer
    country, optional — filtered client-side as well), ``posted_within_days``
    (default 14), ``max_items``. Returns ``{"rfqs": [...], "total": n}`` with
    the same keys the legacy Alibaba Open-Platform connector produced (rfq_id,
    title, buyer_country, quantity, posted_at, …) plus buyer name, target price
    (converted to USD/MT when the buyer stated USD per kg/ton), fit score and
    the actor's quote reply draft.
    """

    name = "apify_alibaba_rfq"
    actor_key = "ALIBABA_RFQ_LEADS"
    default_slug = "alibaba-rfq-leads-ai"
    empty_key = "rfqs"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        products = [p for p in (request.get("products") or []) if p]
        if request.get("query") and request["query"] not in products:
            products.append(request["query"])
        if not products:
            raise ValueError("apify_alibaba_rfq: products or query required")
        country = iso2(request.get("country"))
        actor_input: dict[str, Any] = {
            **self._common_input(request.get("max_items")),
            "sourceMode": "actor",
            "products": products,
            "sellerProfile": request.get("seller_profile") or (
                "Indian merchant exporter (PortLess): verified Indian manufacturers, third-party "
                "pre-shipment inspection (SGS/QIMA/BV/Intertek/TUV), neutral escrow payment, "
                "FOB/CIF terms, 20-40 MT lots."
            ),
            "minScore": int(request.get("min_score", 50)),
            "postedWithinDays": int(request.get("posted_within_days", 14)),
            "onlyOpenRequests": True,
            "draftReplies": bool(request.get("draft_replies", False)),
            "includeBuyerName": True,
            "maxRequestsToRead": int(request.get("max_requests_to_read", 200)),
        }
        if country:
            actor_input["buyerCountries"] = [country]
        rows = await self.run(actor_input, max_items=request.get("max_items"))
        if not rows and not self.configured:
            return self._empty()
        rfqs = []
        for r in rows:
            row = self._rfq(r)
            if country and row.get("buyer_country") and row["buyer_country"] != country:
                continue
            rfqs.append(row)
        return {"rfqs": rfqs, "total": len(rfqs), "actor": self.actor_ref}

    @staticmethod
    def _target_price_per_mt(r: dict[str, Any]) -> float | None:
        price = _safe_float(r.get("targetPrice"))
        if not price or str(r.get("targetPriceCurrency") or "USD").upper() not in ("USD", "US$", "$"):
            return None
        unit = str(r.get("quantityUnit") or "").lower()
        if unit in ("kg", "kgs", "kilogram", "kilograms"):
            return round(price * 1000.0, 2)
        if unit in ("ton", "tons", "tonne", "tonnes", "mt", "metric ton", "metric tons"):
            return round(price, 2)
        return None

    @classmethod
    def _rfq(cls, r: dict[str, Any]) -> dict[str, Any]:
        per_mt = cls._target_price_per_mt(r)
        return {
            "rfq_id": r.get("rfqId") or r.get("itemId"),
            "title": r.get("subjectEnglish") or r.get("subject") or "",
            "description": (r.get("descriptionEnglish") or r.get("description") or "")[:1000],
            "buyer_name": r.get("buyerName"),
            "buyer_country": iso2(r.get("buyerCountryCode")) or r.get("buyerCountry"),
            "quantity": _safe_float(r.get("quantity")),
            "quantity_unit": r.get("quantityUnit"),
            "posted_at": r.get("postedAt"),
            "quotes_left": _safe_int(r.get("quotesLeft")),
            "buyer_stars": _safe_int(r.get("buyerStars")),
            "verified_buyer": bool(r.get("buyerTags")),
            "target_price": _safe_float(r.get("targetPrice")),
            "target_price_currency": r.get("targetPriceCurrency"),
            "unit_price_usd": per_mt,
            "price_unit": "USD/MT" if per_mt else None,
            "incoterm": r.get("incoterm"),
            "destination": r.get("destination"),
            "deadline": r.get("deadline"),
            "url": r.get("url"),
            "quote_url": r.get("quoteUrl"),
            "contact_url": _first(r.get("contactRoute"), r.get("quoteUrl"), r.get("url")),
            "hs_code_suggested": r.get("hsCodeSuggested"),
            "fit_score": _safe_int(r.get("score")),
            "label": r.get("label"),
            "fit_reason": r.get("fitReason") or "",
            "quote_reply": r.get("quoteReply"),
            "temperature": r.get("temperature"),
            "flags": list(r.get("flags") or []),
            "sourcing_type": "rfq",
            "source_actor": r.get("sourceActor"),
        }


class ApifyExportMarketConnector(ApifyActorConnector):
    """``export-market-finder-ai`` — UN Comtrade demand per market + real unit values.

    quote(request): ``hs_code``, ``exporter_country`` (default IN), ``markets``
    (ISO-2 list, optional). Returns ``{"markets": [...]}``; each market carries
    import value, growth, the exporter's share and — the number the margin
    corridor needs — ``unit_value_usd_per_mt`` (what the market pays per MT)
    next to India's own ``exporter_unit_value_usd_per_mt``.
    """

    name = "apify_export_markets"
    actor_key = "EXPORT_MARKET_FINDER"
    default_slug = "export-market-finder-ai"
    empty_key = "markets"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        hs_code = str(request.get("hs_code") or "").strip()
        if not hs_code:
            raise ValueError("apify_export_markets: hs_code required")
        markets = [iso2(m) for m in (request.get("markets") or []) if m]
        actor_input: dict[str, Any] = {
            **self._common_input(request.get("max_items")),
            "sourceMode": "live",
            "hsCodes": [hs_code],
            "exporterCountry": (request.get("exporter_country") or "IN").upper(),
            "minScore": int(request.get("min_score", 0)),
            "minImportValueUsd": int(request.get("min_import_value_usd", 250_000)),
            "frequency": "A",
        }
        actor_input["enableAi"] = bool(request.get("enable_ai", False))  # rules are enough here
        if markets:
            # The actor's markets enum is ISO-2 (US, AE, …), not ISO-3.
            actor_input["markets"] = [m for m in markets if m]
        comtrade_key = os.getenv("COMTRADE_SUBSCRIPTION_KEY", "").strip()
        if comtrade_key:
            actor_input["comtradeKey"] = comtrade_key
        rows = await self.run(actor_input, max_items=request.get("max_items"))
        if not rows and not self.configured:
            return self._empty()
        out = [self._market(r) for r in rows if r.get("marketCountryCode") or r.get("marketCountry")]
        if markets:
            wanted = set(markets)
            out = [m for m in out if m.get("market") in wanted] or out
        return {"markets": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def _market(r: dict[str, Any]) -> dict[str, Any]:
        per_kg = _safe_float(r.get("unitValueUsdPerKg"))
        exp_per_kg = _safe_float(r.get("exporterUnitValueUsdPerKg"))
        world_per_kg = _safe_float(r.get("exporterWorldUnitValueUsdPerKg"))
        return {
            "market": iso2(r.get("marketCountryCode")) or r.get("marketCountry"),
            "market_name": r.get("marketCountry"),
            "hs_code": r.get("hsCode"),
            "hs_description": r.get("hsDescription"),
            "latest_period": r.get("latestPeriod"),
            "import_value_usd": _safe_float(r.get("importValueUsd")),
            "growth_1y_pct": _safe_float(r.get("growth1yPct")),
            "cagr_pct": _safe_float(r.get("cagrPct")),
            "exporter_value_usd": _safe_float(r.get("exporterValueUsd")),
            "exporter_share_pct": _safe_float(r.get("exporterSharePct")),
            "unit_value_usd_per_kg": per_kg,
            "unit_value_usd_per_mt": round(per_kg * 1000.0, 2) if per_kg else None,
            "exporter_unit_value_usd_per_mt": round(exp_per_kg * 1000.0, 2) if exp_per_kg else None,
            "exporter_world_unit_value_usd_per_mt": round(world_per_kg * 1000.0, 2) if world_per_kg else None,
            "top_suppliers": list(r.get("topSuppliers") or [])[:5],
            "score": _safe_int(r.get("score")),
            "flags": list(r.get("flags") or []),
            "summary": r.get("summary") or r.get("generatedText") or "",
            "next_step": r.get("recommendedNextStep") or "",
            "url": r.get("url"),
            "mirror": bool(r.get("mirror")),
        }


# ---------------------------------------------------------------------------
# Seller identification
# ---------------------------------------------------------------------------

class ApifyIndiaSupplierConnector(ApifyActorConnector):
    """``india-supplier-finder-ai`` — Indian manufacturers/exporters for an HS code.

    This actor is the seller-side twin of the buyer actors and is generated
    with ``/actor-pipeline`` (source: IndiaMART / TradeIndia Store actors +
    GSTN format check). Until it exists the connector returns an empty result
    and Phase 4 keeps the IndiaMART / TradeIndia API connectors.

    Expected dataset row (the mapper tolerates the usual aliases):
      name / companyName, city, state, productName, minPrice, maxPrice,
      priceUnit ("USD/kg" | "USD/MT" | "INR/kg"), moq, annualCapacityMt,
      url / listingUrl, contactId, bestEmail / email, phone, website,
      gstin, iec, udyam, score, fitReason, generatedText (RFQ draft)
    """

    name = "apify_india_suppliers"
    actor_key = "INDIA_SUPPLIER_FINDER"
    default_slug = "india-supplier-finder-ai"
    empty_key = "sellers"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        hs_code = str(request.get("hs_code") or "").strip()
        keyword = request.get("keyword") or request.get("query") or ""
        if not hs_code and not keyword:
            raise ValueError("apify_india_suppliers: hs_code or keyword required")
        actor_input: dict[str, Any] = {
            **self._common_input(request.get("max_items")),
            "sourceMode": "actor",
            "hsCodes": [hs_code] if hs_code else [],
            "products": [keyword] if keyword else [],
            "originCountry": "IN",
            "goalDescription": request.get("goal") or (
                f"Indian manufacturers / exporters of HS {hs_code or keyword} with GST registration, "
                f"export capacity of 20+ MT per month and a reachable sales contact."
            ),
            "minScore": int(request.get("min_score", 40)),
        }
        if request.get("state"):
            actor_input["states"] = [request["state"]]
        rows = await self.run(actor_input, max_items=request.get("max_items"))
        if not rows and not self.configured:
            return self._empty()
        sellers = [self._seller(r, hs_code) for r in rows if _first(r.get("name"), r.get("companyName"), r.get("company_name"))]
        return {"sellers": sellers, "total": len(sellers), "actor": self.actor_ref}

    @staticmethod
    def _seller(r: dict[str, Any], hs_code: str) -> dict[str, Any]:
        unit = str(_first(r.get("priceUnit"), r.get("price_unit"), "USD/kg"))
        phones = r.get("phones") or []
        phone = _first(r.get("phone"), phones[0].get("number") if phones and isinstance(phones[0], dict) else (phones[0] if phones else None))
        return {
            "company_id": _first(r.get("itemId"), r.get("dedupeKey"), r.get("companyId"), r.get("company_id")),
            "company_name": _first(r.get("name"), r.get("companyName"), r.get("company_name")),
            "city": _first(r.get("city"), r.get("locationCity"), ""),
            "state": _first(r.get("state"), r.get("locationState"), ""),
            "product_name": _first(r.get("productName"), r.get("product_name"), ""),
            "hs_code": hs_code or r.get("hsCode") or "",
            "min_price": _safe_float(_first(r.get("minPrice"), r.get("min_price"), r.get("unitPrice"))),
            "max_price": _safe_float(_first(r.get("maxPrice"), r.get("max_price"))),
            "price_unit": unit,
            "min_order_qty": _first(r.get("moq"), r.get("minOrderQty"), r.get("min_order_qty"), ""),
            "annual_capacity_mt": _safe_float(_first(r.get("annualCapacityMt"), r.get("annual_capacity_mt"))),
            "listing_url": _first(r.get("listingUrl"), r.get("url"), ""),
            "contact_id": _first(r.get("contactId"), r.get("contact_id"), ""),
            "email": _first(r.get("bestEmail"), r.get("email")),
            "phone": phone,
            "website": r.get("website"),
            "gstin": _first(r.get("gstin"), r.get("gstNumber")),
            "iec": _first(r.get("iec"), r.get("iecNumber")),
            "udyam": r.get("udyam"),
            "export_shipments_12m": _safe_int(_first(r.get("exportShipments12m"), r.get("shipments12m"))),
            "fit_score": _safe_int(r.get("score")),
            "fit_reason": r.get("fitReason") or "",
            "rfq_draft": r.get("generatedText") or "",
            "source_actor": r.get("sourceActor"),
        }


# ---------------------------------------------------------------------------
# Enrichment: contacts, screening, e-mail deliverability
# ---------------------------------------------------------------------------

class ApifyContactFinderConnector(ApifyActorConnector):
    """``company-contact-finder-ai`` — company name → website, checked e-mail, phones, VAT id.

    quote(request): ``companies`` = list of ``{"name", "country", "website"?}``,
    ``goal`` (one sentence about the offer). Returns ``{"contacts": [...]}``
    keyed by the input name (``input_name``) so callers can match results back.
    """

    name = "apify_contact_finder"
    actor_key = "COMPANY_CONTACT_FINDER"
    default_slug = "company-contact-finder-ai"
    empty_key = "contacts"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        companies = [c for c in (request.get("companies") or []) if c and c.get("name")]
        if not companies:
            raise ValueError("apify_contact_finder: companies required")
        lines = []
        for c in companies:
            parts = [str(c["name"]).strip()]
            if c.get("country"):
                parts.append(str(iso2(c["country"])))
            if c.get("website"):
                parts.append(str(c["website"]))
            lines.append(", ".join(parts))
        actor_input: dict[str, Any] = {
            **self._common_input(len(companies)),
            "sourceMode": "live",
            "companies": lines,
            "goalDescription": request.get("goal") or (
                "PortLess is an Indian merchant exporter: verified suppliers, third-party "
                "inspection and neutral escrow — we want the purchasing / import contact."
            ),
            "minScore": 1,
            "includeNotFound": True,
            "preferRoles": ["purchasing", "export", "sales", "general"],
            "domainOnly": False,
            "verifyMailbox": bool(request.get("verify_mailbox", False)),
        }
        rows = await self.run(actor_input, max_items=len(companies))
        if not rows and not self.configured:
            return self._empty()
        contacts = [self._contact(r) for r in rows]
        return {"contacts": contacts, "total": len(contacts), "actor": self.actor_ref}

    @staticmethod
    def _contact(r: dict[str, Any]) -> dict[str, Any]:
        phones = r.get("phones") or []
        phone = None
        if phones:
            p0 = phones[0]
            phone = p0.get("number") or p0.get("phone") if isinstance(p0, dict) else str(p0)
        people = []
        for p in (r.get("people") or [])[:5]:
            if isinstance(p, dict):
                people.append({"name": p.get("name"), "title": p.get("title") or p.get("role"), "email": p.get("email")})
        status = r.get("mailboxStatus") or r.get("bestEmailStatus")
        if status in ("valid", "ok"):
            status = "ok"
        elif status in ("invalid",):
            status = "invalid"
        elif status in ("risky", "catch_all", "unknown"):
            status = "risky"
        elif status is None and r.get("bestEmail"):
            status = "unverified"
        return {
            "input_name": r.get("inputName") or r.get("name"),
            "name": r.get("legalName") or r.get("name"),
            "country": iso2(r.get("inputCountry")),
            "website": r.get("website"),
            "domain": r.get("domain"),
            "confidence": _safe_float(r.get("websiteConfidence")),
            "email": r.get("bestEmail"),
            "email_role": r.get("bestEmailRole"),
            "email_status": status,
            "email_reason": r.get("bestEmailReason"),
            "emails": [e.get("email") if isinstance(e, dict) else e for e in (r.get("emails") or [])][:10],
            "phone": phone,
            "address": r.get("address"),
            "vat_id": r.get("vatId"),
            "legal_name": r.get("legalName"),
            "industry": r.get("industry"),
            "contact_route": r.get("contactRoute"),
            "contact_page_url": r.get("contactPageUrl"),
            "people": people,
            "first_line": r.get("generatedText") or "",
            "not_found": bool(r.get("notFound")),
            "not_found_reason": r.get("notFoundReason"),
            "fit_score": _safe_int(r.get("score")),
            "source_path": r.get("sourcePath"),
        }


# Sites that list OTHER companies (directories, marketplaces, associations, news) —
# a web search for "rice importers, AE" returns them next to real importers.
_NOT_A_COMPANY_DOMAINS = (
    "indiamart.", "tradeindia.", "alibaba.", "exportersindia.", "exportmandi.", "justdial.",
    "yellowpages", "wikipedia.", "linkedin.", "facebook.", "instagram.", "youtube.", "volza.",
    "zauba", "made-in-china.", "go4worldbusiness.", "kompass.", "dnb.com", "airea.net", "apeda.",
    "climateactiontracker.", "tradekey.", "ec21.", "globalsources.", "amazon.", "statista.",
    "reuters.", "economictimes.", "thehindu", "business-standard.", "gov.", "nic.in",
    # seen in live scans (2026-11-06): marketplaces, data sites, boards, trade press
    "chemicalbook.", "tradologie.", "tradeling.", "cbi.eu", "world-grain.", "sio365.", "needsinfo.",
    "uswheat.org", "kswheat.", "indianspices.com", "hazchem.", "chemicalsuppliers.", "exportgenius.",
    "connect2india.", "seair.", "eximpedia.", "importyeti.", "panjiva.", "bizvibe.", "ensun.",
)
_NOT_A_COMPANY_NAME = re.compile(
    r"\b(association|council|federation|directory|marketplace|chamber|ministry|list of|"
    r"news|price (?:list|today|trend)|market (?:report|potential|data)|how to|spices board|commission|"
    r"manufacturing process|wikipedia)\b|\.pdf\b|^https?://", re.IGNORECASE)
# Page titles written for search engines, not company names.
_SEO_TITLE = re.compile(
    r"^(?:best|top|leading|current|cheap|buy|premium|wholesale|indian|basmati|rice)\b|"
    r"\b(?:exporters?|importers?|suppliers?|manufacturers?|traders?|distributors?|wholesalers?|"
    r"markets?)\b.*\b(?:in|from|to)\b", re.IGNORECASE)


# A product listing or pack size, not a company: "88 Motta white Rice 20 KG".
_PRODUCT_TITLE = re.compile(r"\b\d+\s?(?:kg|kgs|g|gm|lb|lbs|mt|ton|tons)\b", re.IGNORECASE)
_LEGAL_SUFFIX = re.compile(
    r"\b(?:l\.?l\.?c|ltd|limited|pvt|private|inc|corp|gmbh|fze|fzco|fzc|llp|trading|co\.)\b", re.IGNORECASE)


def _clean_name(text: str) -> str:
    text = re.sub(r"^\s*welcome\s+to\s+(?:the\s+)?", "", text or "", flags=re.IGNORECASE).strip(" ,.-|")
    # "21, Warsan 1, Dubai, UAE, PO BOX 455012 Arham Foodstuff Trading L.L.C" → the name after the address
    if "," in text and re.search(r"\d|p\.?\s?o\.?\s?box", text, re.IGNORECASE):
        tail = text.rsplit(",", 1)[-1]
        tail = re.sub(r"^.*?p\.?\s?o\.?\s?box\s*\d+\s*", "", tail, flags=re.IGNORECASE).strip()
        if _LEGAL_SUFFIX.search(tail) or len(tail.split()) >= 2:
            text = tail
    return " ".join(text.split())


def _usable(name: str) -> bool:
    return bool(name) and len(name) <= 60 and not _SEO_TITLE.search(name) and not _PRODUCT_TITLE.search(name)


def _domain_label(row: dict[str, Any]) -> str:
    domain = (row.get("domain") or "").lower().removeprefix("www.")
    return domain.split(".")[0] if domain else ""


def _names_the_site(name: str, label: str) -> bool:
    """True when a title is the company's name, not a product or page heading:
    it carries a legal form (Ltd, LLC, GmbH…) or shares a word with the domain
    ("Pepperia Spices" on pepperiaexports.com; "Black Pepper" on biova.de is not)."""
    if _LEGAL_SUFFIX.search(name):
        return True
    squashed = re.sub(r"[^a-z0-9]", "", label.lower())
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 4]
    return bool(squashed) and any(w in squashed for w in words)


def _company_name(row: dict[str, Any]) -> str:
    """The company's name: its stated legal name, else a page title that names the
    site, else the website's domain ("biova.de" → "Biova")."""
    label = _domain_label(row)
    legal = _clean_name(row.get("legal_name") or "")
    legal = re.sub(r"^(?:ltd|limited|llc|inc)\.?,\s*", "", legal, flags=re.IGNORECASE)
    if legal and not _PRODUCT_TITLE.search(legal) and len(legal) <= 80 and (
            _LEGAL_SUFFIX.search(legal) or _names_the_site(legal, label)):
        return legal
    title = (row.get("name") or "").strip()
    head = _clean_name(re.split(r"\s+[|:–—-]\s+|:\s+|\s+\|\s*", title, maxsplit=1)[0])
    if _usable(head) and _names_the_site(head, label):
        return head
    if label:
        return label.replace("-", " ").title()
    return head or title


class ApifyCompanySearchConnector(ApifyActorConnector):
    """``company-contact-finder-ai`` in SEARCH mode — the buyer AND supplier discovery
    source that runs on any Apify plan.

    The actor's own Google reader turns "what the companies do, 2-letter country"
    lines (``rice importers, AE``) into company websites, then reads each site for a
    checked e-mail, phones, address and VAT id. Unlike us-importer-finder-ai /
    alibaba-rfq-leads-ai it calls no third-party Store Actor, so it is not blocked by
    a plan that cannot run public Actors.

    quote(request): ``queries`` = list of ``"what, CC"`` lines (or ``keyword`` +
    ``countries`` + ``role`` to build them), ``role`` = ``buyer`` | ``supplier``,
    ``max_results`` = company websites per line. Returns ``{"companies": [...]}`` —
    the contact-finder row shape plus ``company_name``, ``role``, ``query``.
    Directories, marketplaces and associations are dropped.
    """

    name = "apify_company_search"
    actor_key = "COMPANY_CONTACT_FINDER"
    default_slug = "company-contact-finder-ai"
    empty_key = "companies"

    _BUYER_TERMS = ("importers", "wholesale distributors")
    _SUPPLIER_TERMS = ("exporter", "manufacturer")

    @classmethod
    def build_queries(cls, keyword: str, countries: list[str], role: str) -> list[str]:
        terms = cls._SUPPLIER_TERMS if role == "supplier" else cls._BUYER_TERMS
        lines = []
        for cc in countries:
            code = iso2(cc)
            if code:
                lines.extend(f"{keyword} {t}, {code}" for t in terms)
        return lines

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        role = "supplier" if request.get("role") == "supplier" else "buyer"
        queries = [q for q in (request.get("queries") or []) if q and str(q).strip()]
        if not queries and request.get("keyword"):
            countries = request.get("countries") or (["IN"] if role == "supplier" else [])
            queries = self.build_queries(str(request["keyword"]).strip(), countries, role)
        if not queries:
            raise ValueError("apify_company_search: queries (or keyword + countries) required")
        per_query = max(1, min(int(request.get("max_results") or 5), 20))
        actor_input: dict[str, Any] = {
            **self._common_input(len(queries) * per_query),
            "sourceMode": "live",
            "searchQuery": "\n".join(queries),
            "maxSearchResults": per_query,
            "goalDescription": request.get("goal") or (
                "PortLess is an Indian merchant exporter. We want overseas importers / distributors "
                "that buy this product (buyers)." if role == "buyer" else
                "PortLess is an Indian merchant exporter. We want Indian manufacturers / exporters "
                "that make and ship this product (suppliers)."
            ),
            "minScore": int(request.get("min_score", 30)),
            "preferRoles": (["purchasing", "general", "sales"] if role == "buyer"
                            else ["export", "sales", "general"]),
        }
        rows = await self.run(actor_input, max_items=len(queries) * per_query)
        if not rows and not self.configured:
            return self._empty()
        companies: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows:
            c = ApifyContactFinderConnector._contact(r)
            domain = (c.get("domain") or "").lower()
            if c["not_found"] or not (c.get("email") or c.get("phone")):
                continue
            if any(bad in domain for bad in _NOT_A_COMPANY_DOMAINS) or _NOT_A_COMPANY_NAME.search(c.get("name") or ""):
                continue
            key = domain or (c.get("name") or "").lower()
            if key in seen:
                continue
            seen.add(key)
            found_by = str(r.get("foundBy") or "")
            c.update({
                "company_name": _company_name(c),
                "page_title": c.get("name"),
                "role": role,
                "query": found_by.removeprefix("search:").strip() or None,
                "summary": r.get("summary"),
            })
            companies.append(c)
        return {"companies": companies, "total": len(companies), "queries": queries,
                "actor": self.actor_ref}


class ApifyCounterpartyCheckConnector(ApifyActorConnector):
    """``counterparty-check-ai`` — OFAC/UN/EU/UK sanctions, GLEIF LEI, EU VIES.

    quote(request): ``counterparties`` = list of ``{"name", "country", "vat_number"?, "lei"?}``.
    Returns ``{"results": [...]}`` with a ``verdict`` of ``clear`` / ``review`` /
    ``block`` per counterparty, the hits behind it, LEI/VAT facts and the
    evidence certificate URL. ``listsUnavailable`` non-empty ⇒ never ``clear``.
    """

    name = "apify_counterparty_check"
    actor_key = "COUNTERPARTY_CHECK"
    default_slug = "counterparty-check-ai"
    empty_key = "results"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        parties = [p for p in (request.get("counterparties") or []) if p and p.get("name")]
        if not parties:
            raise ValueError("apify_counterparty_check: counterparties required")
        items = []
        for p in parties:
            item: dict[str, Any] = {"name": str(p["name"]).strip()}
            if p.get("country"):
                item["country"] = iso2(p["country"])
            if p.get("vat_number"):
                item["vatNumber"] = p["vat_number"]
            if p.get("lei"):
                item["lei"] = p["lei"]
            if p.get("registration_number"):
                item["registrationNumber"] = p["registration_number"]
            items.append(item)
        actor_input: dict[str, Any] = {
            **self._common_input(len(parties)),
            "sourceMode": "list",
            "itemsList": items,
            "counterparties": [", ".join(x for x in (i["name"], i.get("country")) if x) for i in items],
            "minScore": 0,
            "includeNoMatch": True,
            "screenParents": True,
            "checkVat": True,
            "minNameScore": int(request.get("min_name_score", 85)),
        }
        rows = await self.run(actor_input, max_items=len(parties))
        if not rows and not self.configured:
            return self._empty()
        results = [self._result(r) for r in rows]
        return {"results": results, "total": len(results), "actor": self.actor_ref}

    @staticmethod
    def _result(r: dict[str, Any]) -> dict[str, Any]:
        verdict = str(r.get("verdict") or "review").lower()
        if verdict not in ("clear", "review", "block"):
            verdict = "review"
        unavailable = list(r.get("listsUnavailable") or [])
        if verdict == "clear" and unavailable:
            verdict = "review"
        hits = []
        for h in (r.get("sanctionsHits") or [])[:10]:
            if isinstance(h, dict):
                hits.append({
                    "list": h.get("list"), "entry_name": h.get("entryName") or h.get("matchedName"),
                    "score": _safe_int(h.get("score") or h.get("nameScore")), "programme": h.get("programme") or h.get("program"),
                })
        return {
            "name": r.get("name"),
            "country": iso2(r.get("country")),
            "verdict": verdict,
            "reason": r.get("verdictReason") or r.get("generatedText") or "",
            "hits": hits,
            "hit_count": _safe_int(r.get("hitCount")) or len(hits),
            "best_hit_score": _safe_int(r.get("bestHitScore")),
            "lei": r.get("lei"),
            "lei_status": r.get("leiStatus"),
            "legal_name": r.get("legalName"),
            "jurisdiction": r.get("jurisdiction"),
            "legal_address": r.get("legalAddress"),
            "vat_number": r.get("vatNumber"),
            "vat_valid": r.get("vatValid"),
            "vat_name": r.get("vatName"),
            "lists_checked": r.get("listsChecked") or {},
            "lists_unavailable": unavailable,
            "evidence_url": r.get("evidenceUrl"),
            "checked_at": r.get("checkedAt") or r.get("scrapedAt"),
            "next_step": r.get("recommendedNextStep") or "",
            "is_sample": bool(r.get("isSample")),
            "score": _safe_int(r.get("score")),
        }


class ApifyEmailQualifierConnector(ApifyActorConnector):
    """``email-list-qualifier-ai`` — deliverability check before the first outbound mail.

    quote(request): ``emails`` (list). Returns ``{"emails": [...]}`` — an
    address missing from the output did not pass the verifier (the actor only
    delivers qualified rows), so callers treat absence as ``invalid``.
    """

    name = "apify_email_qualifier"
    actor_key = "EMAIL_LIST_QUALIFIER"
    default_slug = "email-list-qualifier-ai"
    empty_key = "emails"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        emails = sorted({str(e).strip().lower() for e in (request.get("emails") or []) if e and "@" in str(e)})
        if not emails:
            raise ValueError("apify_email_qualifier: emails required")
        actor_input: dict[str, Any] = {
            **self._common_input(len(emails)),
            "sourceMode": "actor",
            "ownSource": True,
            "emails": emails,
            "minScore": 0,
            "minVerifierScore": int(request.get("min_verifier_score", 60)),
            "allowRoleEmails": True,      # purchasing@ / sales@ are the B2B norm
            "allowCatchAll": True,        # flagged risky, not dropped
            "allowFreeProviders": True,
            "fixTypos": True,
        }
        actor_input["enableAi"] = False  # deliverability only — no ICP scoring needed
        rows = await self.run(actor_input, max_items=len(emails))
        if not rows and not self.configured:
            return self._empty()
        out = []
        seen: set[str] = set()
        for r in rows:
            email = str(r.get("email") or "").lower()
            if not email:
                continue
            seen.add(email)
            risky = bool(r.get("isCatchAll")) or (r.get("mxFound") is False) or r.get("riskType")
            out.append({
                "email": email,
                "status": "risky" if risky else "ok",
                "score": _safe_int(r.get("verifierScore")),
                "is_role": r.get("isRoleBased"),
                "is_catch_all": r.get("isCatchAll"),
                "is_disposable": r.get("isDisposable"),
                "mx_found": r.get("mxFound"),
                "typo_suggestion": r.get("typoSuggestion"),
                "corrected_from": r.get("correctedFrom"),
                "disposition": r.get("disposition"),
                "reason": r.get("reason") or r.get("dispositionNote"),
            })
        for email in emails:
            if email not in seen:
                out.append({"email": email, "status": "invalid", "score": 0, "reason": "not delivered by the verifier"})
        return {"emails": out, "total": len(out), "actor": self.actor_ref}


# ---------------------------------------------------------------------------
# Logistics: carrier / container tracking
# ---------------------------------------------------------------------------

_MILESTONE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DELIVERED", ("delivered", "proof of delivery", "pod ", "empty return", "empty container returned",
                   "received by consignee", "delivery complete")),
    ("CLEARED", ("customs cleared", "customs release", "released by customs", "cleared customs",
                 "import cleared", "clearance complete")),
    ("ARRIVED", ("arrived", "discharged", "unloaded", "at destination", "gate out", "available for pickup",
                 "vessel arrival", "berthed")),
    ("DEPARTED", ("departed", "sailed", "loaded on vessel", "on board", "vessel departure", "gate in",
                  "loaded")),
    ("IN_TRANSIT", ("in transit", "transhipment", "transshipment", "on the way", "en route", "at sea")),
)


def carrier_milestone(raw_status: Any) -> str | None:
    """Normalise a carrier's free-text event into the shield-link milestone
    vocabulary (DEPARTED / IN_TRANSIT / ARRIVED / CLEARED / DELIVERED)."""
    text = str(raw_status or "").strip().lower()
    if not text:
        return None
    upper = text.upper().replace(" ", "_")
    if upper in ("DEPARTED", "IN_TRANSIT", "ARRIVED", "CLEARED", "DELIVERED"):
        return upper
    for milestone, words in _MILESTONE_WORDS:
        if any(w in text for w in words):
            return milestone
    return None


class ApifyCarrierTrackingConnector(ApifyActorConnector):
    """``container-tracking-ai`` — carrier milestones for a booked shipment.

    quote(request): ``container_number`` / ``bl_number`` / ``booking_ref`` (at
    least one), ``carrier`` (SCAC or name, optional). Returns
    ``{"shipments": [{"ref", "carrier", "milestone", "raw_status", "location",
    "timestamp", "eta", "delivered", "events"}]}`` with ``milestone`` normalised
    through ``carrier_milestone``.

    This is the pipeline's external slot: the actor is built and published
    outside ``/actor-pipeline`` (it is not part of the pool's automatic
    research → implement → deploy loop). Until it exists on the account the
    call returns the stub, the watcher's tracking poll is a no-op and the
    delivery-triggered inspection window falls back to
    ``INSPECTION_WINDOW_FALLBACK_DAYS`` after customs clearance.
    """

    name = "apify_carrier_tracking"
    actor_key = "CARRIER_TRACKING"
    default_slug = "container-tracking-ai"
    empty_key = "shipments"
    external = True

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        refs = {k: str(request.get(k) or "").strip() for k in ("container_number", "bl_number", "booking_ref")}
        refs = {k: v for k, v in refs.items() if v}
        if not refs:
            raise ValueError("apify_carrier_tracking: container_number, bl_number or booking_ref required")
        actor_input: dict[str, Any] = {
            **self._common_input(len(refs)),
            "sourceMode": "actor",
            "containerNumbers": [refs["container_number"]] if "container_number" in refs else [],
            "blNumbers": [refs["bl_number"]] if "bl_number" in refs else [],
            "bookingRefs": [refs["booking_ref"]] if "booking_ref" in refs else [],
            "carrier": str(request.get("carrier") or ""),
            "includeEvents": True,
        }
        actor_input["enableAi"] = False  # tracking is deterministic — no scoring tier
        rows = await self.run(actor_input, max_items=len(refs))
        if not rows and not self.configured:
            return self._empty()
        shipments = []
        for r in rows:
            raw_status = _first(r.get("status"), r.get("latestEvent"), r.get("lastEvent"), r.get("milestone"))
            if isinstance(raw_status, dict):
                raw_status = _first(raw_status.get("status"), raw_status.get("description"), raw_status.get("event"))
            milestone = carrier_milestone(raw_status)
            delivered = bool(r.get("delivered")) or milestone == "DELIVERED"
            shipments.append({
                "ref": _first(r.get("containerNumber"), r.get("blNumber"), r.get("bookingRef"), r.get("ref")),
                "carrier": _first(r.get("carrier"), r.get("carrierName"), r.get("scac")),
                "milestone": "DELIVERED" if delivered else milestone,
                "raw_status": raw_status,
                "location": _first(r.get("location"), r.get("lastLocation"), r.get("currentLocation")),
                "timestamp": _first(r.get("eventAt"), r.get("lastEventAt"), r.get("timestamp"), r.get("updatedAt")),
                "eta": _first(r.get("eta"), r.get("estimatedArrival")),
                "delivered": delivered,
                "events": r.get("events") if isinstance(r.get("events"), list) else [],
            })
        return {"shipments": shipments, "total": len(shipments), "actor": self.actor_ref}


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

APIFY_CONNECTORS: tuple[type[ApifyActorConnector], ...] = (
    ApifyUsImporterConnector,
    ApifyAlibabaRfqConnector,
    ApifyExportMarketConnector,
    ApifyIndiaSupplierConnector,
    ApifyContactFinderConnector,
    ApifyCompanySearchConnector,
    ApifyCounterpartyCheckConnector,
    ApifyEmailQualifierConnector,
    ApifyCarrierTrackingConnector,
)


def describe_actors() -> dict[str, Any]:
    """Configuration snapshot for the readiness check and the UI sources panel."""
    return {
        "token_present": apify_enabled(),
        "owner": apify_owner(),
        "ai_tier": _flag("APIFY_ENABLE_AI", True),
        "max_items": _int("APIFY_MAX_ITEMS", 25),
        "max_runs_per_day": max_runs_per_day(),
        "runs_today": runs_today(),
        "actors": [cls().describe() for cls in APIFY_CONNECTORS],
    }
