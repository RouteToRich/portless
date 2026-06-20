"""Regulatory & verification connectors (GSTN, DGFT, MCA, Udyam, VIES, Allianz Trade).

See Appendix 8.1. Adapters implement the uniform Connector interface.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import httpx

from .base import Connector


class GstnConnector(Connector):
    name = "gstn"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class DgftConnector(Connector):
    name = "dgft"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class McaConnector(Connector):
    name = "mca"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class UdyamConnector(Connector):
    name = "udyam"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class ViesConnector(Connector):
    name = "vies"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


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
        """Fetch a short-lived OAuth2 bearer token via client-credentials flow."""
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
            country       (str) – buyer country ISO-2, e.g. "ES"
            vat_number    (str) – EU VAT or local registration number (optional)

        Returns risk_grade (A/B/C), credit_limit_usd, payment_terms_recommendation.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            payload: dict[str, Any] = {
                "companyName": request["company_name"],
                "country": request["country"],
                "vatNumber": request.get("vat_number", ""),
            }
            resp = await client.post(
                f"{self._BASE}/credit-checks",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Submit a trade credit insurance policy application.

        Expected request keys:
            buyer_name    (str)   – debtor company name
            buyer_country (str)   – ISO-2 country
            cover_amount  (float) – requested credit limit in USD
            deal_id       (str)   – PortLess deal reference

        Returns policy_id, status, premium_estimate_usd.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            payload = {
                "buyerName": request["buyer_name"],
                "buyerCountry": request["buyer_country"],
                "requestedCoverAmount": request["cover_amount"],
                "externalReference": request.get("deal_id", ""),
            }
            resp = await client.post(
                f"{self._BASE}/policies",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Poll the status of a credit insurance policy by policy_id.

        ext_ref: policy_id returned by ``book()``.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = await client.get(
                f"{self._BASE}/policies/{ext_ref}",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify the HMAC-SHA256 signature on Allianz Trade event webhooks.

        Allianz Trade sends ``X-Signature: sha256=<hex>`` in the webhook headers.
        Requires ALLIANZ_TRADE_WEBHOOK_SECRET to be set.
        """
        if not self._webhook_secret:
            raise ValueError("ALLIANZ_TRADE_WEBHOOK_SECRET is not configured.")
        sig_header = headers.get("x-signature", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = hmac.new(
            self._webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        provided = sig_header[len("sha256="):]
        return hmac.compare_digest(expected, provided)
