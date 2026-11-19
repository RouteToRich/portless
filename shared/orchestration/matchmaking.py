"""Matchmaking — which buyer and which supplier make the deal most likely to sign.

A prospect (MATCHING deal) collects candidates on both sides: buyers from the
outreach funnel (each with a ``BuyerRequirement`` read from their replies) and
suppliers' offers from RFQ quotes (``SupplierOffer``, also offers that came in
on other prospects for the same HS code). This module scores every buyer ×
supplier pair and drives the prospect toward the best one:

1. **Hard filters** — supplier verification failed, buyer quantity below the
   supplier's MOQ or above what its capacity delivers in time, conflicting
   grades (crude vs refined, technical vs pharma/food), lead time + transit far
   past the buyer's window.
2. **Prices** — stated prices when we have them (supplier quote; buyer target /
   counter / accepted offer), otherwise the price-book estimate with its
   spread. The supplier's FOB goes through the landed-cost bridge to the
   minimum price PortLess can offer in the buyer's incoterm.
3. **P(margin ≥ floor)** — the probability the buyer's price clears that
   minimum, from both prices' uncertainty (normal approximation).
4. **Chance of closing** = P(margin) × spec fit × buyer engagement × supplier
   reliability × timing fit; **expected value** = chance × margin/MT × quantity.

``evaluate()`` then takes the next step for the prospect:
- the best pair is *firm* (quoted supplier price, buyer price stated / countered /
  accepted) and its chance ≥ ``MATCH_MIN_PROBABILITY`` → ``CONFIRM_MATCH`` gate
  (L0): APPROVE makes that buyer and supplier the deal and opens the negotiation;
- an engaged buyer without a firm price and a quoted supplier → a
  ``SEND_INDICATIVE_OFFER`` gate (L0): a price range built from the supplier's
  quote + landed cost + margin, so the buyer's reaction reveals their price.

Everything is recomputed on each reply / quote; nothing here moves money.
"""
from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from shared import pricebook
from shared.orchestration import landed_cost as lc

logger = logging.getLogger(__name__)

CONFIRM_ACTION = "CONFIRM_MATCH"
OFFER_ACTION = "SEND_INDICATIVE_OFFER"

_ENGAGEMENT = {"selected": 1.0, "interested": 0.85, "contacted": 0.35, "followed_up": 0.3, "pending": 0.2}
_EXCLUDED = ("declined", "no_reply")
_FIRM_BUYER = ("stated", "counter", "accepted_offer")
_TRANSIT_DAYS = {"middle_east": 10, "south_asia": 7, "southeast_asia": 14, "east_asia": 20, "europe": 25,
                 "africa": 25, "north_america": 35, "latin_america": 40, "oceania": 25}
_CONFLICTS = ({"crude"}, {"refined", "usp", "bp", "pharma", "pharmaceutical", "food"}), \
             ({"technical", "industrial"}, {"usp", "bp", "pharma", "pharmaceutical", "food"})
_ACCEPT = re.compile(r"\b(accept(?:ed|able)?|agree(?:d)?|go ahead|we confirm|confirmed|proceed|works for us|"
                     r"ok(?:ay)? (?:with|for) (?:the|this|your) (?:price|offer)|send (?:the |your )?(?:pi|proforma|contract))\b",
                     re.IGNORECASE)


