"""Which currency PortLess writes to a counterparty in — from its country.

Contracts, the escrow, the PO and every payout stay in USD; what changes is
how a price is put to the party in e-mails: in its own currency first, with the
USD contract figure and the rate next to it, so nobody converts in their head
and nobody mistakes the equivalent for the contract price.

A country gets its own currency only where PortLess has a rate for it — today
the USD/INR rate the negotiation agent already uses (live search, else
``NEGOTIATION_FALLBACK_USD_INR_RATE``). Every other country is written to in
USD, the currency the trade is invoiced in. Add a country to ``_LOCAL`` only
together with a rate source in ``usd_rate``.
"""
from __future__ import annotations

import re
from typing import Any

_LOCAL: dict[str, str] = {"IN": "INR"}

_NAMES = {"INDIA": "IN", "BHARAT": "IN"}


def _iso2(country: Any) -> str:
    c = str(country or "").strip().upper()
    return _NAMES.get(c, c[:2] if len(c) == 2 else c)


def party_currency(country: Any) -> str:
    """The currency to write to a party of this country in: its own where a rate exists, else USD."""
    return _LOCAL.get(_iso2(country), "USD")


def fallback_rate(currency: str) -> float | None:
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
    return None


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
