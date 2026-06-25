"""Regulatory & verification connectors (GSTN, DGFT, MCA, Udyam, VIES, Allianz Trade).

All adapters implement the uniform Connector interface.

Indian Government API Gateway (API Setu)
-----------------------------------------
GSTN, MCA, and Udyam use the Government of India's API Setu gateway at
``https://apisetu.gov.in``. A single ``API_SETU_KEY`` + ``API_SETU_CLIENT_ID``
covers all three.

Registration:
  1. Create a developer account at https://apisetu.gov.in
  2. Apply for GSTN, MCA, and Udyam API products
  3. Retrieve API key and client ID from your dashboard
  4. Use ``SANDBOX_MODE=1`` env var to target sandbox endpoints during testing

DGFT
----
The DGFT IEC lookup API is a free public API hosted by
https://developer.dgft.gov.in — no key required for basic IEC status checks.

VIES
----
EU VAT validation via the European Commission's VIES service is fully public:
https://ec.europa.eu/taxation_customs/vies — no key required.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

from .base import Connector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_age_years(date_str: str) -> float:
    """Parse an ISO-8601 date string and return age in years from today."""
    if not date_str:
        return 0.0
    try:
        inc = date.fromisoformat(date_str[:10])
        return round((date.today() - inc).days / 365.25, 1)
    except (ValueError, TypeError):
        return 0.0


def _apisetu_headers(key: str, client_id: str) -> dict[str, str]:
    h = {"X-API-Key": key, "Accept": "application/json"}
    if client_id:
        h["X-Client-Id"] = client_id
    return h


# ---------------------------------------------------------------------------
# GSTN — GST Network verification
# ---------------------------------------------------------------------------

class GstnConnector(Connector):
    """GSTN via API Setu: validates GSTIN status and filing compliance.

    Verifies that a supplier's GST registration is ACTIVE and has no major
    filing defaults before a deal is financially committed.

    API reference : https://apisetu.gov.in/gstn/v1
    Env vars      : API_SETU_KEY, API_SETU_CLIENT_ID
    Sandbox       : set GSTN_USE_SANDBOX=1 to target the sandbox base URL
    """

    name = "gstn"
    _PROD_BASE = "https://apisetu.gov.in/gstn/v1"
    _SAND_BASE = "https://sandbox.apisetu.gov.in/gstn/v1"

    def __init__(self) -> None:
        self._key = os.environ["API_SETU_KEY"]
        self._client_id = os.environ.get("API_SETU_CLIENT_ID", "")
        self._base = (
            self._SAND_BASE if os.environ.get("GSTN_USE_SANDBOX") else self._PROD_BASE
        )

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Validate a GSTIN number.

        Expected request keys:
            gstin  (str) – 15-character GST identification number

        Returns:
            gstin, status ("ACTIVE" | "CANCELLED" | "SUSPENDED" | "UNKNOWN"),
            legal_name, registration_date, taxpayer_type
        """
        gstin = request["gstin"].strip().upper()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._base}/gstns/{gstin}",
                headers=_apisetu_headers(self._key, self._client_id),
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "gstin": gstin,
            "status": (
                data.get("gstnStatus") or data.get("status") or "UNKNOWN"
            ).upper(),
            "legal_name": (
                data.get("legalNameOfBusiness") or data.get("tradeName") or ""
            ),
            "registration_date": data.get("registrationDate") or "",
            "taxpayer_type": data.get("taxpayerType") or "",
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("GSTN has no booking operation")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        return await self.quote({"gstin": ext_ref})

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("GSTN does not issue webhooks")


# ---------------------------------------------------------------------------
# DGFT — IEC & denied-party verification
# ---------------------------------------------------------------------------