def enabled() -> bool:
    return os.getenv("MATCHMAKING_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def min_probability() -> float:
    try:
        return float(os.getenv("MATCH_MIN_PROBABILITY", "0.45"))
    except ValueError:
        return 0.45


def price_discovery_all_buyers() -> bool:
    """PRICE_DISCOVERY_ALL_BUYERS (default on): with a firm match proposed, the other interested
    buyers still receive their own indicative range, and the best reaction can win."""
    return os.getenv("PRICE_DISCOVERY_ALL_BUYERS", "true").strip().lower() not in ("0", "false", "no", "off")


def offer_validity_days() -> int:
    try:
        return int(os.getenv("MATCH_OFFER_VALIDITY_DAYS", "30"))
    except ValueError:
        return 30


class PricePoint(BaseModel):
    value: float | None = None
    basis: str = "unknown"
    """supplier: ``quoted`` | ``estimate``; buyer: ``stated`` | ``counter`` | ``accepted_offer`` | ``estimate``."""
    sigma: float | None = None
    incoterm: str | None = None


class PairScore(BaseModel):
    candidate_id: str
    buyer_name: str
    buyer_status: str
    offer_id: str
    supplier_id: str
    supplier_name: str
    origin_deal_id: str | None = None
    quantity_mt: float
    feasible: bool
    blockers: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    buyer_price: PricePoint
    supplier_price: PricePoint
    landed: lc.LandedCost | None = None
    margin_usd_per_mt: float | None = None
    margin_pct: float | None = None
    p_margin: float | None = None
    spec_fit: float
    engagement: float
    reliability: float
    supplier_verified: bool = False
    """GST and IEC both checked and active — the only case an e-mail may call it verified."""
    timing_fit: float
    probability: float
    """Chance of closing — calibrated against past outcomes once enough are resolved."""
    partial_supply_mt: float | None = None
    """With ``DEAL_COMPOSITION_ENABLED``: the supplier can deliver only this much of the buyer's
    quantity in time — kept as a partial leg (flag ``partial_supply``), the rest asked elsewhere."""
    probability_raw: float | None = None
    calibrated: bool = False
    buyer_history: dict[str, Any] | None = None
    supplier_history: dict[str, Any] | None = None
    expected_value_usd: float
    firm: bool
    next_step: str
    # ── negotiation targets (a trader's walk-away and target prices) ───────
    max_supplier_fob_usd_per_mt: float | None = None
    """Highest supplier FOB that still keeps the margin floor at this buyer price."""
    target_supplier_fob_usd_per_mt: float | None = None
    """Supplier FOB that gives the target margin (``MATCH_TARGET_MARGIN_PCT``)."""
    min_buyer_price_usd_per_mt: float | None = None
    """Lowest buyer price that keeps the margin floor at this supplier price."""
    target_buyer_price_usd_per_mt: float | None = None
    supplier_cut_needed_usd_per_mt: float | None = None
    """How much the supplier must come down to reach the floor (0 when it already does)."""
    incentive_usd_per_mt: float | None = None
    freight_basis: str | None = None
    quote_age_days: int | None = None
    quote_expires_in_days: int | None = None


class MatchBoard(BaseModel):
    deal_id: str
    hs_code: str
    market: str | None
    status: str
    priors: dict[str, Any]
    buyer_estimate: pricebook.PriceEstimate
    supplier_estimate: pricebook.PriceEstimate
    buyers: list[dict[str, Any]]
    offers: list[dict[str, Any]]
    pairs: list[PairScore]
    min_probability: float
    assumptions: dict[str, Any]
    next_actions: list[str]
    learning: dict[str, Any] = Field(default_factory=dict)
    """Calibration state + offer acceptance stats from past outcomes."""
    expired_offers: list[dict[str, Any]] = Field(default_factory=list)
    """Quotes past their validity: not scored — ask the supplier to reconfirm."""
    best_quote_usd_per_mt: float | None = None
    """Lowest valid supplier FOB on the board — the leverage for the other suppliers."""
    quote_spread_pct: float | None = None
    target_margin_pct: float | None = None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _tokens(text: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9.]+", (text or "").lower()) if len(t) >= 2}


def _status(c: Any) -> str:
    return str(getattr(c, "status", "") or "")


def grade_fit(buyer_grade: str | None, supplier_grade: str | None) -> tuple[float, str | None, str | None]:
    """(fit, blocker, missing)."""
    b, s = _tokens(buyer_grade), _tokens(supplier_grade)
    if not b or not s:
        return 0.85, None, ("grade" if not b else "supplier grade")
    for left, right in _CONFLICTS:
        if (b & left and s & right and not s & left) or (s & left and b & right and not b & left):
            return 0.0, f"grade mismatch: buyer wants {buyer_grade!r}, supplier offers {supplier_grade!r}", None
    if b & s:
        return 1.0, None, None
    return 0.6, None, "confirm the grade matches"


def _region_transit(market: str | None) -> int:
    """Transit days: the country profile's main port (todo 24), else the region's estimate."""
    try:
        from shared import country_profiles
        days = country_profiles.transit_days(market)
        if days:
            return int(days)
    except Exception:  # noqa: BLE001
        pass
    return _TRANSIT_DAYS.get(lc._REGION_OF.get((market or "").upper(), ""), 25)


def target_margin_pct(learned: dict[str, Any] | None = None, market: str | None = None) -> float:
    """``MATCH_TARGET_MARGIN_PCT`` (12), moved by what buyers of this corridor accepted
    (``outcomes.corridor_margins``: the market's own answers, else all markets of the product)."""
    try:
        base = float(os.getenv("MATCH_TARGET_MARGIN_PCT", "") or 12.0)
    except ValueError:
        base = 12.0
    table = (learned or {}).get("corridor_margins") or {}
    row = table.get((market or "").upper()) or {}
    if not row.get("adjust_pts") and not row.get("responses"):
        row = table.get("*") or {}
    return round(base + float(row.get("adjust_pts") or 0.0), 2)


def negotiation_targets(buyer_price: float | None, supplier_price: float | None, *, freight: float,
                        inspection: float, fee_ins_pct: float, incentive_pct: float, floor_pct: float,
                        target_pct: float) -> dict[str, float | None]:
    """Pure: walk-away and target prices for both sides.

    margin = buyer − (supplier + freight + inspection) − buyer × (fee + insurance)% + supplier × incentive%.
    """
    keep = 1.0 - incentive_pct / 100.0
    out: dict[str, float | None] = {"max_supplier": None, "target_supplier": None, "min_buyer": None, "target_buyer": None}
    if buyer_price:
        for key, pct in (("max_supplier", floor_pct), ("target_supplier", target_pct)):
            v = (buyer_price * (1.0 - (fee_ins_pct + pct) / 100.0) - freight - inspection) / keep
            out[key] = round(v, 2) if v > 0 else None
    if supplier_price:
        for key, pct in (("min_buyer", floor_pct), ("target_buyer", target_pct)):
            denom = 1.0 - (fee_ins_pct + pct) / 100.0
            if denom > 0:
                out[key] = round((supplier_price * keep + freight + inspection) / denom, 2)
    return out


def score_pair(deal: Any, cand: Any, offer: Any, *, buyer_est: pricebook.PriceEstimate,
               supplier_est: pricebook.PriceEstimate, learned: dict[str, Any] | None = None,
               freight_usd_per_mt: float | None = None, freight_basis: str | None = None,
               incentive_pct: float = 0.0) -> PairScore:
    req = getattr(cand, "requirement", None)
    market = getattr(deal, "market", None)
    qty = (getattr(req, "quantity_mt", None) or getattr(cand, "quantity_mt", None)
           or getattr(deal, "quantity_mt", None) or 25.0)
    blockers: list[str] = []
    missing: list[str] = []

    # ── hard filters ────────────────────────────────────────────────────
    if getattr(offer, "verification_failed", False):
        blockers.append("supplier failed GST / IEC verification")
    moq = getattr(offer, "moq_mt", None)
    if moq and qty < moq:
        blockers.append(f"buyer wants {qty:g} MT, supplier minimum is {moq:g} MT")
    window = getattr(req, "delivery_by_days", None) if req else None
    capacity = getattr(offer, "capacity_mt_per_month", None)
    partial_mt = None
    if capacity and window and qty > capacity * max(1.0, window / 30.0):
        can = round(capacity * max(1.0, window / 30.0), 2)
        from shared.orchestration.outreach import composition_enabled
        if composition_enabled():
            # Partial quantity is not a reason to drop a supplier (todo 13.2): keep it for what it
            # can deliver; the sourcing agent asks the others for the rest.
            partial_mt = can
            missing.append(f"partial_supply: supplier can deliver ~{can:g} of {qty:g} MT in {window} days — "
                           "ask its maximum quantity and ready date, the rest from another supplier")
        else:
            blockers.append(f"supplier makes {capacity:g} MT/month — cannot deliver {qty:g} MT in {window} days")

    spec, grade_block, grade_missing = grade_fit(getattr(req, "grade", None) if req else None,
                                                 getattr(offer, "grade", None))
    if grade_block:
        blockers.append(grade_block)
    # Match on the specification, not the HS code (todo 15.3): a conflict on a hard template
    # attribute (variety, size, form, packing) blocks the pair; each matching one raises the fit.
    b_spec = dict(getattr(req, "spec", None) or {}) if req else {}
    s_spec = dict(getattr(offer, "spec", None) or {})
    if b_spec and s_spec:
        try:
            from shared import catalogue, product_spec
            spec_conflicts, spec_matches = product_spec.conflicts(
                catalogue.line_for(str(getattr(deal, "hs_code", "") or "")), b_spec, s_spec)
        except Exception:  # noqa: BLE001
            spec_conflicts, spec_matches = [], 0
        blockers.extend(f"specification mismatch — {c}" for c in spec_conflicts)
        if spec_matches and not grade_block:
            spec = min(1.0, spec + 0.05 * spec_matches)
    if grade_missing:
        missing.append(grade_missing)
    need = {c.upper() for c in (getattr(req, "certifications", None) or [])} if req else set()
    have = {c.upper() for c in (getattr(offer, "certifications", None) or [])}
    if need and have and not need <= have:
        spec *= 0.7
        missing.append(f"supplier to confirm {', '.join(sorted(need - have))}")
    elif need and not have:
        missing.append(f"supplier certificates ({', '.join(sorted(need))})")

    # ── timing ─────────────────────────────────────────────────────────
    lead = getattr(offer, "lead_time_days", None)
    timing = 1.0
    if window and lead:
        total = lead + _region_transit(market)
        timing = 1.0 if total <= window else 0.5 if total <= window * 1.3 else 0.2
        if timing < 1.0:
            missing.append(f"timing: {lead} days lead + ~{_region_transit(market)} days transit vs {window}-day window")
    elif window and not lead:
        missing.append("supplier lead time")

    # ── prices ─────────────────────────────────────────────────────────
    incoterm = (getattr(req, "target_price_incoterm", None) or getattr(req, "incoterm", None) or "CIF") if req else "CIF"
    fob = getattr(offer, "fob_price_usd_per_mt", None)
    if fob:
        from shared.orchestration.trade_costs import exw_to_fob
        fob = exw_to_fob(float(fob), getattr(offer, "incoterm", "FOB"), getattr(offer, "port_km", None))
        if getattr(offer, "price_basis", "quoted") == "quoted":
            sp = PricePoint(value=fob, basis="quoted", sigma=fob * 0.02, incoterm="FOB")
        else:
            # A usual / estimated price is not a commitment: the pair stays an estimate until the
            # supplier quotes in writing (the RFQ reply).
            sp = PricePoint(value=fob, basis="estimate", sigma=fob * 0.06, incoterm="FOB")
            missing.append("supplier's written quote (only its usual price is known)")
    elif supplier_est.median:
        sp = PricePoint(value=supplier_est.median, basis="estimate", sigma=supplier_est.sigma, incoterm="FOB")
        missing.append("supplier price")
    else:
        sp = PricePoint()
        missing.append("supplier price")

    target = getattr(req, "target_price_usd_per_mt", None) if req else None
    if target:
        basis = getattr(req, "target_price_basis", None) or "stated"
        rel = {"accepted_offer": 0.01, "counter": 0.02}.get(basis, 0.03)
        bp = PricePoint(value=float(target), basis=basis, sigma=float(target) * rel, incoterm=lc._norm_incoterm(incoterm))
    elif buyer_est.median:
        est = lc.from_cif(buyer_est.median, incoterm, market)
        bp = PricePoint(value=est, basis="estimate", sigma=buyer_est.sigma, incoterm=lc._norm_incoterm(incoterm))
        missing.append("buyer target price")
    else:
        bp = PricePoint()
        missing.append("buyer target price")

    landed = margin = margin_pct = p_margin = None
    margin_floor = getattr(deal, "margin_floor_pct", None)
    incentive = round(sp.value * incentive_pct / 100.0, 2) if (sp.value and incentive_pct) else None
    if sp.value:
        landed = lc.landed(sp.value, market, qty, incoterm=incoterm, margin_floor_pct=margin_floor,
                           freight_usd_per_mt=freight_usd_per_mt)
    if landed and bp.value:
        margin = round(bp.value - landed.cost_usd_per_mt + (incentive or 0.0), 2)
        margin_pct = round(margin / bp.value * 100.0, 2) if bp.value else None
        k = landed.min_offer_usd_per_mt / max(1e-9, sp.value + landed.freight_usd_per_mt + landed.inspection_usd_per_mt)
        spread = math.sqrt((bp.sigma or 0.0) ** 2 + ((sp.sigma or 0.0) * k) ** 2) or 1e-9
        p_margin = round(_phi((bp.value - landed.min_offer_usd_per_mt) / spread), 4)

    # ── engagement / reliability ───────────────────────────────────────
    status = _status(cand)
    engagement = _ENGAGEMENT.get(status, 0.2)
    io = getattr(cand, "indicative_offer", None)
    if io is not None and getattr(io, "response", None) == "accepted":
        engagement = 1.0
    elif io is not None and getattr(io, "response", None) == "countered":
        engagement = max(engagement, 0.95)
    elif req is not None and any(getattr(req, f, None) for f in ("quantity_mt", "grade", "destination_port", "target_price_usd_per_mt")):
        engagement = min(1.0, engagement + 0.1)
    gst, iec = getattr(offer, "gst_verified", None), getattr(offer, "iec_active", None)
    reliability = 1.0 if (gst is True and iec is True) else 0.8
    if gst is None or iec is None:
        missing.append("supplier GSTIN / IEC")

    # ── what past outcomes taught (track records + calibration) ────────
    learned = learned or {}
    records = learned.get("track_records") or {}
    b_hist = records.get(f"buyer:{cand.candidate_id}")
    s_hist = records.get(f"supplier:{offer.supplier_id}")
    if b_hist:
        engagement = round(engagement * b_hist["factor"], 4)
    if s_hist:
        reliability = round(reliability * s_hist["factor"], 4)

    # ── quote age / validity ───────────────────────────────────────────
    quote_age = expires_in = None
    received = _aware(getattr(offer, "received_at", None))
    if received:
        age = datetime.now(timezone.utc) - received
        quote_age = max(0, age.days)
        validity = getattr(offer, "validity_days", None) or offer_validity_days()
        expires_in = validity - quote_age
        if sp.basis == "quoted" and expires_in <= 3:
            missing.append(f"quote expires in {max(expires_in, 0)} day(s) — ask the supplier to reconfirm")

    # ── negotiation targets ───────────────────────────────────────────
    floor_pct = float(margin_floor if margin_floor is not None else lc.default_margin_floor_pct())
    fee_ins = (landed.escrow_fee_pct + landed.insurance_pct) if landed else (lc.escrow_fee_pct() + lc.insurance_pct())
    fr = landed.freight_usd_per_mt if landed else (freight_usd_per_mt if freight_usd_per_mt is not None else lc.freight_per_mt(market))
    insp = landed.inspection_usd_per_mt if landed else lc.inspection_usd_per_shipment() / qty
    tg = negotiation_targets(bp.value, sp.value, freight=fr, inspection=insp, fee_ins_pct=fee_ins,
                             incentive_pct=incentive_pct, floor_pct=floor_pct, target_pct=max(target_margin_pct(learned, getattr(deal, "market", None)) - key_buyer_discount(b_hist), floor_pct))
    cut = None
    if sp.value and tg["max_supplier"] is not None:
        cut = round(max(0.0, sp.value - tg["max_supplier"]), 2)

    feasible = not blockers and p_margin is not None
    raw = round((p_margin or 0.0) * spec * engagement * reliability * timing, 4) if feasible else 0.0
    table = learned.get("calibration") or {}
    probability = raw
    calibrated = False
    if feasible and table.get("active"):
        from shared.orchestration.outcomes import calibrate
        probability = calibrate(raw, table)
        calibrated = True
    req = getattr(cand, "requirement", None)
    if feasible and getattr(req, "commitment", None) == "loi":
        from shared.orchestration.loi import CHANCE_FACTOR
        probability = round(min(0.95, probability * CHANCE_FACTOR), 4)  # the buyer sent a letter of intent
    ev = round(probability * max(margin or 0.0, 0.0) * qty, 2)
    firm = sp.basis == "quoted" and bp.basis in _FIRM_BUYER

    if blockers:
        step = "Not a match — " + blockers[0]
    elif p_margin is None:
        step = "Waiting for " + " and ".join(m for m in missing if "price" in m) or "Waiting for prices"
    elif firm and probability >= min_probability():
        step = "Confirm this match — it becomes the deal"
    elif sp.basis == "quoted" and cut and cut > 0:
        step = (f"Under the margin floor: push {getattr(offer, 'company_name', '') or 'the supplier'} to "
                f"≤ {tg['max_supplier']:,.0f} FOB (−{cut:,.0f}/MT)"
                + (f" or the buyer to ≥ {tg['min_buyer']:,.0f}" if tg["min_buyer"] else ""))
    elif firm:
        step = f"Firm prices but only {probability:.0%} likely — negotiate the gap or wait for a better quote"
    elif sp.basis == "quoted" and status in ("interested", "selected") and (io is None or io.response is None):
        step = "Send an indicative offer — the buyer's reaction gives their price" if io is None else \
            "Indicative offer sent — waiting for the buyer's reaction"
    elif sp.basis == "quoted":
        step = "Waiting for the buyer to show interest"
    else:
        step = "Waiting for this supplier's quote"

    return PairScore(
        candidate_id=cand.candidate_id, buyer_name=cand.legal_name, buyer_status=status,
        offer_id=offer.offer_id, supplier_id=offer.supplier_id,
        supplier_name=getattr(offer, "company_name", "") or offer.supplier_id,
        origin_deal_id=getattr(offer, "origin_deal_id", None),
        quantity_mt=qty, feasible=feasible, blockers=blockers, missing=list(dict.fromkeys(missing)),
        partial_supply_mt=partial_mt,
        buyer_price=bp, supplier_price=sp, landed=landed, margin_usd_per_mt=margin, margin_pct=margin_pct,
        p_margin=p_margin, spec_fit=round(spec, 3), engagement=round(engagement, 3), reliability=reliability,
        supplier_verified=(gst is True and iec is True), timing_fit=timing, probability=probability, probability_raw=raw, calibrated=calibrated,
        buyer_history=b_hist, supplier_history=s_hist, expected_value_usd=ev, firm=firm, next_step=step,
        max_supplier_fob_usd_per_mt=tg["max_supplier"], target_supplier_fob_usd_per_mt=tg["target_supplier"],
        min_buyer_price_usd_per_mt=tg["min_buyer"], target_buyer_price_usd_per_mt=tg["target_buyer"],
        supplier_cut_needed_usd_per_mt=cut, incentive_usd_per_mt=incentive, freight_basis=freight_basis,
        quote_age_days=quote_age, quote_expires_in_days=expires_in,
    )


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _expired(deal: Any) -> list[Any]:
    """This prospect's own quotes that are past their validity (newest per supplier)."""
    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    for o in getattr(deal, "supplier_offers", None) or []:
        received = _aware(getattr(o, "received_at", None))
        validity = getattr(o, "validity_days", None) or offer_validity_days()
        if received and received + timedelta(days=validity) < now:
            out[o.supplier_id] = o
    return list(out.values())


async def _costs(deal: Any) -> tuple[float | None, str | None, float]:
    """(lane freight USD/MT, basis, incentive % of FOB) — the radar's sources, so the
    board and the radar agree. Never raises: regional estimate / no incentive."""
    freight = basis = None
    incentive_pct = 0.0
    try:
        from shared import catalogue
        from services.trade_min import freight_lanes, incentives  # type: ignore[import]
        _mt, kind = catalogue.container_for(deal.hs_code)
        freight, basis = await freight_lanes.freight_per_mt(getattr(deal, "market", None) or "", (kind or "dry").lower())
        entry = await incentives.entry_for(deal.hs_code)
        if entry is not None and entry.eligible_confirmed:
            incentive_pct = float(entry.rodtep_pct or 0.0) + float(getattr(entry, "rosctl_pct", 0.0) or 0.0) + float(entry.drawback_pct or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Matchmaking: lane freight / incentives unavailable for %s: %s", deal.deal_id, exc)
    return freight, basis, incentive_pct


def _deal_supplier_ids(deal: Any) -> set[str]:
    """The supplier ids a deal is committed to (party, winner)."""
    ids = {getattr(deal, "winning_supplier_id", None)}
    for p in getattr(deal, "parties", None) or []:
        if (getattr(getattr(p, "role", None), "value", getattr(p, "role", None)) or "").lower() == "supplier":
            ids.add(getattr(p, "party_id", None))
    return {i for i in ids if i}


async def offers_for(deal: Any) -> list[Any]:
    """This prospect's offers plus still-valid offers for the same HS code that came
    in on other live prospects (a supplier that quoted glycerine for Spain can
    supply the German buyer too). Newest offer per supplier wins."""
    now = datetime.now(timezone.utc)
    pool: list[Any] = list(getattr(deal, "supplier_offers", None) or [])
    try:
        from services.deals.service import list_deals  # type: ignore[import]
        for other in await list_deals(limit=200):
            if other.deal_id == deal.deal_id or other.hs_code != deal.hs_code:
                continue
            status = getattr(other.status, "value", other.status)
            if status in ("CANCELLED", "REFUNDED", "PAID", "NEGOTIATION_FAILED"):
                continue
            # the supplier that failed QC / is in dispute on its own deal is offered nowhere else
            failed = _deal_supplier_ids(other) if status in ("QC_FAILED", "DISPUTE") else set()
            for o in other.supplier_offers:
                if getattr(o, "verification_failed", False) or o.supplier_id in failed:
                    continue  # a failed check / failed supplier is only shown on its own prospect
                pool.append(o.model_copy(update={"origin_deal_id": o.origin_deal_id or other.deal_id}))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Matchmaking: cross-prospect offers unavailable for %s: %s", deal.deal_id, exc)
    blocked: set[str] = set()
    if any(getattr(o, "origin_deal_id", None) not in (None, deal.deal_id) for o in pool):
        from shared.orchestration.contact_guard import blocked_supplier_keys
        blocked = await blocked_supplier_keys()
    best: dict[str, Any] = {}
    for o in pool:
        if getattr(o, "reconfirm_status", None) == "expired":
            continue  # the supplier never reconfirmed before expiry: no live price
        if blocked and getattr(o, "origin_deal_id", None) not in (None, deal.deal_id):
            from shared.orchestration.contact_guard import supplier_blocked
            if await supplier_blocked(getattr(o, "email", None), keys=blocked):
                continue
        received = _aware(getattr(o, "received_at", None))
        validity = getattr(o, "validity_days", None) or offer_validity_days()
        if received and received + timedelta(days=validity) < now:
            continue
        cur = best.get(o.supplier_id)
        if cur is None or (_aware(cur.received_at) or now) < (received or now):
            best[o.supplier_id] = o
    return list(best.values())


async def build_board(deal: Any) -> MatchBoard:
    market = getattr(deal, "market", None)
    priors = await pricebook.public_priors(deal.hs_code, market)
    buyer_est = await pricebook.estimate(deal.hs_code, "buyer", market=market, priors=priors)
    supplier_est = await pricebook.estimate(deal.hs_code, "supplier", market=market, priors=priors)
    offers = await offers_for(deal)
    from shared.orchestration import outcomes
    learned = await outcomes.learning_context(deal.hs_code)
    cands = [c for c in (getattr(deal, "buyer_candidates", None) or []) if _status(c) not in _EXCLUDED]
    freight, freight_basis, incentive_pct = await _costs(deal)
    pairs = [score_pair(deal, c, o, buyer_est=buyer_est, supplier_est=supplier_est, learned=learned,
                        freight_usd_per_mt=freight, freight_basis=freight_basis, incentive_pct=incentive_pct)
             for c in cands for o in offers]
    pairs.sort(key=lambda p: (p.feasible, p.firm and p.probability >= min_probability(), p.expected_value_usd,
                              p.probability), reverse=True)

    actions: list[str] = []
    if not cands:
        actions.append("No buyer candidate left — add buyers or widen the scan.")
    if not offers:
        actions.append("No supplier quote yet — the RFQ asks for FOB price, MOQ, capacity, lead time and grade.")
    if pairs and pairs[0].feasible:
        actions.append(f"Best pair: {pairs[0].buyer_name} × {pairs[0].supplier_name} — {pairs[0].next_step}")
    asked = {m for p in pairs[:5] for m in p.missing}
    if asked:
        actions.append("Most useful missing facts: " + ", ".join(sorted(asked)[:5]))
    quotes = sorted(float(o.fob_price_usd_per_mt) for o in offers if getattr(o, "fob_price_usd_per_mt", None))
    best_quote = quotes[0] if quotes else None
    spread = round((quotes[-1] - quotes[0]) / quotes[0] * 100.0, 1) if len(quotes) > 1 and quotes[0] else None
    if len(quotes) > 1:
        actions.append(f"Leverage: best quote {best_quote:,.0f} FOB — ask the other {len(quotes) - 1} supplier(s) "
                       "to match or beat it before choosing")
    expired = _expired(deal)
    if expired:
        actions.append(f"{len(expired)} quote(s) expired — ask "
                       + ", ".join(getattr(o, "company_name", "") or o.supplier_id for o in expired[:3])
                       + " to reconfirm their price")

    return MatchBoard(
        deal_id=deal.deal_id, hs_code=deal.hs_code, market=market,
        status=str(getattr(deal.status, "value", deal.status)), priors=priors,
        buyer_estimate=buyer_est, supplier_estimate=supplier_est,
        buyers=[{"candidate_id": c.candidate_id, "legal_name": c.legal_name, "country": c.country,
                 "status": _status(c), "email": c.email,
                 "requirement": c.requirement.model_dump(mode="json") if getattr(c, "requirement", None) else None,
                 "indicative_offer": c.indicative_offer.model_dump(mode="json") if getattr(c, "indicative_offer", None) else None}
                for c in cands],
        offers=[o.model_dump(mode="json") for o in offers],
        pairs=pairs, min_probability=min_probability(),
        assumptions={
            "freight_usd_per_mt": freight if freight is not None else lc.freight_per_mt(market),
            "freight_basis": freight_basis or "region_estimate", "incentive_pct_of_fob": incentive_pct,
            "insurance_pct": lc.insurance_pct(),
            "inspection_usd_per_shipment": lc.inspection_usd_per_shipment(), "escrow_fee_pct": lc.escrow_fee_pct(),
            "margin_floor_pct": getattr(deal, "margin_floor_pct", None) or lc.default_margin_floor_pct(),
            "import_duty_pct": priors.get("tariff_pct"),
        },
        next_actions=actions,
        expired_offers=[o.model_dump(mode="json") for o in expired],
        best_quote_usd_per_mt=best_quote, quote_spread_pct=spread, target_margin_pct=target_margin_pct(learned, market),
        learning={"calibration_active": learned["calibration"]["active"],
                  "resolved_matches": learned["calibration"]["n_resolved"],
                  "calibration_error": learned["calibration"]["ece"],
                  "offer_pricing": learned["offer_pricing"]},
    )


# ---------------------------------------------------------------------------
# Indicative offer
# ---------------------------------------------------------------------------

def indicative_range(pair: PairScore, buyer_est: pricebook.PriceEstimate | None = None,
                     nudge: float = 1.0) -> tuple[float, float]:
    """Price range to show a buyer: anchored on what the market pays when that is
    above our minimum, never below the minimum, at most 30 % over it; ``nudge``
    (from past offer acceptance) moves the anchor up when buyers accept easily and
    down when they rarely do."""
    floor = pair.landed.min_offer_usd_per_mt if pair.landed else 0.0
    anchor = floor * 1.03
    if pair.buyer_price.value and pair.buyer_price.basis == "estimate":
        anchor = max(anchor, min(pair.buyer_price.value, floor * 1.3))
    anchor = max(floor * 1.01, anchor * nudge)
    low = max(floor, anchor * 0.97)
    high = max(low * 1.02, anchor * 1.03)
    return float(round(low / 5) * 5), float(round(high / 5) * 5)


OFFER_VALIDITY_MAX_DAYS = 7


def indicative_validity_days(pair: Any) -> int:
    """An indicative offer never outlives the supplier quote it was priced on (review board B-23):
    7 days, or one day less than the quote has left (at least 1)."""
    left = getattr(pair, "quote_expires_in_days", None)
    if left is None:
        return OFFER_VALIDITY_MAX_DAYS
    return max(1, min(OFFER_VALIDITY_MAX_DAYS, int(left) - 1))


def offer_valid_until(pair: Any, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(days=indicative_validity_days(pair))


def quote_too_short(pair: Any) -> bool:
    """Under 2 days left on the supplier quote: reconfirm it before quoting the buyer."""
    left = getattr(pair, "quote_expires_in_days", None)
    return left is not None and int(left) < 2


def buyer_tier(history: dict[str, Any] | None) -> str:
    """``key`` (≥ ``KEY_BUYER_MIN_SIGNED`` signed deals, 2), ``repeat`` (1), ``new``."""
    signed = int((history or {}).get("signed") or 0)
    try:
        key_min = max(1, int(os.getenv("KEY_BUYER_MIN_SIGNED", "2")))
    except ValueError:
        key_min = 2
    return "key" if signed >= key_min else "repeat" if signed >= 1 else "new"


def key_buyer_discount(history: dict[str, Any] | None) -> float:
    """Target-margin points given back to a key buyer (``KEY_BUYER_MARGIN_DISCOUNT_PTS``, 1):
    their reorders cost no outreach and close faster — never below the floor (the caller clamps)."""
    if buyer_tier(history) != "key":
        return 0.0
    try:
        return max(0.0, float(os.getenv("KEY_BUYER_MARGIN_DISCOUNT_PTS", "1")))
    except ValueError:
        return 1.0


def _supplier_line(pair: Any) -> str:
    """Only say 'verified' when this pair's supplier passed GST and IEC (review board B-20)."""
    if getattr(pair, "supplier_verified", False):
        return "This is supplied by a GST- and IEC-verified Indian manufacturer,"
    return "This is supplied by an Indian manufacturer whose GST and IEC registrations we verify before the order,"


def draft_indicative(deal: Any, cand: Any, pair: PairScore, low: float, high: float,
                     lead_time_days: int | None = None) -> tuple[str, str]:
    from shared import company_profile as cp
    from shared.connectors.qc_providers import inspector_phrase
    from shared.orchestration.outreach import _greeting, _product_label, subject_token
    from shared.product_label import product_title
    product = _product_label(deal)
    req = getattr(cand, "requirement", None)
    grade = getattr(req, "grade", None) if req else None
    port = getattr(req, "destination_port", None) if req else None
    inc = pair.buyer_price.incoterm or "CIF"
    lead = f" Lead time about {lead_time_days} days from order." if lead_time_days else ""
    title = product_title(getattr(deal, "hs_code", "") or "")
    subject = f"Re: {title} — indicative {inc} price {subject_token(deal.deal_id, cand.candidate_id)}"
    body = (
        f"{_greeting(cand)}\n\n"
        "Thank you for your reply and for the details you shared.\n\n"
        f"For {pair.quantity_mt:g} MT of {product}"
        + (f" ({grade})" if grade else "")
        + f", our indicative price is USD {low:,.0f}–{high:,.0f} per MT {inc}"
        + (f" {port}" if port else "")
        + f". {_supplier_line(pair)} and the pre-shipment inspection by {inspector_phrase()} is "
        f"included.{lead}\n\n"
        "Your payment is protected throughout: you pay into a neutral escrow account, not to us. A "
        "production advance is released when the order is placed, and the balance only after the "
        "inspection passes, the goods clear customs and you accept them.\n\n"
        "Does this range work for you? If you have a target price, please let me know and I will see how "
        f"close we can come. This indicative price is valid until {offer_valid_until(pair):%d %B %Y}; a firm "
        "proforma invoice follows as soon as you confirm the grade, quantity and destination port."
        + cp.signature()
    )
    return subject, body


async def _create_gate(deal: Any, action: str, *, summary: str, payload: dict[str, Any], key: str,
                       amount: float | None = None, risk_flags: list[str] | None = None) -> str | None:
    from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
    from services.hitl_approval.service import create_approval  # type: ignore[import]
    from shared.orchestration.autonomy_policy import resolve_gate

    spec = resolve_gate(action, deal, amount=amount)
    approval = await create_approval(CreateApprovalRequest(
        deal_id=deal.deal_id, action=action, summary=summary,
        risk_flags=risk_flags or [], autonomy_level=spec.level,
        expires_at=spec.expires_at_iso(), default_on_timeout=spec.default_on_timeout,
        financial_impact_usd=amount, callback_payload=payload, idempotency_key=key,
    ))
    return approval.approval_id


async def propose_indicative_offer(deal: Any, pair: PairScore, board: MatchBoard | None = None,
                                   lead_time_days: int | None = None) -> str | None:
    cand = next((c for c in deal.buyer_candidates if c.candidate_id == pair.candidate_id), None)
    if cand is None or not pair.landed:
        return None
    to = getattr(cand, "reply_address", None) or cand.email
    if not to:
        return None
    nudge = float(((board.learning or {}).get("offer_pricing") or {}).get("price_nudge", 1.0)) if board else 1.0
    low, high = indicative_range(pair, board.buyer_estimate if board else None, nudge)
    subject, body = draft_indicative(deal, cand, pair, low, high, lead_time_days)
    return await _create_gate(
        deal, OFFER_ACTION,
        summary=(f"Indicative offer → {cand.legal_name} <{to}>: USD {low:,.0f}–{high:,.0f}/MT "
                 f"{pair.buyer_price.incoterm or 'CIF'} for {pair.quantity_mt:g} MT "
                 f"(supplier {pair.supplier_name} FOB {pair.supplier_price.value:,.0f}, "
                 f"minimum {pair.landed.min_offer_usd_per_mt:,.0f})"),
        payload={"deal_id": deal.deal_id, "candidate_id": cand.candidate_id, "offer_id": pair.offer_id,
                 "to_address": to, "subject": subject, "body": body,
                 "price_low_usd_per_mt": low, "price_high_usd_per_mt": high,
                 "incoterm": pair.buyer_price.incoterm or "CIF", "quantity_mt": pair.quantity_mt,
                 "destination_port": getattr(getattr(cand, "requirement", None), "destination_port", None),
                 "min_offer_usd_per_mt": pair.landed.min_offer_usd_per_mt,
                 "buyer_estimate_usd_per_mt": board.buyer_estimate.median if board else None},
        key=f"indicative-{deal.deal_id}-{cand.candidate_id}-{pair.offer_id}",
        amount=round(low * pair.quantity_mt, 2),
        risk_flags=["price_disclosed_to_buyer"] + (["supplier_quote_expiring_reconfirm_first"] if quote_too_short(pair) else []),
    )


async def send_indicative_offer(deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """SEND_INDICATIVE_OFFER APPROVE callback."""
    from services.deals.schemas import IndicativeOffer  # type: ignore[import]
    from services.deals.service import get_deal, update_buyer_candidate  # type: ignore[import]
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    from shared.orchestration.outreach import NOT_DELIVERED

    cid = payload["candidate_id"]
    email_id = f"IND{deal_id.replace('-', '')[-10:]}{re.sub(r'[^A-Z0-9]', '', cid.upper())[:12]}"
    result = await send_email(SendEmailRequest(to_address=payload["to_address"], subject=payload["subject"],
                                               body=payload["body"], negotiation_id="", round_no=0, email_id=email_id))
    outcome = str(getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent")))
    from agents.negotiation_agent.email_history import record_deal_email  # type: ignore[import]
    await record_deal_email(deal_id=deal_id, direction="outbound", party="buyer", to_address=payload["to_address"], from_address="PortLess",
                            subject=payload["subject"], body=payload["body"], kind="indicative_offer", delivered=outcome != "logged")
    if outcome == "logged":
        await update_buyer_candidate(deal_id, cid, {"send_error": NOT_DELIVERED})
        logger.warning("Matchmaking: indicative offer to %s for %s NOT delivered", payload["to_address"], deal_id)
        return {"delivered": False, "candidate_id": cid}
    now = datetime.now(timezone.utc)
    offer = IndicativeOffer(
        offer_id=payload["offer_id"], price_low_usd_per_mt=float(payload["price_low_usd_per_mt"]),
        price_high_usd_per_mt=float(payload["price_high_usd_per_mt"]), incoterm=payload.get("incoterm") or "CIF",
        destination_port=payload.get("destination_port"), quantity_mt=payload.get("quantity_mt"), sent_at=now,
    )
    deal = await update_buyer_candidate(deal_id, cid, {"indicative_offer": offer, "send_error": None})
    from shared.orchestration import outcomes
    await outcomes.record_offer_sent(deal, cid, offer.offer_id, offer.price_low_usd_per_mt,
                                     offer.price_high_usd_per_mt, payload.get("buyer_estimate_usd_per_mt"))
    await pricebook.record(pricebook.PriceObservation(
        hs_code=deal.hs_code, side="buyer", kind="indicative_offer", market=deal.market,
        price_usd_per_mt=lc.to_cif(offer.price_low_usd_per_mt, offer.incoterm, deal.market),
        incoterm=offer.incoterm, quantity_mt=offer.quantity_mt, deal_id=deal_id, counterparty_id=cid,
    ))
    return {"delivered": True, "candidate_id": cid, "email_id": email_id}


# ---------------------------------------------------------------------------
# Confirm match (prospect → deal)
# ---------------------------------------------------------------------------

async def propose_match(deal: Any, pair: PairScore) -> str | None:
    gid = await _create_gate(
        deal, CONFIRM_ACTION,
        summary=(f"Match {pair.buyer_name} × {pair.supplier_name}: buyer "
                 f"{pair.buyer_price.value:,.0f}/MT {pair.buyer_price.incoterm} ({pair.buyer_price.basis}) · "
                 f"supplier FOB {pair.supplier_price.value:,.0f} · margin {pair.margin_pct or 0:.1f}% · "
                 f"{pair.probability:.0%} likely · {pair.quantity_mt:g} MT"),
        payload={"deal_id": deal.deal_id, "candidate_id": pair.candidate_id, "offer_id": pair.offer_id,
                 "pair": pair.model_dump(mode="json")},
        key=f"match-{deal.deal_id}-{pair.candidate_id}-{pair.offer_id}",
        amount=round((pair.buyer_price.value or 0) * pair.quantity_mt, 2),
        risk_flags=[f"probability_{int(pair.probability * 100)}pct"] + (["cross_prospect_offer"] if pair.origin_deal_id else [])
        + (["calibrated"] if pair.calibrated else []),
    )
    from shared.orchestration import outcomes
    await outcomes.record_proposal(deal, pair, gid)
    return gid


_PROPOSAL_ACTIONS = frozenset({"CONFIRM_MATCH", "SEND_INDICATIVE_OFFER"})


async def _withdraw_other_proposals(deal_id: str, candidate_id: str, supplier_id: str) -> None:
    """The match is decided: every other pair still proposed for this deal (and
    any indicative offer not yet sent) is closed, not left for an operator to
    approve into a deal that has moved on. Never fails the confirmation."""
    try:
        from services.hitl_approval.service import list_approvals, withdraw_pending  # type: ignore[import]
        from shared.orchestration import outcomes
        others = [g for g in await list_approvals(status="PENDING", deal_id=deal_id) if g.action in _PROPOSAL_ACTIONS]
        await withdraw_pending(deal_id, _PROPOSAL_ACTIONS,
                               f"match confirmed: {candidate_id} × {supplier_id}")
        for gate in others:
            p = gate.callback_payload or {}
            if gate.action == "CONFIRM_MATCH" and p.get("candidate_id") and p.get("offer_id"):
                await outcomes.mark_decision(deal_id, p["candidate_id"], p["offer_id"], "superseded")
    except Exception as exc:  # noqa: BLE001 — the match itself is confirmed
        logger.warning("Matchmaking: could not withdraw the other proposals of deal %s: %s", deal_id, exc)


async def confirm_match(deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """CONFIRM_MATCH APPROVE callback: the pair becomes the deal and the
    negotiation opens (MATCHING → NEGOTIATING)."""
    from services.deals.schemas import UpdateFinancialsRequest, UpdateStatusRequest  # type: ignore[import]
    from services.deals.service import (  # type: ignore[import]
        get_deal, record_match_result, set_selected_pair, update_buyer_candidate, update_financials,
        update_party, update_status, upsert_supplier_offer,
    )
    from shared.domain.enums import DealStatus

    deal = await get_deal(deal_id)
    if str(getattr(deal.status, "value", deal.status)) != "MATCHING":
        return {"skipped": f"deal is {getattr(deal.status, 'value', deal.status)}, not MATCHING"}
    cid, oid = payload["candidate_id"], payload["offer_id"]
    cand = next((c for c in deal.buyer_candidates if c.candidate_id == cid), None)
    offers = await offers_for(deal)
    offer = next((o for o in offers if o.offer_id == oid), None)
    if cand is None or offer is None:
        raise ValueError(f"pair {cid} × {oid} no longer on deal {deal_id}")
    if offer.origin_deal_id and offer.origin_deal_id != deal_id:
        await upsert_supplier_offer(deal_id, offer)  # the cross-prospect offer now lives on this deal too

    board = await build_board(deal)
    pair = next((p for p in board.pairs if p.candidate_id == cid and p.offer_id == oid), None)

    from shared.orchestration import outcomes
    await outcomes.mark_decision(deal_id, cid, oid, "approved")
    await update_buyer_candidate(deal_id, cid, {"status": "selected"})
    req = cand.requirement
    await update_party(deal_id, "buyer", {
        "party_id": cid, "legal_name": cand.legal_name, "country": cand.country,
        "email": cand.reply_address or cand.email, "website": cand.website, "phone": cand.phone,
        "contact_source": f"match:{cand.source or 'outreach'}",
        "email_status": "ok" if cand.reply_address else "unverified", "sanctions_status": "unchecked",
    }, source=None, replace=True)
    from services.deals.service import set_supplier_party  # type: ignore[import]
    await set_supplier_party(deal_id, offer.supplier_id, company_name=offer.company_name or None, email=offer.email)
    buyer_price = pair.buyer_price.value if pair else (req.target_price_usd_per_mt if req else None)
    await record_match_result(deal_id, offer.supplier_id, company_name=offer.company_name,
                              fob_price_usd=offer.fob_price_usd_per_mt,
                              composite_score=round((pair.probability if pair else 0.0) * 100, 2),
                              make_winner=True)
    fin: dict[str, Any] = {}
    if buyer_price:
        fin["buyer_target_price_usd"] = buyer_price
    if fin:
        await update_financials(deal_id, UpdateFinancialsRequest(**fin))
    await set_selected_pair(deal_id, {
        "candidate_id": cid, "offer_id": oid, "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "quantity_mt": pair.quantity_mt if pair else None, **({"score": pair.model_dump(mode="json")} if pair else {}),
    }, quantity_mt=pair.quantity_mt if pair else None,
        buyer_price_source=f"match:{pair.buyer_price.basis}" if pair else None)
    await update_status(deal_id, UpdateStatusRequest(
        target_status=DealStatus.NEGOTIATING,
        reason=f"Match confirmed: {cand.legal_name} × {offer.company_name or offer.supplier_id}",
    ))
    await _withdraw_other_proposals(deal_id, cid, offer.supplier_id)
    logger.info("Matchmaking: deal %s matched %s × %s → NEGOTIATING", deal_id, cid, offer.supplier_id)
    return {"matched": True, "candidate_id": cid, "supplier_id": offer.supplier_id}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def evaluate(deal_id: str) -> dict[str, Any]:
    """Recompute the board and open the next gate (confirm match or indicative offer)."""
    if not enabled():
        return {"enabled": False}
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    if str(getattr(deal.status, "value", deal.status)) != "MATCHING":
        return {"skipped": "not MATCHING"}
    board = await build_board(deal)
    out: dict[str, Any] = {"pairs": len(board.pairs), "gates": []}
    confirmable = [p for p in board.pairs if p.feasible and p.firm and p.probability >= min_probability()]
    done: set[str] = set()
    if confirmable:
        # The board is sorted by expected value: the first firm pair sells to the buyer who pays
        # most for the risk. The best pair of a different buyer travels with it as the runner-up.
        best = confirmable[0]
        runner = next((p for p in confirmable[1:] if p.candidate_id != best.candidate_id), None)
        gid = await propose_match(deal, best)
        out["gates"].append({"action": CONFIRM_ACTION, "approval_id": gid, "candidate_id": best.candidate_id,
                             "offer_id": best.offer_id})
        if runner is not None:
            out["runner_up"] = {"candidate_id": runner.candidate_id, "offer_id": runner.offer_id,
                                "expected_value_usd": runner.expected_value_usd}
        if not price_discovery_all_buyers():
            return out
        # Price discovery across buyers (todo 19.1): the other interested buyers still get their
        # own indicative range, so a better reaction can win before the match is confirmed.
        done.add(best.candidate_id)
    offers = {o["offer_id"]: o for o in board.offers}
    for p in board.pairs:
        if p.candidate_id in done or not p.feasible or p.firm or p.supplier_price.basis != "quoted":
            continue
        cand = next((c for c in deal.buyer_candidates if c.candidate_id == p.candidate_id), None)
        if cand is None or _status(cand) not in ("interested", "selected"):
            continue
        io = getattr(cand, "indicative_offer", None)
        if io is not None and (io.response is None or io.offer_id == p.offer_id):
            done.add(p.candidate_id)
            continue  # one open offer per buyer; a new one only for a different (better) supplier after a reply
        offer = offers.get(p.offer_id)
        gid = await propose_indicative_offer(deal, p, board, (offer or {}).get("lead_time_days"))
        if gid:
            out["gates"].append({"action": OFFER_ACTION, "approval_id": gid, "candidate_id": p.candidate_id,
                                 "offer_id": p.offer_id})
        done.add(p.candidate_id)
    return out


# ---------------------------------------------------------------------------
# Buyer replies
# ---------------------------------------------------------------------------

async def handle_buyer_reply(deal: Any, cand: Any, from_address: str, body: str, verdict: str) -> dict[str, Any]:
    """Read the reply into the candidate's requirement, settle a pending indicative
    offer (accepted / countered / declined), record the price, re-evaluate."""
    from services.deals.schemas import BuyerRequirement  # type: ignore[import]
    from services.deals.service import update_buyer_candidate  # type: ignore[import]
    from shared.orchestration import extraction
    from shared.orchestration.outreach import _product_label

    try:
        from agents.negotiation_agent.guards import _new_reply_text  # type: ignore[import]
        new_text = _new_reply_text(body) or body
    except Exception:  # noqa: BLE001
        new_text = body
    container_mt = None
    try:
        from shared import catalogue
        container_mt = catalogue.container_for(str(getattr(deal, "hs_code", "") or ""))[0]
    except Exception:  # noqa: BLE001 - the default box size applies
        pass
    fields = await extraction.extract_buyer(new_text, product=_product_label(deal), container_mt=container_mt)
    try:
        from shared import catalogue, product_spec
        spec = product_spec.read_attributes(catalogue.line_for(str(getattr(deal, "hs_code", "") or "")), new_text)
        if spec:
            prev_spec = dict(getattr(getattr(cand, "requirement", None), "spec", None) or {})
            fields["spec"] = {**prev_spec, **spec}
    except Exception:  # noqa: BLE001 - the template is optional
        pass
    prior = getattr(cand, "requirement", None)
    if (prior is not None and getattr(prior, "quantity_confirm", None) and not fields.get("quantity_mt")
            and re.search(r"\b(?:yes|correct|confirm(?:ed)?|that'?s right|right)\b", new_text or "", re.IGNORECASE)):
        fields["quantity_confirmed"] = True  # "yes, correct" to our echoed quantity
    elif fields.get("quantity_confirm"):
        fields["quantity_confirmed"] = False
    now = datetime.now(timezone.utc)
    cur = cand.requirement.model_dump() if getattr(cand, "requirement", None) else {}
    merged = {**cur, **{k: v for k, v in fields.items() if v not in (None, [], "")}}
    price = fields.get("target_price_usd_per_mt")
    io = getattr(cand, "indicative_offer", None)
    updates: dict[str, Any] = {"replied_at": now, "reply_preview": (body or "")[:200], "reply_address": from_address}

    if io is not None and io.response is None:
        if price:
            io = io.model_copy(update={"response": "countered", "responded_at": now})
            merged["target_price_basis"] = "counter"
            merged.setdefault("target_price_incoterm", io.incoterm)
        elif verdict == "declined":
            io = io.model_copy(update={"response": "declined", "responded_at": now})
        elif _ACCEPT.search(new_text):
            io = io.model_copy(update={"response": "accepted", "responded_at": now})
            merged["target_price_usd_per_mt"] = io.price_low_usd_per_mt
            merged["target_price_incoterm"] = io.incoterm
            merged["target_price_basis"] = "accepted_offer"
            price = io.price_low_usd_per_mt
        updates["indicative_offer"] = io
    elif price:
        merged["target_price_basis"] = merged.get("target_price_basis") or "stated"

    has_facts = any(fields.get(k) for k in ("quantity_mt", "grade", "destination_port", "target_price_usd_per_mt"))
    if verdict == "declined" and not price and not (io is not None and io.response == "declined"):
        updates["status"] = "declined"
    elif verdict == "declined" and io is not None and io.response == "declined":
        updates["status"] = "declined"
    elif verdict == "interested" or has_facts or (io is not None and io.response in ("accepted", "countered")):
        if _status(cand) not in ("selected",):
            updates["status"] = "interested"
    merged["updated_at"] = now
    updates["requirement"] = BuyerRequirement(**{k: v for k, v in merged.items() if k in BuyerRequirement.model_fields})
    deal = await update_buyer_candidate(deal.deal_id, cand.candidate_id, updates)
    try:  # kept on the company's directory record, so it outlives the prospect (todo 15.2)
        from services.trade_min import product_facts  # type: ignore[import]
        await product_facts.record_buyer_requirement(deal, cand, updates["requirement"])
        await product_facts.note_person("buyer", {"email": (from_address or "").lower() or None,
                                                  "replied_at": now.isoformat(), "source": "email"},
                                        name=getattr(cand, "legal_name", None), website=getattr(cand, "website", None),
                                        email=getattr(cand, "email", None))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Matchmaking: requirement not kept on the directory: %s", exc)
    responded = getattr(updates.get("indicative_offer"), "response", None)
    if responded:
        from shared.orchestration import outcomes
        await outcomes.record_offer_response(deal.deal_id, cand.candidate_id, responded)

    if price:
        kind = {"counter": "buyer_counter", "accepted_offer": "buyer_accepted"}.get(merged.get("target_price_basis"), "buyer_target")
        await pricebook.record(pricebook.PriceObservation(
            hs_code=deal.hs_code, side="buyer", kind=kind, market=deal.market,
            price_usd_per_mt=lc.to_cif(float(price), merged.get("target_price_incoterm") or merged.get("incoterm"), deal.market),
            grade=merged.get("grade"), incoterm=merged.get("target_price_incoterm") or merged.get("incoterm"),
            quantity_mt=merged.get("quantity_mt"), deal_id=deal.deal_id, counterparty_id=cand.candidate_id,
        ))
    status = updates.get("status", _status(cand))
    result: dict[str, Any] = {"verdict": verdict, "candidate_id": cand.candidate_id, "matched": True,
                              "status": status, "requirement": updates["requirement"].model_dump(mode="json"),
                              "indicative_offer_response": getattr(updates.get("indicative_offer"), "response", None)}
    if status == "declined":
        from shared.orchestration.outreach import advance
        result.update(await advance(deal.deal_id))
    else:
        result["evaluation"] = await evaluate(deal.deal_id)
    return result
