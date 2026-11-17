"""Opportunity Radar → prospect hand-off and the truer profit number (to-do area 16):
ready-to-start checklist, export policy / news / destination requirements before a deal,
data year, export incentives, freight per lane, container per product, profit range,
mandi history trend, yearly potential, what's missing, estimate-vs-outcome correction,
scan cost, and the radar's numbers carried into the deal."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.trade_min import accuracy
from services.trade_min import directory as dr
from services.trade_min import freight_lanes as fl
from services.trade_min import incentives as inc
from services.trade_min import mandi_history as mh
from services.trade_min import profit as pf
from services.trade_min import repository as repo_mod
from services.trade_min import scan_reports as sr
from services.trade_min import service as svc
from services.trade_min import start_check as sc
from services.trade_min.config import DEFAULT_CONFIG as CONFIG
from services.trade_min.schemas import (
    BuyerSignal,
    CounterpartyValidation,
    EnrichedOpportunity,
    ScanRequest,
    SellerCandidate,
    TariffData,
)
from shared import catalogue, pricebook
from shared.connectors import apify as ap
from shared.connectors import mandi
from shared.orchestration import landed_cost as lc

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    repo_mod._repository = repo_mod.InMemoryFallbackRepository()
    dr.reset_directory()
    for mod in (sr, pf, inc, fl, mh, accuracy, pricebook):
        mod.reset_for_tests()
    monkeypatch.setattr(svc, "get_publisher", lambda: MagicMock(publish=AsyncMock()))
    monkeypatch.setattr(accuracy, "_deals", AsyncMock(return_value=[]))
    monkeypatch.delenv("DATA_GOV_IN_API_KEY", raising=False)
    monkeypatch.delenv("COGOPORT_API_KEY", raising=False)
    saved = catalogue._state.model_copy(deep=True)
    yield
    catalogue._state = saved
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
    base = dict(buyer_name="Gulf Foods GmbH", buyer_country="DE", signal_type="web_search",
                source="apify_company_search", urgency_tier=1, email="buy@gulf.de", email_status="ok")
    base.update(kw)
    return BuyerSignal(**base)


def _seller(**kw) -> SellerCandidate:
    base = dict(candidate_id="s1", company_name="Kerala Spices", location_city="", location_state="",
                hs_code="090422", source="apify_company_search", email="sales@ks.in")
    base.update(kw)
    return SellerCandidate(**base)


def _opp(**kw) -> EnrichedOpportunity:
    base = dict(opportunity_id="OPP-1", hs_code="090422", market="DE", scanned_at=NOW, demand_index=60.0,
                price_gap_pct=30.0, source="apify_export_markets", net_price_gap_pct=20.0, arbitrage_rank=50.0,
                confidence_score=40.0, price_benchmark_usd_per_mt=3000.0, india_to_market_usd_per_mt=3000.0)
    base.update(kw)
    return EnrichedOpportunity(**base)


def _line(**kw) -> catalogue.ProductLine:
    base = dict(product_id="P1", hs_code="090422", name="chilli powder", markets=["DE"])
    base.update(kw)
    return catalogue.ProductLine(**base)


# ── 16.1 ready-to-start checklist ────────────────────────────────────────────

class TestChecklist:
    def _status(self, opp, key):
        return next(c for c in sc.checklist(opp) if c["key"] == key)

    def test_no_reachable_buyer_fails(self):
        opp = _opp(buyer_signals=[_buyer(email=None), _buyer(email="x@y.in", flags=["role_suspect"])],
                   seller_candidates=[_seller()])
        c = self._status(opp, "buyers")
        assert c["status"] == "fail" and "send nothing" in c["detail"]

    def test_enough_buyers_and_suppliers_pass(self):
        opp = _opp(buyer_signals=[_buyer(), _buyer(buyer_name="B2", email="b2@x.de")],
                   seller_candidates=[_seller(), _seller(candidate_id="s2", email=None, phone="+91")])
        assert self._status(opp, "buyers")["status"] == "ok"
        assert self._status(opp, "suppliers")["status"] == "ok"

    def test_failed_or_other_product_suppliers_do_not_count(self):
        opp = _opp(seller_candidates=[_seller(flags=["product_mismatch"]),
                                      _seller(candidate_id="s2", validation=CounterpartyValidation(status="failed"))])
        assert self._status(opp, "suppliers")["status"] == "fail"

    def test_policy_news_margin_duty_running_and_destination(self):
        opp = _opp(export_policy={"severity": "review", "message": "HS 090422: not confirmed"},
                   market_check={"verdict": "avoid", "summary": "export ban rumours"},
                   tariff_data=TariffData(hs_code="090422", destination_country="DE", applied_duty_pct=0,
                                          total_landed_cost_add_pct=0, verified=False, fetched_at=NOW),
                   corridor_deals=[{"deal_id": "D1", "status": "MATCHING"}],
                   import_requirements=["EU pesticide MRL certificate"])
        opp.profit = pf.OpportunityProfit(margin_usd_per_mt=-10.0, margin_pct=-0.3)
        got = {c["key"]: c["status"] for c in sc.checklist(opp)}
        assert got["export_policy"] == "warn" and got["news"] == "fail" and got["margin"] == "fail"
        assert got["duty"] == "warn" and got["running_deal"] == "warn" and got["destination"] == "warn"

    def test_blocked_export_policy_fails(self):
        opp = _opp(export_policy={"severity": "block", "message": "prohibited", "destination_severity": None})
        assert self._status(opp, "export_policy")["status"] == "fail" and sc.blocked(opp)


# ── 16.2 what can stop a deal ────────────────────────────────────────────────

class TestStartCheck:
    async def test_embargoed_market_is_dropped_at_scan_without_searching(self, quotes):
        quotes["apify_export_markets"] = {"markets": [{"market": "IR", "unit_value_usd_per_mt": 2000.0,
                                                       "exporter_world_unit_value_usd_per_mt": 1000.0, "score": 70}]}
        phase1 = AsyncMock()
        with patch.object(svc, "_phase1_historical_demand", phase1):
            await svc.scan_opportunities(ScanRequest(hs_codes=["090422"], target_markets=["IR"], worldwide_search=False))
        phase1.assert_not_called()
        report = await sr.latest()
        assert report.corridors[0].result == "dropped" and "embargo" in report.corridors[0].reason

    async def test_prohibited_product_is_never_listed(self, quotes):
        repo = await repo_mod.get_repository()
        await repo.save(_opp(opportunity_id="OPP-OK"))
        await repo.save(_opp(opportunity_id="OPP-IR", market="IR"))
        with patch("services.deals.repository.get_repository", side_effect=RuntimeError("no deals")):
            listed = await svc.list_opportunities()
        assert [o.opportunity_id for o in listed] == ["OPP-OK"]
        assert listed[0].readiness and listed[0].export_policy["status"] == "unverified"

    async def test_news_verdict_from_cache_lowers_the_chance(self, quotes, monkeypatch):
        from shared.orchestration import market_check
        monkeypatch.setenv("MARKET_CHECK_ENABLED", "true")
        cached = market_check.MarketCheck(key="090422:DE", hs_code="090422", market="DE", product="chilli",
                                          verdict="avoid", summary="ban", checked_at=NOW)
        monkeypatch.setattr(market_check, "_cached", AsyncMock(return_value=cached))
        run = AsyncMock()
        monkeypatch.setattr(market_check, "run", run)
        opp = await sc.annotate(_opp(seller_candidates=[_seller(estimated_fob_price_usd=2000.0)]))
        run.assert_not_called()  # list reads the cache only
        assert opp.market_check["verdict"] == "avoid"
        p, flags = await pf.compute(opp)
        assert pf.NEWS_AVOID in flags and any("avoid" in b for b in p.p_close_basis)

    async def test_news_check_runs_on_refresh_when_not_cached(self, monkeypatch):
        from shared.orchestration import market_check
        monkeypatch.setenv("MARKET_CHECK_ENABLED", "true")
        monkeypatch.setattr(market_check, "_cached", AsyncMock(return_value=None))
        fresh = market_check.MarketCheck(key="k", hs_code="090422", market="DE", product="p", verdict="go", checked_at=NOW)
        monkeypatch.setattr(market_check, "run", AsyncMock(return_value=fresh))
        assert (await sc.market_news("090422", "DE", refresh=True))["verdict"] == "go"
        assert await sc.market_news("090422", "DE", refresh=False) is None

    def test_destination_requirements_from_catalogue(self):
        catalogue._state = catalogue.Catalogue(products=[_line(import_requirements={
            "US": ["FDA food-facility registration"], "*": ["phytosanitary certificate"]})])
        assert sc.import_requirements("090422", "US") == ["FDA food-facility registration", "phytosanitary certificate"]
        assert sc.import_requirements("090422", "DE") == ["phytosanitary certificate"]

    async def test_data_year_and_flags_are_kept(self, quotes):
        quotes["apify_export_markets"] = {"markets": [{
            "market": "DE", "import_value_usd": 1e7, "unit_value_usd_per_mt": 3000.0,
            "exporter_unit_value_usd_per_mt": 3000.0, "exporter_world_unit_value_usd_per_mt": 2500.0,
            "latest_period": "2023", "flags": ["stale_period", "fast_growing"], "score": 60}]}
        phase1 = await svc._phase1_historical_demand("090422", "DE", CONFIG)
        assert phase1.data_period == "2023" and phase1.market_data_flags == ["stale_period", "fast_growing"]
        opp = _opp(data_period="2023", market_data_flags=["stale_period"])
        p, flags = await pf.compute(opp)
        assert pf.STALE_DATA in flags and any(m["what"] == "Prices from 2023" for m in p.missing)


# ── 16.3 a truer profit number ───────────────────────────────────────────────

class TestTruerProfit:
    async def test_incentive_counted_only_when_confirmed(self, quotes):
        opp = _opp(seller_candidates=[_seller(estimated_fob_price_usd=2000.0)])
        base, _ = await pf.compute(opp)
        await inc.set_entry(inc.ExportIncentive(hs_code="090422", rodtep_pct=1.5, drawback_pct=1.0))
        unconfirmed, _ = await pf.compute(opp)
        assert unconfirmed.incentive_usd_per_mt is None and "not confirmed" in unconfirmed.incentive_basis
        await inc.set_entry(inc.ExportIncentive(hs_code="0904", rodtep_pct=1.5, drawback_pct=1.0,
                                                rodtep_cap_usd_per_mt=20.0, eligible_confirmed=True, source="Appendix 4R"))
        inc._memory.pop("090422")  # the 4-digit entry now covers the code
        confirmed, _ = await pf.compute(opp)
        assert confirmed.incentive_usd_per_mt == 20.0 + 20.0  # RoDTEP capped at 20, drawback 1 % of 2000
        assert confirmed.margin_usd_per_mt == round(base.margin_usd_per_mt + 40.0, 2)

    async def test_lane_quote_replaces_the_regional_estimate(self, quotes):
        opp = _opp(seller_candidates=[_seller(estimated_fob_price_usd=2000.0)])
        est, _ = await pf.compute(opp)
        assert est.freight_basis == "region_estimate"
        await fl.save(fl.LaneRate(market="DE", usd_per_container=1000.0, container_mt=20.0))
        quoted, _ = await pf.compute(opp)
        assert quoted.freight_usd_per_mt == 50.0 and quoted.freight_basis.startswith("lane_quote (staff")
        old = fl.LaneRate(market="DE", usd_per_container=1000.0, quoted_at=NOW - timedelta(days=30))
        await fl.save(old)
        assert await fl.lane("DE") is None  # too old: back to the estimate

    async def test_reefer_estimate_and_container_per_product(self, quotes):
        catalogue._state = catalogue.Catalogue(products=[_line(container_mt=16.0, container_type="reefer")])
        opp = _opp(seller_candidates=[_seller(estimated_fob_price_usd=2000.0)])
        p, _ = await pf.compute(opp)
        assert (p.container_mt, p.container_type, p.lot_mt) == (16.0, "reefer", 16.0)
        assert p.freight_usd_per_mt == round(lc.freight_per_mt("DE") * fl.reefer_factor(), 2)
        assert p.margin_per_container_usd == round(p.margin_usd_per_mt * 16.0, 2)

    async def test_lane_refresh_only_with_a_real_key(self, monkeypatch):
        conn = SimpleNamespace(quote=AsyncMock(return_value={"rate_usd": 1800.0}))
        with patch("shared.connectors.registry.get_connector", return_value=conn):
            assert await fl.refresh("DE", "090422") is None  # no key: never sent
            conn.quote.assert_not_called()
            monkeypatch.setenv("COGOPORT_API_KEY", "your_cogoport_api_key_here")
            assert await fl.refresh("DE", "090422") is None  # placeholder: never sent
            monkeypatch.setenv("COGOPORT_API_KEY", "real-key")
            rate = await fl.refresh("DE", "090422")
        assert rate.usd_per_mt == 90.0 and rate.provider == "cogoport"

    async def test_stub_quote_is_not_stored(self, monkeypatch):
        monkeypatch.setenv("COGOPORT_API_KEY", "real-key")
        conn = SimpleNamespace(quote=AsyncMock(return_value={"rate_usd": 2850.0, "stub": True}))
        with patch("shared.connectors.registry.get_connector", return_value=conn):
            assert await fl.refresh("DE", "090422") is None

    async def test_profit_range_from_supplier_spread(self, quotes):
        opp = _opp(seller_candidates=[_seller(estimated_fob_price_usd=1800.0),
                                      _seller(candidate_id="s2", estimated_fob_price_usd=2200.0)])
        p, _ = await pf.compute(opp)
        assert p.expected_profit_low_usd < p.expected_profit_usd < p.expected_profit_high_usd
        assert "1,800–2,200" in p.range_basis

    async def test_yearly_potential(self, quotes):
        opp = _opp(import_value_usd=30_000_000.0, market_avg_usd_per_mt=3000.0,
                   seller_candidates=[_seller(estimated_fob_price_usd=2000.0)])
        p, _ = await pf.compute(opp)
        tonnes = min(10_000 * CONFIG.reachable_share_pct / 100.0, p.lot_mt * 12)
        assert p.annual_potential_usd == round(p.margin_usd_per_mt * tonnes * p.p_close, 2)

    async def test_missing_tips_name_where_to_get_each_fact(self, quotes):
        opp = _opp(india_world_fob_usd_per_mt=2000.0,
                   tariff_data=TariffData(hs_code="090422", destination_country="DE", applied_duty_pct=0,
                                          total_landed_cost_add_pct=0, verified=False, fetched_at=NOW))
        p, _ = await pf.compute(opp)
        what = {m["what"] for m in p.missing}
        assert {"No supplier price", "No buyer price", "Duty unverified", "Freight is an estimate",
                "Export incentives not counted"} <= what
        assert any("DATA_GOV_IN_API_KEY" in m["how"] for m in p.missing)

    async def test_mandi_range_live_rate_and_history(self, quotes):
        quotes["mandi_prices"] = mandi.summarise(
            [{"modal_price": "16000", "min_price": "15000", "max_price": "17000", "arrival_date": "17/11/2026"}],
            "Dry Chillies", usd_inr=80.0, usd_inr_source="live")
        p, _ = await pf.compute(_opp())
        assert p.supplier_price_basis == "mandi" and (p.usd_inr, p.usd_inr_source) == (80.0, "live")
        assert p.supplier_price_low_usd_per_mt < p.supplier_price_usd_per_mt < p.supplier_price_high_usd_per_mt
        assert (await mh.points("Dry Chillies"))[0]["modal_inr_per_quintal"] == 16000.0

    async def test_mandi_trend(self):
        today = date.today()
        for i, price in enumerate([10000, 10100, 10200, 11200, 11300]):
            await mh.record({"commodity": "Turmeric", "modal_inr_per_quintal": price}, day=today - timedelta(days=20 - i * 5))
        t = await mh.trend("Turmeric")
        assert t["direction"] == "rising" and t["change_pct"] > 3 and "buy now" in t["advice"]
        assert mh.trend_of([{"day": today.isoformat(), "modal_inr_per_quintal": 1}]) is None


# ── 16.1 estimate vs outcome ─────────────────────────────────────────────────

class TestAccuracy:
    def _deal(self, i, ratio, hs="090422"):
        return SimpleNamespace(deal_id=f"D{i}", hs_code=hs, market="DE", quantity_mt=20.0,
                               radar_estimate={"margin_usd_per_mt": 100.0}, realized_margin_usd=2000.0 * ratio)

    def test_compare(self):
        out = accuracy.compare([self._deal(1, 0.8), self._deal(2, 0.6), SimpleNamespace(
            deal_id="D3", hs_code="090422", market="DE", quantity_mt=20.0, radar_estimate=None, realized_margin_usd=5.0)])
        assert [r["ratio"] for r in out["rows"]] == [0.8, 0.6] and out["by_hs"]["090422"]["median_ratio"] == 0.7

    async def test_factor_applied_after_enough_deals(self, quotes, monkeypatch):
        monkeypatch.setattr(accuracy, "_deals", AsyncMock(return_value=[self._deal(i, 0.7) for i in range(3)]))
        opp = _opp(seller_candidates=[_seller(estimated_fob_price_usd=2000.0)])
        p, _ = await pf.compute(opp)
        assert p.correction_factor == 0.7 and "3 settled deals" in p.correction_basis


# ── carried into the deal, scan cost ─────────────────────────────────────────

class TestHandOff:
    def test_seed_carries_profit_lot_and_flags(self):
        opp = _opp(buyer_signals=[_buyer()], flags=["no_supplier_price"])
        opp.profit = pf.OpportunityProfit(buyer_price_usd_per_mt=3000.0, buyer_price_basis="india_to_market", lot_mt=18.0)
        seed = svc._build_match_seed(opp)
        assert seed.profit["buyer_price_usd_per_mt"] == 3000.0 and seed.lot_mt == 18.0
        assert seed.flags == ["no_supplier_price"]

    def test_deal_keeps_the_radar_estimate_and_uses_its_lot(self, monkeypatch):
        import asyncio
        from services.deals import service as deals
        from services.deals.repository import InMemoryFallbackRepository
        from services.deals.schemas import CreateDealRequest
        repo = InMemoryFallbackRepository()
        monkeypatch.setattr("services.deals.service.get_repository", lambda: repo)
        seed = {"buyer_target_price_usd": None, "top_sellers": [], "lot_mt": 18.0, "flags": ["price_check_needed"],
                "profit": {"buyer_price_usd_per_mt": 3100.0, "buyer_price_basis": "price_book",
                           "margin_usd_per_mt": 400.0, "expected_profit_usd": 2000.0}}
        with (patch.object(deals, "_rescore_opportunity", AsyncMock()),
              patch.object(deals, "_fetch_match_seed", AsyncMock(return_value=seed)),
              patch.object(deals, "_fetch_opportunity", AsyncMock(return_value=None)),
              patch.object(deals, "_mark_opportunity_converted", AsyncMock()),
              patch.object(deals, "_emit_event", AsyncMock())):
            deal = asyncio.get_event_loop().run_until_complete(deals.create_deal(
                CreateDealRequest(operator_id="u1", hs_code="090422", market="DE", opportunity_id="OPP-1"), org_id="o"))
        assert deal.quantity_mt == 18.0 and deal.buyer_target_price_usd == 3100.0
        assert deal.buyer_price_source == "radar:price_book"
        assert deal.radar_estimate["expected_profit_usd"] == 2000.0 and deal.radar_estimate["opportunity_id"] == "OPP-1"
        assert deal.radar_estimate["flags"] == ["price_check_needed"]

    def test_cost_meter_counts_runs_and_usd(self):
        import contextvars
        def inner():
            meter = ap.start_cost_meter()
            ap._meter_run({"usageTotalUsd": 0.12})
            ap._meter_run({"usageTotalUsd": "bad"})
            ap._meter_run(None)
            return meter
        meter = contextvars.copy_context().run(inner)
        assert meter == {"runs": 3, "usd": 0.12}
        ap._meter_run({"usageTotalUsd": 1})  # outside a meter: no-op

    async def test_incentive_and_lane_endpoints(self, quotes):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from services.trade_min.router import _staff, router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@portless.in", is_portless=True)
        client = TestClient(app)
        r = client.put("/trade-min/incentives/090422", json={"rodtep_pct": 1.2, "eligible_confirmed": True,
                                                              "source": "Appendix 4R"})
        assert r.status_code == 200 and r.json()["confirmed_by"] == "ops@portless.in"
        assert client.get("/trade-min/incentives").json()[0]["hs_code"] == "090422"
        r = client.put("/trade-min/freight-lanes", json={"market": "de", "usd_per_container": 1600})
        assert r.status_code == 200 and r.json()["usd_per_mt"] == 80.0 and r.json()["market"] == "DE"
        assert client.get("/trade-min/freight-lanes").json()[0]["provider"] == "staff:ops@portless.in"
        assert client.get("/trade-min/profit/accuracy").json()["rows"] == []
