"""Claims and disputes — the options, computed by code (todo 23.2 "claims and dispute agent").

After a failed QC or a buyer complaint, ``GET /deals/{id}/claim-options`` lists what PortLess can
offer, each with what it costs PortLess, what it costs the supplier and the money it puts at
risk, ranked cheapest for PortLess first:

- **re-inspection** — the supplier reworks the lot; the inspection fee is charged back to the
  supplier (``SUPPLIER_REINSPECTION_CHARGE``) — only while re-inspections are left
  (``QC_REINSPECTION_MAX``);
- **discount** — a price reduction for the buyer, at most what keeps the margin floor
  (estimated margin − floor), passed to the supplier in full when the defect is the supplier's;
- **replacement lot** — the supplier ships a new lot; PortLess pays nothing more but the deal
  waits;
- **partial refund** — part of the escrow back to the buyer for the affected share; the supplier
  is paid that much less;
- **cancel and refund** — the whole escrow back, the supplier not paid.

Nothing is sent: the operator picks one and the e-mails / refund go through their own gates
(RESOLVE_EXCEPTION / REFUND_ESCROW). An LLM may word the proposal later; no number comes from it.
"""
from __future__ import annotations

import os
from typing import Any


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def options(*, sale_value_usd: float, purchase_value_usd: float, margin_floor_pct: float,
            inspection_fee_usd: float, reinspections_left: int, affected_share: float = 1.0,
            supplier_at_fault: bool = True) -> list[dict[str, Any]]:
    """Pure: the resolution options with their cost to PortLess and the supplier."""
    margin = sale_value_usd - purchase_value_usd
    floor = sale_value_usd * margin_floor_pct / 100.0
    room = max(0.0, margin - floor)
    share = min(1.0, max(0.0, affected_share))
    out: list[dict[str, Any]] = []
    if reinspections_left > 0:
        out.append({"option": "reinspection", "portless_cost_usd": 0.0 if supplier_at_fault else inspection_fee_usd,
                    "supplier_cost_usd": inspection_fee_usd if supplier_at_fault else 0.0,
                    "buyer_gets": "the lot reworked and inspected again", "delay": "rework + inspection (1–3 weeks)",
                    "money_at_risk_usd": 0.0})
    discount = round(min(room, sale_value_usd * share * _f("CLAIM_DISCOUNT_MAX_PCT", 10.0) / 100.0), 2)
    if discount > 0:
        out.append({"option": "discount", "discount_usd": discount,
                    "portless_cost_usd": 0.0 if supplier_at_fault else discount,
                    "supplier_cost_usd": discount if supplier_at_fault else 0.0,
                    "buyer_gets": f"USD {discount:,.0f} off the price", "delay": "none",
                    "money_at_risk_usd": discount,
                    "note": "within the margin above the floor" + (" — charged back to the supplier" if supplier_at_fault else "")})
    out.append({"option": "replacement_lot", "portless_cost_usd": 0.0,
                "supplier_cost_usd": round(purchase_value_usd * share, 2) if supplier_at_fault else 0.0,
                "buyer_gets": "a new lot for the affected quantity", "delay": "a new production run",
                "money_at_risk_usd": 0.0})
    refund = round(sale_value_usd * share, 2)
    out.append({"option": "partial_refund" if share < 1.0 else "cancel_and_refund", "refund_usd": refund,
                "portless_cost_usd": round(max(0.0, margin * share) if not supplier_at_fault else 0.0, 2),
                "supplier_cost_usd": round(purchase_value_usd * share, 2) if supplier_at_fault else 0.0,
                "margin_lost_usd": round(margin * share, 2),
                "buyer_gets": f"USD {refund:,.0f} back from escrow", "delay": "refund through the escrow provider",
                "money_at_risk_usd": refund})
    return sorted(out, key=lambda o: (o["portless_cost_usd"], o["money_at_risk_usd"]))


async def claim_options(deal_id: str, *, affected_share: float = 1.0, supplier_at_fault: bool = True) -> dict[str, Any]:
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.documents.facts import deal_facts
    from shared.orchestration import landed_cost as lc
    deal = await get_deal(deal_id)
    facts = await deal_facts(deal_id, deal)
    qty = float(facts.get("shipped_quantity_mt") or getattr(deal, "quantity_mt", None) or 0.0)
    sale = float(facts.get("unit_price") or 0.0) * qty
    po = facts.get("po")
    purchase = float(getattr(po, "agreed_price", 0.0) or 0.0) * qty
    if not sale or not purchase:
        return {"deal_id": deal_id, "options": [], "reason": "no PI / PO price yet — nothing to resolve against"}
    failed = sum(1 for o in (getattr(deal, "inspections", None) or [])
                 if str((o or {}).get("result") if isinstance(o, dict) else getattr(o, "result", "")).upper() == "FAIL")
    left = max(0, int(_f("QC_REINSPECTION_MAX", 1)) - max(0, failed - 1))
    rows = options(sale_value_usd=sale, purchase_value_usd=purchase,
                   margin_floor_pct=getattr(deal, "margin_floor_pct", None) or lc.default_margin_floor_pct(),
                   inspection_fee_usd=lc.inspection_usd_per_shipment(), reinspections_left=left,
                   affected_share=affected_share, supplier_at_fault=supplier_at_fault)
    return {"deal_id": deal_id, "sale_value_usd": round(sale, 2), "purchase_value_usd": round(purchase, 2),
            "affected_share": affected_share, "supplier_at_fault": supplier_at_fault, "options": rows,
            "recommended": rows[0]["option"] if rows else None,
            "note": "Pick one; e-mails and refunds go through their own gates."}
