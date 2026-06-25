"""Payment / escrow connectors (Stripe Treasury, Payoneer).

Each connector checks for its API key at call-time:
  • Key present  → attempts real HTTP call; errors become ConnectorError.
  • Key absent   → returns a realistic stub so the deal pipeline can proceed
                   in dev / CI without live credentials.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any
from uuid import uuid4

import httpx

from .base import Connector

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


class ConnectorError(Exception):
    """Raised when a provider call fails in a non-retryable way."""


# ---------------------------------------------------------------------------
# Stripe Treasury — buyer-side escrow hold
# ---------------------------------------------------------------------------

class StripeTreasuryConnector(Connector):
    """Stripe Treasury / Payment Intents connector for buyer escrow holds.

    Uses Stripe's standard REST API (v1).  Requires STRIPE_SECRET_KEY.
    Without credentials returns stub responses so deal flow can advance.
    """

    name = "stripe_treasury"

    def __init__(self) -> None:
        self._api_key = os.getenv("STRIPE_SECRET_KEY", "")
        self._webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        self._base_url = "https://api.stripe.com/v1"

    def _stub_ref(self) -> str:
        return f"pi_stub_{uuid4().hex}"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Not applicable for payment intents — returns empty dict."""
        return {}

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Create a PaymentIntent (escrow hold).

        ``request`` must contain: ``amount`` (in smallest currency unit),
        ``currency`` (ISO 3-letter), ``metadata.deal_id``.
        """
        if not self._api_key:
            ref = self._stub_ref()
            logger.warning(
                "StripeTreasuryConnector: STRIPE_SECRET_KEY not set — returning stub PI %s", ref
            )
            return {
                "ext_ref": ref,
                "status": "requires_capture",
                "provider": "stripe_treasury",
                "amount": request.get("amount", 0),
                "currency": request.get("currency", "usd").lower(),
                "client_secret": f"{ref}_secret_stub",
                "stub": True,
            }
        amount = request.get("amount", 0)
        currency = request.get("currency", "usd").lower()
        metadata = request.get("metadata", {})
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/payment_intents",
                data={
                    "amount": str(int(amount)),
                    "currency": currency,
                    "capture_method": "manual",
                    "confirm": "false",
                    **{f"metadata[{k}]": str(v) for k, v in metadata.items()},
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(
                    f"Stripe PaymentIntent creation failed: HTTP {resp.status_code} — {resp.text[:300]}"
                )
            data = resp.json()
            return {
                "ext_ref": data["id"],
                "status": data["status"],
                "provider": "stripe_treasury",
                "amount": data["amount"],
                "currency": data["currency"],
                "client_secret": data.get("client_secret"),
            }

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Retrieve a PaymentIntent's current status."""
        if not self._api_key:
            return {
                "ext_ref": ext_ref,
                "status": "requires_capture",
                "provider": "stripe_treasury",
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/payment_intents/{ext_ref}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Stripe status failed: HTTP {resp.status_code}")
            data = resp.json()
            return {
                "ext_ref": data["id"],
                "status": data["status"],
                "amount": data["amount"],
                "currency": data["currency"],
            }

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify Stripe webhook signature using Stripe-Signature header."""
        if not self._webhook_secret:
            logger.warning(
                "StripeTreasuryConnector: STRIPE_WEBHOOK_SECRET not set — accepting all webhooks"
            )
            return True
        stripe_sig = headers.get("stripe-signature", "")
        try:
            # Parse Stripe's t=timestamp,v1=signature format
            parts = {kv.split("=", 1)[0]: kv.split("=", 1)[1] for kv in stripe_sig.split(",")}
            timestamp = parts.get("t", "")
            v1_sig = parts.get("v1", "")
            signed_payload = f"{timestamp}.".encode() + body
            expected = hmac.new(
                self._webhook_secret.encode(), signed_payload, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(v1_sig, expected)
        except Exception:
            return False

    async def capture(self, ext_ref: str) -> dict[str, Any]:
        """Capture a previously authorised PaymentIntent (escrow release)."""
        if not self._api_key:
            return {"ext_ref": ext_ref, "status": "succeeded", "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/payment_intents/{ext_ref}/capture",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Stripe capture failed: HTTP {resp.status_code}")
            data = resp.json()
            return {"ext_ref": data["id"], "status": data["status"]}


# ---------------------------------------------------------------------------
# Payoneer — supplier-side INR payout
# ---------------------------------------------------------------------------

class PayoneerConnector(Connector):
    """Payoneer payout connector for releasing INR to Indian suppliers.

    Payoneer uses OAuth2 client credentials.  Set PAYONEER_CLIENT_ID and
    PAYONEER_CLIENT_SECRET.  Without credentials returns stub responses.
    """

    name = "payoneer"

    def __init__(self) -> None:
        self._client_id = os.getenv("PAYONEER_CLIENT_ID", "")
        self._client_secret = os.getenv("PAYONEER_CLIENT_SECRET", "")
        self._base_url = os.getenv("PAYONEER_BASE_URL", "https://api.payoneer.com/v4")
        self._webhook_secret = os.getenv("PAYONEER_WEBHOOK_SECRET", "")
        self._access_token: str | None = None

    def _stub_ref(self) -> str:
        return f"PAY-STUB-{uuid4().hex[:10].upper()}"

    async def _get_token(self) -> str:
        """Fetch OAuth2 client-credentials token (cached in-process)."""
        if self._access_token:
            return self._access_token
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "read write",
                },
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Payoneer token fetch failed: HTTP {resp.status_code}")
            self._access_token = resp.json()["access_token"]
            return self._access_token

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Estimate FX conversion and fee for a USD → INR payout."""
        if not self._client_id:
            return {
                "provider": "payoneer",
                "from_currency": "USD",
                "to_currency": "INR",
                "fx_rate": 83.50,
                "fee_usd": request.get("amount", 0) * 0.015,
                "stub": True,
            }
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/fx-rates",
                params={"from": "USD", "to": "INR"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Payoneer FX quote failed: HTTP {resp.status_code}")
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Initiate a payout to a registered payee.

        ``request`` must contain: ``payee_id``, ``amount`` (USD),
        ``currency``, ``description``, ``idempotency_key``.
        """
        if not self._client_id:
            ref = self._stub_ref()
            logger.warning(
                "PayoneerConnector: PAYONEER_CLIENT_ID not set — returning stub payout %s", ref
            )
            return {
                "ext_ref": ref,
                "status": "PENDING",
                "provider": "payoneer",
                "amount": request.get("amount", 0),
                "currency": request.get("currency", "USD"),
                "payee_id": request.get("payee_id", ""),
                "stub": True,
            }
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/payments",
                json={
                    "payee_id": request["payee_id"],
                    "amount": request["amount"],
                    "currency": request.get("currency", "USD"),
                    "description": request.get("description", ""),
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": request.get("idempotency_key", ""),
                },
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(
                    f"Payoneer payout failed: HTTP {resp.status_code} — {resp.text[:300]}"
                )
            data = resp.json()
            return {
                "ext_ref": data.get("payment_id", ""),
                "status": data.get("status", "PENDING"),
                "provider": "payoneer",
                "amount": request["amount"],
                "currency": request.get("currency", "USD"),
            }

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if not self._client_id:
            return {"ext_ref": ext_ref, "status": "COMPLETED", "stub": True}
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/payments/{ext_ref}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Payoneer status failed: HTTP {resp.status_code}")
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        if not self._webhook_secret:
            logger.warning(
                "PayoneerConnector: PAYONEER_WEBHOOK_SECRET not set — accepting all webhooks"
            )
            return True
        sig = headers.get("x-payoneer-signature", "")
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
