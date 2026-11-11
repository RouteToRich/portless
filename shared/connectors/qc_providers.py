"""Quality-control (QC) inspection provider connectors — separated from freight.

Top-5 providers, selected for global buyer acceptance, visual evidence in the
report and availability in India (primary export origin). How each one is
reached — checked against what the provider actually publishes (2026-11):

  qima       QIMA (Hong Kong)        channel "api"    — public REST API
             (developers.qima.com): token auth, booking, order status, report
             search, PDF report download, cancel. No webhook: PortLess polls.
  sgs        SGS (Switzerland)       channel "portal" — SGS SMART is a customer
             portal; no public booking / status / report API (integration only
             under a key-account agreement).
  bv         Bureau Veritas (France) channel "portal" — BV OneSource Connect
             (app.onesourceconnect.com, successor of InSpec-BV) is a portal; no
             public API.
  intertek   Intertek (UK)           channel "api"    — generic JSON adapter, base
  tuv_sud    TÜV SÜD (Germany)       channel "api"      URL from <PREFIX>_BASE_URL
             once a commercial agreement fixes the contract.

Portal channel: ``book`` writes the booking request the provider's booking form
needs (sent to ``<PREFIX>_BOOKING_EMAIL`` by Shield-Link, or entered on the
portal by an operator); the confirmation and the report arrive on the signed QC
webhook or from an operator (``POST /shield-link/qc/{ref}/status``).

Live vs sandbox (``shared/integration_mode.py``): with ``INTEGRATION_MODE=live``
(the default) every provider is really called with the configured credentials —
a missing key or a ``.env`` placeholder is refused by the provider and surfaces
as ``ConnectorError``; a portal provider without a real booking-desk address
refuses to book. Only ``INTEGRATION_MODE=sandbox`` (automation, pytest) answers
with stubs, and there ``QC_SANDBOX_PROVIDERS=qima,sgs,bv`` (or ``all``) sends
those providers to the simulator ``services.qc_sandbox`` instead — in process
through ASGI, or at ``QC_SANDBOX_URL``. Readiness fails in production in sandbox.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx

from shared.integration_mode import sandbox as stub_mode

from .base import Connector

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0

# QC provider status vocabulary (normalised across providers)
REQUESTED = "REQUESTED"          # booking placed, not yet confirmed by the provider
CONFIRMED = "CONFIRMED"          # provider validated the order / inspector scheduled
INSPECTED = "INSPECTED"          # inspection done, report not issued yet
REPORT_ISSUED = "REPORT_ISSUED"  # report issued with a result
CANCELLED = "CANCELLED"
QC_STATUSES = (REQUESTED, CONFIRMED, INSPECTED, REPORT_ISSUED, CANCELLED)


class ConnectorError(Exception):
    """Raised when a provider call fails in a non-retryable way."""


# ---------------------------------------------------------------------------
# Sandbox switch
# ---------------------------------------------------------------------------

SANDBOX_PROVIDERS = ("qima", "sgs", "bv")
_SANDBOX_HOST = "http://qc-sandbox.local"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def sandbox_providers() -> set[str]:
    raw = os.getenv("QC_SANDBOX_PROVIDERS", "").strip().lower()
    if raw in ("all", "*", "true", "1", "yes"):
        return set(SANDBOX_PROVIDERS)
    return {p.strip() for p in raw.split(",") if p.strip() in SANDBOX_PROVIDERS}


def in_sandbox(provider: str) -> bool:
    """The provider runs on the simulator — only ever in INTEGRATION_MODE=sandbox."""
    return stub_mode() and provider in sandbox_providers()


def _sandbox_client(timeout: float = _TIMEOUT) -> httpx.AsyncClient:
    """HTTP client for the sandbox: a running one at ``QC_SANDBOX_URL`` or the
    app itself in process (no network)."""
    url = os.getenv("QC_SANDBOX_URL", "").strip().rstrip("/")
    if url:
        return httpx.AsyncClient(base_url=url, timeout=timeout)
    from services.qc_sandbox.main import app as sandbox_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox_app), base_url=_SANDBOX_HOST, timeout=timeout)


def _is_sandbox_url(url: str) -> bool:
    base = (os.getenv("QC_SANDBOX_URL", "").strip().rstrip("/") or _SANDBOX_HOST)
    return url.startswith(base + "/") or url.startswith(_SANDBOX_HOST + "/")


# ---------------------------------------------------------------------------
# Generic QC provider connector (Intertek, TÜV SÜD)
# ---------------------------------------------------------------------------

class QcProviderConnector(Connector):
    """Shared QC-inspection adapter.

    Subclasses configure the provider identity, env-var prefix, default base
    URL, webhook signature header and stub economics. The generic HTTP shape
    (bearer key + JSON on ``/quotes`` · ``/bookings``) applies until a
    commercial agreement dictates the provider's own contract.
    """

    name: str = "qc"
    display_name: str = "QC Provider"
    env_prefix: str = "QC"                      # <PREFIX>_API_KEY / _BASE_URL / _WEBHOOK_SECRET
    default_base_url: str = ""
    signature_header: str = "x-qc-signature"
    ref_prefix: str = "QC"
    channel: str = "api"
    # Stub economics — indicative one-man-day inspection fee plus a small
    # volume component so quotes scale with shipment size.
    stub_base_fee_usd: float = 300.0
    stub_fee_per_mt_usd: float = 1.5
    stub_lead_days: int = 3
    # Evidence the provider's report includes (drives buyer-facing copy).
    provides_photos: bool = True
    provides_video: bool = False

    def __init__(self) -> None:
        self._api_key = os.getenv(f"{self.env_prefix}_API_KEY", "")
        self._base_url = os.getenv(f"{self.env_prefix}_BASE_URL", self.default_base_url)
        self._webhook_secret = os.getenv(f"{self.env_prefix}_WEBHOOK_SECRET", "")

    # ── capability ────────────────────────────────────────────────────────
    def is_configured(self) -> bool:
        """A real credential is set (not empty, not a ``.env`` placeholder)."""
        from shared.integration_mode import is_placeholder
        return bool(self._api_key) and not is_placeholder(self._api_key)

    @property
    def stubbed(self) -> bool:
        """Answers come from a stub: INTEGRATION_MODE=sandbox without the simulator."""
        return stub_mode()

    @property
    def pollable(self) -> bool:
        """True when ``status`` asks the provider (or its simulator), not a stub."""
        return self.channel == "api" and not self.stubbed

    def _stub_ref(self) -> str:
        return f"{self.ref_prefix}-STUB-{uuid4().hex[:10].upper()}"

    def _stub_fee(self, quantity_mt: float) -> float:
        return round(self.stub_base_fee_usd + self.stub_fee_per_mt_usd * max(quantity_mt, 0.0), 2)

    def _estimate(self, request: dict[str, Any], *, source: str = "stub") -> dict[str, Any]:
        quantity_mt = float(request.get("quantity_mt") or 0.0)
        fee = self._stub_fee(quantity_mt)
        return {
            "provider": self.name,
            "provider_display_name": self.display_name,
            "available": True,
            "inspection_date": (date.today() + timedelta(days=self.stub_lead_days)).isoformat(),
            "location": request.get("location", "Supplier warehouse"),
            "fee_usd": fee,
            "qc_cost_per_unit_usd": round(fee / quantity_mt, 4) if quantity_mt > 0 else None,
            "currency": "USD",
            "evidence": {"photos": self.provides_photos, "video": self.provides_video},
            "price_source": source,
            "stub": stub_mode(),
        }

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Fetch inspection availability/price for a given date and location."""
        if self.stubbed:
            logger.warning("%s: INTEGRATION_MODE=sandbox — returning stub quote", type(self).__name__)
            return self._estimate(request)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{self._base_url}/quotes", json=request,
                                     headers={"Authorization": f"Bearer {self._api_key}"})
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"{self.display_name} quote failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Book an inspection appointment and return the booking reference."""
        if self.stubbed:
            ref = self._stub_ref()
            logger.warning("%s: INTEGRATION_MODE=sandbox — returning stub booking %s", type(self).__name__, ref)
            return {
                "ext_ref": ref,
                "status": "BOOKED",
                "provider_status": REQUESTED,
                "channel": "stub",
                "provider": self.name,
                "provider_display_name": self.display_name,
                "inspection_date": request.get("inspection_date")
                or (date.today() + timedelta(days=self.stub_lead_days)).isoformat(),
                "location": request.get("location", "Supplier warehouse"),
                "inspector_contact": f"inspector@{self.name}-stub.com",
                "evidence": {"photos": self.provides_photos, "video": self.provides_video},
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{self._base_url}/bookings", json=request,
                                     headers={"Authorization": f"Bearer {self._api_key}"})
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"{self.display_name} book failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Poll the status of an existing inspection booking."""
        if self.stubbed:
            return {"ext_ref": ext_ref, "status": "BOOKED", "provider": self.name, "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{self._base_url}/bookings/{ext_ref}",
                                    headers={"Authorization": f"Bearer {self._api_key}"})
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"{self.display_name} status failed: HTTP {resp.status_code}")
            return resp.json()

    async def documents(self, ext_ref: str, product_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Report files the provider holds for the order (``file_id``, ``file_name``, ``file_type``)."""
        return []

    async def download(self, ref: str) -> tuple[bytes, str]:
        """(bytes, content type) of a report file: a provider file id or a URL."""
        if ref.startswith(("http://", "https://")):
            if _is_sandbox_url(ref) and sandbox_providers():
                path = "/" + ref.split("://", 1)[1].split("/", 1)[1]
                async with _sandbox_client() as client:
                    resp = await client.get(path)
            else:
                async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
                    resp = await client.get(ref)
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"{self.display_name} report download failed: HTTP {resp.status_code}")
            return resp.content, resp.headers.get("content-type", "application/octet-stream").split(";")[0]
        raise ConnectorError(f"{self.display_name}: no file download for {ref!r}")

    async def cancel(self, ext_ref: str, reason: str) -> dict[str, Any]:
        """Cancel a booking at the provider. Providers without a cancel call
        answer ``{"cancelled": False}`` — an operator cancels it with them."""
        return {"ext_ref": ext_ref, "cancelled": False, "channel": self.channel,
                "detail": f"{self.display_name} has no cancel call — cancel it with the provider"}

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify provider webhook using HMAC-SHA256 in the provider's header."""
        if not self._webhook_secret:
            logger.warning("%s: %s_WEBHOOK_SECRET not set — accepting all webhooks", type(self).__name__, self.env_prefix)
            return True
        sig = headers.get(self.signature_header, "")
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# QIMA — the published Public API
# ---------------------------------------------------------------------------

_CONTINENT = {
    "IN": "Asia", "CN": "Asia", "VN": "Asia", "BD": "Asia", "PK": "Asia", "LK": "Asia", "TH": "Asia", "ID": "Asia",
    "MY": "Asia", "PH": "Asia", "KH": "Asia", "TR": "Europe", "AE": "Asia", "SA": "Asia", "EG": "Africa",
    "KE": "Africa", "NG": "Africa", "ZA": "Africa", "US": "North America", "MX": "North America", "BR": "South America",
    "DE": "Europe", "IT": "Europe", "ES": "Europe", "PT": "Europe", "GB": "Europe", "FR": "Europe", "NL": "Europe",
}
_COUNTRY = {
    "IN": "India", "CN": "China", "VN": "Vietnam", "BD": "Bangladesh", "PK": "Pakistan", "LK": "Sri Lanka",
    "TH": "Thailand", "ID": "Indonesia", "MY": "Malaysia", "PH": "Philippines", "KH": "Cambodia", "TR": "Turkey",
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "EG": "Egypt", "KE": "Kenya", "NG": "Nigeria",
    "ZA": "South Africa", "US": "United States", "MX": "Mexico", "BR": "Brazil", "DE": "Germany", "IT": "Italy",
    "ES": "Spain", "PT": "Portugal", "GB": "United Kingdom", "FR": "France", "NL": "Netherlands",
}

# token cache shared by every connector instance: (base_url, account) → session
_QIMA_SESSIONS: dict[tuple[str, str], dict[str, Any]] = {}


def reset_qima_sessions() -> None:
    _QIMA_SESSIONS.clear()


def _qima_date(value: Any) -> str:
    """ISO date / date → QIMA's ``dd-MMM-yyyy`` (e.g. ``18-Dec-2030``)."""
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return d.strftime("%d-%b-%Y")


def _iso_from_qima(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):  # report search: Unix ms
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    text = str(value).strip()
    for pattern in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.title() if pattern != "%Y-%m-%d" else text[:10], pattern).date().isoformat()
        except ValueError:
            continue
    return None


