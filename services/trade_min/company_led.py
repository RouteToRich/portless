"""Company-led opportunities (todo 18.1): opportunities from the directory, not only from
market statistics.

Per catalogue product × buyer country: where at least ``COMPANY_LED_MIN_BUYERS`` (3) buyers
are checked (``verified``) and contactable, and at least ``COMPANY_LED_MIN_SUPPLIERS`` (2)
Indian suppliers of the product are contactable, an opportunity is created even without a
Comtrade gap. It is priced from the price book (buyer side: observed targets / counters, else
the public prior; supplier side: quotes, else the mandi / Comtrade prior), carries the directory
buyers and suppliers as its candidates, runs the same pair matching, start check and profit as a
scanned one, and is marked ``found_by="directory"``. A corridor that already has a listed
opportunity is left alone.

``GET /trade-min/opportunities/company-led`` previews the groups; ``POST`` creates them (staff).
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from . import directory
from .markets import contactable

logger = logging.getLogger(__name__)


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _hs6(code: Any) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


async def groups() -> list[dict[str, Any]]:
    """Product × market groups the directory supports (pure read)."""
    from shared import catalogue
    store = await directory.get_directory()
    buyers = await store.list("buyer", limit=100_000)
    suppliers = await store.list("supplier", limit=100_000)
    min_b, min_s = _int("COMPANY_LED_MIN_BUYERS", 3), _int("COMPANY_LED_MIN_SUPPLIERS", 2)
    out = []
    for line in catalogue.active_lines():
        code = _hs6(line.hs_code)
        sups = [s for s in suppliers if contactable(s) and (s.country or "IN").upper() == "IN"
                and any(_hs6(h)[:len(code)] == code[:len(_hs6(h))] for h in s.hs_codes if h)]
        if len(sups) < min_s:
            continue
        by_country: dict[str, list[Any]] = {}
        for b in buyers:
            country = (b.country or "").upper()
            if not country or country == "IN" or not contactable(b):
                continue
            if b.validation is None or b.validation.status != "verified":
                continue
            if any(_hs6(h)[:len(code)] == code[:len(_hs6(h))] for h in b.hs_codes if h):
                by_country.setdefault(country, []).append(b)
        for country, bs in by_country.items():
            if len(bs) >= min_b:
                out.append({"hs_code": line.hs_code, "product": line.name, "market": country,
                            "buyers": bs, "suppliers": sups})
    return out


def _buyer_signal(rec: Any, hs: str) -> Any:
    from .schemas import BuyerSignal
    return BuyerSignal(buyer_name=rec.name, buyer_country=(rec.country or "").upper(), company_id=rec.website,
                       signal_type="web_search", source="apify_company_search", urgency_tier=1,
                       website=rec.website, email=rec.email, phone=rec.phone, address=rec.address,
                       fit_score=rec.fit_score, fit_reason=rec.fit_reason, hs_codes=[hs],
                       validation=rec.validation, flags=list(rec.flags or []),
                       annual_volume_mt=rec.annual_volume_mt, shipments_12m=rec.shipments_12m)


def _seller(rec: Any, hs: str) -> Any:
    from .schemas import SellerCandidate
    return SellerCandidate(candidate_id=rec.id, company_name=rec.name, location_city="", location_state="",
                           hs_code=hs, estimated_fob_price_usd=rec.estimated_fob_price_usd,
                           source="apify_company_search", email=rec.email, phone=rec.phone, website=rec.website,
                           gstin=rec.gstin, iec=rec.iec, fit_score=rec.fit_score, country="IN",
                           validation=rec.validation, address=rec.address, flags=list(rec.flags or []))


async def create(now: datetime | None = None) -> dict[str, Any]:
    from shared import pricebook
    from . import pair_match, profit, start_check
    from .repository import get_repository
    from .schemas import EnrichedOpportunity
    now = now or datetime.now(timezone.utc)
    repo = await get_repository()
    created, skipped = [], []
    for g in await groups():
        hs, market = g["hs_code"], g["market"]
        existing = await repo.list_by_hs_market(hs, market)
        if any(getattr(o, "status", "ACTIVE") in ("ACTIVE", "MARGINAL") for o in existing):
            skipped.append({"hs_code": hs, "market": market, "reason": "a listed opportunity exists"})
            continue
        est = await pricebook.estimate(hs, "buyer", market=market)
        buyers = [_buyer_signal(b, hs) for b in g["buyers"][:15]]
        sellers = [_seller(s, hs) for s in g["suppliers"][:15]]
        verified = sum(1 for b in g["buyers"] if b.validation and b.validation.status == "verified")
        rank = round(min(100.0, 30.0 + 8.0 * verified + 5.0 * len(sellers)), 1)
        oid = "OPP-DIR-" + hashlib.sha1(f"{hs}:{market}".encode()).hexdigest()[:10].upper()
        opp = EnrichedOpportunity(
            opportunity_id=oid, hs_code=hs, market=market, status="MARGINAL", scanned_at=now,
            demand_index=0.0, price_gap_pct=0.0, net_price_gap_pct=0.0, source="apify_company_search",
            arbitrage_rank=rank, confidence_score=0.4,
            confidence_basis=[f"{verified} checked buyers and {len(sellers)} reachable suppliers in the directory; "
                              "no market statistics measured a gap"],
            price_benchmark_usd_per_mt=est.median, price_benchmark_source="price_book" if est.median else None,
            found_by="directory", buyer_signals=buyers, seller_candidates=sellers, urgency_tier=1,
            expires_at=now + timedelta(days=14),
        )
        pair_match.attach(opp)
        try:
            await start_check.annotate(opp, refresh_news=False)
            await profit.attach(opp)
        except Exception as exc:  # noqa: BLE001 - the opportunity is still worth listing
            logger.debug("CompanyLed: checks skipped for %s: %s", oid, exc)
        await repo.save(opp)
        created.append({"opportunity_id": oid, "hs_code": hs, "market": market, "buyers": len(buyers),
                        "suppliers": len(sellers)})
    return {"created": created, "skipped": skipped}


async def preview() -> list[dict[str, Any]]:
    return [{"hs_code": g["hs_code"], "product": g["product"], "market": g["market"],
             "buyers": [b.name for b in g["buyers"][:10]], "suppliers": [s.name for s in g["suppliers"][:10]]}
            for g in await groups()]
