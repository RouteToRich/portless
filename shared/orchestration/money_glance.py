"""Money at a glance for the Deal Board: buy price, sell price and profit per deal.

- **sell** (USD/MT): the PI's agreed price; before a PI, the buyer target (``estimate``).
- **buy**  (USD/MT): the PO's agreed price; before a PO, the winning supplier's FOB from the
  match results or the cheapest valid supplier offer (``estimate``).
- **profit** (USD): the realised margin once the escrow settled, else the estimated margin.

Each figure says whether it is ``firm`` (a document) or an ``estimate``.
``GET /deals/board/money`` returns one row per deal the caller can see.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _buy_estimate(deal: Any) -> float | None:
    win = getattr(deal, "winning_supplier_id", None)
    for m in getattr(deal, "match_results", None) or []:
        if win and m.supplier_id == win and getattr(m, "fob_price_usd", None):
            return float(m.fob_price_usd)
    prices = [o.fob_price_usd_per_mt for o in (getattr(deal, "supplier_offers", None) or []) if o.fob_price_usd_per_mt]
    return float(min(prices)) if prices else None


async def row(deal: Any) -> dict[str, Any]:
    pi = po = None
    try:
        from services.dealmaker.repository import get_repository  # type: ignore[import]
        repo = get_repository()
        pi = await repo.get_pi_by_deal(deal.deal_id)
        po = await repo.get_po_by_deal(deal.deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Money glance: documents of %s unavailable: %s", deal.deal_id, exc)
    sell = float(pi.agreed_price) if pi is not None else getattr(deal, "buyer_target_price_usd", None)
    buy = float(po.agreed_price) if po is not None else _buy_estimate(deal)
    qty = (float(pi.quantity_mt) if pi is not None else getattr(deal, "quantity_mt", None))
    realized = getattr(deal, "realized_margin_usd", None)
    profit = realized if realized is not None else getattr(deal, "est_margin_usd", None)
    return {
        "deal_id": deal.deal_id,
        "sell_usd_per_mt": round(sell, 2) if sell else None, "sell_basis": "firm" if pi is not None else "estimate",
        "buy_usd_per_mt": round(buy, 2) if buy else None, "buy_basis": "firm" if po is not None else "estimate",
        "quantity_mt": qty,
        "profit_usd": round(float(profit), 2) if profit is not None else None,
        "profit_basis": "realised" if realized is not None else "estimate",
    }


async def rows(deals: list[Any]) -> dict[str, dict[str, Any]]:
    return {d.deal_id: await row(d) for d in deals}
