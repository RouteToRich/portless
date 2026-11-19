"""Reorder / account agent (todo 23.2): before a buyer's usual next order, offer the repeat.

For each buyer in ``reorders.due`` (``REORDER_AGENT_DAYS_BEFORE`` within the usual gap, default
the reminder window) with no open prospect for the product:
1. ``reorders.reorder`` opens the repeat prospect (same buyer, supplier, product, quantity) — the
   creation checks and the supplier's RFQ for a fresh quote run as for any prospect;
2. code prices a repeat range: the last agreed buyer price moved by the change in the supplier's
   price since then (price book: recent quotes, else the last PO price), never below the
   minimum offer that keeps the margin floor (``landed_cost``) — the LLM decides no number;
3. the buyer is added as an interested candidate and a ``SEND_INDICATIVE_OFFER`` gate (L0 as
   usual — a price goes out) carries the e-mail on the buyer's thread. A "yes" is read by the
   normal reply path; the prospect then moves on like any other.

``REORDER_AGENT_ENABLED`` (default on). Runs from the deal watcher once a day per buyer
(``reorder_offered_at`` on the old deal); ``POST /deals/reorders/{deal_id}/offer`` runs one now.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.getenv("REORDER_AGENT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def repeat_price(last_buyer: float | None, supplier_then: float | None, supplier_now: float | None,
                 min_offer: float | None) -> float | None:
    """Pure: last buyer price + the supplier's price move, floored at the minimum offer."""
    if not last_buyer and not min_offer:
        return None
    price = float(last_buyer or 0.0)
    if last_buyer and supplier_then and supplier_now:
        price = last_buyer + (supplier_now - supplier_then)
    if min_offer:
        price = max(price, float(min_offer))
    return round(price, 2)


async def _prices(old: Any) -> dict[str, float | None]:
    out: dict[str, float | None] = {"buyer": None, "supplier_then": None, "supplier_now": None}
    try:
        from services.dealmaker.repository import get_repository as dm_repo  # type: ignore[import]
        pi = await dm_repo().get_pi_by_deal(old.deal_id)
        po = await dm_repo().get_po_by_deal(old.deal_id)
        out["buyer"] = float(pi.agreed_price) if pi is not None else None
        out["supplier_then"] = float(po.agreed_price) if po is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ReorderAgent: PI / PO of %s unavailable: %s", old.deal_id, exc)
    out["buyer"] = out["buyer"] or getattr(old, "buyer_target_price_usd", None)
    try:
        from shared import pricebook
        est = await pricebook.estimate(old.hs_code, "supplier", market=old.market)
        if est.n_observations:
            out["supplier_now"] = est.median
    except Exception:  # noqa: BLE001
        pass
    out["supplier_now"] = out["supplier_now"] or out["supplier_then"]
    return out


async def offer_repeat(deal_id: str, *, by: str = "reorder_agent") -> dict[str, Any]:
    from services.deals.schemas import BuyerCandidate  # type: ignore[import]
    from services.deals.service import add_buyer_candidate, get_deal  # type: ignore[import]
    from shared.orchestration import landed_cost as lc
    from shared.orchestration import matchmaking, reorders
    old = await get_deal(deal_id)
    buyer = reorders._party(old, "buyer")  # noqa: SLF001
    if buyer is None or not getattr(buyer, "email", None):
        return {"deal_id": deal_id, "offered": False, "reason": "the buyer has no e-mail"}
    prices = await _prices(old)
    qty = float(getattr(old, "quantity_mt", None) or 25.0)
    min_offer = None
    if prices["supplier_now"]:
        min_offer = lc.landed(float(prices["supplier_now"]), old.market, qty,
                              margin_floor_pct=getattr(old, "margin_floor_pct", None)).min_offer_usd_per_mt
    low = repeat_price(prices["buyer"], prices["supplier_then"], prices["supplier_now"], min_offer)
    if not low:
        return {"deal_id": deal_id, "offered": False, "reason": "no price to repeat"}
    high = round(low * 1.02, 2)
    new = await reorders.reorder(deal_id, by=by, quantity_mt=qty)
    cid = f"REORDER-{deal_id}"
    cand = BuyerCandidate(candidate_id=cid, legal_name=buyer.legal_name, country=getattr(buyer, "country", "") or "",
                          email=buyer.email, source="reorder", status="interested")
    try:
        new = await add_buyer_candidate(new.deal_id, cand)
    except ValueError:
        pass
    cand = next(c for c in new.buyer_candidates if c.candidate_id == cid)
    from shared import company_profile as cp
    from shared.orchestration.outreach import _greeting, _product_label, subject_token
    from shared.product_label import product_title
    subject = f"{product_title(old.hs_code)} — your next order {subject_token(new.deal_id, cid)}"
    body = (f"{_greeting(cand)}\n\nThank you again for your last order of {_product_label(old)}. Based on your "
            f"usual ordering rhythm, you may be planning the next one.\n\nFor {qty:g} MT on the same terms, our "
            f"indicative price is USD {low:,.0f}–{high:,.0f} per MT CIF, with the same supplier, the same "
            "pre-shipment inspection and your payment protected in escrow as before.\n\nShall I prepare the "
            "proforma invoice? A short reply with the quantity and the shipment month is enough." + cp.signature())
    gid = await matchmaking._create_gate(  # noqa: SLF001
        new, matchmaking.OFFER_ACTION,
        summary=(f"Repeat offer → {buyer.legal_name} <{buyer.email}>: USD {low:,.0f}–{high:,.0f}/MT CIF for "
                 f"{qty:g} MT (last {prices['buyer'] or 0:,.0f}; supplier {prices['supplier_then'] or 0:,.0f} → "
                 f"{prices['supplier_now'] or 0:,.0f}; minimum {min_offer or 0:,.0f})"),
        payload={"deal_id": new.deal_id, "candidate_id": cid, "offer_id": f"REPEAT-{deal_id}",
                 "to_address": buyer.email, "subject": subject, "body": body,
                 "price_low_usd_per_mt": low, "price_high_usd_per_mt": high, "incoterm": "CIF",
                 "quantity_mt": qty, "min_offer_usd_per_mt": min_offer, "reorder_of": deal_id},
        key=f"reorder-offer-{deal_id}-{new.deal_id}", amount=round(low * qty, 2),
        risk_flags=["price_disclosed_to_buyer", "repeat_offer"],
    )
    from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
    await deals_repo().set_fields(deal_id, {"reorder_offered_at": datetime.now(timezone.utc)})
    return {"deal_id": deal_id, "offered": True, "new_deal_id": new.deal_id, "approval_id": gid,
            "price_low_usd_per_mt": low, "price_high_usd_per_mt": high}


async def sweep(now: datetime | None = None) -> dict[str, int]:
    counts = {"offered": 0}
    if not enabled():
        return counts
    now = now or datetime.now(timezone.utc)
    from shared.orchestration import reorders
    from services.deals.service import get_deal  # type: ignore[import]
    for row in await reorders.due(now):
        try:
            old = await get_deal(row["deal_id"])
            last = getattr(old, "reorder_offered_at", None)
            if last is not None and (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) > now - timedelta(days=30):
                continue
            res = await offer_repeat(row["deal_id"])
            counts["offered"] += int(bool(res.get("offered")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ReorderAgent: %s not offered: %s", row.get("deal_id"), exc)
    return counts