def map_qima_order_status(code: Any, text: Any, confirmed: Any = False) -> str:
    """QIMA order status → PortLess QC status. QIMA documents ``20`` =
    ``WAITING_FOR_VALIDATION`` and lists ``60`` as completed; any other value is
    read by its label, so a code QIMA adds later still lands somewhere sane."""
    c = str(code or "").strip()
    t = str(text or "").upper().replace(" ", "_")
    if "CANCEL" in t:
        return CANCELLED
    if c == "60" or any(k in t for k in ("COMPLET", "INSPECTED", "REPORT", "DONE", "FINISH")):
        return INSPECTED
    if c == "20":
        return REQUESTED
    if any(k in t for k in ("WAITING", "PENDING", "DRAFT", "SUBMITTED")):
        return CONFIRMED if confirmed is True else REQUESTED
    if confirmed is True or any(k in t for k in ("VALIDATED", "CONFIRMED", "SCHEDULED", "ASSIGNED", "ALLOCATED",
                                                 "IN_PROGRESS", "PROCESSING")):
        return CONFIRMED
    return REQUESTED


def qima_result(reports: list[dict[str, Any]]) -> str | None:
    """Overall result from QIMA report items (``overrallResult``, sic): any
    FAILED → FAIL; every one PASSED → PASS; anything else (e.g. PENDING, the
    client decides) → PENDING. No issued report → None."""
    issued = [r for r in reports if int(r.get("status") or 0) >= 30]
    if not issued:
        return None
    verdicts = [str(r.get("overrallResult") or r.get("overallResult") or "").upper() for r in issued]
    if any(v.startswith("FAIL") for v in verdicts):
        return "FAIL"
    if verdicts and all(v.startswith("PASS") for v in verdicts):
        return "PASS"
    return "PENDING"


