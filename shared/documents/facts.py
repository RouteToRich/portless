"""Everything already true about a deal, gathered once for a document.

Each lookup is best-effort (a document is still rendered when, say, the freight
booking does not exist yet), but a value that *does* exist always wins over a
default: the PI's agreed price over the buyer's opening target, the inspector's
loaded quantity over the contract quantity, the booked vessel over a blank.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_HS_FALLBACK = {"1006": "Rice", "1001": "Wheat", "1507": "Soybean oil", "1511": "Palm oil", "1701": "Sugar",
                "0901": "Coffee", "0902": "Tea", "0904": "Pepper", "0910": "Spices", "1005": "Maize"}


def _value(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


def party(deal: Any, role: str) -> dict[str, Any]:
    p = next((p for p in (getattr(deal, "parties", None) or []) if _value(getattr(p, "role", "")).lower() == role), None)
    if p is None:
        return {}
    return {k: getattr(p, k, None) for k in ("party_id", "legal_name", "country", "address", "email", "phone",
                                             "website", "gstin", "iec", "vat_number")}


def product_name(hs_code: str | None) -> str:
    from shared.product_label import product_label
    label = product_label(hs_code)
    if label:
        return label[:1].upper() + label[1:]
    digits = re.sub(r"\D", "", hs_code or "")
    return _HS_FALLBACK.get(digits[:4], f"Goods of HS {hs_code}")


async def _try(label: str, make: Any) -> Any:
    """``make`` builds the awaitable inside the guard: a repository that was
    never initialised raises when it is fetched, not when awaited."""
    try:
        return await make()
    except Exception as exc:  # noqa: BLE001
        logger.debug("DocumentFacts: %s unavailable: %s", label, exc)
        return None


async def deal_facts(deal_id: str, deal: Any | None = None) -> dict[str, Any]:
    from services.dealmaker.repository import get_repository as dm_repo
    from services.escrow_guard.repository import get_repository as eg_repo
    from services.shield_link.repository import get_repository as sl_repo

    if deal is None:
        from services.deals.service import get_deal
        deal = await get_deal(deal_id)
    pi = await _try("PI", lambda: dm_repo().get_pi_by_deal(deal_id))
    po = await _try("PO", lambda: dm_repo().get_po_by_deal(deal_id))
    orders = await _try("orders", lambda: sl_repo().list_by_deal(deal_id)) or []
    qc = next((o for o in reversed(orders) if o.order_type == "QC"), None)
    freight = next((o for o in reversed(orders) if o.order_type == "FREIGHT"), None)
    cha = next((o for o in reversed(orders) if o.order_type == "CHA"), None)
    hold = await _try("escrow", lambda: eg_repo().get_hold_by_deal(deal_id))
    payouts = await _try("payouts", lambda: eg_repo().list_payouts_by_deal(deal_id)) or []

    from shared.orchestration import compliance, supplier_commitment
    record = await _try("compliance", lambda: compliance.get_record(deal_id))
    loading = getattr(record, "loading", None)
    commitment = await _try("commitment", lambda: supplier_commitment.get(deal_id))
    session = None
    try:
        from agents.negotiation_agent.repository import get_repository as neg_repo
        session = await neg_repo().get_session_by_deal(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("DocumentFacts: negotiation unavailable: %s", exc)
    sheet = getattr(session, "cost_sheet", None)
    route_port = getattr(getattr(sheet, "supplier_route", None), "port", None)

    quantity = (getattr(pi, "quantity_mt", None) or getattr(deal, "quantity_mt", None)
                or getattr(session, "quantity_mt", None) or 0.0)
    loaded = getattr(loading, "loaded_quantity_mt", None)
    origin = (getattr(freight, "origin_port", None) or getattr(po, "port_of_loading", None)
              or (f"{route_port}, India" if route_port else None))
    destination = getattr(pi, "port_of_destination", None) or getattr(freight, "destination_port", None)
    containers = list(getattr(loading, "container_numbers", None) or [])
    if not containers and getattr(freight, "container_number", None):
        containers = [freight.container_number]
    pp = getattr(freight, "provider_payload", None) or {}
    return {
        "deal": deal, "deal_id": deal_id, "hs_code": getattr(deal, "hs_code", ""), "market": getattr(deal, "market", ""),
        "product": product_name(getattr(deal, "hs_code", "")),
        "buyer": party(deal, "buyer"), "supplier": party(deal, "supplier"),
        "pi": pi, "po": po, "qc": qc, "freight": freight, "cha": cha, "hold": hold, "payouts": payouts,
        "commitment": commitment, "loading": loading, "session": session,
        "currency": getattr(pi, "currency", None) or "USD",
        "unit_price": getattr(pi, "agreed_price", None),
        "quantity_mt": float(quantity or 0.0),
        "loaded_quantity_mt": float(loaded) if loaded else None,
        "shipped_quantity_mt": float(loaded or quantity or 0.0),
        "incoterm": getattr(pi, "incoterm", None) or "CIF",
        "payment_terms": getattr(pi, "payment_terms", None),
        "port_of_loading": origin,
        "port_of_destination": destination,
        "container_numbers": containers,
        "seal_numbers": list(getattr(loading, "seal_numbers", None) or []),
        "vessel": getattr(freight, "vessel", None), "voyage": getattr(freight, "voyage", None),
        "etd": getattr(freight, "etd", None), "eta": getattr(freight, "eta", None),
        "bl_number": pp.get("bl_number") or pp.get("bill_of_lading_number") or getattr(freight, "ext_ref", None),
        "qc_provider": (getattr(qc, "provider", None) or getattr(pi, "qc_provider", None) or getattr(session, "qc_provider", None)),
        "qc_ref": getattr(qc, "ext_ref", None), "qc_report_url": getattr(qc, "qc_report_url", None),
        "qc_result": getattr(qc, "qc_result", None), "qc_completed_at": getattr(qc, "qc_completed_at", None),
        "let_export_order": (getattr(cha, "provider_payload", None) or {}).get("leo_number"),
        "shipping_bill_number": (getattr(cha, "provider_payload", None) or {}).get("shipping_bill_number"),
    }
