"""Buyer × seller pair matching for the Opportunity Radar.

Until now each side of an opportunity carried its own ``fit_score`` from the
Apify actors — a buyer was "good" and a seller was "good", but nothing said
whether *this* buyer and *this* seller make a deal together. This module scores
every buyer × seller pair of an opportunity on ten factors and picks one-to-one
recommended pairings.

How the score is built (all pure functions — no I/O, no LLM, reproducible):

1. **Hard blockers** — the pair cannot close: different product chapter, the
   buyer's price below PortLess's landed cost, a supplier that failed the
   gatekeeper. A blocked pair scores 0 and says why.
2. **Ten factors, each 0–100 with a reason** — product fit (HS hierarchy),
   margin after landed cost, volume fit, logistics, supplier trust, buyer
   quality, buying intent and timing, reachability, price confidence and
   track record. A factor with no data is *unknown*, never assumed perfect.
3. **Two-sided (reciprocal) scoring** — the buyer's view of the seller and the
   seller's view of the buyer are scored separately and combined with a
   harmonic mean, so a pair only scores high when *both* sides want it
   (the approach reciprocal recommender systems use for two-sided markets).
4. **Non-compensatory combination** — each view is a weighted *geometric* mean,
   so one weak factor drags the score down instead of being hidden by strong
   ones; a great price does not paper over a supplier with no export licence.
5. **Uncertainty shrinkage** — unknown factors pull the score toward a neutral
   50 and lower the pair's ``confidence``. A "Perfect" grade needs both a high
   score *and* enough real data behind it.
6. **One-to-one assignment** — pairs are taken best-first so each buyer gets a
   distinct seller (``recommended``), the way a deal desk would split the work.

Every pair also lists ``improve`` — the concrete data or checks that would
raise its score (get the buyer's target price, verify the IEC, …). That is the
honest path to a perfect score: close the gaps, not inflate the weights.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from shared.orchestration import landed_cost as lc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .schemas import BuyerSignal, EnrichedOpportunity, SellerCandidate


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PairMatchConfig:
    # Weights per side. Factors absent from a side do not count for it.
    buyer_view: dict[str, float] = field(default_factory=lambda: {
        "product_fit": 0.22, "margin": 0.18, "volume_fit": 0.15,
        "logistics": 0.12, "supplier_trust": 0.23, "track_record": 0.10,
    })
    seller_view: dict[str, float] = field(default_factory=lambda: {
        "product_fit": 0.18, "margin": 0.22, "volume_fit": 0.12,
        "buyer_quality": 0.16, "intent": 0.16, "reachability": 0.08,
        "price_confidence": 0.08,
    })
    neutral: float = 50.0
    """Value an unknown factor is shrunk toward."""
    floor: float = 1.0
    """Smallest factor value entering the geometric mean (keeps log finite)."""
    margin_full_multiple: float = 2.5
    """Margin at this multiple of the floor scores 100; the floor itself scores 60."""
    max_port_distance_km: float = 600.0
    shipments_full_supplier: int = 24
    shipments_full_buyer: int = 12
    perfect_score: float = 85.0
    perfect_confidence: float = 0.75
    max_pairs: int = 15


DEFAULT_PAIR_CONFIG = PairMatchConfig()

_SIGNAL_INTENT = {"tender": 95.0, "rfq": 90.0, "disruption": 80.0, "manifest": 70.0,
                  "historical": 60.0, "web_search": 45.0}
_TRANSIT_DAYS = {"middle_east": 10, "south_asia": 7, "southeast_asia": 14, "east_asia": 20,
                 "europe": 25, "africa": 25, "north_america": 35, "latin_america": 40, "oceania": 25}


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class MatchFactor(BaseModel):
    key: str
    label: str
    score: float | None
    """0–100, or None when the inputs are unknown."""
    reason: str


class PairMatch(BaseModel):
    buyer_index: int
    buyer_name: str
    buyer_country: str
    seller_id: str
    seller_name: str
    score: float
    """0–100 reciprocal match score (0 when blocked)."""
    grade: str
    """Perfect | Strong | Good | Fair | Weak | Blocked."""
    confidence: float
    """Share of factor weight backed by real data (0–1)."""
    buyer_view: float
    seller_view: float
    margin_pct: float | None = None
    margin_usd_per_mt: float | None = None
    buyer_price_usd_per_mt: float | None = None
    buyer_price_basis: str | None = None
    landed_cost_usd_per_mt: float | None = None
    factors: list[MatchFactor] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    improve: list[str] = Field(default_factory=list)
    recommended: bool = False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _is_mt(unit: str | None) -> bool:
    u = (unit or "").upper()
    return not u or "MT" in u or "TON" in u or u in ("T", "TNE")


def _to_mt(qty: float | None, unit: str | None) -> float | None:
    if qty is None or qty <= 0:
        return None
    u = (unit or "").upper()
    if "KG" in u:
        return qty / 1000.0
    if _is_mt(u):
        return qty
    return None  # pieces, litres… not comparable to MT capacity


def _digits(code: str | None) -> str:
    return "".join(ch for ch in (code or "") if ch.isdigit())


def _has(v: Any) -> bool:
    return bool(v and str(v).strip())


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Factor functions — each returns (score | None, reason)
# ---------------------------------------------------------------------------

def product_fit(buyer_hs: list[str], seller_hs: str, corridor_hs: str) -> tuple[float | None, str]:
    """HS hierarchy overlap: same 6-digit subheading 100, heading 75, chapter 40."""
    s = _digits(seller_hs) or _digits(corridor_hs)
    codes = [_digits(c) for c in buyer_hs if _digits(c)]
    if not codes:
        # Found for this corridor, so the product is implied — but not confirmed.
        if s[:6] == _digits(corridor_hs)[:6]:
            return 85.0, f"Buyer found for HS {corridor_hs}; their own product list is unknown"
        return None, "Neither side lists a comparable HS code"
    best = 0
    for c in codes:
        n = 0
        for a, b in zip(c[:6], s[:6]):
            if a != b:
                break
            n += 1
        best = max(best, n)
    if best >= 6:
        return 100.0, f"Same HS subheading {s[:6]}"
    if best >= 4:
        return 75.0, f"Same HS heading {s[:4]} (subheading differs)"
    if best >= 2:
        return 40.0, f"Same HS chapter {s[:2]} only"
    return 0.0, f"Buyer imports {', '.join(codes[:3])}; seller offers {s[:6]}"


def margin_score(margin_pct: float | None, floor_pct: float,
                 cfg: PairMatchConfig = DEFAULT_PAIR_CONFIG) -> float | None:
    """0 at break-even, 60 at the margin floor, 100 at ``margin_full_multiple`` × floor."""
    if margin_pct is None:
        return None
    if margin_pct <= 0:
        return 0.0
    floor_pct = max(floor_pct, 0.1)
    if margin_pct <= floor_pct:
        return round(margin_pct / floor_pct * 60.0, 1)
    full = floor_pct * cfg.margin_full_multiple
    return round(_clamp(60.0 + (margin_pct - floor_pct) / (full - floor_pct) * 40.0), 1)


def volume_fit(demand_mt: float | None, capacity_mt: float | None) -> tuple[float | None, str]:
    """Capacity vs the buyer's annual demand — enough to supply, not absurdly oversized."""
    if demand_mt is None or capacity_mt is None or capacity_mt <= 0:
        return None, "Buyer volume or supplier capacity unknown"
    ratio = capacity_mt / demand_mt
    if ratio >= 1.0:
        if ratio <= 25.0:
            return 100.0, f"Capacity {capacity_mt:,.0f} MT covers demand {demand_mt:,.0f} MT"
        return 80.0, f"Supplier is {ratio:,.0f}× the buyer's demand — small account for them"
    score = round(100.0 * ratio ** 0.8, 1)
    return score, f"Capacity covers {ratio:.0%} of the buyer's {demand_mt:,.0f} MT/yr"