class QimaConnector(QcProviderConnector):
    """QIMA Public API (developers.qima.com).

    Auth: ``POST /auth/v2/token`` with ``account``, MD5 ``password``,
    ``userType=client`` and the ``Ai-Api-Access-Token`` QIMA issues; every later
    call sends ``Ai-Api-Access-Token``, ``Ai-User-Id``, ``Authorization: Bearer``
    and ``Referer``. Booking ``POST /v1.0/inspection``; status
    ``GET /user/{userId}/inspection-order/{orderId}`` + report search
    ``GET /user/{userId}/reports``; report PDF via ``pdf-info`` → ``file``;
    cancel ``DELETE /user/{userId}/inspection/{orderId}?reason=``.

    Env: ``QIMA_API_ACCESS_TOKEN``, ``QIMA_ACCOUNT``, ``QIMA_PASSWORD`` (or
    ``QIMA_PASSWORD_MD5``), ``QIMA_BASE_URL`` (default the documented
    pre-production host ``https://ppapi.qima.com``; production host from QIMA),
    ``QIMA_REFERER``, ``QIMA_SERVICE_TYPE`` (``1`` = PSI),
    ``QIMA_PRODUCT_CATEGORY_ID`` / ``_FAMILY_ID`` / ``_TYPE_ID`` (from
    ``GET /parameter/v2/product-tree``), ``QIMA_AQL_CRITICAL`` / ``_MAJOR`` /
    ``_MINOR``, ``QIMA_SAMPLE_LEVEL``, ``QIMA_ALLOW_DATE_CHANGE``.
    """

    name = "qima"
    display_name = "QIMA"
    env_prefix = "QIMA"
    default_base_url = "https://ppapi.qima.com"
    signature_header = "x-qima-signature"
    ref_prefix = "QIMA"
    channel = "api"
    stub_base_fee_usd = 289.0
    stub_lead_days = 2  # inspector onsite within 48h
    provides_photos = True
    provides_video = True  # live-video remote inspection product option

    def __init__(self) -> None:
        super().__init__()
        self._sandbox = in_sandbox(self.name)
        self._access_token = os.getenv("QIMA_API_ACCESS_TOKEN", "") or os.getenv("QIMA_API_KEY", "")
        self._account = os.getenv("QIMA_ACCOUNT", "")
        password = os.getenv("QIMA_PASSWORD", "")
        self._password_md5 = os.getenv("QIMA_PASSWORD_MD5", "").lower() or (
            hashlib.md5(password.encode("utf-8")).hexdigest() if password else "")  # noqa: S324 — QIMA's documented hash
        self._referer = os.getenv("QIMA_REFERER", "https://preprodmy.qima.com/")
        from shared.integration_mode import is_placeholder
        real = all(v and not is_placeholder(v) for v in (self._access_token, self._account,
                                                          os.getenv("QIMA_PASSWORD", "") or self._password_md5))
        if self._sandbox and not real:
            from services.qc_sandbox import qima as sandbox
            self._access_token = sandbox.SANDBOX_ACCESS_TOKEN
            self._account = sandbox.SANDBOX_ACCOUNT
            self._password_md5 = sandbox.md5_hex(sandbox.SANDBOX_PASSWORD)
            self._referer = sandbox.REFERER

    def is_configured(self) -> bool:
        from shared.integration_mode import is_placeholder
        return self._sandbox or all(v and not is_placeholder(v) for v in (self._access_token, self._account, self._password_md5))

    @property
    def stubbed(self) -> bool:
        return stub_mode() and not self._sandbox

    # ── HTTP plumbing ─────────────────────────────────────────────────────
    def _client(self) -> httpx.AsyncClient:
        if self._sandbox:
            return _sandbox_client()
        return httpx.AsyncClient(base_url=self._base_url.rstrip("/"), timeout=_TIMEOUT)

    def _session_key(self) -> tuple[str, str]:
        return ("sandbox" if self._sandbox else self._base_url, self._account)

    async def _login(self, client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.post("/auth/v2/token", json={
            "account": self._account, "password": self._password_md5, "userType": "client",
        }, headers={"Ai-Api-Access-Token": self._access_token, "Referer": self._referer,
                    "Content-Type": "application/json"})
        if resp.status_code != 200:
            raise ConnectorError(f"QIMA sign-in failed: HTTP {resp.status_code} — {resp.text[:200]}")
        token = (resp.json().get("content") or {}).get("token") or {}
        if not token.get("token") or not token.get("userId"):
            raise ConnectorError("QIMA sign-in answered without content.token.token / userId")
        session = {"token": token["token"], "user_id": token["userId"],
                   "valid_before": int(token.get("validBefore") or 0), "refresh_key": token.get("refreshKey")}
        _QIMA_SESSIONS[self._session_key()] = session
        return session

    async def _session(self, client: httpx.AsyncClient) -> dict[str, Any]:
        session = _QIMA_SESSIONS.get(self._session_key())
        if session is None or session["valid_before"] - 60 < time.time():
            session = await self._login(client)
        return session

    def _headers(self, session: dict[str, Any]) -> dict[str, str]:
        return {"Ai-Api-Access-Token": self._access_token, "Ai-User-Id": session["user_id"],
                "Authorization": f"Bearer {session['token']}", "Referer": self._referer}

    async def _call(self, method: str, path: str, *, json_body: Any = None,
                    params: dict[str, Any] | None = None) -> httpx.Response:
        """One authenticated call; a 401 signs in again once (token revoked or expired early)."""
        async with self._client() as client:
            session = await self._session(client)
            for attempt in (1, 2):
                url = path.format(userId=session["user_id"])
                resp = await client.request(method, url, json=json_body, params=params, headers=self._headers(session))
                if resp.status_code == 401 and attempt == 1:
                    _QIMA_SESSIONS.pop(self._session_key(), None)
                    session = await self._login(client)
                    continue
                return resp
        raise ConnectorError("QIMA call failed")  # pragma: no cover — loop always returns

    @staticmethod
    def _content(resp: httpx.Response, what: str) -> Any:
        if resp.status_code not in range(200, 300):
            raise ConnectorError(f"QIMA {what} failed: HTTP {resp.status_code} — {resp.text[:300]}")
        return resp.json().get("content")

    # ── booking ───────────────────────────────────────────────────────────
    def service_type(self, inspection_type: str) -> str:
        """QIMA ``serviceType`` for a phase: PSI ``QIMA_SERVICE_TYPE`` (1), DUPRO
        ``QIMA_SERVICE_TYPE_DUPRO`` (2) — both in QIMA's documented list; PEO has no
        documented booking id, so ``QIMA_SERVICE_TYPE_PEO`` must hold the id QIMA
        gives the account (the simulator uses 5)."""
        phase = (inspection_type or "PSI").upper()
        if phase == "DUPRO":
            return os.getenv("QIMA_SERVICE_TYPE_DUPRO", "") or "2"
        if phase == "PEO":
            value = os.getenv("QIMA_SERVICE_TYPE_PEO", "") or ("5" if self._sandbox else "")
            if not value:
                raise ConnectorError("QIMA has no documented service type for a pre-customs clearance inspection (PEO) "
                                     "— set QIMA_SERVICE_TYPE_PEO to the id QIMA gives your account")
            return value
        return os.getenv("QIMA_SERVICE_TYPE", "") or "1"

    def build_inspection_order(self, request: dict[str, Any], user_id: str) -> dict[str, Any]:
        """PortLess booking request → QIMA ``InspectionOrder`` (the documented
        ``POST /v1.0/inspection`` body)."""
        supplier = request.get("supplier") or {}
        cc = str(supplier.get("country_code") or "IN").upper()
        category = os.getenv("QIMA_PRODUCT_CATEGORY_ID", "") or ("bigCat3" if self._sandbox else "")
        family = os.getenv("QIMA_PRODUCT_FAMILY_ID", "") or ("bigCat3_s1" if self._sandbox else "")
        type_id = os.getenv("QIMA_PRODUCT_TYPE_ID", "") or ("bigCat3_s1_t200" if self._sandbox else "")
        if not (category and family and type_id):
            raise ConnectorError(
                "QIMA product category not configured — set QIMA_PRODUCT_CATEGORY_ID / QIMA_PRODUCT_FAMILY_ID / "
                "QIMA_PRODUCT_TYPE_ID from GET /parameter/v2/product-tree")
        aql = request.get("aql") or {}
        quantity = float(request.get("quantity_mt") or 0)
        po = request.get("po_number") or request.get("deal_id")
        inspection_date = request.get("inspection_date") or (date.today() + timedelta(days=self.stub_lead_days)).isoformat()
        services = request.get("services") or []
        from shared.orchestration import packing as packing_mod
        spec = packing_mod.as_spec(request.get("packing"))
        checks = packing_mod.qc_checklist(spec)
        outer = "; ".join(filter(None, ["palletised" if spec and spec.palletised else "",
                                        "fumigated" if spec and spec.fumigation else "",
                                        "container lined" if spec and spec.container_lining else ""])) or None
        return {
            "userId": user_id,
            "serviceDate": _qima_date(inspection_date),
            "shipDate": _qima_date(request.get("ship_date") or inspection_date),
            "serviceType": self.service_type(request.get("inspection_type") or "PSI"),
            "bookingType": "General",
            "previousOrderNumber": request.get("previous_order_number"),
            "referenceNumber": po,
            "supplierCode": None,
            "supplierName": supplier.get("name") or "Supplier",
            "supplierContactName": supplier.get("contact_name") or supplier.get("name") or "Factory manager",
            "supplierContactPhone": supplier.get("phone") or "",
            "supplierContactEmail": supplier.get("email") or "",
            "supplierContinentID": _CONTINENT.get(cc, "Asia"),
            "supplierCountryID": _COUNTRY.get(cc, supplier.get("country") or cc),
            "supplierProvinceID": supplier.get("state"),
            "supplierCityID": supplier.get("city"),
            "supplierAddress": supplier.get("address") or request.get("location") or "",
            "supplierProductLines": [family],
            "products": [{
                "name": request.get("product_name") or f"HS {request.get('hs_code', '')}",
                "categoryId": category,
                "familyId": family,
                "typeId": type_id,
                "prodReference": request.get("hs_code"),
                "poNumber": po,
                "prodQuantity": int(round(quantity)),
                "expectedFinishedPct": int(request.get("expected_finished_pct") or 100),
                "expectedPackedPct": (0 if (request.get("inspection_type") or "PSI") == "DUPRO"
                                      else int(request.get("min_packed_pct") or 80)),
                "unitPacking": packing_mod.describe(spec) or None,
                "outerPacking": outer,
                "sampleReferenceClientComments": " | ".join(filter(None, [*checks, request.get("note")])) or None,
                "customField1": f"Quantity {quantity:g} MT (metric tonnes)",
                "refs": [{"poNumber": po, "skuCode": request.get("hs_code") or "", "qty": int(round(quantity))}],
                "checkList": {
                    "sampleLevel": aql.get("sample_level") or os.getenv("QIMA_SAMPLE_LEVEL", "II"),
                    "aqlCritical": str(aql.get("critical") or os.getenv("QIMA_AQL_CRITICAL", "0")),
                    "aqlMajor": str(aql.get("major") or os.getenv("QIMA_AQL_MAJOR", "2.5")),
                    "aqlMinor": str(aql.get("minor") or os.getenv("QIMA_AQL_MINOR", "4.0")),
                },
            }],
            "customField1": request.get("deal_id"),
            "customField2": request.get("tpo_id"),
            "customField3": " · ".join(filter(None, [
                request.get("inspection_type") or "PSI",
                "LOADING_SUPERVISION requested" if "LOADING_SUPERVISION" in services else "",
            ])),
            "performInQimaone": None,
            "allowChangeInspectionDate": os.getenv("QIMA_ALLOW_DATE_CHANGE", "true").lower() in ("1", "true", "yes"),
        }

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """QIMA publishes no quote endpoint (the price is on the order once
        priced: ``orderPrice``); the catalogue fee is the estimate."""
        return self._estimate(request, source="catalogue_estimate")

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.stubbed:
            return await super().book(request)
        async with self._client() as client:
            session = await self._session(client)
        body = self.build_inspection_order(request, session["user_id"])
        resp = await self._call("POST", "/v1.0/inspection", json_body=body)
        if resp.status_code == 409:
            # Duplicate: the order already exists (a retry after a crash) — find it.
            existing = await self._find_order(body["referenceNumber"], body["serviceDate"])
            if existing is None:
                raise ConnectorError(f"QIMA answered 409 duplicate booking and the order was not found: {resp.text[:200]}")
            detail = await self._order_detail(existing)
            return {**self._normalise_order(detail), "duplicate": True, "request": body}
        content = self._content(resp, "booking")
        normalised = self._normalise_order(content)
        return {**normalised, "message": resp.json().get("message"), "http_status": resp.status_code, "request": body}

    async def _find_order(self, reference: str, service_date: str) -> str | None:
        resp = await self._call("GET", "/user/{userId}/inspection-orders",
                                params={"keyword": reference, "pageSize": 50, "pageNumber": 1})
        for item in (self._content(resp, "order search") or {}).get("pageItems") or []:
            if item.get("clientReference") == reference and str(item.get("serviceDate", "")).lower() == service_date.lower():
                return item.get("orderId")
        return None

    async def _order_detail(self, order_id: str) -> dict[str, Any]:
        return self._content(await self._call("GET", f"/user/{{userId}}/inspection-order/{order_id}"), "order detail") or {}

    def _normalise_order(self, content: dict[str, Any]) -> dict[str, Any]:
        gi = content.get("orderGeneralInfo") or {}
        extra = content.get("orderExtra") or {}
        products = [p.get("productBean", {}).get("productId") for p in content.get("products") or []]
        return {
            "ext_ref": gi.get("orderId"),
            "provider_order_number": gi.get("orderNumber"),
            "status": "BOOKED",
            "provider_status": map_qima_order_status(gi.get("status"), gi.get("statusText"), gi.get("confirmed")),
            "provider_status_code": str(gi.get("status") or ""),
            "provider_status_text": gi.get("statusText"),
            "inspection_date": _iso_from_qima(gi.get("actualInspectionDate") or gi.get("expectedInspectionDate")),
            "cancelable": extra.get("cancelable"),
            "product_ids": [p for p in products if p],
            "price": content.get("orderPrice"),
            "provider": self.name,
            "provider_display_name": self.display_name,
            "channel": "sandbox" if self._sandbox else "api",
            "evidence": {"photos": self.provides_photos, "video": self.provides_video},
            "stub": False,
        }

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Order status + (once issued) the report result and defect counts."""
        if self.stubbed:
            return await super().status(ext_ref)
        detail = await self._order_detail(ext_ref)
        out = self._normalise_order(detail)
        out["result"] = None
        out["reports"] = []
        if out["provider_status"] != CANCELLED and out["provider_order_number"]:
            resp = await self._call("GET", "/user/{userId}/reports",
                                    params={"keyword": out["provider_order_number"], "page": 1, "page-size": 50})
            items = [r for r in (self._content(resp, "report search") or {}).get("pageItems") or []
                     if r.get("orderId") == ext_ref]
            out["reports"] = [{
                "product_id": r.get("productId"), "report_status": r.get("status"), "result": r.get("overrallResult"),
                "inspection_date": _iso_from_qima(r.get("inspectionDate")),
                "defects": {k: r.get(k) for k in ("criticalFound", "majorFound", "minorFound",
                                                   "criticalMax", "majorMax", "minorMax")},
                "workmanship": r.get("workmanshipResult"), "remark": r.get("reportRemark"),
            } for r in items]
            out["result"] = qima_result(items)
            if out["result"] is not None:
                out["provider_status"] = REPORT_ISSUED
                out["product_ids"] = [r.get("productId") for r in items if r.get("productId")] or out["product_ids"]
        return out

    async def documents(self, ext_ref: str, product_ids: list[str] | None = None) -> list[dict[str, Any]]:
        if self.stubbed:
            return []
        ids = list(product_ids or [])
        if not ids:
            ids = (await self.status(ext_ref)).get("product_ids") or []
        files: list[dict[str, Any]] = []
        for pid in ids:
            resp = await self._call("GET", f"/user/{{userId}}/report/{pid}/pdf-info", params={"isSingleFile": "true"})
            for meta in self._content(resp, "report pdf-info") or []:
                files.append({"file_id": meta.get("id"), "file_name": meta.get("fileName"),
                              "file_type": meta.get("fileType"), "size": meta.get("fileSize"), "product_id": pid})
        return files

    async def download(self, ref: str) -> tuple[bytes, str]:
        if ref.startswith(("http://", "https://")):
            return await super().download(ref)
        resp = await self._call("GET", f"/user/{{userId}}/file/{ref}")
        if resp.status_code not in range(200, 300):
            raise ConnectorError(f"QIMA file download failed: HTTP {resp.status_code}")
        mime = resp.headers.get("content-type", "application/octet-stream").split(";")[0]
        if mime == "application/octet-stream" and resp.content[:5] == b"%PDF-":
            mime = "application/pdf"
        return resp.content, mime

    async def cancel(self, ext_ref: str, reason: str) -> dict[str, Any]:
        if self.stubbed:
            return {"ext_ref": ext_ref, "cancelled": True, "stub": True}
        resp = await self._call("DELETE", f"/user/{{userId}}/inspection/{ext_ref}", params={"reason": reason or "request by client"})
        return {"ext_ref": ext_ref, "cancelled": bool(self._content(resp, "cancel")), "channel": "api"}


# ---------------------------------------------------------------------------
# Portal channel — SGS SMART, Bureau Veritas OneSource Connect
# ---------------------------------------------------------------------------

class PortalQcConnector(QcProviderConnector):
    """Provider booked through its customer portal / booking desk (no public API)."""

    channel = "portal"
    portal_name: str = ""
    portal_url: str = ""

    def __init__(self) -> None:
        super().__init__()
        self._sandbox = in_sandbox(self.name)
        self._booking_email = os.getenv(f"{self.env_prefix}_BOOKING_EMAIL", "").strip()

    def is_configured(self) -> bool:
        """A real booking-desk address is set."""
        return bool(_EMAIL.fullmatch(self._booking_email or ""))

    @property
    def stubbed(self) -> bool:
        return stub_mode() and not self._sandbox

    @property
    def booking_email(self) -> str:
        return self._booking_email

    @property
    def pollable(self) -> bool:
        return False  # status is pushed (webhook / operator), never polled

    def booking_request(self, request: dict[str, Any], ref: str) -> dict[str, Any]:
        """What the provider's booking form asks for — sent to the booking desk."""
        from shared.orchestration import packing as packing_mod
        supplier = request.get("supplier") or {}
        aql = request.get("aql") or {}
        services = request.get("services") or ["PRE_SHIPMENT_INSPECTION"]
        return {
            "portless_ref": ref,
            "deal_id": request.get("deal_id"),
            "po_number": request.get("po_number"),
            "services": services,
            "inspection_type": request.get("inspection_type") or "PSI",
            "notes": request.get("note"),
            "inspection_date": request.get("inspection_date"),
            "goods_ready_date": request.get("goods_ready_date"),
            "ship_date": request.get("ship_date"),
            "product": {"description": request.get("product_name"), "hs_code": request.get("hs_code"),
                        "quantity_mt": request.get("quantity_mt")},
            "inspection_site": {"company": supplier.get("name"), "address": supplier.get("address") or request.get("location"),
                                "city": supplier.get("city"), "country": supplier.get("country_code"),
                                "contact": supplier.get("contact_name") or supplier.get("name"),
                                "phone": supplier.get("phone"), "email": supplier.get("email")},
            "sampling": {"standard": "ISO 2859-1", "level": aql.get("sample_level") or "II",
                         "aql_critical": aql.get("critical") or "0", "aql_major": aql.get("major") or "2.5",
                         "aql_minor": aql.get("minor") or "4.0"},
            "readiness": {"produced_pct": int(request.get("expected_finished_pct") or 100),
                          "packed_pct_min": (0 if (request.get("inspection_type") or "PSI") == "DUPRO"
                                             else int(request.get("min_packed_pct") or 80))},
            "packing": {"required": packing_mod.describe(request.get("packing")) or "supplier's standard export packing",
                        "checks": packing_mod.qc_checklist(request.get("packing"))},
            "report_to": request.get("report_to"),
        }

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._estimate(request, source="catalogue_estimate")

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        ref = f"{self.ref_prefix}-REQ-{uuid4().hex[:10].upper()}"
        booking = self.booking_request(request, ref)
        if self.stubbed:
            logger.warning("%s: INTEGRATION_MODE=sandbox — stub portal booking %s", type(self).__name__, ref)
            return {"ext_ref": ref, "status": "BOOKED", "provider_status": REQUESTED, "channel": "stub",
                    "portal": self.portal_name, "booking_request": booking, "provider": self.name,
                    "provider_display_name": self.display_name, "inspection_date": request.get("inspection_date"),
                    "evidence": {"photos": self.provides_photos, "video": self.provides_video}, "stub": True}
        if not self._sandbox and not self.is_configured():
            raise ConnectorError(
                f"{self.display_name}: {self.env_prefix}_BOOKING_EMAIL is not a real address — set the "
                f"{self.portal_name} booking-desk e-mail (the booking request is sent there)")
        if self._sandbox:
            async with _sandbox_client() as client:
                resp = await client.post(f"/_sandbox/{self.name}/bookings", json=booking)
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"{self.display_name} sandbox refused the booking: HTTP {resp.status_code} — {resp.text[:200]}")
        return {
            "ext_ref": ref,
            "status": "BOOKED",
            "provider_status": REQUESTED,
            "channel": "sandbox" if self._sandbox else "portal",
            "portal": self.portal_name,
            "portal_url": self.portal_url,
            "booking_email": self._booking_email or None,
            "booking_request": booking,
            "provider": self.name,
            "provider_display_name": self.display_name,
            "inspection_date": request.get("inspection_date"),
            "evidence": {"photos": self.provides_photos, "video": self.provides_video},
            "stub": False,
        }

    async def status(self, ext_ref: str) -> dict[str, Any]:
        return {"ext_ref": ext_ref, "status": None, "provider": self.name, "channel": "portal",
                "detail": f"{self.portal_name} has no status API — updates arrive by webhook or from an operator"}


