"""Exposure limits (DEF-14): how much PortLess commits to before a person looks.

Checked before every Proforma Invoice — the moment PortLess commits to a buyer. Each limit is
off until set (USD):

* ``EXPOSURE_MAX_DEAL_USD``      — the PI value of this deal;
* ``EXPOSURE_MAX_BUYER_USD``     — this buyer's open deals (PI sent → not yet paid) + this one;
* ``EXPOSURE_MAX_SUPPLIER_USD``  — the same for the supplier;
* ``EXPOSURE_MAX_DAILY_USD``     — PIs issued today + this one;
* ``EXPOSURE_MAX_ESCROW_USD``    — money held in all escrows (funded, not yet released) + this one.

A breach withholds the PI and raises ``deal.exception`` (stage ``EXPOSURE:<limit>``) naming the
limit and the amounts. A person who accepts the risk records it
(``POST /deals/{id}/exposure/accept``, reason, audited) — then the PI goes out.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_OPEN = ("PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING", "ESCROW_FUNDED", "PO_ISSUED", "SUPPLIER_DISPATCH_READY",
         "QC_PASSED", "IN_TRANSIT", "CLEARED", "DISPUTE")
LIMITS = ("deal", "buyer", "supplier", "daily", "escrow", "country")


def country_limit(country: str | None) -> float | None:
    """EXPOSURE_MAX_COUNTRY_USD, or the lower EXPOSURE_MAX_STANDARD_COUNTRY_USD for a market whose
    profile is not ``preferred`` (one country's ban, strike or currency crisis hits less of the book)."""
    base = limit("country")
    try:
        from shared import country_profiles
        if country and country_profiles.tier(country) != "preferred":
            lower = limit("standard_country")
            if lower is not None:
                return min(lower, base) if base is not None else lower
    except Exception:  # noqa: BLE001
        pass
    return base


def limit(name: str) -> float | None:
    raw = os.getenv(f"EXPOSURE_MAX_{name.upper()}_USD", "").strip()
    try:
        value = float(raw) if raw else None
    except ValueError:
        return None
    return value if value and value > 0 else None


def _status(d: Any) -> str:
    return str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))


def _party_key(deal: Any, role: str) -> str | None:
    for p in getattr(deal, "parties", None) or []:
        if str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower() == role:
            return (getattr(p, "party_id", None) or getattr(p, "email", None) or getattr(p, "legal_name", None) or "").lower() or None
    return None


def _value(deal: Any) -> float:
    price = getattr(deal, "agreed_price_usd", None) or getattr(deal, "buyer_target_price_usd", None) or 0.0
    return float(price or 0.0) * float(getattr(deal, "quantity_mt", None) or 0.0)


def breaches(deal: Any, amount: float, deals: list[Any], escrow_held: float, now: datetime | None = None) -> list[dict[str, Any]]:
    """Pure: every limit this PI would cross."""
    now = now or datetime.now(timezone.utc)
    others = [d for d in deals if d.deal_id != deal.deal_id]
    totals: dict[str, float] = {"deal": amount, "escrow": escrow_held + amount}
    buyer, supplier = _party_key(deal, "buyer"), _party_key(deal, "supplier")
    totals["buyer"] = amount + sum(_value(d) for d in others if _status(d) in _OPEN and buyer and _party_key(d, "buyer") == buyer)
    totals["supplier"] = amount + sum(_value(d) for d in others
                                      if _status(d) in _OPEN and supplier and _party_key(d, "supplier") == supplier)
    today = now.date()
    totals["daily"] = amount + sum(_value(d) for d in others if _status(d) in _OPEN and getattr(d, "updated_at", None)
                                   and d.updated_at.date() == today and _status(d) == "PI_SENT")
    country = str(getattr(deal, "market", "") or "").upper()
    totals["country"] = amount + sum(_value(d) for d in others if _status(d) in _OPEN
                                     and str(getattr(d, "market", "") or "").upper() == country)
    out = []
    for name in LIMITS:
        cap = country_limit(country) if name == "country" else limit(name)
        if cap is not None and totals[name] > cap:
            out.append({"limit": name, "cap_usd": cap, "would_be_usd": round(totals[name], 2)})
    return out


async def check(deal: Any, amount: float) -> list[dict[str, Any]]:
    """Breaches for this PI, unless a person already accepted the exposure. Never raises."""
    if getattr(deal, "exposure_accepted_by", None) or not (any(limit(n) for n in LIMITS) or limit("standard_country")):
        return []
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        deals = []
    held = 0.0
    try:
        from services.escrow_guard.repository import get_repository as eg_repo  # type: ignore[import]
        for h in await eg_repo().list_holds():
            if h.status in ("FUNDED", "PARTIALLY_FUNDED"):
                held += float(h.funded_amount or h.amount or 0.0) - float(h.released_total or 0.0)
    except Exception:  # noqa: BLE001
        pass
    return breaches(deal, amount, deals, held)
