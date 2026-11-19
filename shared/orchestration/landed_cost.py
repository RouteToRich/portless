"""Landed-cost bridge between a supplier's FOB price and a buyer's price.

A supplier quotes FOB India; a buyer thinks in CIF (or CFR / FOB) at their port.
To compare them PortLess adds what it pays to move the goods and run the deal:

    cost/MT   = FOB + freight + inspection/MT [+ insurance on CIF]
    min offer = cost / (1 − escrow fee − insurance − margin floor)    (CIF)

so the minimum offer still leaves the deal's margin floor after the escrow
provider's fee. Import duty is the buyer's (CIF/CFR) — it is reported, not added,
because it decides whether PortLess is competitive in that market, not its cost.

Freight is an estimate per MT by destination region (sea, 20-40 MT lots, mid
2026 levels) until a freight quote exists; ``LANDED_FREIGHT_USD_PER_MT_<CC>``
overrides one country and ``LANDED_FREIGHT_USD_PER_MT`` all of them.
"""
from __future__ import annotations

import os

from pydantic import BaseModel

# USD per MT, India west coast → destination region (indicative, sea freight).
_REGION_FREIGHT = {
    "middle_east": 45.0, "south_asia": 40.0, "southeast_asia": 55.0, "east_asia": 65.0,
    "europe": 95.0, "africa": 85.0, "north_america": 120.0, "latin_america": 130.0, "oceania": 110.0,
}
_REGION_OF: dict[str, str] = {}
for _r, _ccs in {
    "middle_east": "AE SA QA KW OM BH IQ IR JO LB IL YE TR EG",
    "south_asia": "BD LK NP PK MV BT AF",
    "southeast_asia": "SG MY TH VN ID PH MM KH LA BN",
    "east_asia": "CN JP KR TW HK MN",
    "europe": "DE FR GB IT ES NL BE PL SE DK FI NO IE PT AT CH CZ GR RO HU BG HR SK SI LT LV EE LU RU UA",
    "africa": "ZA NG KE TZ GH ET MA DZ TN UG SN CI CM AO MZ",
    "north_america": "US CA MX",
    "latin_america": "BR AR CL CO PE VE EC UY PY BO",
    "oceania": "AU NZ",
}.items():
    for _cc in _ccs.split():
        _REGION_OF[_cc] = _r

INCOTERMS = ("EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP")
"""The eleven Incoterms® 2020 rules."""
D_TERMS = ("DAP", "DPU", "DDP")
NO_LOADING = ("FAS", "FCA")
"""The seller does not load the vessel (FAS: alongside; FCA: handed to the carrier)."""


def term(incoterm: str | None) -> str:
    """The exact Incoterms® 2020 rule (``C&F`` / ``CNF`` → CFR; unknown → CIF)."""
    inc = (incoterm or "CIF").upper().strip()
    if inc in ("CNF", "C&F"):
        return "CFR"
    return inc if inc in INCOTERMS else "CIF"


def ddp_refused(incoterm: str | None) -> bool:
    """DDP makes PortLess the importer of record abroad (duty, VAT, import licence) — never offered;
    the negotiation holds it at L0 (``ddp_requested``) and proposes DAP or CIF."""
    return term(incoterm) == "DDP"


def loading_usd_per_mt() -> float:
    """TRADE_COST_LOADING_USD_PER_MT (4): vessel loading the seller does not pay under FAS / FCA."""
    return _f("TRADE_COST_LOADING_USD_PER_MT", 4.0)


def destination_usd_per_mt(incoterm: str | None, quantity_mt: float | None = None,
                           container_mt: float | None = None) -> dict[str, float]:
    """Costs at destination PortLess carries under a D-term: destination terminal handling
    (``TRADE_COST_DTHC_USD_PER_CONTAINER`` 250), on-carriage to the named place
    (``TRADE_COST_DEST_HAULAGE_USD_PER_MT`` 12) and, for DPU only, unloading there
    (``TRADE_COST_DEST_UNLOADING_USD_PER_MT`` 5). Zero for every other rule."""
    inc = term(incoterm)
    if inc not in D_TERMS:
        return {}
    per_box = container_mt if container_mt and container_mt > 0 else 25.0
    out = {"dthc": _f("TRADE_COST_DTHC_USD_PER_CONTAINER", 250.0) / per_box,
           "destination_haulage": _f("TRADE_COST_DEST_HAULAGE_USD_PER_MT", 12.0)}
    if inc == "DPU":
        out["unloading"] = _f("TRADE_COST_DEST_UNLOADING_USD_PER_MT", 5.0)
    return {k: round(v, 2) for k, v in out.items()}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def freight_per_mt(market: str | None) -> float:
    cc = (market or "").upper()
    specific = os.getenv(f"LANDED_FREIGHT_USD_PER_MT_{cc}") if cc else None
    if specific:
        try:
            return float(specific)
        except ValueError:
            pass
    flat = os.getenv("LANDED_FREIGHT_USD_PER_MT")
    if flat:
        try:
            return float(flat)
        except ValueError:
            pass
    return _REGION_FREIGHT.get(_REGION_OF.get(cc, ""), 90.0)