class DgftConnector(Connector):
    """DGFT Developer Portal: IEC status + denied-party list check.

    Verifies that a supplier holds an active Importer-Exporter Code and is
    not listed on DGFT's Denied Entities register.

    API reference : https://developer.dgft.gov.in
    Auth          : No key required for basic IEC status queries.
    Denied list   : The full denied-entity list is a downloadable file.
                    Production deployments should cache it and check locally.
    """

    name = "dgft"
    _IEC_BASE = "https://www.dgft.gov.in/CP/iecList/GovtAPI"

    def __init__(self) -> None:
        # No authentication required for DGFT public APIs
        pass

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Verify IEC number status.

        Expected request keys:
            iec  (str) – 10-digit IEC number

        Returns:
            iec, status ("ACTIVE" | "CANCELLED" | "SURRENDERED" | "UNKNOWN"),
            entity_name, nature_of_concern
        """
        iec = request["iec"].strip().upper()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._IEC_BASE}/IECDetails/{iec}",
                headers={"Accept": "application/json"},
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "iec": iec,
            "status": (data.get("iecStatus") or data.get("status") or "UNKNOWN").upper(),
            "entity_name": data.get("entityName") or "",
            "nature_of_concern": data.get("natureOfConcern") or "",
            "file_number": data.get("fileNumber") or "",
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Check if an entity is on the DGFT denied-party list.

        NOTE: The DGFT denied entity list is distributed as a downloadable
        PDF/CSV document at dgft.gov.in rather than a queryable live API.
        This stub returns ``on_denied_list=False`` for all queries.  Production
        deployments should download and cache the list via a scheduled job and
        implement name-matching logic against the cached data.

        Expected request keys (optional):
            entity_name  (str) – legal entity name to check
            iec          (str) – IEC number to check

        Returns:
            on_denied_list (bool), checked_at (ISO-8601 string)
        """
        logger.debug(
            "DGFT denied-list check is a stub (always returns False): %s", request
        )
        return {
            "on_denied_list": False,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "note": "Stub — cache dgft.gov.in denied list in production",
        }

    async def status(self, ext_ref: str) -> dict[str, Any]:
        return await self.quote({"iec": ext_ref})

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("DGFT does not issue webhooks")


# ---------------------------------------------------------------------------
# MCA — Company registration & age
# ---------------------------------------------------------------------------

class McaConnector(Connector):
    """MCA21 (Ministry of Corporate Affairs) via API Setu.

    Checks CIN status, incorporation date (→ business age), and director
    listings.  Ensures the supplier is a legitimately registered company and
    not a shell or broker.

    API reference : https://apisetu.gov.in/mca/v3
    Env vars      : API_SETU_KEY, API_SETU_CLIENT_ID
    """

    name = "mca"
    _BASE = "https://apisetu.gov.in/mca/v3"

    def __init__(self) -> None:
        self._key = os.environ["API_SETU_KEY"]
        self._client_id = os.environ.get("API_SETU_CLIENT_ID", "")

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Look up a company by CIN (Corporate Identification Number).

        Expected request keys:
            cin  (str) – 21-character CIN, e.g. ``"U74999TN2010PTC078123"``

        Returns:
            cin, company_name, company_status ("ACTIVE" | "DEFUNCT" | ...),
            incorporation_date (ISO-8601), age_years (float), category
        """
        cin = request["cin"].strip().upper()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._BASE}/companies/{cin}",
                headers=_apisetu_headers(self._key, self._client_id),
            )
            resp.raise_for_status()
            data = resp.json()

        inc_date = data.get("dateOfIncorporation") or ""
        return {
            "cin": cin,
            "company_name": data.get("companyName") or "",
            "company_status": (data.get("companyStatus") or "UNKNOWN").upper(),
            "incorporation_date": inc_date,
            "age_years": _compute_age_years(inc_date),
            "category": data.get("companyCategory") or "",
            "class_of_company": data.get("classOfCompany") or "",
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("MCA has no booking operation")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        return await self.quote({"cin": ext_ref})

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("MCA does not issue webhooks")


# ---------------------------------------------------------------------------
# Udyam — MSME registration
# ---------------------------------------------------------------------------

class UdyamConnector(Connector):
    """Udyam / MSME Registry via API Setu.

    Confirms MSME certification (micro / small / medium enterprise).  Filters
    out retail brokers and validates that RFQs are sent to real manufacturing
    units eligible for government export incentives.

    API reference : https://apisetu.gov.in/udyam/v1
    Env vars      : API_SETU_KEY, API_SETU_CLIENT_ID
    """

    name = "udyam"
    _BASE = "https://apisetu.gov.in/udyam/v1"

    def __init__(self) -> None:
        self._key = os.environ["API_SETU_KEY"]
        self._client_id = os.environ.get("API_SETU_CLIENT_ID", "")

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Verify a Udyam Registration Number.

        Expected request keys:
            udyam_number  (str) – e.g. ``"UDYAM-TN-01-0000123"``

        Returns:
            udyam_number, registered (bool), enterprise_name, msme_type
            ("micro" | "small" | "medium")
        """
        udyam_no = request["udyam_number"].strip().upper()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._BASE}/registrations/{udyam_no}",
                headers=_apisetu_headers(self._key, self._client_id),
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "udyam_number": udyam_no,
            "registered": bool(data.get("isActive", False)),
            "enterprise_name": data.get("enterpriseName") or "",
            "msme_type": (data.get("msmeType") or "").lower(),
            "activity": data.get("majorActivity") or "",
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Udyam has no booking operation")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        return await self.quote({"udyam_number": ext_ref})

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("Udyam does not issue webhooks")