class SgsConnector(PortalQcConnector):
    """SGS — world's largest TIC firm; CRF is the trade standard. Booked on SGS SMART."""

    name = "sgs"
    display_name = "SGS"
    env_prefix = "SGS"
    signature_header = "x-sgs-signature"
    ref_prefix = "SGS"
    portal_name = "SGS SMART"
    portal_url = "https://www.sgs.com/en/services/sgs-smart"
    stub_base_fee_usd = 320.0
    stub_lead_days = 3
    provides_photos = True
    provides_video = True  # QiiQ remote inspection


class BureauVeritasConnector(PortalQcConnector):
    """Bureau Veritas — #2 globally; booked on BV OneSource Connect."""

    name = "bv"
    display_name = "Bureau Veritas"
    env_prefix = "BUREAU_VERITAS"
    signature_header = "x-bv-signature"
    ref_prefix = "BV"
    portal_name = "BV OneSource Connect"
    portal_url = "https://app.onesourceconnect.com"
    stub_base_fee_usd = 340.0
    stub_lead_days = 3
    provides_photos = True
    provides_video = False


class IntertekConnector(QcProviderConnector):
    """Intertek — InterLink 2.0 ready-to-integrate APIs; eReporting photos."""

    name = "intertek"
    display_name = "Intertek"
    env_prefix = "INTERTEK"
    default_base_url = "https://api.intertek.com/interlink/v2"
    signature_header = "x-intertek-signature"
    ref_prefix = "ITK"
    stub_base_fee_usd = 330.0
    stub_lead_days = 3
    provides_photos = True
    provides_video = False