def logistics_score(port_km: float | None, market: str,
                    cfg: PairMatchConfig = DEFAULT_PAIR_CONFIG) -> tuple[float | None, str]:
    region = lc._REGION_OF.get((market or "").upper(), "")
    transit = _TRANSIT_DAYS.get(region, 25)
    transit_s = _clamp(100.0 - (transit - 7) * 2.0)
    if port_km is None:
        return round(transit_s, 1), f"~{transit} days sea transit; plant-to-port distance unknown"
    port_s = 100.0 if port_km <= 0 else _clamp((1.0 - port_km / cfg.max_port_distance_km) * 100.0)
    return round(0.6 * port_s + 0.4 * transit_s, 1), f"{port_km:,.0f} km to port, ~{transit} days transit"


def supplier_trust(s: SellerCandidate, cfg: PairMatchConfig = DEFAULT_PAIR_CONFIG) -> tuple[float | None, str]:
    parts: list[str] = []
    pts = 0.0
    if _has(s.iec):
        pts += 35
        parts.append("IEC")
    if _has(s.gstin):
        pts += 25
        parts.append("GSTIN")
    if _has(s.udyam):
        pts += 10
        parts.append("Udyam")
    if s.export_shipments_12m:
        pts += 30 * min(s.export_shipments_12m / cfg.shipments_full_supplier, 1.0)
    credentials = pts
    if s.fit_score is not None:
        credentials = 0.75 * pts + 0.25 * s.fit_score
    if not parts and not s.export_shipments_12m and s.fit_score is None:
        return None, "No credentials or export history on record"
    have = ", ".join(parts) if parts else "no registrations"
    return round(_clamp(credentials), 1), f"{have}; {s.export_shipments_12m or 0} export shipments/12m"


