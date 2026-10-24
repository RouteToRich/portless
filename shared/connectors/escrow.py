"""Neutral escrow-provider connectors (Tazapay, Escrow.com).

These are buyer-selectable, licensed escrow custodians that hold the buyer's
funds in a **segregated account** and release them to PortLess **in tranches**
as the trade advances (see
``services/escrow_guard/workflow_escrow_guard.md``).

They follow the same key-at-call-time graceful-stub rule as ``payments.py``:
  • key present → attempts a real HTTP call; errors become ``ConnectorError``.
  • key absent  → returns a realistic stub so the deal pipeline runs in dev/CI.

Interface (superset of the base ``Connector``):
  book(request)            → open the escrow (hold funds); returns ext_ref + pay-in URL
  release(request)         → release one tranche to the beneficiary (PortLess)
  refund(request)          → refund the undisbursed balance to the buyer
  status(ext_ref)          → poll escrow status
  list_payment_methods()   → pay-in methods the provider supports
  webhook_verify(h, body)  → HMAC verification of an inbound provider webhook
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
from .payments import ConnectorError

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Tazapay — cross-border B2B escrow (local pay-in + INR payout)
# ---------------------------------------------------------------------------

class TazapayEscrowConnector(Connector):
    """Tazapay neutral-escrow connector. Requires TAZAPAY_API_KEY / _API_SECRET."""

    name = "tazapay"

    def __init__(self) -> None:
        self._api_key = os.getenv("TAZAPAY_API_KEY", "")
        self._api_secret = os.getenv("TAZAPAY_API_SECRET", "")
        self._webhook_secret = os.getenv("TAZAPAY_WEBHOOK_SECRET", "")
        self._base_url = os.getenv("TAZAPAY_BASE_URL", "https://api.tazapay.com/v3")

    def _stub_ref(self) -> str:
        return f"tzp_stub_{uuid4().hex[:12]}"

    async def list_payment_methods(self, corridor: dict[str, Any] | None = None) -> list[str]:
        return ["card", "bank_transfer", "local_payment", "wire"]

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """FX / fee estimate for a USD→INR corridor (stubbed without creds)."""
        return {"provider": "tazapay", "fx_rate": 83.20, "fee_pct": 1.0, "stub": True}

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Open an escrow (hold the buyer's funds). Returns ext_ref + pay-in URL."""
        if not self._api_key:
            ref = self._stub_ref()
            logger.warning("TazapayEscrowConnector: TAZAPAY_API_KEY not set — stub escrow %s", ref)
            return {
                "ext_ref": ref,
                "provider": "tazapay",
                "status": "AWAITING_FUNDING",
                "amount": request.get("amount", 0),
                "currency": request.get("currency", "USD"),
                "checkout_url": f"https://pay.tazapay.com/checkout/{ref}",
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/escrow",
                json={
                    "amount": request.get("amount"),
                    "currency": request.get("currency", "USD"),
                    "reference": request.get("reference", ""),
                    "transaction_description": request.get("description", ""),
                },
                auth=(self._api_key, self._api_secret),
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(
                    f"Tazapay create-escrow failed: HTTP {resp.status_code} — {resp.text[:300]}"
                )
            data = resp.json()
            return {
                "ext_ref": data.get("id") or data.get("escrow_id", ""),
                "provider": "tazapay",
                "status": data.get("status", "AWAITING_FUNDING"),
                "amount": request.get("amount", 0),
                "currency": request.get("currency", "USD"),
                "checkout_url": data.get("checkout_url"),
            }

    async def release(self, request: dict[str, Any]) -> dict[str, Any]:
        """Release one tranche of the held funds to the beneficiary (PortLess)."""
        ext_ref = request.get("ext_ref", "")
        if not self._api_key:
            return {
                "ext_ref": ext_ref,
                "status": "RELEASED",
                "amount": request.get("amount", 0),
                "tranche_id": request.get("tranche_id"),
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/escrow/{ext_ref}/release",
                json={"amount": request.get("amount"), "note": request.get("note", "")},
                auth=(self._api_key, self._api_secret),
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Tazapay release failed: HTTP {resp.status_code} — {resp.text[:300]}")
            return resp.json()

    async def refund(self, request: dict[str, Any]) -> dict[str, Any]:
        """Refund the undisbursed balance to the buyer."""
        ext_ref = request.get("ext_ref", "")
        if not self._api_key:
            return {"ext_ref": ext_ref, "status": "REFUNDED", "amount": request.get("amount", 0), "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/escrow/{ext_ref}/refund",
                json={"amount": request.get("amount"), "reason": request.get("reason", "")},
                auth=(self._api_key, self._api_secret),
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Tazapay refund failed: HTTP {resp.status_code}")
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if not self._api_key:
            return {"ext_ref": ext_ref, "status": "AWAITING_FUNDING", "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{self._base_url}/escrow/{ext_ref}", auth=(self._api_key, self._api_secret))
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Tazapay status failed: HTTP {resp.status_code}")
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        if not self._webhook_secret:
            logger.warning("TazapayEscrowConnector: TAZAPAY_WEBHOOK_SECRET not set — accepting all webhooks")
            return True
        sig = headers.get("x-tazapay-signature", "")
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# Escrow.com — established licensed escrow (milestone transactions)
# ---------------------------------------------------------------------------

class EscrowComConnector(Connector):
    """Escrow.com connector. Uses HTTP Basic auth (email + API key)."""

    name = "escrow_com"

    def __init__(self) -> None:
        self._api_key = os.getenv("ESCROW_COM_API_KEY", "")
        self._email = os.getenv("ESCROW_COM_EMAIL", "")
        self._webhook_secret = os.getenv("ESCROW_COM_WEBHOOK_SECRET", "")
        self._base_url = os.getenv("ESCROW_COM_BASE_URL", "https://api.escrow.com/2017-09-01")

    def _stub_ref(self) -> str:
        return f"esc_stub_{uuid4().hex[:12]}"

    async def list_payment_methods(self, corridor: dict[str, Any] | None = None) -> list[str]:
        return ["wire_transfer", "credit_card", "paypal"]

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"provider": "escrow_com", "fee_pct": 0.89, "stub": True}

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Open a milestone escrow transaction. Returns ext_ref + pay-in URL."""
        if not self._api_key:
            ref = self._stub_ref()
            logger.warning("EscrowComConnector: ESCROW_COM_API_KEY not set — stub escrow %s", ref)
            return {
                "ext_ref": ref,
                "provider": "escrow_com",
                "status": "awaiting_payment",
                "amount": request.get("amount", 0),
                "currency": request.get("currency", "USD"),
                "checkout_url": f"https://www.escrow.com/pay/{ref}",
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/transaction",
                json={
                    "currency": request.get("currency", "usd").lower(),
                    "description": request.get("description", ""),
                    "items": [{
                        "title": request.get("reference", "PortLess trade"),
                        "type": "general_merchandise",
                        "inspection_period": request.get("inspection_period", 259200),
                        "quantity": 1,
                        "schedule": [{"amount": request.get("amount", 0), "payer_customer": "buyer",
                                      "beneficiary_customer": "portless"}],
                    }],
                },
                auth=(self._email, self._api_key),
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(
                    f"Escrow.com create-transaction failed: HTTP {resp.status_code} — {resp.text[:300]}"
                )
            data = resp.json()
            return {
                "ext_ref": str(data.get("id", "")),
                "provider": "escrow_com",
                "status": data.get("status", {}).get("in_dispute") and "in_dispute" or "awaiting_payment",
                "amount": request.get("amount", 0),
                "currency": request.get("currency", "USD"),
                "checkout_url": data.get("landing_page"),
            }

    async def release(self, request: dict[str, Any]) -> dict[str, Any]:
        """Disburse a milestone to the beneficiary (PortLess)."""
        ext_ref = request.get("ext_ref", "")
        if not self._api_key:
            return {
                "ext_ref": ext_ref,
                "status": "released",
                "amount": request.get("amount", 0),
                "tranche_id": request.get("tranche_id"),
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.patch(
                f"{self._base_url}/transaction/{ext_ref}",
                json={"action": "disburse", "amount": request.get("amount")},
                auth=(self._email, self._api_key),
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Escrow.com disburse failed: HTTP {resp.status_code}")
            return resp.json()

    async def refund(self, request: dict[str, Any]) -> dict[str, Any]:
        ext_ref = request.get("ext_ref", "")
        if not self._api_key:
            return {"ext_ref": ext_ref, "status": "refunded", "amount": request.get("amount", 0), "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.patch(
                f"{self._base_url}/transaction/{ext_ref}",
                json={"action": "cancel", "reason": request.get("reason", "")},
                auth=(self._email, self._api_key),
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Escrow.com refund failed: HTTP {resp.status_code}")
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if not self._api_key:
            return {"ext_ref": ext_ref, "status": "awaiting_payment", "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{self._base_url}/transaction/{ext_ref}", auth=(self._email, self._api_key))
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Escrow.com status failed: HTTP {resp.status_code}")
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        if not self._webhook_secret:
            logger.warning("EscrowComConnector: ESCROW_COM_WEBHOOK_SECRET not set — accepting all webhooks")
            return True
        sig = headers.get("x-escrow-signature", "")
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
