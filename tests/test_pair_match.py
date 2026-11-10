"""Buyer × seller pair matching on the Opportunity Radar (services/trade_min/pair_match.py)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from services.trade_min import pair_match as pm
from services.trade_min.schemas import BuyerSignal, EnrichedOpportunity, SellerCandidate

TODAY = date(2026, 11, 10)


def _buyer(**kw) -> BuyerSignal:
    base = dict(buyer_name="Gulf Foods LLC", buyer_country="AE", signal_type="rfq", source="apify_company_search",
                urgency_tier=3, unit_price_usd=1400.0, price_unit="USD/MT", annual_volume_mt=500.0,
                risk_score=15.0, shipments_12m=12, fit_score=85, email="buy@gulf.ae", phone="+971",
                website="gulf.ae", hs_codes=["090411"], last_import_date="2026-10-01")
    base.update(kw)
    return BuyerSignal(**base)


def _seller(**kw) -> SellerCandidate:
    base = dict(candidate_id="s1", company_name="Kerala Spices Pvt", location_city="Kochi",
                location_state="Kerala", hs_code="090411", estimated_fob_price_usd=1000.0,
                annual_capacity_mt=2000.0, source="apify_india_suppliers", supplier_to_port_km=20.0,
                email="sales@ks.in", phone="+91", website="ks.in", gstin="32ABCDE1234F1Z5",
                iec="0512345678", udyam="UDYAM-KL-01", export_shipments_12m=30, fit_score=90)
    base.update(kw)
    return SellerCandidate(**base)


def _opp(buyers, sellers, benchmark=None) -> EnrichedOpportunity:
    return EnrichedOpportunity(
        opportunity_id="opp-1", hs_code="090411", market="AE", scanned_at=datetime.now(UTC),
        demand_index=70.0, price_gap_pct=30.0, source="apify_export_markets", net_price_gap_pct=25.0,
        arbitrage_rank=60.0, confidence_score=70.0, buyer_signals=buyers, seller_candidates=sellers,
        price_benchmark_usd_per_mt=benchmark,
    )


def test_complete_strong_pair_scores_perfect():
    p = pm.score_pair(_buyer(), 0, _seller(), _opp([], []), today=TODAY)
    assert not p.blockers
    assert p.score >= 85 and p.confidence >= 0.9
    assert p.grade == "Perfect"
    assert p.margin_pct and p.margin_pct > 20
    assert {f.key for f in p.factors} == set(pm._LABELS)


def test_buyer_price_below_landed_cost_blocks():
    p = pm.score_pair(_buyer(unit_price_usd=900.0), 0, _seller(), _opp([], []), today=TODAY)
    assert p.score == 0 and p.grade == "Blocked"
    assert "Buyer price below landed cost" in p.blockers


def test_different_chapter_blocks():
    p = pm.score_pair(_buyer(hs_codes=["520100"]), 0, _seller(), _opp([], []), today=TODAY)
    assert "Different product chapter" in p.blockers


def test_hs_hierarchy():
    assert pm.product_fit(["090411"], "090411", "090411")[0] == 100
    assert pm.product_fit(["090412"], "090411", "090411")[0] == 75
    assert pm.product_fit(["091000"], "090411", "090411")[0] == 40
    assert pm.product_fit([], "090411", "090411")[0] == 85


def test_missing_data_lowers_confidence_and_lists_improvements():
    thin_b = _buyer(unit_price_usd=None, annual_volume_mt=None, risk_score=None, shipments_12m=None,
                    fit_score=None, email=None)
    thin_s = _seller(iec=None, gstin=None, udyam=None, export_shipments_12m=None, fit_score=None,
                     annual_capacity_mt=None)
    p = pm.score_pair(thin_b, 0, thin_s, _opp([], [], benchmark=1400.0), today=TODAY)
    full = pm.score_pair(_buyer(), 0, _seller(), _opp([], []), today=TODAY)
    assert p.confidence < full.confidence
    assert p.grade != "Perfect"
    assert p.buyer_price_basis == "market_benchmark"
    joined = " ".join(p.improve)
    assert "IEC" in joined and "target price" in joined and "annual volume" in joined


def test_weak_factor_drags_score_non_compensatory():
    good = pm.score_pair(_buyer(), 0, _seller(), _opp([], []), today=TODAY)
    weak = pm.score_pair(_buyer(), 0, _seller(annual_capacity_mt=20.0), _opp([], []), today=TODAY)
    assert weak.score < good.score - 5


def test_margin_score_curve():
    assert pm.margin_score(0, 8) == 0
    assert pm.margin_score(8, 8) == 60
    assert pm.margin_score(20, 8) == 100
    assert pm.margin_score(None, 8) is None


def test_harmonic_needs_both_sides():
    assert pm.harmonic(90, 90) == 90
    assert pm.harmonic(100, 20) < 40
    assert pm.harmonic(0, 90) == 0


def test_one_to_one_assignment():
    buyers = [_buyer(buyer_name="A"), _buyer(buyer_name="B", unit_price_usd=1300.0)]
    sellers = [_seller(candidate_id="s1"), _seller(candidate_id="s2", estimated_fob_price_usd=1100.0)]
    pairs = pm.match_opportunity(_opp(buyers, sellers), today=TODAY)
    rec = [p for p in pairs if p.recommended]
    assert len(rec) == 2
    assert len({p.buyer_index for p in rec}) == 2 and len({p.seller_id for p in rec}) == 2
    assert pairs[0].recommended and pairs[0].score == max(p.score for p in pairs)


def test_attach_sets_fields_and_never_raises(monkeypatch):
    opp = _opp([_buyer()], [_seller()])
    pm.attach(opp)
    assert opp.pair_matches and opp.best_match_score == opp.pair_matches[0].score

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(pm, "match_opportunity", boom)
    pm.attach(opp)
    assert opp.pair_matches == [] and opp.best_match_score is None


def test_high_risk_buyer_blocks():
    p = pm.score_pair(_buyer(risk_score=90.0), 0, _seller(), _opp([], []), today=TODAY)
    assert any("risk" in b for b in p.blockers)


@pytest.mark.parametrize("unit,expected", [("KG", 0.5), ("MT", 500.0), ("pcs", None)])
def test_quantity_units(unit, expected):
    assert pm._to_mt(500.0, unit) == expected
