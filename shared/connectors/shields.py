"""Third-party "shield" connectors: freight booking + CHA (customs).

QC inspection connectors moved to ``shared.connectors.qc_providers`` — the
QC provider set (SGS, QIMA, Bureau Veritas, Intertek, TÜV SÜD) is a distinct,
buyer-selectable ecosystem, separated from transportation management. The key
QC symbols are re-exported here for backward compatibility.

``INTEGRATION_MODE`` (``shared/integration_mode.py``) decides, not the key:
  • live (default) → always a real HTTP call with the configured credentials;
    a missing key or a ``your_..._here`` placeholder is refused by the provider
    and surfaces as ``ConnectorError`` — never a made-up success.
  • sandbox        → a realistic stub (automation runs and pytest only).

All connectors share the uniform ``quote / book / status / webhook_verify``
interface defined in ``shared.connectors.base``.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

import httpx

from shared.integration_mode import sandbox as stub_mode

from .base import Connector
from .qc_providers import (  # noqa: F401 — re-exported for backward compat
    ConnectorError,
    QC_PROVIDER_CATALOG,
    SgsConnector,
    get_qc_connector,
    resolve_qc_provider,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Cogoport — freight spot rates + container booking
# ---------------------------------------------------------------------------

class CogoportConnector(Connector):
    """Cogoport freight connector for FCL/LCL spot rates and container booking."""

    name = "cogoport"

    def __init__(self) -> None:
        self._api_key = os.getenv("COGOPORT_API_KEY", "")
        self._base_url = os.getenv("COGOPORT_BASE_URL", "https://api.cogoport.com/v1")
        self._webhook_secret = os.getenv("COGOPORT_WEBHOOK_SECRET", "")

    def _stub_ref(self) -> str:
        return f"CGP-STUB-{uuid4().hex[:10].upper()}"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Fetch FCL/LCL spot rates for origin→destination with HS code."""
        if stub_mode():
            logger.warning("CogoportConnector: INTEGRATION_MODE=sandbox — returning stub rates")
            return {
                "provider": "cogoport",
                "origin": request.get("origin_port", "Chennai, IN"),
                "destination": request.get("destination_port", "Valencia, ES"),
                "container_type": request.get("container_type", "40HC"),
                "rate_usd": 2850.0,
                "transit_days": 22,
                "valid_until": (date.today() + timedelta(days=3)).isoformat(),
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/rates/fcl/search",
                json=request,
                headers={"Authorization": f"Token {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Cogoport quote failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Confirm container booking and return booking reference + BL draft date."""
        if stub_mode():
            ref = self._stub_ref()
            logger.warning("CogoportConnector: INTEGRATION_MODE=sandbox — returning stub booking %s", ref)
            return {
                "ext_ref": ref,
                "status": "CONFIRMED",
                "provider": "cogoport",
                "vessel": "MV PORTLESS EXPRESS",
                "voyage": f"PX{uuid4().hex[:4].upper()}",
                "etd": (date.today() + timedelta(days=7)).isoformat(),
                "eta": (date.today() + timedelta(days=29)).isoformat(),
                "container_number": f"CGPU{uuid4().hex[:7].upper()}",
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/shipments",
                json=request,
                headers={"Authorization": f"Token {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Cogoport book failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if stub_mode():
            return {"ext_ref": ext_ref, "status": "IN_TRANSIT", "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/shipments/{ext_ref}",
                headers={"Authorization": f"Token {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Cogoport status failed: HTTP {resp.status_code}")
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        if not self._webhook_secret:
            logger.warning("CogoportConnector: COGOPORT_WEBHOOK_SECRET not set — accepting all webhooks")
            return True
        sig = headers.get("x-cogoport-signature", "")
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# Freightos — freight rate aggregator (fallback / alternative to Cogoport)
# ---------------------------------------------------------------------------

class FreightosConnector(Connector):
    """Freightos rate aggregator — used as fallback when Cogoport rates are unavailable."""

    name = "freightos"

    def __init__(self) -> None:
        self._api_key = os.getenv("FREIGHTOS_API_KEY", "")
        self._base_url = os.getenv("FREIGHTOS_BASE_URL", "https://api.freightos.com/v1")
        self._webhook_secret = os.getenv("FREIGHTOS_WEBHOOK_SECRET", "")

    def _stub_ref(self) -> str:
        return f"FTO-STUB-{uuid4().hex[:10].upper()}"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        if stub_mode():
            logger.warning("FreightosConnector: INTEGRATION_MODE=sandbox — returning stub rates")
            return {
                "provider": "freightos",
                "origin": request.get("origin_port", "Chennai, IN"),
                "destination": request.get("destination_port", "Valencia, ES"),
                "rate_usd": 2950.0,
                "transit_days": 24,
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/rates",
                json=request,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Freightos quote failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        if stub_mode():
            ref = self._stub_ref()
            logger.warning("FreightosConnector: INTEGRATION_MODE=sandbox — returning stub booking %s", ref)
            return {
                "ext_ref": ref,
                "status": "CONFIRMED",
                "provider": "freightos",
                "vessel": "MV FREIGHT STAR",
                "etd": (date.today() + timedelta(days=8)).isoformat(),
                "eta": (date.today() + timedelta(days=32)).isoformat(),
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/bookings",
                json=request,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Freightos book failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if stub_mode():
            return {"ext_ref": ext_ref, "status": "IN_TRANSIT", "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/bookings/{ext_ref}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"Freightos status failed: HTTP {resp.status_code}")
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        if not self._webhook_secret:
            logger.warning("FreightosConnector: FREIGHTOS_WEBHOOK_SECRET not set — accepting all webhooks")
            return True
        sig = headers.get("x-freightos-signature", "")
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# ICEGATE — Customs EDI (CHA filing)
# ---------------------------------------------------------------------------

class IcegateConnector(Connector):
    """Customs EDI connector for filing Shipping Bills via ICEGATE / registered CHA."""

    name = "icegate"

    def __init__(self) -> None:
        self._api_key = os.getenv("ICEGATE_API_KEY", "")
        self._base_url = os.getenv("ICEGATE_BASE_URL", "https://www.icegate.gov.in")

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Not applicable for customs filing — returns empty dict."""
        return {}

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Submit a Shipping Bill to ICEGATE. Returns SB number and LEO."""
        if stub_mode():
            sb_num = f"SB{uuid4().hex[:8].upper()}"
            leo = f"LEO-{uuid4().hex[:8].upper()}"
            logger.warning(
                "IcegateConnector: INTEGRATION_MODE=sandbox — returning stub SB %s / LEO %s",
                sb_num, leo,
            )
            return {
                "shipping_bill_number": sb_num,
                "let_export_order": leo,
                "status": "FILED",
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/api/shipping-bill",
                json=request,
                headers={"X-Api-Key": self._api_key},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"ICEGATE submission failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if stub_mode():
            return {"shipping_bill_number": ext_ref, "status": "FILED", "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/api/shipping-bill/{ext_ref}",
                headers={"X-Api-Key": self._api_key},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"ICEGATE status failed: HTTP {resp.status_code}")
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        return True  # ICEGATE uses IP allowlisting, not HMAC


# ---------------------------------------------------------------------------
# Connector registry (freight/transport only — QC lives in qc_providers.py)
# ---------------------------------------------------------------------------

_FREIGHT_CONNECTORS: dict[str, type] = {
    "cogoport": CogoportConnector,
    "freightos": FreightosConnector,
}


def get_freight_connector(provider: str):
    """Return an instantiated freight connector (``maersk`` = carrier booking,
    ``cogoport`` / ``freightos`` = forwarders). Defaults to Cogoport."""
    if provider.lower() == "maersk":
        from .maersk import MaerskConnector
        return MaerskConnector()
    cls = _FREIGHT_CONNECTORS.get(provider.lower(), CogoportConnector)
    return cls()
