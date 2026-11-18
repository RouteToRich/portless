"""Start a prospect from a supplier (supply first).

A known supplier offers a lot — a mill with 200 MT of Ponni rice — and PortLess looks for the
buyer. ``POST /trade-min/suppliers/{id}/start-prospect`` (staff) with the market (and optional
HS code / quantity):

1. creates a deal for that product × market, with the supplier as the supplier party and its
   usual price as a supplier offer on the match board (an estimate until it confirms in writing);
2. adds the best contactable buyers of the product from the directory as buyer candidates
   (not failed, not do-not-contact, not declined, with an e-mail; best score first, at most
   ``SUPPLY_FIRST_MAX_BUYERS`` = 10; buyers in the chosen market first);
3. moves the deal to MATCHING and starts the outreach funnel (the introductions go through
   the usual ``SEND_BUYER_OUTREACH`` gate).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from services.trade_min import directory, relationships

logger = logging.getLogger(__name__)


class CannotStart(ValueError):
    pass


def max_buyers() -> int:
    try:
        return max(1, int(os.getenv("SUPPLY_FIRST_MAX_BUYERS", "10")))
    except ValueError:
        return 10


async def buyers_for(hs_code: str, market: str) -> list[Any]:
    store = await directory.get_directory()
    rows = await relationships.annotate(await store.list("buyer", limit=100_000))
    hs6 = hs_code[:6]
    out = []
    for r in rows:
        if not r.email or r.do_not_contact or (r.validation and r.validation.status == "failed"):
            continue
        if (r.relationship or {}).get("stage") in ("declined", "do_not_contact"):
            continue
        if "role_suspect" in r.flags or not any(c[:6] == hs6 or c.startswith(hs6) or hs6.startswith(c[:4]) for c in r.hs_codes):
            continue
        out.append(r)
    out.sort(key=lambda r: ((r.country or "").upper() != market.upper(), -(r.score or 0)))
    return out[:max_buyers()]


async def start(record_id: str, market: str, *, by: str, org_id: str, hs_code: str | None = None,
                quantity_mt: float | None = None) -> dict[str, Any]:
    from services.deals import service as deals  # type: ignore[import]
    from services.deals.schemas import (  # type: ignore[import]
        BuyerCandidate,
        CreateDealRequest,
        SupplierOffer,
        UpdateStatusRequest,
    )
    from shared.domain.enums import DealStatus
    from shared.domain.models import Party

    store = await directory.get_directory()
    sup = await store.get(record_id)
    if sup is None or sup.role != "supplier":
        raise LookupError(f"{record_id} is not a supplier in the directory")
    if sup.do_not_contact or (sup.validation and sup.validation.status == "failed"):
        raise CannotStart(f"{sup.name} cannot be traded with (do-not-contact or failed check)")
    hs = (hs_code or (sup.hs_codes[0] if sup.hs_codes else "")).strip()
    if not hs:
        raise CannotStart("say which product (HS code) — the supplier has none on record")
    market = (market or "").strip().upper()
    if len(market) != 2:
        raise CannotStart("market must be a 2-letter country code")
    buyers = await buyers_for(hs, market)
    if not buyers:
        raise CannotStart(f"no contactable buyer of HS {hs} in the directory yet — run a buyer search for it first")

    party = Party(role="supplier", party_id=sup.id, legal_name=sup.name, country=sup.country or "IN",
                  email=sup.email, phone=sup.phone, website=sup.website, address=sup.address, gstin=sup.gstin,
                  iec=sup.iec, contact_source="directory",
                  verification_status="scan_verified" if sup.validation and sup.validation.status == "verified" else "unverified")
    deal = await deals.create_deal(CreateDealRequest(operator_id=by, hs_code=hs, market=market, parties=[party]), org_id)
    if quantity_mt:
        try:
            from services.deals.repository import get_repository  # type: ignore[import]
            await get_repository().set_fields(deal.deal_id, {"quantity_mt": float(quantity_mt)})
        except Exception as exc:  # noqa: BLE001
            logger.debug("SupplyFirst: quantity not set on %s: %s", deal.deal_id, exc)
    manual = sup.manual or {}
    await deals.upsert_supplier_offer(deal.deal_id, SupplierOffer(
        offer_id=f"OFR-SF-{deal.deal_id[-8:]}", supplier_id=sup.id, company_name=sup.name, email=sup.email,
        fob_price_usd_per_mt=sup.estimated_fob_price_usd, price_basis="usual", moq_mt=manual.get("moq_mt"),
        capacity_mt_per_month=manual.get("capacity_mt_per_month"), port_of_loading=manual.get("port_of_loading"),
        source="directory_usual_price"))
    for b in buyers:
        cid = (b.id.split(":", 1)[-1])[:60]
        try:
            await deals.add_buyer_candidate(deal.deal_id, BuyerCandidate(
                candidate_id=cid, legal_name=b.name, country=b.country or market, email=b.email, website=b.website,
                phone=b.phone, source=f"directory:{b.source}", fit_score=b.fit_score,
                validation_status=b.validation.status if b.validation else None))
        except ValueError:
            continue  # same candidate twice
    deal = await deals.update_status(deal.deal_id, UpdateStatusRequest(
        target_status=DealStatus.MATCHING, reason=f"supply-first prospect from {sup.name} (by {by})"))
    started = 0
    try:
        from shared.orchestration import outreach
        started = await outreach.start_outreach(await deals.get_deal(deal.deal_id))
    except Exception as exc:  # noqa: BLE001 - the operator can press "Contact next buyers"
        logger.warning("SupplyFirst: outreach not started on %s: %s", deal.deal_id, exc)
    logger.info("SupplyFirst: %s → deal %s (%s → %s), %d buyer(s), %d introduction(s)",
                sup.name, deal.deal_id, hs, market, len(buyers), started)
    return {"deal_id": deal.deal_id, "hs_code": hs, "market": market, "buyers": len(buyers),
            "introductions": started}