def buyer_quality(b: BuyerSignal, cfg: PairMatchConfig = DEFAULT_PAIR_CONFIG) -> tuple[float | None, str]:
    vals: list[float] = []
    bits: list[str] = []
    if b.risk_score is not None:
        vals.append(_clamp(100.0 - b.risk_score))
        bits.append(f"risk {b.risk_score:.0f}")
    if b.shipments_12m is not None:
        vals.append(_clamp(b.shipments_12m / cfg.shipments_full_buyer * 100.0))
        bits.append(f"{b.shipments_12m} imports/12m")
    if b.fit_score is not None:
        vals.append(float(b.fit_score))
        bits.append(f"fit {b.fit_score}")
    if not vals:
        return None, "No risk, import history or fit score for the buyer"
    return round(sum(vals) / len(vals), 1), ", ".join(bits)


def intent_score(b: BuyerSignal, today: date | None = None) -> tuple[float, str]:
    """Signal strength + urgency + reorder timing from the last import."""
    base = _SIGNAL_INTENT.get(b.signal_type, 50.0)
    base += (b.urgency_tier - 1) * 4.0
    why = [f"{b.signal_type} signal", f"urgency {b.urgency_tier}"]
    last = _parse_date(b.last_import_date)
    if last:
        today = today or datetime.now(UTC).date()
        days = (today - last).days
        if b.shipments_12m and b.shipments_12m > 0:
            interval = 365.0 / b.shipments_12m
            p = 1.0 / (1.0 + math.exp(-5.0 * (days / interval - 1.0)))
            base = 0.7 * base + 0.3 * (p * 100.0)
            why.append(f"reorder likelihood {p:.0%}")
        elif days > 540:
            base -= 15.0
            why.append(f"last import {days} days ago")
    return round(_clamp(base), 1), ", ".join(why)


def reachability(b: BuyerSignal, s: SellerCandidate) -> tuple[float, str]:
    def side(email: Any, phone: Any, web: Any) -> float:
        return (50.0 if _has(email) else 0.0) + (30.0 if _has(phone) else 0.0) + (20.0 if _has(web) else 0.0)
    bs = side(b.email, b.phone, b.website or b.contact_url)
    ss = side(s.email, s.phone, s.website or s.listing_url)
    return round((bs + ss) / 2.0, 1), f"buyer contact {bs:.0f}/100, seller contact {ss:.0f}/100"


def track_record(s: SellerCandidate, market: str) -> tuple[float | None, str]:
    """Export volume as a proxy for being able to run an international shipment."""
    n = s.export_shipments_12m
    if n is None:
        return None, "Export history unknown"
    if n == 0:
        return 10.0, "No export shipments in the last 12 months"
    return round(_clamp(30.0 + 70.0 * math.log1p(n) / math.log1p(50)), 1), f"{n} export shipments in 12 months"


# ---------------------------------------------------------------------------
# Combination
# ---------------------------------------------------------------------------

_LABELS = {
    "product_fit": "Product fit", "margin": "Margin after landed cost", "volume_fit": "Volume fit",
    "logistics": "Logistics", "supplier_trust": "Supplier trust", "buyer_quality": "Buyer quality",
    "intent": "Buying intent & timing", "reachability": "Reachability",
    "price_confidence": "Price confidence", "track_record": "Export track record",
}


