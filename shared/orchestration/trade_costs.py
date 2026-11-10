"""Every cost between the supplier's gate and the buyer's port — worked out
before the first e-mail, so each price PortLess quotes already carries them.

PortLess buys from the supplier (FOB Indian port, sometimes ex-works) and sells
to the buyer CIF their port. Between the two it pays:

    inland transport + origin handling   (only when the supplier quotes ex-works;
                                          FOB already includes them)
    export customs agent (CHA) + export documents / certificates
    ocean freight to the destination port
    pre-shipment inspection
    marine insurance                     (% of the CIF value)
    escrow provider fee                  (% of what the buyer pays)
    contingency                          (% — demurrage, FX moves, small surprises)

The **cost sheet** keeps them as two numbers the pricing uses everywhere:

    fixed_usd_per_mt   — per-MT costs on top of the supplier's price
    pct_of_price       — costs that scale with the buyer's price (insurance, fee, contingency)

    net margin %       = (buyer − supplier − fixed − buyer × pct) / buyer × 100
    lowest buyer price = (supplier + fixed) / (1 − (pct + margin floor) / 100)
    highest supplier   = buyer × (1 − (pct + margin floor) / 100) − fixed

Every line says where its figure comes from (``quote`` / ``estimate`` / ``setting``)
and who pays it, so an operator can see exactly what a quoted price rests on.

Inland distance is estimated from the supplier's address (PIN code, else a city
or state name) to the usual export port of that state; truck freight is
``TRADE_COST_INLAND_INR_PER_TONNE_KM`` per tonne-km. All other defaults are
``TRADE_COST_*`` settings (USD) and only estimates until real quotes exist.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Supplier location → export port (India)
# ---------------------------------------------------------------------------

# State → (usual export port for its cargo, typical road km from the state's
# main producing / industrial area to that port). Approximate by design.
_STATE_PORT: dict[str, tuple[str, float]] = {
    "Delhi": ("Mundra", 1150), "Haryana": ("Mundra", 1200), "Punjab": ("Mundra", 1300),
    "Himachal Pradesh": ("Mundra", 1450), "Jammu and Kashmir": ("Mundra", 1650),
    "Uttarakhand": ("Mundra", 1350), "Uttar Pradesh": ("Mundra", 1350),
    "Rajasthan": ("Mundra", 700), "Gujarat": ("Mundra", 300), "Maharashtra": ("Nhava Sheva", 250),
    "Goa": ("Mormugao", 50), "Madhya Pradesh": ("Nhava Sheva", 700), "Chhattisgarh": ("Visakhapatnam", 550),
    "Telangana": ("Krishnapatnam", 500), "Andhra Pradesh": ("Kakinada", 200), "Karnataka": ("Chennai", 350),
    "Tamil Nadu": ("Chennai", 250), "Kerala": ("Kochi", 120), "Puducherry": ("Chennai", 160),
    "West Bengal": ("Kolkata", 150), "Odisha": ("Paradip", 200), "Bihar": ("Kolkata", 550),
    "Jharkhand": ("Haldia", 400), "Assam": ("Kolkata", 1050), "North East": ("Kolkata", 1200),
}

# First two digits of an Indian PIN code → state.
_PIN_STATE: dict[range, str] = {
    range(11, 12): "Delhi", range(12, 14): "Haryana", range(14, 17): "Punjab", range(17, 18): "Himachal Pradesh",
    range(18, 20): "Jammu and Kashmir", range(20, 24): "Uttar Pradesh", range(24, 25): "Uttarakhand",
    range(25, 29): "Uttar Pradesh", range(30, 35): "Rajasthan", range(36, 40): "Gujarat",
    range(40, 41): "Maharashtra", range(41, 45): "Maharashtra", range(45, 49): "Madhya Pradesh",
    range(49, 50): "Chhattisgarh", range(50, 51): "Telangana", range(51, 54): "Andhra Pradesh",
    range(56, 60): "Karnataka", range(60, 65): "Tamil Nadu", range(67, 70): "Kerala",
    range(70, 75): "West Bengal", range(75, 78): "Odisha", range(78, 79): "Assam", range(79, 80): "North East",
    range(80, 82): "Bihar", range(82, 84): "Jharkhand", range(84, 86): "Bihar",
}

# Producing towns that often appear in a supplier address without a PIN code.
_TOWN_STATE: dict[str, str] = {
    "karnal": "Haryana", "panipat": "Haryana", "kurukshetra": "Haryana", "sonipat": "Haryana", "gurgaon": "Haryana",
    "gurugram": "Haryana", "amritsar": "Punjab", "ludhiana": "Punjab", "jalandhar": "Punjab", "new delhi": "Delhi",
    "delhi": "Delhi", "mumbai": "Maharashtra", "pune": "Maharashtra", "nashik": "Maharashtra", "nagpur": "Maharashtra",
    "ahmedabad": "Gujarat", "rajkot": "Gujarat", "surat": "Gujarat", "vadodara": "Gujarat", "gandhidham": "Gujarat",
    "chennai": "Tamil Nadu", "coimbatore": "Tamil Nadu", "madurai": "Tamil Nadu", "erode": "Tamil Nadu",
    "tiruppur": "Tamil Nadu", "kolkata": "West Bengal", "hyderabad": "Telangana", "bengaluru": "Karnataka",
    "bangalore": "Karnataka", "mysuru": "Karnataka", "kakinada": "Andhra Pradesh", "vijayawada": "Andhra Pradesh",
    "guntur": "Andhra Pradesh", "visakhapatnam": "Andhra Pradesh", "kochi": "Kerala", "cochin": "Kerala",
    "indore": "Madhya Pradesh", "bhopal": "Madhya Pradesh", "raipur": "Chhattisgarh", "jaipur": "Rajasthan",
    "jodhpur": "Rajasthan", "lucknow": "Uttar Pradesh", "kanpur": "Uttar Pradesh", "agra": "Uttar Pradesh",
    "patna": "Bihar", "ranchi": "Jharkhand", "bhubaneswar": "Odisha", "guwahati": "Assam",
}

_PIN = re.compile(r"(?<!\d)([1-8]\d{5})(?!\d)")


class SupplierRoute(BaseModel):
    state: str | None = None
    port: str | None = None
    km: float | None = None
    basis: str = "unknown"
    """pin | town | state | unknown"""


def locate_supplier(address: str | None) -> SupplierRoute:
    """Supplier address → state → usual export port and road distance (estimate)."""
    text = (address or "").strip()
    if not text:
        return SupplierRoute()
    m = _PIN.search(text)
    if m:
        prefix = int(m.group(1)[:2])
        state = next((s for r, s in _PIN_STATE.items() if prefix in r), None)
        if state:
            port, km = _STATE_PORT[state]
            return SupplierRoute(state=state, port=port, km=km, basis="pin")
    low = text.lower()
    for town, state in _TOWN_STATE.items():
        if re.search(rf"\b{re.escape(town)}\b", low):
            port, km = _STATE_PORT[state]
            return SupplierRoute(state=state, port=port, km=km, basis="town")
    for state, (port, km) in _STATE_PORT.items():
        if state.lower() in low:
            return SupplierRoute(state=state, port=port, km=km, basis="state")
    return SupplierRoute()


# ---------------------------------------------------------------------------
# The cost sheet
# ---------------------------------------------------------------------------

class CostLine(BaseModel):
    key: str
    label: str
    usd_per_mt: float = 0.0
    pct_of_price: float = 0.0
    paid_by: str = "PortLess"
    source: str = "estimate"
    """quote | estimate | setting"""
    note: str = ""


class CostSheet(BaseModel):
    quantity_mt: float
    market: str | None = None
    supplier_price_basis: str = "FOB"
    supplier_route: SupplierRoute = Field(default_factory=SupplierRoute)
    lines: list[CostLine] = Field(default_factory=list)
    fixed_usd_per_mt: float = 0.0
    pct_of_price: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    computed_at: datetime | None = None


def _sum(lines: list[CostLine]) -> tuple[float, float]:
    fixed = sum(l.usd_per_mt for l in lines if l.paid_by == "PortLess")
    pct = sum(l.pct_of_price for l in lines if l.paid_by == "PortLess")
    return round(fixed, 4), round(pct, 4)


def build(
    *,
    quantity_mt: float | None,
    market: str | None,
    freight_usd_per_mt: float | None,
    freight_is_quote: bool,
    qc_usd_per_mt: float | None,
    qc_is_quote: bool,
    supplier_address: str | None,
    supplier_price_basis: str = "FOB",
    escrow_fee_pct: float | None = None,
    usd_inr_rate: float | None = None,
) -> CostSheet:
    """The deal's cost sheet from what is known now (quotes where they exist)."""
    from shared.orchestration import landed_cost

    qty = float(quantity_mt) if quantity_mt and quantity_mt > 0 else 20.0
    basis = (supplier_price_basis or "FOB").upper()
    rate = float(usd_inr_rate or _f("NEGOTIATION_FALLBACK_USD_INR_RATE", 96.0))
    route = locate_supplier(supplier_address)
    warnings: list[str] = []
    lines: list[CostLine] = []

    # 1. Supplier gate → Indian port.
    inr_tkm = _f("TRADE_COST_INLAND_INR_PER_TONNE_KM", 3.5)
    handling_inr = _f("TRADE_COST_INLAND_HANDLING_INR_PER_MT", 250.0)
    if route.km:
        inland = (route.km * inr_tkm + handling_inr) / rate
        where = f"~{route.km:,.0f} km by road from {route.state} to {route.port}"
    else:
        inland = (_f("TRADE_COST_INLAND_DEFAULT_KM", 600.0) * inr_tkm + handling_inr) / rate
        where = "supplier location unknown — assumed ~600 km to the port"
        warnings.append("supplier location unknown: inland transport is a rough estimate")
    ex_works = basis in ("EXW", "EX-WORKS", "EX WORKS", "FCA")
    lines.append(CostLine(
        key="inland_transport", label="Inland transport, supplier to port",
        usd_per_mt=round(inland, 2) if ex_works else 0.0,
        paid_by="PortLess" if ex_works else "supplier (inside FOB)",
        source="estimate", note=where + (f"; est. USD {inland:,.2f}/MT" if not ex_works else ""),
    ))
    containers = max(1, round(qty / 25.0 + 0.49))
    origin_handling = _f("TRADE_COST_ORIGIN_HANDLING_USD_PER_CONTAINER", 180.0) * containers / qty
    lines.append(CostLine(
        key="origin_handling", label="Origin terminal handling and container stuffing",
        usd_per_mt=round(origin_handling, 2) if ex_works else 0.0,
        paid_by="PortLess" if ex_works else "supplier (inside FOB)", source="setting",
        note=f"{containers} container(s)",
    ))

    # 2. Export clearance (PortLess is the exporter of record).
    cha = _f("TRADE_COST_CHA_USD_PER_SHIPMENT", 150.0) / qty
    docs = _f("TRADE_COST_DOCUMENTS_USD_PER_SHIPMENT", 120.0) / qty
    lines.append(CostLine(key="customs_agent", label="Export customs agent (CHA) and shipping bill",
                          usd_per_mt=round(cha, 2), source="setting", note="per shipment, spread over the lot"))
    lines.append(CostLine(key="export_documents", label="Export documents and certificates (origin, phytosanitary)",
                          usd_per_mt=round(docs, 2), source="setting", note="per shipment, spread over the lot"))

    # 3. Sea leg.
    if freight_usd_per_mt:
        freight, f_src = float(freight_usd_per_mt), ("quote" if freight_is_quote else "estimate")
    else:
        freight, f_src = landed_cost.freight_per_mt(market), "estimate"
    if f_src != "quote":
        warnings.append("ocean freight is an estimate until a forwarder quote exists")
    lines.append(CostLine(key="ocean_freight", label=f"Ocean freight to {market or 'the destination'}",
                          usd_per_mt=round(freight, 2), source=f_src))

    # 4. Inspection.
    if qc_usd_per_mt:
        qc, q_src = float(qc_usd_per_mt), ("quote" if qc_is_quote else "estimate")
    else:
        qc, q_src = landed_cost.inspection_usd_per_shipment() / qty, "estimate"
    lines.append(CostLine(key="inspection", label="Independent pre-shipment inspection and loading supervision",
                          usd_per_mt=round(qc, 2), source=q_src))

    # 5. Costs that scale with the price.
    lines.append(CostLine(key="marine_insurance", label="Marine cargo insurance (CIF)",
                          pct_of_price=landed_cost.insurance_pct(), source="setting"))
    fee = escrow_fee_pct if escrow_fee_pct is not None else landed_cost.escrow_fee_pct()
    lines.append(CostLine(key="escrow_fee", label="Escrow provider fee", pct_of_price=float(fee),
                          source="quote" if escrow_fee_pct is not None else "setting"))
    contingency = _f("TRADE_COST_CONTINGENCY_PCT", 0.5)
    if contingency:
        lines.append(CostLine(key="contingency", label="Contingency (demurrage, FX moves, small surprises)",
                              pct_of_price=contingency, source="setting"))

    fixed, pct = _sum(lines)
    return CostSheet(quantity_mt=qty, market=market, supplier_price_basis=basis, supplier_route=route,
                     lines=lines, fixed_usd_per_mt=fixed, pct_of_price=pct, warnings=warnings,
                     computed_at=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Arithmetic on a sheet (dict or model — sessions store it as a dict)
# ---------------------------------------------------------------------------

def _nums(sheet: Any) -> tuple[float, float]:
    if sheet is None:
        return 0.0, 0.0
    if isinstance(sheet, dict):
        return float(sheet.get("fixed_usd_per_mt") or 0.0), float(sheet.get("pct_of_price") or 0.0)
    return float(sheet.fixed_usd_per_mt), float(sheet.pct_of_price)


def net_margin_pct(sheet: Any, buyer_price: float, supplier_price: float) -> float:
    if not buyer_price or buyer_price <= 0:
        return 0.0
    fixed, pct = _nums(sheet)
    net = buyer_price - supplier_price - fixed - buyer_price * pct / 100.0
    return net / buyer_price * 100.0


def min_buyer_price(sheet: Any, supplier_price: float, margin_pct: float) -> float:
    fixed, pct = _nums(sheet)
    denom = 1.0 - (pct + margin_pct) / 100.0
    return (supplier_price + fixed) / denom if denom > 0 else float("inf")


def max_supplier_price(sheet: Any, buyer_price: float, margin_pct: float) -> float:
    fixed, pct = _nums(sheet)
    return buyer_price * (1.0 - (pct + margin_pct) / 100.0) - fixed


def cost_usd(sheet: Any, buyer_price: float, quantity_mt: float) -> dict[str, float]:
    """Per-line USD for a whole shipment at a given buyer price (for the books)."""
    out: dict[str, float] = {}
    lines = sheet.get("lines", []) if isinstance(sheet, dict) else [l.model_dump() for l in sheet.lines]
    for line in lines:
        if line.get("paid_by") != "PortLess":
            continue
        usd = (float(line.get("usd_per_mt") or 0.0) + buyer_price * float(line.get("pct_of_price") or 0.0) / 100.0) * quantity_mt
        if usd > 0:
            out[line["key"]] = round(usd, 2)
    return out


# ---------------------------------------------------------------------------
# What a counterparty may be told about how a price is built
# ---------------------------------------------------------------------------

def _route_of(sheet: Any) -> dict[str, Any]:
    if not sheet:
        return {}
    if isinstance(sheet, dict):
        return sheet.get("supplier_route") or {}
    return sheet.supplier_route.model_dump()


def buyer_price_basis(sheet: Any, qc_provider: str | None = None) -> list[str]:
    """What the buyer's CIF price includes — never an amount (that would expose our costs)."""
    route = _route_of(sheet)
    port = route.get("port")
    inspector = qc_provider.upper() if qc_provider else "the inspection agency you choose"
    return [
        f"ocean freight from {('the port of ' + port) if port else 'the Indian port of loading'} to your destination port",
        "marine cargo insurance to your port (CIF)",
        f"independent pre-shipment inspection and supervised container loading by {inspector}",
        "export customs clearance, shipping bill and export certificates (certificate of origin, phytosanitary where required)",
        "payment protection through a neutral escrow account, released in stages",
    ]


def supplier_price_basis(sheet: Any) -> list[str]:
    """Why our FOB price is a sound order for the supplier — never the buyer's price."""
    route = _route_of(sheet)
    port = route.get("port")
    return [
        f"FOB basis: you deliver to {('the port of ' + port) if port else 'your Indian port of loading'}; PortLess "
        "pays the ocean freight, marine insurance, export documentation and the inspection",
        "the purchase order is issued only after the buyer has paid into escrow, with a production advance — "
        "no credit risk and no letter-of-credit charges",
        "PortLess finds and manages the overseas buyer, so there is no sales or marketing cost on your side",
    ]
