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
PROFIT_VERSION = 2

PRICE_CHECK_NEEDED = "price_check_needed"
TARIFF_UNVERIFIED = "tariff_unverified"
NO_SUPPLIER_PRICE = "no_supplier_price"
BELOW_MARGIN_FLOOR = "below_margin_floor"
NEGATIVE_MARGIN = "negative_margin"
INDIA_CHEAPER = "india_cheaper_than_leader"

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


async def supplier_price(opp: EnrichedOpportunity) -> tuple[float | None, str | None, str | None]:
    usable = [s for s in opp.seller_candidates if "product_mismatch" not in s.flags]
    listed = _median([s.estimated_fob_price_usd for s in usable if s.estimated_fob_price_usd])
    if listed:
        return listed, "supplier_quote", f"median of {sum(1 for s in usable if s.estimated_fob_price_usd)} supplier price(s)"
    book = await _price_book(opp.hs_code, "supplier", opp.market)
    if book:
        return book, "price_book", "PortLess price book (quotes and agreed prices)"
    mandi = await _mandi(opp.hs_code)
    if mandi.get("fob_estimate_usd_per_mt"):
        note = (f"{mandi.get('commodity')} median INR {mandi.get('modal_inr_per_quintal'):,.0f}/quintal "
                f"in {mandi.get('markets')} mandis (Agmarknet {mandi.get('as_of') or ''}) "
                f"+ {mandi.get('markup_pct'):.0f}% to FOB")
        return round(float(mandi["fob_estimate_usd_per_mt"]), 2), "mandi", note
    rescored = _median([s.fob_estimate_usd_per_mt for s in usable if s.fob_estimate_usd_per_mt])
    if rescored:
        return rescored, "price_book", "price-book estimate from the pre-deal rescore"
    india = opp.india_world_fob_usd_per_mt
    if not india and opp.price_benchmark_usd_per_mt and opp.price_gap_pct is not None and opp.price_gap_pct > -90:
        # Scanned before the export price was stored: the gross gap was measured
        # against it, so it is benchmark / (1 + gap) — as the price book's prior.
        india = opp.price_benchmark_usd_per_mt / (1 + opp.price_gap_pct / 100.0)
    if india:
        return (round(india, 2), "india_export_average",
                "India's average export price to the world (every grade) — the weakest basis")
    return None, None, None


def _lot(opp: EnrichedOpportunity, config: TradeMinConfig, buyer_price: float | None = None) -> tuple[float, str]:
    try:
        from shared import catalogue
        qty = catalogue.default_quantity_for(opp.hs_code)
        if qty and qty > 0:
            return float(qty), "catalogue default quantity"
    except Exception:  # noqa: BLE001
        pass
    lot, basis = config.default_lot_mt, "one container"
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


async def compute(opp: EnrichedOpportunity, config: TradeMinConfig = DEFAULT_CONFIG) -> tuple[OpportunityProfit, list[str]]:
    from shared.orchestration import landed_cost as lc

    from . import ranking
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

    bp, bbasis = await buyer_price(opp)
    sp, sbasis, snote = await supplier_price(opp)
    if sbasis not in ("supplier_quote", "price_book"):
        flags.append(NO_SUPPLIER_PRICE)
    lot, lot_basis = _lot(opp, config, bp)
    floor = lc.default_margin_floor_pct()
    out = OpportunityProfit(
        buyer_price_usd_per_mt=bp, buyer_price_basis=bbasis,
        supplier_price_usd_per_mt=sp, supplier_price_basis=sbasis, supplier_price_note=snote,
        container_mt=config.container_mt, lot_mt=lot, lot_basis=lot_basis,
        estimated=not (bbasis == "buyer_quote" and sbasis == "supplier_quote"),
        margin_floor_pct=floor, version=PROFIT_VERSION,
    )

    # Chance of closing — needed even without prices, so the radar can still sort.
    best = opp.best_match_score
    p = config.p_close_floor + config.p_close_span * ((best if best is not None else 25.0) / 100.0)
    basis = [f"best pair score {best:.0f}" if best is not None else "no pair score yet (assumed 25)"]
    top = opp.buyer_signals[:5]
    if top:
        verified = sum(1 for b in top if b.validation is not None and b.validation.status == "verified")
        share = verified / len(top)
        p *= 0.7 + 0.3 * share
        basis.append(f"{verified}/{len(top)} top buyers verified")
    if opp.gap_suspect:
        p *= 0.5
        basis.append("price gap implausible (×0.5)")

    if bp and sp:
        landed = lc.landed(sp, opp.market, lot, incoterm="CIF", margin_floor_pct=0.0)
        margin = round(bp - landed.cost_usd_per_mt, 2)
        pct = round(margin / bp * 100.0, 2) if bp else None
        out.landed_cost_usd_per_mt = landed.cost_usd_per_mt
        out.freight_usd_per_mt = landed.freight_usd_per_mt
        out.margin_usd_per_mt = margin
        out.margin_pct = pct
        out.margin_per_container_usd = round(margin * config.container_mt, 2)
        out.meets_floor = pct is not None and pct >= floor
        if margin <= 0:
            flags.append(NEGATIVE_MARGIN)
        elif pct is not None and pct < floor:
            flags.append(BELOW_MARGIN_FLOOR)
            p *= 0.5
            basis.append(f"margin under the {floor:.0f}% floor (×0.5)")

    rows = await _outcomes()
    if rows:
        try:
            from shared.orchestration import outcomes
            table = outcomes.calibration_table(rows)
            if table.get("active"):
                p_cal = outcomes.calibrate(p, table)
                basis.append(f"calibrated on {table['n_resolved']} resolved matches")
                p = p_cal
        except Exception:  # noqa: BLE001
            pass
        factor, why = _corridor_factor(rows, opp.hs_code, opp.market, config)
        if why:
            p *= factor
            basis.append(why)
    p = round(max(0.01, min(0.95, p)), 3)
    out.p_close = p
    out.p_close_basis = basis
    if out.margin_usd_per_mt is not None:
        out.expected_profit_usd = round(out.margin_usd_per_mt * lot * p, 2)
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
