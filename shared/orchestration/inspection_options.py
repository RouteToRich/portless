"""Inspection cost options (todo 11.2): compare the inspectors' prices for this booking and
offer the buyer a cheaper inspection plan when it is enough.

``GET /deals/{id}/inspection-options``:
- **per inspector** — every enabled QC provider (``QC_PROVIDERS_ENABLED``) is asked for its price
  for the deal's quantity (``quote``; QIMA has no quote endpoint, so its catalogue fee), cheapest
  first;
- **per plan** — the deal's current plan (DUPRO / PSI / PEO) priced as one visit per phase, and
  the lighter plans with what each saves. A lighter plan is marked ``enough`` only when it keeps
  the pre-shipment inspection (PSI — the PO tranche and the shipment check need it) and the lot
  is at most ``INSPECTION_PSI_ONLY_MAX_MT`` (100 MT) or the supplier has passed at least two
  inspections with no failure. The buyer chooses: the plan only changes through
  ``PUT /deals/{id}/inspection-plan``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def psi_only_max_mt() -> float:
    try:
        return float(os.getenv("INSPECTION_PSI_ONLY_MAX_MT", "100") or 100)
    except ValueError:
        return 100.0


def plans_from(current: list[str], fee_per_visit: float, *, quantity_mt: float,
               supplier_record: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Pure: the current plan and every lighter plan that keeps the PSI, with cost and savings."""
    cur = [p.upper() for p in current] or ["PSI"]
    candidates = [cur]
    for alt in (["DUPRO", "PSI"], ["PSI", "PEO"], ["PSI"]):
        if set(alt) < set(cur) and alt not in candidates:
            candidates.append(alt)
    passed = int((supplier_record or {}).get("qc_passed") or 0)
    failed = int((supplier_record or {}).get("qc_failed") or 0)
    proven = passed >= 2 and failed == 0
    out = []
    for plan in candidates:
        cost = round(fee_per_visit * len(plan), 2)
        saving = round(fee_per_visit * (len(cur) - len(plan)), 2)
        if plan == cur:
            why, enough = "current plan", True
        elif quantity_mt <= psi_only_max_mt() or proven:
            enough = True
            why = (f"lot of {quantity_mt:g} MT is within {psi_only_max_mt():g} MT" if quantity_mt <= psi_only_max_mt()
                   else f"supplier passed {passed} inspections with no failure")
        else:
            enough, why = False, f"lot of {quantity_mt:g} MT and no proven inspection record — keep the full plan"
        out.append({"plan": plan, "visits": len(plan), "cost_usd": cost, "saving_usd": saving,
                    "enough": enough, "why": why, "current": plan == cur})
    return out


async def options(deal_id: str) -> dict[str, Any]:
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.connectors.qc_providers import enabled_qc_providers, get_qc_connector
    from shared.orchestration import inspection_plan
    deal = await get_deal(deal_id)
    qty = float(getattr(deal, "quantity_mt", None) or 25.0)
    inspectors = []
    for name in enabled_qc_providers():
        try:
            q = await get_qc_connector(name).quote({"quantity_mt": qty, "deal_id": deal_id})
            inspectors.append({"provider": name, "fee_usd": q.get("fee_usd"), "source": q.get("price_source"),
                               "inspection_date": q.get("inspection_date"), "evidence": q.get("evidence")})
        except Exception as exc:  # noqa: BLE001
            inspectors.append({"provider": name, "fee_usd": None, "error": str(exc)[:200]})
    priced = sorted((i for i in inspectors if i.get("fee_usd")), key=lambda i: i["fee_usd"])
    fee = float(priced[0]["fee_usd"]) if priced else 0.0
    record = None
    try:
        from shared.orchestration import supplier_performance
        perf = await supplier_performance.cached()
        supplier = next((p for p in getattr(deal, "parties", None) or []
                         if str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower() == "supplier"), None)
        if supplier is not None and isinstance(perf, dict):
            record = next((v for k, v in perf.items() if supplier.legal_name and str(supplier.legal_name).lower() in str(k).lower()), None)
    except Exception:  # noqa: BLE001
        record = None
    current = await inspection_plan.plan_for(deal_id)
    plans = plans_from(current, fee, quantity_mt=qty, supplier_record=record)
    cheaper = [p for p in plans if not p["current"] and p["enough"]]
    return {"deal_id": deal_id, "quantity_mt": qty, "inspectors": priced + [i for i in inspectors if not i.get("fee_usd")],
            "cheapest_inspector": priced[0]["provider"] if priced else None, "plans": plans,
            "offer_buyer": max(cheaper, key=lambda p: p["saving_usd"]) if cheaper else None,
            "note": "The buyer chooses the plan (PUT /deals/{id}/inspection-plan); the PSI always stays."}
