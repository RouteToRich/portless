"""Expected profit per opportunity — what the radar ranks on.

PortLess earns ``buyer price − supplier price − landed costs``, so a corridor is
worth what that margin times a likely lot times the chance of closing is worth:

    landed/MT        = supplier FOB + freight + inspection/MT + insurance + escrow fee
                       (shared.orchestration.landed_cost, the same bridge deals use)
    margin/MT        = buyer price − landed/MT
    expected profit  = margin/MT × lot MT × P(close)

Every input carries its basis, best first:

* buyer price    — a buyer's own price (signal) → the price book (quotes, targets,
                   counters, agreed prices) → what the market pays for INDIA's goods
                   → the market's all-origin average.
* supplier price — a supplier listing / quote → the price book → Agmarknet mandi
                   price + export mark-up → India's average export price (Comtrade,
                   every grade: the weakest).
* lot            — the catalogue's default quantity → one container.
* P(close)       — floor + span × best buyer ⇄ seller pair score, scaled by how
                   many buyers passed the counterparty check, halved for a suspect
                   gap or a margin under the floor, then corrected by what really
                   happened: the outcome calibration table once enough matches
                   resolved, and this corridor's own signed / failed record.

Never raises: the radar must load even when a price source is down.
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Any

from .config import DEFAULT_CONFIG, TradeMinConfig
from .schemas import EnrichedOpportunity, OpportunityProfit

logger = logging.getLogger(__name__)

# Bump when the computation changes: listed opportunities are recomputed once.
PROFIT_VERSION = 3

PRICE_CHECK_NEEDED = "price_check_needed"
TARIFF_UNVERIFIED = "tariff_unverified"
NO_SUPPLIER_PRICE = "no_supplier_price"
BELOW_MARGIN_FLOOR = "below_margin_floor"
NEGATIVE_MARGIN = "negative_margin"
INDIA_CHEAPER = "india_cheaper_than_leader"
EXPORT_BLOCKED = "export_blocked"
EXPORT_REVIEW = "export_review"
NEWS_AVOID = "market_news_avoid"
NEWS_CAUTION = "market_news_caution"
STALE_DATA = "stale_market_data"

_OUTCOME_TTL = 300.0
_outcome_cache: tuple[float, list[Any]] | None = None


def reset_for_tests() -> None:
    global _outcome_cache
    _outcome_cache = None


def _median(values: list[float]) -> float | None:
    vals = [float(v) for v in values if v is not None and float(v) > 0]
    return round(statistics.median(vals), 2) if vals else None


async def _outcomes() -> list[Any]:
    global _outcome_cache
    if _outcome_cache and time.monotonic() - _outcome_cache[0] < _OUTCOME_TTL:
        return _outcome_cache[1]
    try:
        from shared.orchestration import outcomes
        rows = await outcomes.all_outcomes()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Profit: outcomes unavailable: %s", exc)
        rows = []
    _outcome_cache = (time.monotonic(), rows)
    return rows


async def _price_book(hs_code: str, side: str, market: str) -> float | None:
    """The price book's median — only when it rests on real observations, not just the prior."""
    try:
        from shared import pricebook
        est = await pricebook.estimate(hs_code, side, market=market, priors={})  # type: ignore[arg-type]
        if est.n_observations > 0 and est.median:
            return round(float(est.median), 2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Profit: price book unavailable for %s: %s", hs_code, exc)
    return None


async def _mandi(hs_code: str) -> dict[str, Any]:
    try:
        from .service import _safe_quote
        return await _safe_quote("mandi_prices", {"hs_code": hs_code}) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Profit: mandi prices unavailable for %s: %s", hs_code, exc)
        return {}


async def buyer_price(opp: EnrichedOpportunity) -> tuple[float | None, str | None]:
    quoted = _median([b.unit_price_usd for b in opp.buyer_signals
                      if b.unit_price_usd and (b.price_unit or "USD/MT") == "USD/MT"
                      and "role_suspect" not in b.flags])
    if quoted:
        return quoted, "buyer_quote"
    book = await _price_book(opp.hs_code, "buyer", opp.market)
    if book:
        return book, "price_book"
    if opp.india_to_market_usd_per_mt:
        return round(opp.india_to_market_usd_per_mt, 2), "india_to_market"
    if opp.price_benchmark_usd_per_mt:
        return round(opp.price_benchmark_usd_per_mt, 2), "market_average"
    return None, None


async def supplier_inputs(opp: EnrichedOpportunity) -> dict[str, Any]:
    """The supplier price with its basis, plus its spread and the mandi summary when used."""
    usable = [s for s in opp.seller_candidates if "product_mismatch" not in s.flags]
    prices = [s.estimated_fob_price_usd for s in usable if s.estimated_fob_price_usd]
    listed = _median(prices)
    if listed:
        return {"price": listed, "basis": "supplier_quote", "note": f"median of {len(prices)} supplier price(s)",
                "low": round(min(prices), 2), "high": round(max(prices), 2)}
    book = await _price_book_estimate(opp.hs_code, "supplier", opp.market)
    if book:
        return {"price": book["median"], "basis": "price_book", "note": "PortLess price book (quotes and agreed prices)",
                "low": book.get("p25"), "high": book.get("p75")}
    mandi = await _mandi(opp.hs_code)
    if mandi.get("fob_estimate_usd_per_mt"):
        note = (f"{mandi.get('commodity')} median INR {mandi.get('modal_inr_per_quintal'):,.0f}/quintal "
                f"in {mandi.get('markets')} mandis (Agmarknet {mandi.get('as_of') or ''}) "
                f"+ {mandi.get('markup_pct'):.0f}% to FOB at USD/INR {mandi.get('usd_inr')} "
                f"({mandi.get('usd_inr_source', 'fallback')})")
        return {"price": round(float(mandi["fob_estimate_usd_per_mt"]), 2), "basis": "mandi", "note": note,
                "low": mandi.get("fob_low_usd_per_mt"), "high": mandi.get("fob_high_usd_per_mt"), "mandi": mandi}
    rescored = _median([s.fob_estimate_usd_per_mt for s in usable if s.fob_estimate_usd_per_mt])
    if rescored:
        return {"price": rescored, "basis": "price_book", "note": "price-book estimate from the pre-deal rescore"}
    india = opp.india_world_fob_usd_per_mt
    if not india and opp.price_benchmark_usd_per_mt and opp.price_gap_pct is not None and opp.price_gap_pct > -90:
        # Scanned before the export price was stored: the gross gap was measured
        # against it, so it is benchmark / (1 + gap) — as the price book's prior.
        india = opp.price_benchmark_usd_per_mt / (1 + opp.price_gap_pct / 100.0)
    if india:
        # Every grade averaged together: assume ±15 % until a supplier quotes.
        return {"price": round(india, 2), "basis": "india_export_average",
                "note": "India's average export price to the world (every grade) — the weakest basis",
                "low": round(india * 0.85, 2), "high": round(india * 1.15, 2)}
    return {"price": None, "basis": None, "note": None}


async def supplier_price(opp: EnrichedOpportunity) -> tuple[float | None, str | None, str | None]:
    i = await supplier_inputs(opp)
    return i["price"], i["basis"], i["note"]


async def _price_book_estimate(hs_code: str, side: str, market: str) -> dict[str, Any] | None:
    try:
        from shared import pricebook
        est = await pricebook.estimate(hs_code, side, market=market, priors={})  # type: ignore[arg-type]
        if est.n_observations > 0 and est.median:
            return {"median": round(float(est.median), 2), "p25": est.p25, "p75": est.p75}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Profit: price book unavailable for %s: %s", hs_code, exc)
    return None


def _container(opp: EnrichedOpportunity, config: TradeMinConfig) -> tuple[float, str]:
    try:
        from shared import catalogue
        mt, kind = catalogue.container_for(opp.hs_code)
    except Exception:  # noqa: BLE001
        mt, kind = None, None
    return (float(mt) if mt and mt > 0 else config.container_mt), (kind or "dry").lower()


def _lot(opp: EnrichedOpportunity, config: TradeMinConfig, buyer_price: float | None = None,
         container_mt: float | None = None) -> tuple[float, str]:
    try:
        from shared import catalogue
        qty = catalogue.default_quantity_for(opp.hs_code)
        if qty and qty > 0:
            return float(qty), "catalogue default quantity"
    except Exception:  # noqa: BLE001
        pass
    from .volume import stated_order_mt
    stated, stated_basis = stated_order_mt(list(getattr(opp, "buyer_signals", None) or [])[:10])
    lot = stated or container_mt or config.default_lot_mt
    basis = stated_basis or "one container"
    if buyer_price and buyer_price * lot > config.max_lot_value_usd:
        lot = round(config.max_lot_value_usd / buyer_price, 3)
        basis = f"first order capped at ${config.max_lot_value_usd:,.0f}"
    return lot, basis


def _corridor_factor(rows: list[Any], hs_code: str, market: str, config: TradeMinConfig) -> tuple[float, str | None]:
    """This corridor's signed rate vs everyone's, shrunk; 1.0 until enough resolved."""
    resolved = [o for o in rows if getattr(o, "kind", "match") == "match" and getattr(o, "result", None) in ("signed", "failed")]
    mine = [o for o in resolved if o.hs_code == hs_code and (o.market or "").upper() == market.upper()]
    if len(mine) < config.corridor_learning_min or not resolved:
        return 1.0, None
    signed_all = sum(1 for o in resolved if o.result == "signed")
    signed = sum(1 for o in mine if o.result == "signed")
    rate = (signed + 1) / (len(mine) + 2)
    base = (signed_all + 1) / (len(resolved) + 2)
    factor = max(0.5, min(1.5, rate / base)) if base > 0 else 1.0
    return round(factor, 3), f"corridor history {signed}/{len(mine)} signed (×{factor:.2f})"


def _annual(opp: EnrichedOpportunity, margin_per_mt: float, lot: float, p: float,
            config: TradeMinConfig) -> tuple[float | None, str | None]:
    """Margin per year if PortLess wins a small share of the market's yearly imports,
    bounded by one lot a month."""
    price = opp.market_avg_usd_per_mt or opp.price_benchmark_usd_per_mt
    if not opp.import_value_usd or not price or price <= 0 or margin_per_mt <= 0:
        return None, None
    market_mt = opp.import_value_usd / price
    tonnes = min(market_mt * config.reachable_share_pct / 100.0, lot * 12)
    if tonnes <= 0:
        return None, None
    return (round(margin_per_mt * tonnes * p, 2),
            f"{tonnes:,.0f} MT/yr = {config.reachable_share_pct:g}% of the market's ~{market_mt:,.0f} MT imports "
            f"(at most one lot a month) × margin × chance")


def _missing(opp: EnrichedOpportunity, out: OpportunityProfit, flags: list[str]) -> list[dict[str, str]]:
    """What the estimate lacks and where to get it."""
    tips: list[dict[str, str]] = []
    if out.supplier_price_basis not in ("supplier_quote", "price_book"):
        tips.append({"what": "No supplier price",
                     "how": "Set DATA_GOV_IN_API_KEY for mandi prices, Rescore for a price-book estimate, "
                            "or start the prospect: the RFQ asks suppliers for a firm FOB",
                     "action": "rescore"})
    if out.buyer_price_basis in ("india_to_market", "market_average"):
        tips.append({"what": "No buyer price",
                     "how": "Comtrade average only — the buyer introduction asks each buyer for its target price",
                     "action": "start"})
    if TARIFF_UNVERIFIED in flags:
        tips.append({"what": "Duty unverified",
                     "how": "World Bank WITS has no line for this product and market: check Access2Markets (EU) "
                            "or MacMap (MACMAP_API_KEY)", "action": "settings"})
    if PRICE_CHECK_NEEDED in flags:
        tips.append({"what": "Price check needed",
                     "how": "The gap is implausible: compare with a supplier quote before trusting the margin",
                     "action": "rescore"})
    if out.freight_basis and out.freight_basis.startswith("region_estimate"):
        tips.append({"what": "Freight is an estimate",
                     "how": "Set COGOPORT_API_KEY / FREIGHTOS_API_KEY for lane quotes, or enter a quote you received "
                            "(PUT /trade-min/freight-lanes)", "action": "settings"})
    if out.incentive_usd_per_mt is None:
        tips.append({"what": "Export incentives not counted",
                     "how": "Enter the RoDTEP / drawback rate for this HS code from the DGFT / CBIC schedule and confirm "
                            "eligibility with the CA (PUT /trade-min/incentives/{hs})", "action": "settings"})
    if (opp.export_policy or {}).get("severity") == "review":
        tips.append({"what": "Export policy not confirmed",
                     "how": "Confirm the HS code against ITC-HS Schedule 2 once (Confirm export policy)",
                     "action": "confirm_hs"})
    if opp.data_period and opp.data_period[:4].isdigit() and int(opp.data_period[:4]) < _this_year() - 2:
        tips.append({"what": f"Prices from {opp.data_period}",
                     "how": "Comtrade has no newer year for this market: rely on supplier and buyer quotes",
                     "action": "start"})
    return tips


def _this_year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year


async def compute(opp: EnrichedOpportunity, config: TradeMinConfig = DEFAULT_CONFIG) -> tuple[OpportunityProfit, list[str]]:
    from shared.orchestration import landed_cost as lc

    from . import accuracy, freight_lanes, incentives, mandi_history, ranking
    if not opp.gap_suspect and ranking.is_suspect_gap(opp.price_gap_pct or 0.0, config):
        # Scanned before the check existed: flag it now, the same way a scan would.
        opp.gap_suspect = True
        opp.gap_note = (f"Gross gap {opp.price_gap_pct:.0f}% is above {config.suspect_gap_pct:.0f}%: "
                        "most likely a different grade or origin mix, not a real arbitrage. "
                        "Check the price with a supplier quote before trusting it.")
    flags: list[str] = []
    if opp.gap_suspect:
        flags.append(PRICE_CHECK_NEEDED)
    if opp.tariff_data is not None and not opp.tariff_data.verified:
        flags.append(TARIFF_UNVERIFIED)
    if opp.india_price_advantage_pct is not None and opp.india_price_advantage_pct > 0:
        flags.append(INDIA_CHEAPER)
    ep = opp.export_policy or {}
    if ep.get("severity") == "block":
        flags.append(EXPORT_BLOCKED)
    elif ep.get("severity") == "review":
        flags.append(EXPORT_REVIEW)
    verdict = (opp.market_check or {}).get("verdict")
    if verdict == "avoid":
        flags.append(NEWS_AVOID)
    elif verdict == "caution":
        flags.append(NEWS_CAUTION)
    if "stale_period" in (opp.market_data_flags or []):
        flags.append(STALE_DATA)

    bp, bbasis = await buyer_price(opp)
    sup = await supplier_inputs(opp)
    sp, sbasis, snote = sup["price"], sup["basis"], sup["note"]
    if sbasis not in ("supplier_quote", "price_book"):
        flags.append(NO_SUPPLIER_PRICE)
    container_mt, container_type = _container(opp, config)
    lot, lot_basis = _lot(opp, config, bp, container_mt)
    floor = lc.default_margin_floor_pct()
    out = OpportunityProfit(
        buyer_price_usd_per_mt=bp, buyer_price_basis=bbasis,
        supplier_price_usd_per_mt=sp, supplier_price_basis=sbasis, supplier_price_note=snote,
        supplier_price_low_usd_per_mt=sup.get("low"), supplier_price_high_usd_per_mt=sup.get("high"),
        container_mt=container_mt, container_type=container_type, lot_mt=lot, lot_basis=lot_basis,
        estimated=not (bbasis == "buyer_quote" and sbasis == "supplier_quote"),
        margin_floor_pct=floor, version=PROFIT_VERSION,
    )
    if sup.get("mandi"):
        m = sup["mandi"]
        out.usd_inr, out.usd_inr_source = m.get("usd_inr"), m.get("usd_inr_source")
        await mandi_history.record(m)
        out.mandi_trend = await mandi_history.trend(m.get("commodity"))

    # Chance of closing — needed even without prices, so the radar can still sort.
    best = opp.best_match_score
    p = config.p_close_floor + config.p_close_span * ((best if best is not None else 25.0) / 100.0)
    basis = [f"best pair score {best:.0f}" if best is not None else "no pair score yet (assumed 25)"]
    from shared import country_profiles
    tier = country_profiles.tier(opp.market)
    tier_factor = {"preferred": 1.1, "standard": 1.0, "review": 0.7, "blocked": 0.0}[tier]
    if tier_factor != 1.0:
        p *= tier_factor
        basis.append(f"{tier} market (×{tier_factor:g})")
    top = opp.buyer_signals[:5]
    if top:
        verified = sum(1 for b in top if b.validation is not None and b.validation.status == "verified")
        share = verified / len(top)
        p *= 0.7 + 0.3 * share
        basis.append(f"{verified}/{len(top)} top buyers verified")
    if opp.gap_suspect:
        p *= 0.5
        basis.append("price gap implausible (×0.5)")
    if verdict == "avoid":
        p *= 0.5
        basis.append("market news says avoid (×0.5)")
    elif verdict == "caution":
        p *= 0.85
        basis.append("market news says caution (×0.85)")

    if bp and sp:
        freight, out.freight_basis = await freight_lanes.freight_per_mt(opp.market, container_type)
        incentive, out.incentive_basis = incentives.income_per_mt(await incentives.entry_for(opp.hs_code), sp)
        out.incentive_usd_per_mt = incentive

        def margin_at(buyer: float, supplier: float) -> float:
            cost = lc.landed(supplier, opp.market, lot, incoterm="CIF", margin_floor_pct=0.0,
                             freight_usd_per_mt=freight).cost_usd_per_mt
            inc = incentives.income_per_mt_value(incentive, supplier, sp)
            return buyer - cost + inc

        landed = lc.landed(sp, opp.market, lot, incoterm="CIF", margin_floor_pct=0.0, freight_usd_per_mt=freight)
        margin = round(margin_at(bp, sp), 2)
        factor, why = await accuracy.factor_for(opp.hs_code)
        if factor is not None and margin > 0:
            margin = round(margin * factor, 2)
            out.correction_factor, out.correction_basis = factor, why
        pct = round(margin / bp * 100.0, 2) if bp else None
        out.landed_cost_usd_per_mt = landed.cost_usd_per_mt
        out.freight_usd_per_mt = landed.freight_usd_per_mt
        out.margin_usd_per_mt = margin
        out.margin_pct = pct
        out.margin_per_container_usd = round(margin * container_mt, 2)
        out.meets_floor = pct is not None and pct >= floor
        if margin <= 0:
            flags.append(NEGATIVE_MARGIN)
        elif pct is not None and pct < floor:
            flags.append(BELOW_MARGIN_FLOOR)
            p *= 0.5
            basis.append(f"margin under the {floor:.0f}% floor (×0.5)")

    if EXPORT_BLOCKED in flags:
        p = 0.0
        basis.append("export policy forbids it")
    rows = await _outcomes()
    if rows and p > 0:
        try:
            from shared.orchestration import outcomes
            table = outcomes.calibration_table(rows)
            if table.get("active"):
                p_cal = outcomes.calibrate(p, table)
                basis.append(f"calibrated on {table['n_resolved']} resolved matches")
                p = p_cal
        except Exception:  # noqa: BLE001
            pass
        cf, cwhy = _corridor_factor(rows, opp.hs_code, opp.market, config)
        if cwhy:
            p *= cf
            basis.append(cwhy)
    p = 0.0 if EXPORT_BLOCKED in flags else round(max(0.01, min(0.95, p)), 3)
    out.p_close = p
    out.p_close_basis = basis
    if out.margin_usd_per_mt is not None and bp and sp:
        out.expected_profit_usd = round(out.margin_usd_per_mt * lot * p, 2)
        # Range: the supplier price spread (best case low price, worst case high price).
        lo_sp = sup.get("low") or sp
        hi_sp = sup.get("high") or sp
        cf = out.correction_factor or 1.0
        hi_m = margin_at(bp, lo_sp) * (cf if margin_at(bp, lo_sp) > 0 else 1.0)
        lo_m = margin_at(bp, hi_sp) * (cf if margin_at(bp, hi_sp) > 0 else 1.0)
        out.expected_profit_low_usd = round(lo_m * lot * p, 2)
        out.expected_profit_high_usd = round(hi_m * lot * p, 2)
        out.range_basis = (f"supplier price {lo_sp:,.0f}–{hi_sp:,.0f} USD/MT"
                           if (lo_sp, hi_sp) != (sp, sp) else "single price, no spread known")
        out.annual_potential_usd, out.annual_basis = _annual(opp, out.margin_usd_per_mt, lot, p, config)
    out.missing = _missing(opp, out, flags)
    return out, flags


async def attach(opp: EnrichedOpportunity, config: TradeMinConfig = DEFAULT_CONFIG) -> EnrichedOpportunity:
    """Fill ``opp.profit`` and ``opp.flags`` in place (never raises)."""
    try:
        opp.profit, opp.flags = await compute(opp, config)
    except Exception as exc:  # noqa: BLE001 - profit must never break the radar
        logger.warning("Profit: could not compute %s: %s", opp.opportunity_id, exc)
    return opp


def sort_key(opp: EnrichedOpportunity) -> tuple:
    """Radar order: expected profit first (unknown last), then the market score."""
    p = getattr(opp, "profit", None)
    ep = p.expected_profit_usd if p is not None else None
    return (ep is not None, ep if ep is not None else float("-inf"), getattr(opp, "arbitrage_rank", 0.0) or 0.0)
