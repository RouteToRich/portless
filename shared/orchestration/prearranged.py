"""Pre-arranged trade: buyer and supplier both known, entered by staff (todo: start the workflow
from a manual supplier — the third way).

No outreach, no matching: the deal is created with both parties, the buyer's price and the
supplier's price (a phone or e-mail quote), and moved DRAFT → MATCHING → NEGOTIATING. The
creation checks still run on the way (screening, trade compliance, the supplier's RFQ thread is
not needed because the price is known), then the negotiation opens from the two prices — the
normal path to the supplier's firm order request, the PI and the escrow.

The supplier's price is recorded as a manual quote (``SupplierOffer`` basis ``quoted`` only when
staff say it is a written quote, else ``usual``), so the match board, the price book and the
margin guard see it like any other.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


async def start(*, by: str, org_id: str, hs_code: str, market: str, quantity_mt: float, buyer: dict[str, Any],
                supplier: dict[str, Any], buyer_price_usd: float, supplier_price_usd: float,
                supplier_quote_written: bool = False, incoterm: str = "FOB") -> Any:
    from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
    from services.deals.schemas import (  # type: ignore[import]
        CreateDealRequest, DealStatus, MatchResult, SupplierOffer, UpdateStatusRequest,
    )
    from services.deals.service import create_deal, get_deal, update_status, upsert_supplier_offer  # type: ignore[import]
    from shared.domain.enums import PartyRole
    from shared.domain.models import Party

    if supplier_price_usd >= buyer_price_usd:
        raise ValueError("the supplier's price must be below the buyer's — there is no margin")
    supplier_id = supplier.get("supplier_id") or f"SUP-{uuid4().hex[:8].upper()}"
    parties = [
        Party(role=PartyRole.BUYER, legal_name=buyer["legal_name"], country=buyer.get("country") or market,
              email=buyer.get("email"), phone=buyer.get("phone"), contact_source="staff:prearranged",
              party_id=buyer.get("party_id") or f"BUY-{uuid4().hex[:8].upper()}"),
        Party(role=PartyRole.SUPPLIER, legal_name=supplier["legal_name"], country=supplier.get("country") or "IN",
              email=supplier.get("email"), phone=supplier.get("phone"), gstin=supplier.get("gstin"),
              iec=supplier.get("iec"), address=supplier.get("address"), contact_source="staff:prearranged",
              party_id=supplier_id),
    ]
    deal = await create_deal(CreateDealRequest(operator_id=by, hs_code=hs_code, market=market, parties=parties),
                             org_id=org_id)
    match = MatchResult(match_id=f"MATCH-{deal.deal_id}", supplier_id=supplier_id, company_name=supplier["legal_name"],
                        composite_score=100.0, gatekeeper_passed=True, arbitrage_score=100.0, proximity_score=100.0,
                        reliability_score=100.0, decision="SOURCE", fob_price_usd=float(supplier_price_usd))
    current = await get_deal(deal.deal_id)
    await deals_repo().save(current.model_copy(update={
        "quantity_mt": float(quantity_mt), "buyer_target_price_usd": float(buyer_price_usd),
        "winning_supplier_id": supplier_id, "match_results": [match], "prearranged_by": by,
        "updated_at": datetime.now(timezone.utc),
    }))
    await upsert_supplier_offer(deal.deal_id, SupplierOffer(
        offer_id=f"OFR-{uuid4().hex[:8].upper()}", supplier_id=supplier_id, company_name=supplier["legal_name"],
        email=supplier.get("email"), fob_price_usd_per_mt=float(supplier_price_usd), incoterm=incoterm,
        price_basis="quoted" if supplier_quote_written else "usual", received_at=datetime.now(timezone.utc),
        origin_deal_id=deal.deal_id,
    ))
    if str(getattr(deal.status, "value", deal.status)) == "DRAFT":
        await update_status(deal.deal_id, UpdateStatusRequest(target_status=DealStatus.MATCHING,
                                                              reason=f"pre-arranged trade entered by {by}"))
    await update_status(deal.deal_id, UpdateStatusRequest(target_status=DealStatus.NEGOTIATING,
                                                          reason="pre-arranged: buyer and supplier known"))
    logger.info("Prearranged: deal %s started by %s (buy %.2f / sell %.2f)", deal.deal_id, by,
                supplier_price_usd, buyer_price_usd)
    return await get_deal(deal.deal_id)
