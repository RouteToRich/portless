"""Payment / escrow connectors (Stripe Treasury, Payoneer).

``INTEGRATION_MODE`` (``shared/integration_mode.py``) decides, not the key:
  • live (default) → always a real HTTP call with the configured credentials;
    a missing key or a ``your_..._here`` placeholder is refused by the provider
    and surfaces as ``ConnectorError`` — never a made-up success.
  • sandbox        → a realistic stub (automation runs and pytest only).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any
from uuid import uuid4

import httpx

from shared.integration_mode import sandbox as stub_mode

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
    Stub responses only with INTEGRATION_MODE=sandbox.
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
        if stub_mode():
            ref = self._stub_ref()
            logger.warning(
                "StripeTreasuryConnector: INTEGRATION_MODE=sandbox — returning stub PI %s", ref
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
        if stub_mode():
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
            # Replay window: a signed payload older than the tolerance is rejected
            # even when the HMAC matches (Stripe's own recommendation is 5 min).
            tolerance = int(os.getenv("STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300") or 300)
            if timestamp and tolerance > 0:
                import time as _time
                if abs(_time.time() - int(timestamp)) > tolerance:
                    logger.warning("StripeTreasuryConnector: webhook timestamp outside tolerance — rejected")
                    return False
            signed_payload = f"{timestamp}.".encode() + body
            expected = hmac.new(
                self._webhook_secret.encode(), signed_payload, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(v1_sig, expected)
        except Exception:
            return False

    async def capture(self, ext_ref: str) -> dict[str, Any]:
        """Capture a previously authorised PaymentIntent (escrow release)."""
        if stub_mode():
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
        if stub_mode():
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
        if stub_mode():
            ref = self._stub_ref()
            logger.warning(
                "PayoneerConnector: INTEGRATION_MODE=sandbox — returning stub payout %s", ref
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
        if stub_mode():
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
