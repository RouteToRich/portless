"""Markets the directory points to, beyond the scanned corridors (todo 18.1).

- **Proposed markets** (``GET /trade-min/catalogue/proposed-markets``): a country where buyers
  of a catalogue product were found — mostly by the worldwide search, which is "not tied to a
  scanned market" — at least ``WORLDWIDE_MARKET_MIN_BUYERS`` (3) times, and that is not yet one
  of the line's markets. ``POST …/accept`` adds it to the line, so the next scan covers it.
- **Markets for a supplier** (``GET /trade-min/suppliers/{id}/markets``): every market whose
  directory buyers want the supplier's products, with how many are contactable and checked —
  supplier-led opportunities beyond the scanned corridors; ``POST /trade-min/suppliers/{id}/start-prospect``
  (supply_first.py) starts one.

Only buyers that may be contacted count: a usable e-mail, not do-not-contact, not failed the
check, not ``role_suspect`` (an Indian exporter found as a "buyer").
"""
from __future__ import annotations

import os
from typing import Any

from . import directory
from .schemas import CounterpartyRecord


def min_buyers() -> int:
    try:
        return max(1, int(os.getenv("WORLDWIDE_MARKET_MIN_BUYERS", "3")))
    except ValueError:
        return 3


def _standard_extra() -> int:
    """WORLDWIDE_MARKET_STANDARD_EXTRA (2): a ``standard`` market needs this many more buyers than a
    ``preferred`` one before it is proposed; ``review`` / ``blocked`` markets are never proposed."""
    try:
        return max(0, int(os.getenv("WORLDWIDE_MARKET_STANDARD_EXTRA", "2")))
    except ValueError:
        return 2


def contactable(rec: CounterpartyRecord) -> bool:
    if rec.do_not_contact or not rec.email or "role_suspect" in (rec.flags or []):
        return False
    v = rec.validation
    if v is not None and (v.status == "failed" or (v.email or "") in ("invalid", "bounced")):
        return False
    return True


def _hs6(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


async def proposed_markets() -> list[dict[str, Any]]:
    from shared import catalogue
    lines = catalogue.active_lines()
    if not lines:
        return []
    store = await directory.get_directory()
    buyers = await store.list("buyer", limit=100_000)
    out: list[dict[str, Any]] = []
    for line in lines:
        prefix = _hs6(line.hs_code)
        counts: dict[str, dict[str, Any]] = {}
        for b in buyers:
            country = (b.country or "").upper()
            if not country or country == "IN" or country in line.markets:
                continue
            if not any(_hs6(h).startswith(prefix[:len(prefix)]) or prefix.startswith(_hs6(h)) for h in b.hs_codes if h):
                continue
            if not contactable(b):
                continue
            row = counts.setdefault(country, {"buyers": 0, "verified": 0, "worldwide": 0, "examples": []})
            row["buyers"] += 1
            row["verified"] += int(b.validation is not None and b.validation.status == "verified")
            row["worldwide"] += int(b.last_outcome == directory.WORLDWIDE_OUTCOME)
            if len(row["examples"]) < 3:
                row["examples"].append(b.name)
        from shared import country_profiles
        for country, row in counts.items():
            tier = country_profiles.tier(country)
            need = {"preferred": min_buyers(), "standard": min_buyers() + _standard_extra()}.get(tier)
            if need is not None and row["buyers"] >= need:
                out.append({"product_id": line.product_id, "hs_code": line.hs_code, "product": line.name,
                            "market": country, "tier": tier, **row})
    return sorted(out, key=lambda r: (-r["verified"], -r["buyers"]))


async def accept_market(product_id: str, market: str, *, by: str) -> dict[str, Any]:
    """Add a proposed market to the catalogue line (staff, one click)."""
    from shared import catalogue
    market = (market or "").strip().upper()[:2]
    if len(market) != 2:
        raise ValueError("market must be an ISO-2 country code")
    lines = list(catalogue.get_catalogue().products)
    line = next((p for p in lines if p.product_id == product_id), None)
    if line is None:
        raise LookupError(f"no catalogue line {product_id}")
    if market not in line.markets:
        line = line.model_copy(update={"markets": sorted({*line.markets, market})})
        lines = [line if p.product_id == product_id else p for p in lines]
        await catalogue.set_catalogue(lines, updated_by=by)
    return {"product_id": product_id, "markets": line.markets}


async def markets_for_supplier(record_id: str) -> dict[str, Any]:
    store = await directory.get_directory()
    sup = await store.get(record_id if record_id.startswith("supplier:") else f"supplier:{record_id}")
    if sup is None or sup.role != "supplier":
        raise LookupError(f"{record_id} not in the supplier directory")
    codes = {_hs6(h) for h in sup.hs_codes if h}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for b in await store.list("buyer", limit=100_000):
        match = next((c for c in codes for h in b.hs_codes if h and _hs6(h)[:4] == c[:4]), None)
        country = (b.country or "").upper()
        if not match or not country or country == "IN":
            continue
        row = rows.setdefault((match, country), {"hs_code": match, "market": country, "buyers": 0,
                                                 "contactable": 0, "verified": 0, "examples": []})
        row["buyers"] += 1
        ok = contactable(b)
        row["contactable"] += int(ok)
        row["verified"] += int(ok and b.validation is not None and b.validation.status == "verified")
        if ok and len(row["examples"]) < 3:
            row["examples"].append(b.name)
    markets = sorted((r for r in rows.values() if r["contactable"]),
                     key=lambda r: (-r["verified"], -r["contactable"]))
    return {"supplier_id": sup.id, "name": sup.name, "hs_codes": sorted(codes), "markets": markets}
