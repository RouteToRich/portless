"""Demand signals create opportunities (todo 18.1).

The strongest demand there is — a buyer who wrote to us, a buy request or tender, a mission's importer
list, a past buyer due to reorder — used to have no way onto the radar. ``from_lead`` puts it there:

- an **open** opportunity for the product × market (ACTIVE / MARGINAL) gets the buyer **first** in its
  ``buyer_signals`` (a later scan keeps it; the same buyer is never added twice);
- otherwise a new opportunity is created, ``found_by="demand_signal"``, flags ``from_demand_signal`` +
  ``no_market_statistics``, the buyer as its only signal, the directory's suppliers of the product as
  candidates — no market gap and no price is invented; the profit block says what is missing.

The product is the lead's HS code, else the catalogue line whose name / synonym the lead's product
text names. A lead with neither, or without a market, is left in the directory only. Hooks:
``demand_leads.ingest`` (portal enquiries, buy requests, tenders, mission lists) and ``from_reorders``
(daily from the deal watcher with the demand-leads job).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_KIND_SIGNAL = {"tender": ("tender", 3), "buy_request": ("rfq", 3), "enquiry": ("rfq", 3), "reorder": ("historical", 2),
                "mission_list": ("web_search", 1)}


def product_for(lead: dict[str, Any], lines: list[Any]) -> str | None:
    """Pure: the 6-digit HS code a lead is about — its own code, else a catalogue line it names."""
    hs = "".join(ch for ch in str(lead.get("hs_code") or "") if ch.isdigit())
    if len(hs) >= 4:
        for line in lines:
            if str(getattr(line, "hs_code", ""))[:4] == hs[:4]:
                return str(line.hs_code)
        return hs[:6] if len(hs) >= 6 else None
    text = " " + " ".join(str(lead.get("product") or "").lower().split()) + " "
    for line in lines:
        for name in [getattr(line, "name", ""), *(getattr(line, "synonyms", None) or [])]:
            n = " ".join(str(name or "").lower().split())
            if n and f" {n}" in text:
                return str(line.hs_code)
    return None


def signal_for(lead: dict[str, Any], hs_code: str) -> Any:
    from .schemas import BuyerSignal
    signal_type, urgency = _KIND_SIGNAL.get(str(lead.get("kind") or ""), ("rfq", 2))
    return BuyerSignal(
        buyer_name=lead["buyer_name"], buyer_country=(lead.get("country") or "").upper(), company_id=lead.get("website"),
        signal_type=signal_type, source="apify_company_search" if lead.get("kind") == "mission_list" else "alibaba_rfq",
        urgency_tier=lead.get("urgency_tier") or urgency, email=lead.get("email"), phone=lead.get("phone"),
        website=lead.get("website"), annual_volume_mt=None, quantity=lead.get("quantity_mt"), quantity_unit="MT",
        fit_reason=f"{str(lead.get('kind') or 'lead').replace('_', ' ')} from {lead.get('source')}: {lead.get('product') or hs_code}",
        hs_codes=[hs_code], flags=list(lead.get("flags") or []) + ["demand_signal"],
    )


async def from_lead(lead: dict[str, Any]) -> dict[str, Any] | None:
    """Create or refresh the product × market opportunity with this buyer first. Never raises."""
    try:
        from shared import catalogue
        hs = product_for(lead, catalogue.active_lines())
        market = (lead.get("country") or "").upper()
        if not hs or not market or not lead.get("buyer_name"):
            return None
        from .repository import get_repository
        repo = await get_repository()
        signal = signal_for(lead, hs)
        open_ = [o for o in await repo.list_by_hs_market(hs, market) if o.status in ("ACTIVE", "MARGINAL")]
        if open_:
            opp = max(open_, key=lambda o: o.scanned_at)
            names = {(b.buyer_name or "").lower() for b in opp.buyer_signals}
            if signal.buyer_name.lower() in names:
                return {"opportunity_id": opp.opportunity_id, "action": "already_listed"}
            opp.buyer_signals = [signal, *opp.buyer_signals]
            opp.flags = list(dict.fromkeys([*opp.flags, "demand_signal"]))
            await repo.save(opp)
            return {"opportunity_id": opp.opportunity_id, "action": "buyer_added_first"}
        opp = await _new(hs, market, signal)
        await repo.save(opp)
        return {"opportunity_id": opp.opportunity_id, "action": "created"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("DemandOpportunities: lead not turned into an opportunity: %s", exc)
        return None


async def _new(hs: str, market: str, signal: Any) -> Any:
    from .config import settings
    from .schemas import EnrichedOpportunity, SellerCandidate
    from .service import _new_opportunity_id
    now = datetime.now(timezone.utc)
    sellers: list[SellerCandidate] = []
    try:
        from .directory import get_directory
        for rec in (await (await get_directory()).list("supplier", hs_code=hs))[:10]:
            if rec.do_not_contact or (rec.validation is not None and rec.validation.status == "failed"):
                continue
            sellers.append(SellerCandidate(
                candidate_id=rec.id, company_name=rec.name, location_city="", location_state="", hs_code=hs,
                estimated_fob_price_usd=rec.estimated_fob_price_usd, source="apify_company_search",
                email=rec.email, phone=rec.phone, website=rec.website, country=rec.country or "IN",
                price_unit="USD/MT" if rec.estimated_fob_price_usd else None, validation=rec.validation))
    except Exception as exc:  # noqa: BLE001
        logger.debug("DemandOpportunities: directory suppliers unavailable: %s", exc)
    opp = EnrichedOpportunity(
        opportunity_id=_new_opportunity_id(), hs_code=hs, market=market, status="ACTIVE", scanned_at=now,
        demand_index=50.0, price_gap_pct=0.0, source=signal.source, found_by="demand_signal",
        buyer_signals=[signal], seller_candidates=sellers, net_price_gap_pct=0.0, tariff_viable=True,
        arbitrage_rank=0.0, confidence_score=40.0, urgency_tier=signal.urgency_tier,
        expires_at=now + timedelta(hours=int(getattr(settings, "demand_signal_ttl_hours", 0) or 24 * 14)),
        flags=["from_demand_signal", "no_market_statistics"],
    )
    try:
        from . import pair_match, profit
        from .config import TradeMinConfig
        pair_match.attach(opp)
        await profit.attach(opp, TradeMinConfig())
    except Exception as exc:  # noqa: BLE001
        logger.debug("DemandOpportunities: profit not attached for %s: %s", opp.opportunity_id, exc)
    return opp


async def from_reorders(now: datetime | None = None) -> list[dict[str, Any]]:
    """Past buyers due to reorder (reorders.due) → their product × market opportunity, buyer first."""
    from shared.orchestration import reorders
    out = []
    for r in await reorders.due(now):
        if not r.get("buyer"):
            continue
        res = await from_lead({"source": "reorder", "kind": "reorder", "buyer_name": r["buyer"], "country": r.get("market"),
                               "hs_code": r.get("hs_code"), "product": None})
        if res:
            out.append({**res, "deal_id": r.get("deal_id")})
    return out