def insurance_pct() -> float:
    return _f("LANDED_INSURANCE_PCT", 0.5)


def inspection_usd_per_shipment() -> float:
    return _f("LANDED_INSPECTION_USD_PER_SHIPMENT", 600.0)


def escrow_fee_pct() -> float:
    return _f("LANDED_ESCROW_FEE_PCT", 1.0)


def default_margin_floor_pct() -> float:
    return _f("DEALMAKER_PO_MARGIN_FLOOR_PCT", 8.0)


class LandedCost(BaseModel):
    fob_usd_per_mt: float
    freight_usd_per_mt: float
    inspection_usd_per_mt: float
    insurance_pct: float
    escrow_fee_pct: float
    margin_floor_pct: float
    incoterm: str
    destination_usd_per_mt: float = 0.0
    """D-terms only: destination handling, on-carriage and (DPU) unloading."""
    loading_saved_usd_per_mt: float = 0.0
    """FAS / FCA: vessel loading the seller does not pay."""
    term: str = ""
    """The exact Incoterms® 2020 rule asked for (``incoterm`` is its pricing group)."""
    cost_usd_per_mt: float
    """What PortLess pays per MT before its own margin (insurance and fee on the minimum offer included)."""
    min_offer_usd_per_mt: float
    """Lowest buyer price (in ``incoterm``) that still leaves the margin floor."""


def _norm_incoterm(incoterm: str | None) -> str:
    inc = (incoterm or "CIF").upper().strip()
    if inc in ("CNF", "C&F", "CFR"):
        return "CFR"
    if inc in ("FOB", "FCA", "FAS", "EXW"):
        return "FOB"
    return "CIF"  # CIF / CIP / CPT / DAP / DPU / DDP priced from CIF (+ destination costs for D-terms)


def landed(fob: float, market: str | None, quantity_mt: float | None = None, *,
           incoterm: str | None = "CIF", margin_floor_pct: float | None = None,
           freight_usd_per_mt: float | None = None) -> LandedCost:
    """``freight_usd_per_mt`` overrides the regional estimate (a lane quote)."""
    inc = _norm_incoterm(incoterm)
    qty = quantity_mt if quantity_mt and quantity_mt > 0 else 25.0
    freight = 0.0 if inc == "FOB" else (freight_usd_per_mt if freight_usd_per_mt is not None else freight_per_mt(market))
    inspection = inspection_usd_per_shipment() / qty
    ins = insurance_pct() if inc == "CIF" else 0.0
    fee = escrow_fee_pct()
    margin = default_margin_floor_pct() if margin_floor_pct is None else margin_floor_pct
    exact = term(incoterm)
    dest = sum(destination_usd_per_mt(exact, qty).values())
    saved = loading_usd_per_mt() if exact in NO_LOADING else 0.0
    base = fob + freight + inspection + dest - saved
    denom = 1.0 - (fee + ins + margin) / 100.0
    min_offer = base / denom if denom > 0 else float("inf")
    cost = base + min_offer * (fee + ins) / 100.0
    return LandedCost(
        fob_usd_per_mt=round(fob, 2), freight_usd_per_mt=round(freight, 2),
        inspection_usd_per_mt=round(inspection, 2), insurance_pct=ins, escrow_fee_pct=fee,
        margin_floor_pct=margin, incoterm=inc, term=exact, destination_usd_per_mt=round(dest, 2),
        loading_saved_usd_per_mt=round(saved, 2), cost_usd_per_mt=round(cost, 2),
        min_offer_usd_per_mt=round(min_offer, 2),
    )


def to_cif(price: float, incoterm: str | None, market: str | None) -> float:
    """A buyer price in another incoterm → the CIF equivalent (for the price book)."""
    inc = _norm_incoterm(incoterm)
    if inc == "FOB":
        return round((price + freight_per_mt(market)) * (1 + insurance_pct() / 100.0), 2)
    if inc == "CFR":
        return round(price * (1 + insurance_pct() / 100.0), 2)
    return round(price, 2)


def from_cif(price_cif: float, incoterm: str | None, market: str | None) -> float:
    inc = _norm_incoterm(incoterm)
    if inc == "FOB":
        return round(price_cif / (1 + insurance_pct() / 100.0) - freight_per_mt(market), 2)
    if inc == "CFR":
        return round(price_cif / (1 + insurance_pct() / 100.0), 2)
    return round(price_cif, 2)
