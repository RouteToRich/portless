"""Opportunity Radar ranking by expected profit (services/trade_min/profit.py), the
corrected gap math (duty + freight, no VAT, suspect gaps), counterparty quality
flags (quality.py), the scan report, mandi prices and the buyer-introduction line."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.trade_min import pair_match as pm
from services.trade_min import profit as pf
from services.trade_min import quality as q
from services.trade_min import ranking
from services.trade_min import repository as repo_mod
from services.trade_min import scan_reports as sr
from services.trade_min import service as svc
from services.trade_min import directory as dr
from services.trade_min.config import DEFAULT_CONFIG as CONFIG
from services.trade_min.schemas import (
    BuyerSignal,
    CounterpartyValidation,
    EnrichedOpportunity,
    ScanRequest,
    SellerCandidate,
    TariffData,
)
from shared import pricebook
from shared.connectors import apify as ap
from shared.connectors import mandi
from shared.orchestration import landed_cost as lc
from tests.factories import make

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    repo_mod._repository = repo_mod.InMemoryFallbackRepository()
    dr.reset_directory()
    sr.reset_for_tests()
    pf.reset_for_tests()
    pricebook.reset_for_tests()
    monkeypatch.setattr(svc, "get_publisher", lambda: MagicMock(publish=AsyncMock()))
    monkeypatch.delenv("DATA_GOV_IN_API_KEY", raising=False)
    yield
    repo_mod._repository = None
    dr.reset_directory()


@pytest.fixture
def quotes(monkeypatch):
    replies: dict[str, dict | None] = {}

    async def fake(name, _request):
        return replies.get(name)

    monkeypatch.setattr(svc, "_safe_quote", fake)
    return replies


def _buyer(**kw) -> BuyerSignal:
    base = dict(buyer_name="Gulf Foods LLC", buyer_country="AE", signal_type="web_search",
                source="apify_company_search", urgency_tier=1, website="https://gulf.ae", company_id="gulf.ae")
    base.update(kw)
    return BuyerSignal(**base)


def _seller(**kw) -> SellerCandidate:
    base = dict(candidate_id="s1", company_name="Kerala Spices", location_city="", location_state="",
                hs_code="090422", source="apify_company_search")
    base.update(kw)
    return SellerCandidate(**base)


def _opp(**kw) -> EnrichedOpportunity:
    base = dict(opportunity_id="OPP-1", hs_code="090422", market="DE", scanned_at=NOW, demand_index=60.0,
                price_gap_pct=30.0, source="apify_export_markets", net_price_gap_pct=20.0, arbitrage_rank=50.0,
                confidence_score=40.0, price_benchmark_usd_per_mt=3000.0)
    base.update(kw)
    return EnrichedOpportunity(**base)


def _verified() -> CounterpartyValidation:
    return CounterpartyValidation(status="verified")


# ── Ranking and gap math ─────────────────────────────────────────────────────

class TestRanking:
    def test_suspect_gap_never_scores_the_maximum(self):
        assert ranking.is_suspect_gap(90.0) and not ranking.is_suspect_gap(60.0)
        plain = ranking.arbitrage_rank(64.0, 100.0)
        suspect = ranking.arbitrage_rank(64.0, 100.0, gap_suspect=True)
        assert plain == 80.0 and suspect == round((64.0 * CONFIG.suspect_gap_score) ** 0.5, 2)

    def test_net_gap_subtracts_duty_and_freight_not_vat(self):
        phase1 = svc._Phase1Result(gross_price_gap_pct=40.0, price_benchmark_usd_per_mt=1900.0)
        tariff = TariffData(hs_code="1", destination_country="DE", applied_duty_pct=5.0, gst_vat_pct=7.0,
                            total_landed_cost_add_pct=5.0, fetched_at=NOW)
        net, freight_pct = svc._net_gap(phase1, tariff, "DE")
        assert freight_pct == round(lc.freight_per_mt("DE") / 1900.0 * 100, 2)
        assert net == round(40.0 - 5.0 - freight_pct, 2)  # VAT (7 %) is not subtracted

    async def test_unverified_tariff_is_flagged_not_assumed(self, quotes):
        tariff = await svc._phase3_tariff_gate("090422", "DE", 30.0, CONFIG)
        assert tariff.applied_duty_pct == 0.0 and not tariff.verified
        assert tariff.total_landed_cost_add_pct == 0.0 and tariff.gst_vat_pct == 7.0

    async def test_assembled_opportunity_carries_corridor_facts_and_profit(self, quotes):
        phase1 = svc._Phase1Result(
            demand_index=64.0, gross_price_gap_pct=120.0, active_connectors=1, source="apify_export_markets",
            price_gap_measured=True, price_benchmark_usd_per_mt=3000.0, india_to_market_usd_per_mt=3000.0,
            india_world_fob_usd_per_mt=1364.0, import_growth_pct=8.0, india_share_pct=12.0,
            competitors=[{"country": "Vietnam", "share_pct": 40.0, "unit_value_usd_per_mt": 3300.0}],
            india_price_advantage_pct=9.1,
        )
        tariff = TariffData(hs_code="090422", destination_country="DE", applied_duty_pct=0.0, gst_vat_pct=7.0,
                            total_landed_cost_add_pct=0.0, verified=False, fetched_at=NOW)
        opp = await svc._phase5_assemble_and_persist("090422", "DE", phase1, svc._Phase2Result(), tariff,
                                                     [_seller()], CONFIG)
        assert opp is not None and opp.gap_suspect and "grade or origin" in opp.gap_note
        assert opp.india_share_pct == 12.0 and opp.competitors[0]["country"] == "Vietnam"
        assert {pf.PRICE_CHECK_NEEDED, pf.TARIFF_UNVERIFIED, pf.INDIA_CHEAPER} <= set(opp.flags)
        assert opp.profit is not None and opp.profit.supplier_price_basis == "india_export_average"
        assert opp.profit.buyer_price_basis == "india_to_market"


# ── Expected profit ──────────────────────────────────────────────────────────

class TestProfit:
    async def test_margin_per_tonne_container_and_expected_profit(self, quotes):
        opp = _opp(india_to_market_usd_per_mt=3000.0, buyer_signals=[_buyer(validation=_verified())],
                   seller_candidates=[_seller(estimated_fob_price_usd=2200.0)], best_match_score=70.0)
        p, flags = await pf.compute(opp)
        landed = lc.landed(2200.0, "DE", CONFIG.default_lot_mt, incoterm="CIF", margin_floor_pct=0.0)
        assert (p.buyer_price_basis, p.supplier_price_basis) == ("india_to_market", "supplier_quote")
        assert p.landed_cost_usd_per_mt == landed.cost_usd_per_mt
        assert p.margin_usd_per_mt == round(3000.0 - landed.cost_usd_per_mt, 2)
        assert p.margin_per_container_usd == round(p.margin_usd_per_mt * CONFIG.container_mt, 2)
        expected_p = CONFIG.p_close_floor + CONFIG.p_close_span * 0.70  # 1/1 buyers verified → ×1.0
        assert p.p_close == round(expected_p, 3)
        assert p.expected_profit_usd == round(p.margin_usd_per_mt * CONFIG.default_lot_mt * p.p_close, 2)
        assert p.meets_floor and pf.NO_SUPPLIER_PRICE not in flags and p.estimated

    async def test_buyer_quote_beats_benchmarks_and_ignores_flagged_buyers(self, quotes):
        opp = _opp(india_to_market_usd_per_mt=3000.0, buyer_signals=[
            _buyer(unit_price_usd=3400.0, price_unit="USD/MT"),
            _buyer(unit_price_usd=9999.0, price_unit="USD/MT", flags=[q.ROLE_SUSPECT]),
        ])
        assert await pf.buyer_price(opp) == (3400.0, "buyer_quote")

    async def test_price_book_then_mandi_then_india_average(self, quotes, monkeypatch):
        opp = _opp(india_world_fob_usd_per_mt=1500.0)
        assert (await pf.supplier_price(opp))[:2] == (1500.0, "india_export_average")
        quotes["mandi_prices"] = {"fob_estimate_usd_per_mt": 2100.0, "commodity": "Dry Chillies",
                                  "modal_inr_per_quintal": 15000.0, "markets": 12, "markup_pct": 15.0}
        price, basis, note = await pf.supplier_price(opp)
        assert (price, basis) == (2100.0, "mandi") and "12 mandis" in note
        await pricebook.record(pricebook.PriceObservation(hs_code="090422", side="supplier", kind="supplier_quote",
                                                          price_usd_per_mt=1900.0))
        assert (await pf.supplier_price(opp))[:2] == (1900.0, "price_book")

    async def test_product_mismatch_seller_price_is_not_used(self, quotes):
        opp = _opp(india_world_fob_usd_per_mt=1500.0,
                   seller_candidates=[_seller(estimated_fob_price_usd=500.0, flags=[q.PRODUCT_MISMATCH])])
        assert (await pf.supplier_price(opp))[1] == "india_export_average"

    async def test_below_floor_and_suspect_gap_halve_the_chance(self, quotes):
        opp = _opp(india_to_market_usd_per_mt=2400.0, gap_suspect=True,
                   seller_candidates=[_seller(estimated_fob_price_usd=2100.0)], best_match_score=50.0)
        p, flags = await pf.compute(opp)
        assert pf.BELOW_MARGIN_FLOOR in flags or pf.NEGATIVE_MARGIN in flags
        assert any("implausible" in b for b in p.p_close_basis)

    async def test_corridor_history_moves_the_chance(self, quotes, monkeypatch):
        rows = ([SimpleNamespace(kind="match", result="signed", hs_code="090422", market="DE")] * 4
                + [SimpleNamespace(kind="match", result="failed", hs_code="100630", market="AE")] * 8)
        monkeypatch.setattr(pf, "_outcomes", AsyncMock(return_value=rows))
        base = _opp(india_to_market_usd_per_mt=3000.0, seller_candidates=[_seller(estimated_fob_price_usd=2000.0)],
                    best_match_score=60.0)
        p, _ = await pf.compute(base)
        assert any("corridor history 4/4 signed" in b for b in p.p_close_basis)
        assert p.p_close > CONFIG.p_close_floor + CONFIG.p_close_span * 0.6 * 0.7

    async def test_no_prices_still_gives_a_chance_and_no_profit(self, quotes):
        p, flags = await pf.compute(_opp(price_benchmark_usd_per_mt=None))
        assert p.expected_profit_usd is None and p.p_close is not None and pf.NO_SUPPLIER_PRICE in flags

    async def test_high_value_product_lot_is_capped_by_order_value(self, quotes):
        opp = _opp(hs_code="091020", india_to_market_usd_per_mt=300_000.0,
                   seller_candidates=[_seller(estimated_fob_price_usd=180_000.0)])
        p, _ = await pf.compute(opp)
        assert p.lot_mt == round(CONFIG.max_lot_value_usd / 300_000.0, 3) and "capped" in p.lot_basis

    async def test_old_opportunity_derives_india_price_and_gets_the_gap_check(self, quotes):
        opp = _opp(price_benchmark_usd_per_mt=4430.0, price_gap_pct=142.08)  # no india_world_fob stored
        p, flags = await pf.compute(opp)
        assert p.supplier_price_basis == "india_export_average"
        assert p.supplier_price_usd_per_mt == round(4430.0 / 2.4208, 2)
        assert opp.gap_suspect and pf.PRICE_CHECK_NEEDED in flags and p.version == pf.PROFIT_VERSION

    async def test_list_recomputes_profit_from_an_older_version(self, quotes):
        repo = await repo_mod.get_repository()
        old = _opp(opportunity_id="OPP-V1", india_to_market_usd_per_mt=3000.0,
                   seller_candidates=[_seller(estimated_fob_price_usd=2000.0)])
        old.profit = pf.OpportunityProfit(expected_profit_usd=1.0, version=1)
        await repo.save(old)
        with patch("services.deals.repository.get_repository", side_effect=RuntimeError("no deals")):
            listed = await svc.list_opportunities()
        assert listed[0].profit.version == pf.PROFIT_VERSION and listed[0].profit.expected_profit_usd != 1.0

    def test_sort_key_puts_unknown_profit_last(self):
        a = _opp(opportunity_id="a", arbitrage_rank=90.0)
        b = _opp(opportunity_id="b", arbitrage_rank=10.0)
        b.profit = pf.OpportunityProfit(expected_profit_usd=5000.0)
        assert sorted([a, b], key=pf.sort_key, reverse=True)[0].opportunity_id == "b"
        assert pf.sort_key(SimpleNamespace(arbitrage_rank=3.0)) == (False, float("-inf"), 3.0)

    async def test_list_backfills_profit_sorts_and_shows_running_deals(self, quotes, monkeypatch):
        repo = await repo_mod.get_repository()
        low = _opp(opportunity_id="OPP-LOW", hs_code="090422", market="DE", arbitrage_rank=95.0,
                   india_to_market_usd_per_mt=2300.0, seller_candidates=[_seller(estimated_fob_price_usd=2000.0)])
        high = _opp(opportunity_id="OPP-HIGH", hs_code="091030", market="GB", arbitrage_rank=20.0,
                    india_to_market_usd_per_mt=3000.0, seller_candidates=[_seller(estimated_fob_price_usd=1500.0)])
        await repo.save(low)
        await repo.save(high)
        deal = SimpleNamespace(deal_id="DEAL-1", status="NEGOTIATING", hs_code="091030", market="GB",
                               opportunity_id="OPP-OLD")
        done = SimpleNamespace(deal_id="DEAL-2", status="PAID", hs_code="091030", market="GB", opportunity_id=None)
        fake_repo = SimpleNamespace(list_active=AsyncMock(return_value=[deal, done]))
        with patch("services.deals.repository.get_repository", return_value=fake_repo):
            listed = await svc.list_opportunities()
        assert [o.opportunity_id for o in listed] == ["OPP-HIGH", "OPP-LOW"]
        assert listed[0].corridor_deals == [{"deal_id": "DEAL-1", "status": "NEGOTIATING", "opportunity_id": "OPP-OLD"}]
        assert (await repo.get("OPP-LOW")).profit is not None  # backfilled and saved


# ── Counterparty quality ─────────────────────────────────────────────────────

class TestQuality:
    @pytest.mark.parametrize("kw", [
        {"company_id": "naturalspicesllp.in"},
        {"buyer_name": "Natural Spices LLP"},
        {"phone": "+91 98200 00000"},
        {"fit_reason": "Leading exporters of spices from India since 1990"},
    ])
    def test_indian_exporter_as_buyer_is_flagged_and_blocked(self, kw):
        b = _buyer(**kw)
        assert q.check_buyers([b], "DE") == 1 and q.ROLE_SUSPECT in b.flags
        pair = pm.score_pair(b, 0, _seller(), _opp())
        assert pair.score == 0 and any("Indian exporter" in x for x in pair.blockers)

    def test_real_importer_is_not_flagged(self):
        b = _buyer(buyer_name="Gewürz Mayer GmbH", company_id="gewuerzmayer.de", phone="+49 89 1234",
                   fit_reason="Importer and wholesaler of spices in Germany")
        assert q.check_buyers([b], "DE") == 0 and not b.flags

    def test_seller_about_another_product_is_flagged(self):
        cumin = _seller(company_name="VORA SPICE MILLS LLP", fit_reason="Cumin exporters in India")
        chilli = _seller(candidate_id="s2", company_name="Red Chilli Exports", fit_reason="Chilli powder manufacturer")
        mixed = _seller(candidate_id="s3", company_name="Masala Co", fit_reason="Cumin, turmeric and chilli powder")
        assert q.check_sellers([cumin, chilli, mixed], "chilli powder") == 1
        assert q.PRODUCT_MISMATCH in cumin.flags and not chilli.flags and not mixed.flags
        pair = pm.score_pair(_buyer(), 0, cumin, _opp())
        assert next(f for f in pair.factors if f.key == "product_fit").score == 30.0

    def test_red_pepper_is_chilli_not_pepper(self):
        assert q.products_named("Red pepper flakes") == {"chilli"}
        assert q.products_named("Black pepper and red pepper") == {"pepper", "chilli"}

    def test_supplier_town_and_port_from_address(self):
        s = _seller(address="Plot 12, GIDC, Unjha, Gujarat 384170, India")
        q.check_sellers([s], "cumin seeds")
        assert s.location_state == "Gujarat" and s.supplier_to_port_km is not None

    def test_match_seed_skips_flagged_buyers(self):
        opp = _opp(buyer_signals=[_buyer(buyer_name="Real Importer"), _buyer(buyer_name="Spices LLP", flags=[q.ROLE_SUSPECT])],
                   seller_candidates=[_seller(flags=[q.PRODUCT_MISMATCH]), _seller(candidate_id="s2", company_name="Good")])
        seed = svc._build_match_seed(opp)
        assert [b["buyer_name"] for b in seed.top_buyers] == ["Real Importer"]
        assert seed.top_sellers[0]["supplier_id"] == "s2"


# ── Names ────────────────────────────────────────────────────────────────────

class TestNames:
    @pytest.mark.parametrize(("label", "expected"), [
        ("jimmyandsons", "Jimmy & Sons"), ("naturalspicesllp", "Natural Spices LLP"),
        ("pepperiaexports", "Pepperia Exports"), ("andhraspices", "Andhra Spices"), ("biova", "Biova"),
    ])
    def test_domain_label_split(self, label, expected):
        assert ap.split_domain_label(label) == expected

    def test_company_inside_a_sentence(self):
        row = {"domain": "voraspices.com", "legal_name": "As Cumin Exporters in India, our motto at VORA SPICE MILLS LLP"}
        assert ap._company_name(row) == "VORA SPICE MILLS LLP" and ap.company_name_source(row) == "site"

    def test_name_source_domain(self):
        assert ap.company_name_source({"domain": "jimmyandsons.in"}) == "domain"
        rows = ap.refine_companies([{"domain": "jimmyandsons.in", "email": "x@jimmyandsons.in"}])
        assert rows[0]["name_source"] == "domain" and rows[0]["company_name"] == "Jimmy & Sons"


# ── Scan report ──────────────────────────────────────────────────────────────

class TestScanReport:
    async def test_scan_reports_every_corridor_with_its_reason(self, quotes):
        quotes["apify_export_markets"] = {"markets": [{
            "market": "AE", "import_value_usd": 1e8, "unit_value_usd_per_mt": 1300.0,
            "exporter_unit_value_usd_per_mt": 1300.0, "exporter_world_unit_value_usd_per_mt": 1000.0, "score": 70}]}
        await svc.scan_opportunities(ScanRequest(hs_codes=["100630"], target_markets=["AE", "DE"],
                                                 worldwide_search=False))
        report = await sr.latest()
        assert report is not None and report.finished_at is not None
        by_market = {c.market: c for c in report.corridors}
        assert by_market["AE"].result == "opportunity" and by_market["AE"].opportunity_id
        assert by_market["DE"].result == "dropped" and "no live demand" in by_market["DE"].reason
        assert (report.opportunities, report.dropped) == (1, 1)

    def test_record_outside_a_scan_is_a_no_op(self):
        sr.record("1", "AE", "dropped", "x")
        assert sr.current() is None

    async def test_report_endpoint(self, quotes):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from services.trade_min.router import router
        token = sr.start()
        sr.record("100630", "DE", "dropped", "net price gap 0.5% after duty and freight is below the 1% floor")
        await sr.finish(token)
        app = FastAPI()
        app.include_router(router)
        body = TestClient(app).get("/trade-min/scan/report").json()
        assert body["dropped"] == 1 and body["corridors"][0]["reason"].startswith("net price gap")


# ── Mandi prices ─────────────────────────────────────────────────────────────

class TestMandi:
    def test_commodity_mapping(self):
        assert mandi.commodity_for("090422") == "Dry Chillies"
        assert mandi.commodity_for("091030") == "Turmeric"
        assert mandi.commodity_for("290543") is None

    def test_summary_converts_quintal_to_usd_per_mt_with_markup(self, monkeypatch):
        monkeypatch.setattr(mandi, "_usd_inr", lambda: 80.0)
        out = mandi.summarise([{"modal_price": "16000", "arrival_date": "16/11/2026"},
                               {"modal_price": "18000", "arrival_date": "17/11/2026"},
                               {"modal_price": "bad"}], "Dry Chillies")
        assert out["modal_inr_per_quintal"] == 17000.0 and out["markets"] == 2
        assert out["mandi_usd_per_mt"] == 2125.0 and out["fob_estimate_usd_per_mt"] == 2443.75
        assert mandi.summarise([], "X") == {}

    async def test_no_key_no_network(self, monkeypatch):
        called = AsyncMock()
        monkeypatch.setattr(httpx.AsyncClient, "get", called)
        assert await mandi.MandiPriceConnector().quote({"hs_code": "090422"}) == {}
        called.assert_not_called()

    async def test_live_call_with_key(self, monkeypatch):
        monkeypatch.setenv("DATA_GOV_IN_API_KEY", "k")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"records": [{"modal_price": "20000", "arrival_date": "17/11/2026"}]})

        real = httpx.AsyncClient
        monkeypatch.setattr(mandi.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        monkeypatch.setattr(mandi, "live_usd_inr", AsyncMock(return_value=(88.0, "live")))
        out = await mandi.MandiPriceConnector().quote({"hs_code": "091030"})
        assert seen["params"]["filters[commodity]"] == "Turmeric" and out["markets"] == 1
        assert (out["usd_inr"], out["usd_inr_source"]) == (88.0, "live")


# ── Buyer introduction ───────────────────────────────────────────────────────

class TestOutreachLine:
    async def test_india_cheaper_line_only_from_measured_data(self):
        from shared.orchestration import outreach
        repo = await repo_mod.get_repository()
        await repo.save(_opp(opportunity_id="OPP-CHEAP", india_price_advantage_pct=12.0,
                             competitors=[{"country": "Vietnam", "share_pct": 40.0, "unit_value_usd_per_mt": 3400.0}]))
        await repo.save(_opp(opportunity_id="OPP-DEAR", india_price_advantage_pct=-20.0,
                             competitors=[{"country": "Vietnam", "share_pct": 40.0, "unit_value_usd_per_mt": 2000.0}]))
        deal = SimpleNamespace(deal_id="D1", hs_code="090422", opportunity_id="OPP-CHEAP", quantity_mt=None)
        line = await outreach.market_fact(deal)
        assert line and "12% below Vietnam" in line
        assert await outreach.market_fact(SimpleNamespace(deal_id="D2", hs_code="090422", opportunity_id="OPP-DEAR")) is None
        assert await outreach.market_fact(SimpleNamespace(deal_id="D3", hs_code="090422", opportunity_id=None)) is None
        cand = SimpleNamespace(candidate_id="C1", legal_name="Gulf Foods", pitch_angle=None, quantity_mt=None)
        _, body = outreach.draft(deal, cand, market_line=line)
        assert line in body
