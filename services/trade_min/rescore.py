"""Pre-deal rescore — fill what can be filled before an opportunity becomes a deal,
then score the buyer × seller pairs again.

The radar's pair score is built from whatever the scan found. Several of its gaps
can be closed *before* anyone is contacted, so the deal starts from the better
pair and the Match Engine scores the supplier with real inputs:

  1. counterparty check   sanctions + e-mail + GST / IEC (validation.py) for the pair's
                          buyer and seller, even outside the scan's top N
  2. supplier contact     the contact finder (company-contact-finder-ai) on the
                          supplier's own website → address, phone, e-mail, GSTIN when
                          the site states one
  3. plant → port         the address → state → usual export port and road km
                          (trade_costs.locate_supplier) → the Logistics factor
  4. supplier price       no price from the supplier → the price book's estimate
                          (India's export unit value + observed quotes) → an
                          *estimated* margin, at a lower price confidence

What only the counterparty can give — a firm FOB quote, the buyer's target price,
volumes, capacity — stays in each pair's ``still_needed``: that is the deal's job
(RFQ and buyer outreach). Nothing here invents a value; every filled field names
its source in ``changes``.

``POST /trade-min/opportunities/{id}/rescore``; ``deals.create_deal`` runs it first
when the opportunity was not rescored in the last ``RESCORE_REUSE_MINUTES``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import pair_match, validation
from .schemas import BuyerSignal, EnrichedOpportunity, SellerCandidate

logger = logging.getLogger(__name__)

RESCORE_REUSE_MINUTES = 60
_GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _norm(v: str | None) -> str:
    return " ".join((v or "").lower().replace(",", " ").split())


def _pair_key(p: pair_match.PairMatch) -> str:
    return f"{p.buyer_index}:{p.seller_id}"


def _pick_pairs(pairs: list[pair_match.PairMatch], limit: int) -> list[pair_match.PairMatch]:
    """The pairs worth paying to improve: recommended first, then the best others."""
    ranked = sorted(pairs, key=lambda p: (not p.recommended, -p.score))
    return [p for p in ranked if p.score > 0 or p.recommended][:limit] or ranked[:limit]


def recently_rescored(opp: EnrichedOpportunity, minutes: int = RESCORE_REUSE_MINUTES) -> bool:
    if opp.rescored_at is None:
        return False
    at = opp.rescored_at if opp.rescored_at.tzinfo else opp.rescored_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at < timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Fillers — each returns the list of human-readable changes it made
# ---------------------------------------------------------------------------

def _connector(name: str):
    try:
        from shared.connectors.registry import get_connector
        return get_connector(name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Rescore: connector %s unavailable: %s", name, exc)
        return None


async def _find_contacts(sellers: list[SellerCandidate], buyers: list[BuyerSignal]) -> list[str]:
    """Contact finder on the companies still missing an address / GSTIN / e-mail / phone."""
    want_s = [s for s in sellers if not (s.address and s.gstin and s.email and s.phone)]
    want_b = [b for b in buyers if not (b.email and b.phone)]
    if not want_s and not want_b:
        return []
    conn = _connector("apify_contact_finder")
    if conn is None:
        return []
    companies = ([{"name": s.company_name, "country": s.country or "IN", "website": s.website} for s in want_s]
                 + [{"name": b.buyer_name, "country": b.buyer_country, "website": b.website} for b in want_b])
    try:
        result = await conn.quote({"companies": companies})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rescore: contact finder failed: %s", exc)
        return []
    if result.get("stub"):
        return []
    rows = {_norm(c.get("input_name") or c.get("name")): c for c in result.get("contacts", [])
            if not c.get("not_found")}
    changes: list[str] = []
    for s in want_s:
        c = rows.get(_norm(s.company_name))
        if not c:
            continue
        if c.get("address") and not s.address:
            s.address = c["address"]
            changes.append(f"{s.company_name}: address found on its website")
        vat = str(c.get("vat_id") or "").strip().upper()
        if vat and not s.gstin and _GSTIN.match(vat):
            s.gstin = vat
            changes.append(f"{s.company_name}: GSTIN {vat} found on its website")
        if c.get("phone") and not s.phone:
            s.phone = c["phone"]
            changes.append(f"{s.company_name}: phone found")
        if c.get("email") and not s.email:
            s.email, s.email_status = c["email"], c.get("email_status")
            changes.append(f"{s.company_name}: e-mail found")
    for b in want_b:
        c = rows.get(_norm(b.buyer_name))
        if not c:
            continue
        if c.get("email") and not b.email:
            b.email, b.email_status = c["email"], c.get("email_status")
            changes.append(f"{b.buyer_name}: e-mail found")
        if c.get("phone") and not b.phone:
            b.phone = c["phone"]
            changes.append(f"{b.buyer_name}: phone found")
        if c.get("address") and not b.address:
            b.address = c["address"]
    return changes


def _route(sellers: list[SellerCandidate]) -> list[str]:
    from shared.orchestration.trade_costs import locate_supplier
    changes: list[str] = []
    for s in sellers:
        if s.supplier_to_port_km is not None or (s.country or "IN").upper() != "IN":
            continue
        place = s.address or ", ".join(x for x in (s.location_city, s.location_state) if x)
        route = locate_supplier(place)
        if route.km is not None:
            s.supplier_to_port_km = route.km
            s.location_state = s.location_state or (route.state or "")
            changes.append(f"{s.company_name}: ~{route.km:,.0f} km to {route.port} "
                           f"(from its {route.basis}, an estimate)")
    return changes


async def _price_estimates(opp: EnrichedOpportunity, sellers: list[SellerCandidate]) -> list[str]:
    need = [s for s in sellers if not s.estimated_fob_price_usd and not s.fob_estimate_usd_per_mt]
    if not need:
        return []
    try:
        from shared import pricebook
        est = await pricebook.estimate(opp.hs_code, "supplier", market=opp.market)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rescore: price book estimate failed: %s", exc)
        return []
    if not est.median:
        return []
    basis = (f"price book, {est.n_observations} observed price(s)" if est.n_observations
             else "India's export unit value (UN Comtrade)")
    for s in need:
        s.fob_estimate_usd_per_mt = est.median
        s.fob_estimate_basis = basis
    return [f"Supplier price estimated at ${est.median:,.0f}/MT for {len(need)} supplier(s) ({basis}); "
            f"the RFQ replaces it with a real quote"]


async def _store_checks(buyers: list[BuyerSignal], sellers: list[SellerCandidate]) -> None:
    """Keep the new check results on the directory records (reused by later scans)."""
    try:
        from . import directory
        store = await directory.get_directory()
        for role, items, name_of in (("buyer", buyers, lambda x: x.buyer_name),
                                     ("supplier", sellers, lambda x: x.company_name)):
            for item in items:
                if item.validation is None:
                    continue
                rec = await store.get(directory.record_key(role, name_of(item), item.website))
                if rec is not None:
                    await store.set_validation(rec.model_copy(update={"validation": item.validation}))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Rescore: directory not updated: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def rescore(opportunity_id: str, *, pairs: int = 3) -> EnrichedOpportunity | None:
    """Fill the gaps of the opportunity's best pairs, rescore, save. None when not found."""
    from .repository import get_repository
    repo = await get_repository()
    opp = await repo.get(opportunity_id)
    if opp is None:
        return None

    before = {_pair_key(p): p for p in pair_match.match_opportunity(opp)}
    chosen = _pick_pairs(list(before.values()), pairs)
    buyers = list({p.buyer_index: opp.buyer_signals[p.buyer_index] for p in chosen
                   if p.buyer_index < len(opp.buyer_signals)}.values())
    wanted = {p.seller_id for p in chosen}
    sellers = [s for s in opp.seller_candidates if s.candidate_id in wanted]

    changes: list[str] = []
    changes += await _find_contacts(sellers, buyers)
    changes += _route(sellers)
    changes += await _price_estimates(opp, sellers)
    unchecked = [x for x in [*buyers, *sellers] if x.validation is None]
    counts = await validation.validate_scan(buyers, sellers, top_n=max(len(buyers), len(sellers)))
    newly = [x for x in unchecked if x.validation is not None]
    for x in newly:
        name = getattr(x, "buyer_name", None) or getattr(x, "company_name", "")
        changes.append(f"{name}: counterparty check {x.validation.status}")
    await _store_checks(buyers, sellers)

    after_pairs = pair_match.match_opportunity(opp)
    after = {_pair_key(p): p for p in after_pairs}
    rows: list[dict[str, Any]] = []
    for p in chosen:
        a = after.get(_pair_key(p)) or p
        rows.append({
            "buyer": p.buyer_name, "seller": p.seller_name,
            "before": {"score": p.score, "grade": p.grade, "confidence": p.confidence},
            "after": {"score": a.score, "grade": a.grade, "confidence": a.confidence},
            "still_needed": a.improve,
            "blockers": a.blockers,
        })
    now = datetime.now(timezone.utc)
    opp.rescored_at = now
    opp.rescore = {
        "at": now.isoformat(), "pairs": rows, "changes": changes,
        "checks": counts,
        "note": ("Firm supplier price, buyer target price, volumes and capacity come from the "
                 "counterparties: the deal's RFQ and buyer outreach ask for them."),
    }
    opp.pair_matches = after_pairs
    opp.best_match_score = max((x.score for x in after_pairs), default=None)
    try:
        await repo.save(opp)
    except Exception as exc:  # noqa: BLE001
        logger.error("Rescore: could not save %s: %s", opportunity_id, exc)
    return opp
