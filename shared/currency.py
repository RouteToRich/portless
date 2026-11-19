"""Which currency PortLess writes to a counterparty in — from its country.

Contracts, the escrow, the PO and every payout stay in USD; what changes is
how a price is put to the party in e-mails: in its own currency first, with the
USD contract figure and the rate next to it, so nobody converts in their head
and nobody mistakes the equivalent for the contract price.

A country gets its own currency only where PortLess has a rate for it — today
the USD/INR rate the negotiation agent already uses (live search, else
``NEGOTIATION_FALLBACK_USD_INR_RATE``). Every other country is written to in
USD, the currency the trade is invoiced in. Other countries in ``_COUNTRY_CURRENCY`` get their
own currency once ``shared.fx_rates`` has a usable rate (staff reference rate, the daily feed,
or a ``FX_RATE_<CCY>`` setting); the USD/INR fallback is the latest reference rate before the
static setting.
"""
from __future__ import annotations

import re
from typing import Any

_LOCAL: dict[str, str] = {"IN": "INR"}
_EUR = ("DE", "FR", "IT", "ES", "NL", "BE", "AT", "IE", "PT", "FI", "GR", "LU", "SK", "SI", "EE", "LV", "LT", "MT", "CY", "HR")
_COUNTRY_CURRENCY: dict[str, str] = {
    "AE": "AED", "SA": "SAR", "QA": "QAR", "OM": "OMR", "KW": "KWD", "BH": "BHD", "GB": "GBP",
    "SG": "SGD", "MY": "MYR", "JP": "JPY", "CN": "CNY", "AU": "AUD", "CA": "CAD",
    **{c: "EUR" for c in _EUR},
}

_NAMES = {"INDIA": "IN", "BHARAT": "IN"}


def _iso2(country: Any) -> str:
    c = str(country or "").strip().upper()
    return _NAMES.get(c, c[:2] if len(c) == 2 else c)


def _profile_currency(iso: str) -> str | None:
    """The country profile's currency (todo 24) for a country the built-in table does not know."""
    from shared import country_profiles
    p = country_profiles.get(iso)
    return p.currency if p is not None and p.currency and p.convertible else None


def party_currency(country: Any) -> str:
    """The currency to write to a party of this country in: its own where a rate exists, else USD."""
    iso = _iso2(country)
    if iso in _LOCAL:
        return _LOCAL[iso]
    cur = _COUNTRY_CURRENCY.get(iso) or _profile_currency(iso)
    if cur:
        try:
            from shared import fx_rates
            if fx_rates.has_rate(cur):
                return cur
        except Exception:  # noqa: BLE001
            pass
    return "USD"


def fallback_rate(currency: str) -> float | None:
    try:
        from shared import fx_rates
        row = fx_rates.cached(currency)
        if row:
            return float(row["rate"])
    except Exception:  # noqa: BLE001
        pass
    if currency == "INR":
        try:
            from agents.negotiation_agent.config import settings
            return float(settings.fallback_usd_inr_rate)
        except Exception:  # noqa: BLE001
            return 96.0
    return None


async def usd_rate(currency: str) -> float | None:
    """Units of ``currency`` per USD (1.0 for USD; None when PortLess has no rate)."""
    if currency == "USD":
        return 1.0
    if currency == "INR":
        try:
            from agents.negotiation_agent.market_intel import get_usd_inr_rate
            return float(await get_usd_inr_rate())
        except Exception:  # noqa: BLE001
            return fallback_rate(currency)
    try:
        from shared import fx_rates
        row = await fx_rates.usable(currency)
        return float(row["rate"]) if row else None
    except Exception:  # noqa: BLE001
        return None


async def rate_info(currency: str = "INR") -> dict[str, Any]:
    """The rate the console shows beside USD prices: ``{currency, rate, source}`` —
    ``live`` (web search) or ``fallback`` (the static ``NEGOTIATION_FALLBACK_USD_INR_RATE``)."""
    live = None
    if currency == "INR":
        try:
            from agents.negotiation_agent.market_intel import fetch_usd_inr_rate
            live = await fetch_usd_inr_rate()
        except Exception:  # noqa: BLE001
            live = None
    if live:
        return {"currency": currency, "rate": float(live), "source": "live"}
    row = None
    try:
        from shared import fx_rates
        row = await fx_rates.usable(currency)
    except Exception:  # noqa: BLE001
        row = None
    if row:
        return {"currency": currency, "rate": float(row["rate"]), "source": "reference",
                "reference": row.get("source"), "as_of": row.get("as_of")}
    rate = fallback_rate(currency)
    return {"currency": currency, "rate": rate, "source": "fallback" if rate else "none"}


def money(usd: Any, currency: str = "USD", rate: float | None = None, *, decimals: int = 2) -> str:
    """``USD 910.00`` — or, for a local currency with a rate, ``INR 87,360 (USD 910.00)``."""
    try:
        value = float(usd)
    except (TypeError, ValueError):
        return f"USD {usd}"
    if currency == "USD" or not rate:
        return f"USD {value:,.{decimals}f}"
    return f"{currency} {value * rate:,.0f} (USD {value:,.{decimals}f})"


def rate_note(currency: str, rate: float | None) -> str:
    """One line for the e-mail: the contract currency and the rate the local figures use."""
    if currency == "USD" or not rate:
        return ""
    return (f"{currency} figures are at {rate:,.2f} {currency} per USD for your reference; the Purchase Order "
            "and the payments are in USD.")


_INR = re.compile(r"(?:\bINR\b|\bRs\.?\s*\d|₹|\brupees?\b|\blakh\b)", re.IGNORECASE)
_USD = re.compile(r"(?:\bUSD\b|US\$|\$\s*\d|\bdollars?\b)", re.IGNORECASE)


def quoted_currency(text: str | None) -> str | None:
    """The currency a counterparty's own words price in (None when they name none, or both)."""
    if not text:
        return None
    inr, usd = bool(_INR.search(text)), bool(_USD.search(text))
    if inr and not usd:
        return "INR"
    if usd and not inr:
        return "USD"
    return None
