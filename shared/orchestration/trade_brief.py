"""One description of the trade for every outside party PortLess calls.

The carrier (Maersk), the inspection agency (QIMA / SGS / BV …), the escrow
provider (Tazapay / Escrow.com) and the payout rail (Payoneer) each get the
same references in their own free-text / reference fields, so nobody on the
other side has to ask "which order is this?":

  PortLess deal D-… · PI … · PO … · <product> (HS …) · <qty> MT · <incoterm> <POL> → <POD>
  · inspection: <types> by <agency> · escrow: <provider> · carrier booking … · contact <desk>

``brief(deal_id)`` gathers the facts once; ``note(brief, audience)`` words them
for one party. Nothing commercial leaks: a carrier / inspector note carries no
price and no buyer name; an escrow note carries no supplier and no supplier
price; a payout note carries no buyer and no sale price.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# UN/LOCODEs of the ports PortLess ships between most (override / extend with
# PORT_UNLOCODES="Port name=CODE,…").
_UNLOCODES = {
    "chennai": "INMAA", "nhava sheva": "INNSA", "jnpt": "INNSA", "mumbai": "INBOM", "mundra": "INMUN",
    "kolkata": "INCCU", "haldia": "INHAL", "tuticorin": "INTUT", "thoothukudi": "INTUT", "kochi": "INCOK",
    "cochin": "INCOK", "visakhapatnam": "INVTZ", "vizag": "INVTZ", "kandla": "INIXY", "pipavav": "INPAV",
    "kattupalli": "INKAT", "krishnapatnam": "INKRI",
    "jebel ali": "AEJEA", "dubai": "AEJEA", "abu dhabi": "AEAUH", "khalifa": "AEKHL", "sharjah": "AESHJ",
    "dammam": "SADMM", "jeddah": "SAJED", "doha": "QADOH", "hamad": "QAHMD", "sohar": "OMSOH", "salalah": "OMSLL",
    "kuwait": "KWSWK", "shuwaikh": "KWSWK", "bahrain": "BHKBS",
    "rotterdam": "NLRTM", "antwerp": "BEANR", "hamburg": "DEHAM", "bremerhaven": "DEBRV", "felixstowe": "GBFXT",
    "london gateway": "GBLGP", "valencia": "ESVLC", "barcelona": "ESBCN", "genoa": "ITGOA", "le havre": "FRLEH",
    "singapore": "SGSIN", "port klang": "MYPKG", "colombo": "LKCMB", "chittagong": "BDCGP", "laem chabang": "THLCH",
    "ho chi minh": "VNSGN", "jakarta": "IDJKT", "manila": "PHMNL", "shanghai": "CNSHA", "ningbo": "CNNGB",
    "busan": "KRPUS", "tokyo": "JPTYO",
    "durban": "ZADUR", "mombasa": "KEMBA", "dar es salaam": "TZDAR", "lagos": "NGAPP", "apapa": "NGAPP",
    "tema": "GHTEM", "djibouti": "DJJIB", "port said": "EGPSD", "alexandria": "EGALY",
    "los angeles": "USLAX", "long beach": "USLGB", "new york": "USNYC", "newark": "USEWR", "houston": "USHOU",
    "savannah": "USSAV", "vancouver": "CAVAN", "montreal": "CAMTR", "santos": "BRSSZ", "sydney": "AUSYD",
    "melbourne": "AUMEL",
}


def unlocode(port: str | None) -> str | None:
    """UN/LOCODE for a port name such as "Jebel Ali, AE" (None when unknown)."""
    if not port:
        return None
    text = str(port).strip()
    head = text.split(",")[0].strip()
    if len(head) == 5 and head.isalpha() and head.isupper():
        return head
    extra = dict(p.split("=", 1) for p in os.getenv("PORT_UNLOCODES", "").split(",") if "=" in p)
    for name, code in {**_UNLOCODES, **{k.strip().lower(): v.strip() for k, v in extra.items()}}.items():
        if name in text.lower():
            return code
    return None


async def brief(deal_id: str) -> dict[str, Any]:
    """Every reference of the trade that exists so far. Never raises."""
    from shared import company_profile
    from shared.product_label import product_title

    out: dict[str, Any] = {"deal_id": deal_id}
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        deal = await deals_repo().get(deal_id)
        if deal is not None:
            out.update({
                "hs_code": deal.hs_code, "market": getattr(deal, "market", None),
                "quantity_mt": getattr(deal, "quantity_mt", None),
                "inspection_types": list(getattr(deal, "inspection_types", None) or []),
                "escrow_provider": getattr(deal, "escrow_provider", None),
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("TradeBrief: deal %s unavailable: %s", deal_id, exc)
    try:
        from services.dealmaker.repository import get_repository as dm_repo  # type: ignore[import]
        pi = await dm_repo().get_pi_by_deal(deal_id)
        po = await dm_repo().get_po_by_deal(deal_id)
        if pi is not None:
            out.update({"pi_id": pi.pi_id, "incoterm": pi.incoterm, "port_of_destination": pi.port_of_destination,
                        "quantity_mt": out.get("quantity_mt") or pi.quantity_mt,
                        "escrow_provider": out.get("escrow_provider") or pi.escrow_provider})
        if po is not None:
            out.update({"po_id": po.po_id, "port_of_loading": po.port_of_loading,
                        "quantity_mt": out.get("quantity_mt") or po.quantity_mt})
    except Exception as exc:  # noqa: BLE001
        logger.debug("TradeBrief: PI/PO of %s unavailable: %s", deal_id, exc)
    try:
        from services.shield_link.repository import (
            get_repository as sl_repo,  # type: ignore[import]
        )
        orders = await sl_repo().list_by_deal(deal_id)
        qc = [o for o in orders if o.order_type == "QC" and o.status != "FAILED"]
        freight = [o for o in orders if o.order_type == "FREIGHT" and o.status != "FAILED"]
        if qc:
            out["inspection_agency"] = qc[-1].provider
        if freight:
            out.update({"carrier": freight[-1].provider, "carrier_booking": freight[-1].ext_ref})
    except Exception as exc:  # noqa: BLE001
        logger.debug("TradeBrief: orders of %s unavailable: %s", deal_id, exc)
    out["product"] = product_title(out.get("hs_code")) if out.get("hs_code") else None
    out["desk"] = " | ".join(x for x in (
        os.getenv("PORTLESS_CONTACT_EMAIL", "").strip(), os.getenv("PORTLESS_PHONE", "").strip(),
    ) if x) or company_profile.brand()
    return out


def _qty(b: dict[str, Any]) -> str:
    q = b.get("quantity_mt")
    return f"{float(q):g} MT" if q else ""


def note(b: dict[str, Any], audience: str, extra: str = "") -> str:
    """The trade in one line for ``carrier`` / ``inspector`` / ``escrow`` / ``payout`` / ``customs``."""
    product = f"{b['product']} (HS {b['hs_code']})" if b.get("product") else (f"HS {b['hs_code']}" if b.get("hs_code") else "")
    route = " → ".join(x for x in (b.get("port_of_loading"), b.get("port_of_destination")) if x)
    refs = [f"PortLess deal {b['deal_id']}"]
    if audience in ("escrow", "customs") and b.get("pi_id"):
        refs.append(f"PI {b['pi_id']}")
    if audience in ("carrier", "inspector", "payout", "customs") and b.get("po_id"):
        refs.append(f"PO {b['po_id']}")
    parts = [" · ".join(refs), product, _qty(b)]
    if audience in ("carrier", "escrow", "customs"):
        parts.append(" ".join(x for x in (b.get("incoterm"), route) if x))
    if audience in ("inspector", "escrow") and b.get("inspection_types"):
        parts.append("inspection " + "/".join(b["inspection_types"])
                     + (f" by {str(b.get('inspection_agency')).upper()}" if b.get("inspection_agency") else ""))
    if audience == "carrier":
        parts.append("container stuffing supervised and sealed by the inspector")
    if audience in ("escrow", "payout", "customs") and b.get("carrier_booking"):
        parts.append(f"{str(b.get('carrier') or 'carrier').title()} booking {b['carrier_booking']}")
    if audience == "escrow" and b.get("escrow_provider"):
        parts.append("staged release per the PI")
    if extra:
        parts.append(extra)
    parts.append(f"contact {b.get('desk')}")
    return " · ".join(p for p in parts if p)