class TuvSudConnector(QcProviderConnector):
    """TÜV SÜD — premium EU buyer acceptance; digital inspection reports."""

    name = "tuv_sud"
    display_name = "TÜV SÜD"
    env_prefix = "TUV_SUD"
    default_base_url = "https://api.tuvsud.com/inspection/v1"
    signature_header = "x-tuvsud-signature"
    ref_prefix = "TUV"
    stub_base_fee_usd = 380.0
    stub_lead_days = 4
    provides_photos = True
    provides_video = False


# ---------------------------------------------------------------------------
# Registry + buyer-facing catalog
# ---------------------------------------------------------------------------

QC_CONNECTOR_CLASSES: dict[str, type[QcProviderConnector]] = {
    "sgs": SgsConnector,
    "qima": QimaConnector,
    "bv": BureauVeritasConnector,
    "intertek": IntertekConnector,
    "tuv_sud": TuvSudConnector,
}

# Buyer-facing metadata for each supported provider. Served by
# GET /shield-link/qc/providers and injected into negotiation emails so the
# buyer can opt for the inspector they trust.
QC_PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "sgs": {
        "provider": "sgs",
        "display_name": "SGS",
        "headquarters": "Switzerland",
        "acceptance": "Global standard — CRF rarely disputed by buyers or banks",
        "api_integration": "Portal (SGS SMART) — no public API; booking request to the SGS desk, result by webhook or operator",
        "channel": "portal",
        "evidence": {"photos": True, "video": True},
        "available_in_india": True,
        "indicative_fee_usd": 320.0,
        "notes": "Default provider. QiiQ remote inspection adds live video.",
    },
    "qima": {
        "provider": "qima",
        "display_name": "QIMA",
        "headquarters": "Hong Kong",
        "acceptance": "Widely accepted by importers; ISO 17020 accredited",
        "api_integration": "Public REST API (developers.qima.com) — booking, status poll, report PDF, cancel",
        "channel": "api",
        "evidence": {"photos": True, "video": True},
        "available_in_india": True,
        "indicative_fee_usd": 289.0,
        "notes": "Most API-native; same-day photo-rich reports; onsite within 48h.",
    },
    "bv": {
        "provider": "bv",
        "display_name": "Bureau Veritas",
        "headquarters": "France",
        "acceptance": "Global standard — strong in government PSI programs",
        "api_integration": "Portal (BV OneSource Connect) — no public API; booking request to the BV desk, result by webhook or operator",
        "channel": "portal",
        "evidence": {"photos": True, "video": False},
        "available_in_india": True,
        "indicative_fee_usd": 340.0,
        "notes": "Best-in-class buyer acceptance alongside SGS.",
    },
    "intertek": {
        "provider": "intertek",
        "display_name": "Intertek",
        "headquarters": "United Kingdom",
        "acceptance": "Widely accepted by Western buyers (Total Quality Assurance)",
        "api_integration": "InterLink 2.0 ready-to-integrate APIs",
        "channel": "api",
        "evidence": {"photos": True, "video": False},
        "available_in_india": True,
        "indicative_fee_usd": 330.0,
        "notes": "eReporting embeds photos, attachments and e-signatures.",
    },
    "tuv_sud": {
        "provider": "tuv_sud",
        "display_name": "TÜV SÜD",
        "headquarters": "Germany",
        "acceptance": "Premium recognition with EU buyers, esp. regulated goods",
        "api_integration": "Enterprise (customer portal integration)",
        "channel": "api",
        "evidence": {"photos": True, "video": False},
        "available_in_india": True,
        "indicative_fee_usd": 380.0,
        "notes": "Strongest choice for DACH/EU-destination deals.",
    },
}

