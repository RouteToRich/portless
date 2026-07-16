"""Quality-control (QC) inspection provider connectors — separated from freight.

Top-5 providers, selected for: global buyer acceptance, REST-API feasibility,
visual evidence (photos/videos) in the inspection deliverable, and confirmed
availability in India (primary export origin):

  sgs        SGS (Switzerland)        — de-facto CRF standard; QiiQ video
  qima       QIMA (Hong Kong)         — only true public REST API; photo-rich
  bv         Bureau Veritas (France)  — OneSource/BVPort; govt PSI programs
  intertek   Intertek (UK)            — InterLink 2.0 APIs; eReporting photos
  tuv_sud    TÜV SÜD (Germany)        — premium EU acceptance; digital reports

Each connector checks for its API key at call-time:
  • Key present  → attempts real HTTP call; any error becomes a ConnectorError.
  • Key absent   → returns a realistic stub dict so the deal flow can proceed
                   in dev / CI without live credentials.

All connectors share the uniform ``quote / book / status / webhook_verify``
interface defined in ``shared.connectors.base``. Base URLs and endpoint paths
are placeholders modeled on each provider's public/partner documentation —
configure via ``<PREFIX>_BASE_URL`` once a commercial agreement is in place.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

import httpx

from .base import Connector

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


class ConnectorError(Exception):
    """Raised when a provider call fails in a non-retryable way."""


# ---------------------------------------------------------------------------
# Generic QC provider connector — subclasses only set class attributes
# ---------------------------------------------------------------------------

class QcProviderConnector(Connector):
    """Shared QC-inspection adapter.

    Subclasses configure the provider identity, env-var prefix, default base
    URL, webhook signature header, and stub economics. The HTTP call shape is
    identical across providers (bearer key + JSON) until real commercial API
    agreements dictate provider-specific request/response mapping.
    """

    name: str = "qc"
    display_name: str = "QC Provider"
    env_prefix: str = "QC"                      # <PREFIX>_API_KEY / _BASE_URL / _WEBHOOK_SECRET
    default_base_url: str = ""
    signature_header: str = "x-qc-signature"
    ref_prefix: str = "QC"
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

    def _stub_ref(self) -> str:
        return f"{self.ref_prefix}-STUB-{uuid4().hex[:10].upper()}"

    def _stub_fee(self, quantity_mt: float) -> float:
        return round(self.stub_base_fee_usd + self.stub_fee_per_mt_usd * max(quantity_mt, 0.0), 2)

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Fetch inspection availability/price for a given date and location."""
        quantity_mt = float(request.get("quantity_mt") or 0.0)
        if not self._api_key:
            logger.warning(
                "%s: %s_API_KEY not set — returning stub quote",
                type(self).__name__, self.env_prefix,
            )
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
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/quotes",
                json=request,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(
                    f"{self.display_name} quote failed: HTTP {resp.status_code} — {resp.text[:200]}"
                )
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Book an inspection appointment and return the booking reference."""
        if not self._api_key:
            ref = self._stub_ref()
            logger.warning(
                "%s: %s_API_KEY not set — returning stub booking %s",
                type(self).__name__, self.env_prefix, ref,
            )
            return {
                "ext_ref": ref,
                "status": "BOOKED",
                "provider": self.name,
                "provider_display_name": self.display_name,
                "inspection_date": (date.today() + timedelta(days=self.stub_lead_days)).isoformat(),
                "location": request.get("location", "Supplier warehouse"),
                "inspector_contact": f"inspector@{self.name}-stub.com",
                "evidence": {"photos": self.provides_photos, "video": self.provides_video},
                "stub": True,
            }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/bookings",
                json=request,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(
                    f"{self.display_name} book failed: HTTP {resp.status_code} — {resp.text[:200]}"
                )
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Poll the status of an existing inspection booking."""
        if not self._api_key:
            return {"ext_ref": ext_ref, "status": "BOOKED", "provider": self.name, "stub": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/bookings/{ext_ref}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code not in range(200, 300):
                raise ConnectorError(f"{self.display_name} status failed: HTTP {resp.status_code}")
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify provider webhook using HMAC-SHA256 in the provider's header."""
        if not self._webhook_secret:
            logger.warning(
                "%s: %s_WEBHOOK_SECRET not set — accepting all webhooks",
                type(self).__name__, self.env_prefix,
            )
            return True
        sig = headers.get(self.signature_header, "")
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------

class SgsConnector(QcProviderConnector):
    """SGS — world's largest TIC firm; CRF is the trade standard.

    Enterprise API/EDI under key-account agreement; QiiQ adds live video.
    """

    name = "sgs"
    display_name = "SGS"
    env_prefix = "SGS"
    default_base_url = "https://api.sgs.com/inspection/v1"
    signature_header = "x-sgs-signature"
    ref_prefix = "SGS"
    stub_base_fee_usd = 320.0
    stub_lead_days = 3
    provides_photos = True
    provides_video = True  # QiiQ remote inspection


class QimaConnector(QcProviderConnector):
    """QIMA — only provider with a true public REST API (developers.qima.com).

    Order create/search/cancel + report PDF download map directly onto
    book/status; photo-rich same-day reports.
    """

    name = "qima"
    display_name = "QIMA"
    env_prefix = "QIMA"
    default_base_url = "https://api.qima.com/openapi/v1"
    signature_header = "x-qima-signature"
    ref_prefix = "QIMA"
    stub_base_fee_usd = 289.0
    stub_lead_days = 2  # inspector onsite within 48h
    provides_photos = True
    provides_video = True  # live-video remote inspection product option


class BureauVeritasConnector(QcProviderConnector):
    """Bureau Veritas — #2 globally; OneSource/BVPort platform feeds."""

    name = "bv"
    display_name = "Bureau Veritas"
    env_prefix = "BUREAU_VERITAS"
    default_base_url = "https://api.bureauveritas.com/onesource/v1"
    signature_header = "x-bv-signature"
    ref_prefix = "BV"
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
        "api_integration": "Enterprise API/EDI (key-account agreement)",
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
        "api_integration": "Public REST API (developers.qima.com)",
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
        "api_integration": "Enterprise (OneSource / BVPort feeds)",
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


def cheapest_qc_provider(candidates: list[str] | None = None) -> str:
    """Registry key of the cheapest provider by indicative fee.

    ``candidates`` restricts the choice to the buyer's named acceptable
    agencies (resolved keys); unknown names are ignored. With no (valid)
    candidates the whole catalog competes — used when the buyer left the
    choice open ("book whichever is cheaper").
    """
    keys = [k for k in (candidates or []) if k in QC_PROVIDER_CATALOG] or list(QC_PROVIDER_CATALOG)
    return min(keys, key=lambda k: QC_PROVIDER_CATALOG[k]["indicative_fee_usd"])


def get_qc_connector(provider: str) -> QcProviderConnector:
    """Return an instantiated QC connector for the given provider name/alias.

    Unknown providers fall back to SGS (the default) so legacy records with
    free-text provider names keep working.
    """
    key = resolve_qc_provider(provider) or str(provider or "").strip().lower()
    cls = QC_CONNECTOR_CLASSES.get(key, SgsConnector)
    return cls()