def view_score(factors: dict[str, float | None], weights: dict[str, float],
               cfg: PairMatchConfig = DEFAULT_PAIR_CONFIG) -> tuple[float, float]:
    """Weighted geometric mean with unknowns shrunk to neutral. Returns (score, confidence)."""
    total = sum(weights.values())
    known = 0.0
    log_sum = 0.0
    for key, w in weights.items():
        v = factors.get(key)
        if v is None:
            v = cfg.neutral
        else:
            known += w
        log_sum += w * math.log(max(v, cfg.floor))
    score = math.exp(log_sum / total) if total > 0 else 0.0
    return round(score, 1), round(known / total if total else 0.0, 3)


def harmonic(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return 0.0
    return round(2.0 * a * b / (a + b), 1)


def grade(score: float, confidence: float, blocked: bool,
          cfg: PairMatchConfig = DEFAULT_PAIR_CONFIG) -> str:
    if blocked:
        return "Blocked"
    if score >= cfg.perfect_score and confidence >= cfg.perfect_confidence:
        return "Perfect"
    if score >= 70:
        return "Strong"
    if score >= 55:
        return "Good"
    if score >= 40:
        return "Fair"
    return "Weak"


# ---------------------------------------------------------------------------
# Pair scoring
# ---------------------------------------------------------------------------

def _buyer_price(b: BuyerSignal, benchmark: float | None) -> tuple[float | None, str | None]:
    if b.unit_price_usd and b.unit_price_usd > 0 and _is_mt(b.price_unit):
        return float(b.unit_price_usd), "buyer_stated"
    if benchmark and benchmark > 0:
        return float(benchmark), "market_benchmark"
    return None, None


def score_pair(
    b: BuyerSignal,
    buyer_index: int,
    s: SellerCandidate,
    opp: EnrichedOpportunity,
    cfg: PairMatchConfig = DEFAULT_PAIR_CONFIG,
    today: date | None = None,
) -> PairMatch:
    market = b.buyer_country or opp.market
    blockers: list[str] = []
    improve: list[str] = []
    reasons: dict[str, str] = {}
    f: dict[str, float | None] = {}

    f["product_fit"], reasons["product_fit"] = product_fit(b.hs_codes, s.hs_code, opp.hs_code)
    if f["product_fit"] == 0.0:
        blockers.append("Different product chapter")

    demand = _to_mt(b.annual_volume_mt, "MT") or _to_mt(b.quantity, b.quantity_unit)
    buyer_price, basis = _buyer_price(b, opp.price_benchmark_usd_per_mt)
    floor = lc.default_margin_floor_pct()
    margin_pct = margin_usd = landed_cost = None
    fob = s.estimated_fob_price_usd if _is_mt(s.price_unit) else None
    if buyer_price and fob and fob > 0:
        lcost = lc.landed(fob, market, demand, incoterm="CIF", margin_floor_pct=0.0)
        landed_cost = lcost.cost_usd_per_mt
        margin_usd = round(buyer_price - landed_cost, 2)
        margin_pct = round(margin_usd / buyer_price * 100.0, 2)
        f["margin"] = margin_score(margin_pct, floor, cfg)
        reasons["margin"] = (f"Buyer ${buyer_price:,.0f}/MT ({basis.replace('_', ' ')}) vs "
                             f"landed ${landed_cost:,.0f}/MT → {margin_pct:.1f}% (floor {floor:.0f}%)")
        if margin_pct <= 0:
            blockers.append("Buyer price below landed cost")
        elif margin_pct < floor:
            improve.append(f"Margin {margin_pct:.1f}% is under the {floor:.0f}% floor — negotiate the supplier's FOB down")
    else:
        f["margin"] = None
        reasons["margin"] = "Needs both the buyer's price and the supplier's FOB"
        if not fob:
            improve.append("Get the supplier's FOB quote (RFQ)")
        if not buyer_price:
            improve.append("Get the buyer's target price")

    f["price_confidence"] = {"buyer_stated": 100.0, "market_benchmark": 60.0}.get(basis or "", 0.0) \
        if fob else (30.0 if basis else None)
    reasons["price_confidence"] = (
        "Buyer stated their price" if basis == "buyer_stated"
        else "Buyer price is the market's Comtrade unit value" if basis == "market_benchmark"
        else "No buyer price")
    if basis == "market_benchmark":
        improve.append("Ask the buyer for their target price (score uses the market benchmark)")

    f["volume_fit"], reasons["volume_fit"] = volume_fit(demand, s.annual_capacity_mt)
    if demand is None:
        improve.append("Ask the buyer for annual volume")
    if s.annual_capacity_mt is None:
        improve.append("Ask the supplier for annual capacity")
    if f["volume_fit"] is not None and f["volume_fit"] < 25:
        improve.append("Supplier capacity is far below demand — add a second supplier")

    f["logistics"], reasons["logistics"] = logistics_score(s.supplier_to_port_km, market, cfg)

    f["supplier_trust"], reasons["supplier_trust"] = supplier_trust(s, cfg)
    if not _has(s.iec):
        improve.append("Verify the supplier's IEC (needed to export)")
    if not _has(s.gstin):
        improve.append("Verify the supplier's GSTIN")

    f["buyer_quality"], reasons["buyer_quality"] = buyer_quality(b, cfg)
    if f["buyer_quality"] is None:
        improve.append("Enrich the buyer (risk score, import history)")
    elif b.risk_score is not None and b.risk_score >= 80:
        blockers.append(f"Buyer risk score {b.risk_score:.0f}")

    f["intent"], reasons["intent"] = intent_score(b, today)
    f["reachability"], reasons["reachability"] = reachability(b, s)
    if not _has(b.email):
        improve.append("Find the buyer's e-mail")
    if not _has(s.email):
        improve.append("Find the supplier's e-mail")
    f["track_record"], reasons["track_record"] = track_record(s, market)

    bv, bconf = view_score(f, cfg.buyer_view, cfg)
    sv, sconf = view_score(f, cfg.seller_view, cfg)
    blocked = bool(blockers)
    score = 0.0 if blocked else harmonic(bv, sv)
    conf = round((bconf + sconf) / 2.0, 3)

    factors = [MatchFactor(key=k, label=_LABELS[k], score=f[k], reason=reasons[k]) for k in _LABELS]
    # De-duplicate while keeping order.
    seen: set[str] = set()
    improve = [x for x in improve if not (x in seen or seen.add(x))]

    return PairMatch(
        buyer_index=buyer_index, buyer_name=b.buyer_name, buyer_country=b.buyer_country,
        seller_id=s.candidate_id, seller_name=s.company_name,
        score=score, grade=grade(score, conf, blocked, cfg), confidence=conf,
        buyer_view=bv, seller_view=sv,
        margin_pct=margin_pct, margin_usd_per_mt=margin_usd,
        buyer_price_usd_per_mt=buyer_price, buyer_price_basis=basis,
        landed_cost_usd_per_mt=landed_cost,
        factors=factors, blockers=blockers, improve=improve,
    )


def assign(pairs: list[PairMatch]) -> list[PairMatch]:
    """Mark one-to-one recommended pairings, best first (greedy maximum weight)."""
    used_b: set[int] = set()
    used_s: set[str] = set()
    for p in sorted(pairs, key=lambda p: (-p.score, -p.confidence)):
        if p.score <= 0 or p.buyer_index in used_b or p.seller_id in used_s:
            continue
        p.recommended = True
        used_b.add(p.buyer_index)
        used_s.add(p.seller_id)
    return pairs


def match_opportunity(opp: EnrichedOpportunity, cfg: PairMatchConfig = DEFAULT_PAIR_CONFIG,
                      today: date | None = None) -> list[PairMatch]:
    """Score every buyer × seller pair; return recommended pairs first, then the best others."""
    pairs = [score_pair(b, i, s, opp, cfg, today)
             for i, b in enumerate(opp.buyer_signals)
             for s in opp.seller_candidates]
    assign(pairs)
    pairs.sort(key=lambda p: (not p.recommended, -p.score, -p.confidence))
    return pairs[: cfg.max_pairs]


def attach(opp: EnrichedOpportunity) -> EnrichedOpportunity:
    """Fill ``opp.pair_matches`` / ``best_match_score`` in place (never raises)."""
    try:
        opp.pair_matches = match_opportunity(opp)
        opp.best_match_score = max((p.score for p in opp.pair_matches), default=None)
    except Exception:  # noqa: BLE001 - scoring must never break the radar
        opp.pair_matches = []
        opp.best_match_score = None
    return opp