# Free-text names a buyer might use in an email, mapped to registry keys.
_QC_PROVIDER_ALIASES: dict[str, str] = {
    "sgs": "sgs",
    "qima": "qima",
    "asiainspection": "qima",
    "asia inspection": "qima",
    "bv": "bv",
    "bureau veritas": "bv",
    "bureau_veritas": "bv",
    "bureauveritas": "bv",
    "intertek": "intertek",
    "tuv": "tuv_sud",
    "tuv sud": "tuv_sud",
    "tuv_sud": "tuv_sud",
    "tuvsud": "tuv_sud",
    "tüv": "tuv_sud",
    "tüv süd": "tuv_sud",
}


def resolve_qc_provider(name: str | None) -> str | None:
    """Map a free-text provider name/alias to a registry key, or None."""
    if not name:
        return None
    key = re.sub(r"[\s\-]+", " ", str(name).strip().lower())
    return _QC_PROVIDER_ALIASES.get(key) or _QC_PROVIDER_ALIASES.get(key.replace(" ", ""))


def detect_qc_provider_mention(text: str | None) -> str | None:
    """Deterministic scan of free text for exactly one supported provider name.

    Used as a fallback when the LLM intent classifier is unavailable. Returns
    the registry key only when a single distinct provider is mentioned —
    multiple mentions are ambiguous and return None.
    """
    if not text:
        return None
    lowered = text.lower()
    found: set[str] = set()
    for alias, key in _QC_PROVIDER_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            found.add(key)
    return found.pop() if len(found) == 1 else None


