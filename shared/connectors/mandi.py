"""Indian wholesale (mandi) prices — the supplier-side price before any quote.

Source: Agmarknet's "Current daily price of various commodities from various
markets (Mandi)" on the Open Government Data platform (data.gov.in). Free with
an API key from https://data.gov.in (``DATA_GOV_IN_API_KEY``); without one the
connector answers an empty result and makes no network call.

Prices come in INR per quintal (100 kg) for the raw commodity at the mandi. An
exporter's FOB price adds cleaning / sorting / grading, export packing, inland
transport to the port and its own margin: ``MANDI_TO_FOB_MARKUP_PCT`` (default
15 %) turns the median modal price into an estimated FOB India per MT. It is an
estimate and is always labelled as one — a supplier quote replaces it.

quote(request): ``hs_code`` (6 digits). Returns::

    {"commodity": "Dry Chillies", "modal_inr_per_quintal": 18500.0,
     "markets": 23, "as_of": "17/11/2026", "usd_inr": 84.0,
     "mandi_usd_per_mt": 2202.38, "fob_estimate_usd_per_mt": 2532.74,
     "markup_pct": 15.0, "source": "agmarknet"}

or ``{}`` when the key is unset, the HS code has no Agmarknet commodity, or no
market reported a price.
"""
from __future__ import annotations

import logging
import os
import statistics
from typing import Any

import httpx

from .base import Connector

logger = logging.getLogger(__name__)

RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
_BASE = f"https://api.data.gov.in/resource/{RESOURCE_ID}"

# HS 6-digit (then 4-digit) → Agmarknet commodity name. Only commodities that are
# traded at a mandi in roughly the form PortLess exports.
HS_TO_COMMODITY: dict[str, str] = {
    "090411": "Black pepper",
    "090412": "Black pepper",
    "090421": "Dry Chillies",
    "090422": "Dry Chillies",
    "090831": "Cardamoms",
    "090832": "Cardamoms",
    "090921": "Corriander seed",
    "090922": "Corriander seed",
    "090931": "Cummin Seed(Jeera)",
    "090932": "Cummin Seed(Jeera)",
    "091011": "Dry Ginger",
    "091012": "Dry Ginger",
    "091030": "Turmeric",
    "091091": "Methi Seeds",
    "100610": "Paddy(Dhan)(Common)",
    "100630": "Rice",
    "100199": "Wheat",
    "120740": "Sesamum(Sesame,Gingelly,Til)",
    "080131": "Cashewnuts",
    "080132": "Cashewnuts",
    "070310": "Onion",
    "071220": "Onion",
    "0904": "Dry Chillies",
    "0909": "Cummin Seed(Jeera)",
    "1006": "Rice",
    "1001": "Wheat",
}


def commodity_for(hs_code: str) -> str | None:
    hs = "".join(ch for ch in str(hs_code or "") if ch.isdigit())
    return HS_TO_COMMODITY.get(hs[:6]) or HS_TO_COMMODITY.get(hs[:4])


def markup_pct() -> float:
    try:
        return float(os.getenv("MANDI_TO_FOB_MARKUP_PCT", "") or 15.0)
    except ValueError:
        return 15.0


def _api_key() -> str:
    key = os.getenv("DATA_GOV_IN_API_KEY", "").strip()
    return "" if key.startswith("your_") else key


def _usd_inr() -> float:
    try:
        from shared.currency import fallback_rate
        return float(fallback_rate("INR") or 84.0)
    except Exception:  # noqa: BLE001
        return 84.0


def summarise(records: list[dict[str, Any]], commodity: str) -> dict[str, Any]:
    """Pure: Agmarknet rows → median modal price and the FOB estimate."""
    prices: list[float] = []
    dates: list[str] = []
    for r in records or []:
        try:
            v = float(str(r.get("modal_price") or r.get("Modal_Price") or "").replace(",", ""))
        except ValueError:
            continue
        if v > 0:
            prices.append(v)
            d = r.get("arrival_date") or r.get("Arrival_Date")
            if d:
                dates.append(str(d))
    if not prices:
        return {}
    modal = statistics.median(prices)
    rate = _usd_inr()
    mandi_usd = modal * 10.0 / rate  # INR / quintal → INR / MT → USD / MT
    mk = markup_pct()
    return {
        "commodity": commodity,
        "modal_inr_per_quintal": round(modal, 2),
        "markets": len(prices),
        "as_of": max(dates) if dates else None,
        "usd_inr": rate,
        "mandi_usd_per_mt": round(mandi_usd, 2),
        "fob_estimate_usd_per_mt": round(mandi_usd * (1 + mk / 100.0), 2),
        "markup_pct": mk,
        "source": "agmarknet",
    }


class MandiPriceConnector(Connector):
    """Agmarknet daily mandi prices via data.gov.in (supplier-side price anchor)."""

    name = "mandi_prices"

    def __init__(self) -> None:
        pass  # key read per call so a key set later is picked up

    @property
    def configured(self) -> bool:
        return bool(_api_key())

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        commodity = commodity_for(str(request.get("hs_code") or ""))
        key = _api_key()
        if not commodity or not key:
            return {}
        params = {
            "api-key": key, "format": "json", "limit": 500,
            "filters[commodity]": commodity,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(_BASE, params=params)
        if resp.status_code != 200:
            logger.warning("Mandi prices: data.gov.in answered %s for %s", resp.status_code, commodity)
            return {}
        try:
            records = resp.json().get("records") or []
        except ValueError:
            return {}
        return summarise(records, commodity)

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Mandi prices are read-only.")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError("Mandi prices are read-only.")

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("Mandi prices have no webhooks.")