# ---------------------------------------------------------------------------
# VIES — EU VAT validation
# ---------------------------------------------------------------------------

class ViesConnector(Connector):
    """EU VIES (VAT Information Exchange System) — European Commission.

    Validates European buyer VAT numbers in real-time.  Prevents onboarding of
    fake or unregistered EU entities at zero cost.

    API reference : https://ec.europa.eu/taxation_customs/vies/rest-api
    Auth          : None — fully public European Commission API.
    """

    name = "vies"
    _BASE = "https://ec.europa.eu/taxation_customs/vies/rest-api"

    def __init__(self) -> None:
        # No API key required
        pass

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Validate an EU VAT registration number.

        Expected request keys:
            country_code  (str) – 2-letter EU member state code, e.g. ``"DE"``
            vat_number    (str) – VAT number without the country prefix

        Returns:
            vat_number, country_code, valid (bool), company_name, company_address
        """
        country = request["country_code"].strip().upper()
        vat = request["vat_number"].strip().upper()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self._BASE}/ms/{country}/vat/{vat}",
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "vat_number": vat,
            "country_code": country,
            "valid": bool(data.get("isValid", False)),
            "company_name": data.get("traderName") or "",
            "company_address": data.get("traderAddress") or "",
            "request_date": data.get("requestDate") or "",
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("VIES is a read-only verification service")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError("VIES has no order status")

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("VIES does not issue webhooks")


# ---------------------------------------------------------------------------
# Allianz Trade — corporate risk & trade credit insurance
# ---------------------------------------------------------------------------

class AllianzTradeConnector(Connector):
    """Allianz Trade (Euler Hermes): corporate risk scores and trade credit insurance.

    Used by Trade-Min Agent to auto-underwrite credit limits for overseas buyers.
    Flags if 100 % advance escrow is mandatory (high-risk counterparty).

    API reference : https://developer.allianz-trade.com
    Env vars      : ALLIANZ_TRADE_CLIENT_ID, ALLIANZ_TRADE_CLIENT_SECRET
                    ALLIANZ_TRADE_WEBHOOK_SECRET  (for webhook_verify)
    """

    name = "allianz_trade"
    _BASE = "https://api.allianz-trade.com/v1"
    _TOKEN_URL = "https://auth.allianz-trade.com/oauth2/token"

    def __init__(self) -> None:
        self._client_id = os.environ["ALLIANZ_TRADE_CLIENT_ID"]
        self._client_secret = os.environ["ALLIANZ_TRADE_CLIENT_SECRET"]
        self._webhook_secret = os.environ.get("ALLIANZ_TRADE_WEBHOOK_SECRET", "")

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(
            self._TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return str(resp.json()["access_token"])

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Fetch dynamic credit risk score and auto-underwritten insurance limit.

        Expected request keys:
            company_name  (str) – legal name of the overseas buyer
            country       (str) – buyer country ISO-2, e.g. ``"ES"``
            vat_number    (str) – EU VAT or local registration number (optional)

        Returns risk_grade (A/B/C), credit_limit_usd, payment_terms_recommendation.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = await client.post(
                f"{self._BASE}/credit-checks",
                headers=headers,
                json={
                    "companyName": request["company_name"],
                    "country": request["country"],
                    "vatNumber": request.get("vat_number", ""),
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Submit a trade credit insurance policy application.

        Expected: buyer_name, buyer_country, cover_amount (USD), deal_id.
        Returns policy_id, status, premium_estimate_usd.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = await client.post(
                f"{self._BASE}/policies",
                headers=headers,
                json={
                    "buyerName": request["buyer_name"],
                    "buyerCountry": request["buyer_country"],
                    "requestedCoverAmount": request["cover_amount"],
                    "externalReference": request.get("deal_id", ""),
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = await client.get(
                f"{self._BASE}/policies/{ext_ref}", headers=headers
            )
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify HMAC-SHA256 signature on Allianz Trade event webhooks.

        Allianz Trade sends ``X-Signature: sha256=<hex>`` in webhook headers.
        Requires ALLIANZ_TRADE_WEBHOOK_SECRET to be set.
        """
        if not self._webhook_secret:
            raise ValueError("ALLIANZ_TRADE_WEBHOOK_SECRET is not configured.")
        sig_header = headers.get("x-signature", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        provided = sig_header[len("sha256="):]
        return hmac.compare_digest(expected, provided)