def enabled_qc_providers() -> list[str]:
    """Providers PortLess books today, in catalogue order. ``QC_PROVIDERS_ENABLED``
    (comma list or ``all``; default ``qima`` — the only one with a live API
    integration). The other connectors stay in the code, dormant."""
    raw = os.getenv("QC_PROVIDERS_ENABLED", "qima").strip().lower()
    if raw in ("all", "*"):
        return list(QC_PROVIDER_CATALOG)
    wanted = {resolve_qc_provider(p) or p.strip() for p in raw.split(",") if p.strip()}
    return [k for k in QC_PROVIDER_CATALOG if k in wanted] or ["qima"]


def qc_provider_enabled(provider: str | None) -> bool:
    return bool(provider) and provider in enabled_qc_providers()


def enabled_qc_catalog() -> dict[str, dict[str, Any]]:
    return {k: QC_PROVIDER_CATALOG[k] for k in enabled_qc_providers()}


def bookable_qc_provider(name: str | None) -> str:
    """The provider to actually book for a (buyer's) choice: the choice when it
    is enabled, else the cheapest enabled provider."""
    key = resolve_qc_provider(name)
    return key if qc_provider_enabled(key) else cheapest_qc_provider()


def cheapest_qc_provider(candidates: list[str] | None = None) -> str:
    """Registry key of the cheapest enabled provider by indicative fee.

    ``candidates`` restricts the choice to the buyer's named acceptable
    agencies (resolved keys); unknown or disabled names are ignored. With no
    (valid) candidates every enabled provider competes — used when the buyer
    left the choice open ("book whichever is cheaper").
    """
    enabled = enabled_qc_providers()
    keys = [k for k in (candidates or []) if k in enabled] or enabled
    return min(keys, key=lambda k: QC_PROVIDER_CATALOG[k]["indicative_fee_usd"])


def get_qc_connector(provider: str) -> QcProviderConnector:
    """Return an instantiated QC connector for the given provider name/alias.

    Unknown providers fall back to SGS (the default) so legacy records with
    free-text provider names keep working.
    """
    key = resolve_qc_provider(provider) or str(provider or "").strip().lower()
    cls = QC_CONNECTOR_CLASSES.get(key, SgsConnector)
    return cls()
