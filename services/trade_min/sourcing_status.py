"""Where each opportunity's supplier side stands, for the Opportunity Radar.

An opportunity becomes a deal (``DealRecord.opportunity_id``, else the deal of the same
product × market — the radar shows only the latest scan of a corridor); the deal sends RFQs,
receives quotes and finally asks one supplier to confirm the order at its own price
(``shared/orchestration/supplier_commitment.py``). For each opportunity this returns
the furthest step reached:

  confirmed              a supplier confirmed the firm order at its price
  awaiting_confirmation  a supplier was asked to confirm and has not answered yet
  quoted                 suppliers replied to the RFQ with a price (best price given)
  rfq_sent               RFQs went out, no price yet
  no_rfq                 a deal exists, no RFQ sent
  (absent)               the opportunity has no deal
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _deals_by_opportunity(opportunity_ids: set[str]) -> dict[str, Any]:
    """Opportunity id → the newest deal made from it, else the newest deal of the same
    product × market. The radar lists only the latest scan of each corridor, while the deal
    usually hangs off an earlier (now CONVERTED) scan of that corridor."""
    from services.deals.repository import get_repository  # type: ignore[import]
    from .repository import get_repository as opportunities_repo

    deals = await get_repository().list_active(limit=1000)
    by_opp: dict[str, Any] = {}
    by_corridor: dict[tuple[str, str], Any] = {}

    def newer(a: Any, b: Any | None) -> bool:
        return b is None or a.created_at > b.created_at

    for d in deals:
        oid = getattr(d, "opportunity_id", None)
        if oid and newer(d, by_opp.get(oid)):
            by_opp[oid] = d
        corridor = (str(getattr(d, "hs_code", "") or ""), str(getattr(d, "market", "") or "").upper())
        if all(corridor) and newer(d, by_corridor.get(corridor)):
            by_corridor[corridor] = d

    out: dict[str, Any] = {}
    repo = await opportunities_repo()
    for oid in opportunity_ids:
        if oid in by_opp:
            out[oid] = by_opp[oid]
            continue
        try:
            opp = await repo.get(oid)
        except Exception:  # noqa: BLE001
            opp = None
        if opp is not None:
            deal = by_corridor.get((opp.hs_code, (opp.market or "").upper()))
            if deal is not None:
                out[oid] = deal
    return out


async def _one(deal: Any) -> dict[str, Any]:
    from services.sourcing.repository import get_repository as sourcing_repo  # type: ignore[import]
    from shared.orchestration import supplier_commitment

    repo = sourcing_repo()
    rfqs = await repo.list_rfqs(deal.deal_id)
    quotes = await repo.list_quotes(deal_id=deal.deal_id)
    asked = {d.supplier_id for r in rfqs for d in r.dispatch_results if d.success}
    best = min(quotes, key=lambda q: q.unit_price_usd) if quotes else None
    status: dict[str, Any] = {
        "deal_id": deal.deal_id,
        "deal_status": str(getattr(deal.status, "value", deal.status)),
        "rfqs_sent": len(rfqs),
        "suppliers_asked": len(asked),
        "quotes": len(quotes),
        "best_quote_usd_per_mt": best.unit_price_usd if best else None,
        "best_quote_supplier": (best.company_name or best.supplier_id) if best else None,
        "confirmed": False,
        "confirmed_price_usd_per_mt": None,
        "confirmed_supplier": None,
        "awaiting_supplier": None,
    }
    commitment = None
    try:
        commitment = await supplier_commitment.get(deal.deal_id)
    except Exception as exc:  # noqa: BLE001 — the radar still shows the RFQ facts
        logger.debug("Sourcing status: commitment of %s unavailable: %s", deal.deal_id, exc)
    if commitment is not None and commitment.status == "confirmed":
        current = commitment.current
        status.update(stage="confirmed", confirmed=True,
                      confirmed_price_usd_per_mt=commitment.confirmed_price or (current.price_usd_per_mt if current else None),
                      confirmed_supplier=(current.company_name or current.supplier_id) if current else commitment.confirmed_supplier_id)
    elif commitment is not None and commitment.status == "requested" and commitment.current:
        status.update(stage="awaiting_confirmation",
                      awaiting_supplier=commitment.current.company_name or commitment.current.supplier_id,
                      confirmed_price_usd_per_mt=None)
        status["requested_price_usd_per_mt"] = commitment.current.price_usd_per_mt
    elif quotes:
        status["stage"] = "quoted"
    elif rfqs:
        status["stage"] = "rfq_sent"
    else:
        status["stage"] = "no_rfq"
    return status


async def sourcing_status(opportunity_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Opportunity id → its supplier-side status (only opportunities that have a deal)."""
    wanted = {i for i in opportunity_ids if i}
    if not wanted:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for oid, deal in (await _deals_by_opportunity(wanted)).items():
        try:
            out[oid] = await _one(deal)
        except Exception as exc:  # noqa: BLE001 — one broken deal never hides the rest
            logger.warning("Sourcing status for opportunity %s failed: %s", oid, exc)
    return out
