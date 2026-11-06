"""Buyer / supplier directory (services/trade_min/directory.py): every counterparty
a scan finds is kept — even when the corridor yields no opportunity or deal."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from services.trade_min import directory, service as svc
from services.trade_min.schemas import BuyerSignal, SellerCandidate, TariffData


@pytest.fixture(autouse=True)
def _fresh_directory():
    directory.reset_directory()
    yield
    directory.reset_directory()


def _buyer(name="Al Turab Foods", website="https://www.alturabfood.com/", email="purchase@alturabfood.com", **kw):
    return BuyerSignal(buyer_name=name, buyer_country="AE", signal_type="web_search", source="apify_company_search",
                       urgency_tier=1, website=website, email=email, **kw)


def _seller(name="Jashn Foods", website="https://www.jashnfoods.com/", email="info@jashnfoods.com"):
    return SellerCandidate(candidate_id="jashnfoods.com", company_name=name, location_city="", location_state="",
                           hs_code="100630", source="apify_company_search", website=website, email=email)


class TestRecordScan:
    @pytest.mark.asyncio
    async def test_records_buyers_and_suppliers_without_an_opportunity(self):
        n_b, n_s = await directory.record_scan("100630", "AE", [_buyer()], [_seller()],
                                               outcome="no opportunity: tariff gate")
        assert (n_b, n_s) == (1, 1)
        buyers = await directory.list_counterparties("buyer")
        suppliers = await directory.list_counterparties("supplier")
        assert buyers[0].id == "buyer:alturabfood.com" and buyers[0].email == "purchase@alturabfood.com"
        assert buyers[0].last_outcome == "no opportunity: tariff gate" and buyers[0].opportunity_ids == []
        assert suppliers[0].name == "Jashn Foods" and suppliers[0].country == "IN" and suppliers[0].markets == ["AE"]

    @pytest.mark.asyncio
    async def test_merges_the_same_company_across_scans(self):
        await directory.record_scan("100630", "AE", [_buyer(phone=None)], [], outcome="x")
        await directory.record_scan("100630", "SA", [_buyer(email=None, phone="+971 4 000")], [],
                                    outcome="opportunity OPP-1", opportunity_id="OPP-1")
        [b] = await directory.list_counterparties("buyer")
        assert b.times_seen == 2 and b.email == "purchase@alturabfood.com" and b.phone == "+971 4 000"
        assert b.opportunity_ids == ["OPP-1"] and b.last_outcome == "opportunity OPP-1"

    @pytest.mark.asyncio
    async def test_skips_placeholders_without_contact(self):
        placeholder = BuyerSignal(buyer_name="Alibaba Buyer (AE)", buyer_country="AE", signal_type="rfq",
                                  source="alibaba_rfq", urgency_tier=3)
        assert await directory.record_scan("100630", "AE", [placeholder], [], outcome="x") == (0, 0)

    @pytest.mark.asyncio
    async def test_filters(self):
        await directory.record_scan("100630", "AE", [_buyer()], [], outcome="x")
        await directory.record_scan("090411", "US", [_buyer("Spice Importers Inc", "https://spice.example",
                                                            "buy@spice.example")], [], outcome="x")
        assert [b.name for b in await directory.list_counterparties("buyer", market="us")] == ["Spice Importers Inc"]
        assert [b.name for b in await directory.list_counterparties("buyer", hs_code="100630")] == ["Al Turab Foods"]
        assert [b.name for b in await directory.list_counterparties("buyer", q="SPICE")] == ["Spice Importers Inc"]


class TestScanWiring:
    @pytest.mark.asyncio
    async def test_tariff_discarded_corridor_still_records_counterparties(self):
        phase1 = svc._Phase1Result()
        phase1.source = "apify_export_markets"     # live data, measured gap — dropped by duty
        phase1.price_gap_measured = True
        phase1.gross_price_gap_pct = 5.0
        phase2 = svc._Phase2Result()
        phase2.buyer_signals = [_buyer()]
        tariff = TariffData(hs_code="100630", destination_country="AE", applied_duty_pct=50.0,
                            total_landed_cost_add_pct=50.0, fetched_at=datetime.now(timezone.utc))

        async def p1(*a, **k): return phase1
        async def p2(*a, **k): return phase2
        async def p3(*a, **k): return tariff
        async def p4(*a, **k): return [_seller()]

        with patch.object(svc, "_phase1_historical_demand", p1), patch.object(svc, "_phase2_live_signals", p2), \
             patch.object(svc, "_phase3_tariff_gate", p3), patch.object(svc, "_phase4_seller_discovery", p4):
            assert await svc._scan_one("100630", "AE", svc.DEFAULT_CONFIG) is None
        [b] = await directory.list_counterparties("buyer")
        [s] = await directory.list_counterparties("supplier")
        assert "no opportunity" in b.last_outcome and s.name == "Jashn Foods"


class TestNoSimulation:
    @pytest.mark.asyncio
    async def test_no_live_demand_data_no_opportunity_but_counterparties_kept(self):
        async def p1(*a, **k): return svc._Phase1Result()          # no source answered
        async def p2(*a, **k):
            r = svc._Phase2Result()
            r.buyer_signals = [_buyer()]
            return r
        async def p4(*a, **k): return [_seller()]
        tariff_called = False

        async def p3(*a, **k):
            nonlocal tariff_called
            tariff_called = True

        with patch.object(svc, "_phase1_historical_demand", p1), patch.object(svc, "_phase2_live_signals", p2), \
             patch.object(svc, "_phase3_tariff_gate", p3), patch.object(svc, "_phase4_seller_discovery", p4):
            assert await svc._scan_one("100630", "AE", svc.DEFAULT_CONFIG) is None
        assert tariff_called is False
        [b] = await directory.list_counterparties("buyer")
        [s] = await directory.list_counterparties("supplier")
        assert b.last_outcome == "no opportunity: no live demand data for this product and market"
        assert s.name == "Jashn Foods"

    def test_simulated_is_not_a_data_source(self):
        from typing import get_args

        from services.trade_min.schemas import DataSource
        assert "simulated" not in get_args(DataSource)


class TestWorldwide:
    def test_worldwide_lines_have_no_country_and_cctld_gives_the_country(self):
        from shared.connectors.apify import WORLDWIDE, ApifyCompanySearchConnector, country_from_domain
        assert ApifyCompanySearchConnector.build_queries("rice", [WORLDWIDE, "IN"], "supplier") == [
            "rice exporter", "rice manufacturer", "rice exporter, IN", "rice manufacturer, IN"]
        assert country_from_domain("acme.co.in") == "IN"
        assert country_from_domain("acme.co.uk") == "GB"
        assert country_from_domain("acme.de") == "DE"
        assert country_from_domain("acme.com") is None and country_from_domain("acme.io") is None

    @pytest.mark.asyncio
    async def test_worldwide_search_records_every_country_without_a_market(self):
        calls = []

        async def fake_quote(name, req):
            calls.append(req)
            if req["role"] == "buyer":
                return {"companies": [{"company_name": "Hamburg Reis GmbH", "country": "DE",
                                       "website": "https://reis.de", "email": "einkauf@reis.de"}]}
            return {"companies": [
                {"company_name": "Thai Rice Co", "country": "TH", "website": "https://thairice.co.th",
                 "email": "sales@thairice.co.th"},
                {"company_name": "Global Rice", "country": None, "website": "https://globalrice.com",
                 "email": "info@globalrice.com"},
            ]}

        with patch.object(svc, "_safe_quote", fake_quote):
            await svc._worldwide_counterparties("100630")
        assert {tuple(r["countries"]) for r in calls} == {("ALL",)}
        [b] = await directory.list_counterparties("buyer")
        assert b.country == "DE" and b.markets == [] and b.last_outcome == directory.WORLDWIDE_OUTCOME
        suppliers = {s.name: s for s in await directory.list_counterparties("supplier")}
        assert suppliers["Thai Rice Co"].country == "TH" and suppliers["Global Rice"].country is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("on", [True, False])
    async def test_scan_request_toggle_controls_the_worldwide_search(self, on):
        from services.trade_min.schemas import ScanRequest
        searched: list[str] = []

        async def fake_worldwide(hs):
            searched.append(hs)

        async def no_opp(*a, **k):
            return None

        with patch.object(svc, "_worldwide_counterparties", fake_worldwide), \
             patch.object(svc, "_scan_one", no_opp):
            await svc.scan_opportunities(ScanRequest(hs_codes=["100630", "090411"], target_markets=["AE", "DE"],
                                                     worldwide_search=on))
        assert searched == (["100630", "090411"] if on else [])

    @pytest.mark.asyncio
    async def test_worldwide_outcome_never_replaces_a_corridor_outcome(self):
        await directory.record_scan("100630", "AE", [_buyer()], [], outcome="opportunity OPP-9",
                                    opportunity_id="OPP-9")
        await directory.record_scan("100630", "", [_buyer()], [], outcome=directory.WORLDWIDE_OUTCOME)
        [b] = await directory.list_counterparties("buyer")
        assert b.last_outcome == "opportunity OPP-9" and b.markets == ["AE"] and b.times_seen == 2

    @pytest.mark.asyncio
    async def test_country_filter_india_only(self):
        foreign = SellerCandidate(candidate_id="thairice", company_name="Thai Rice Co", location_city="",
                                  location_state="", hs_code="100630", source="apify_company_search",
                                  website="https://thairice.co.th", country="TH")
        await directory.record_scan("100630", "AE", [], [_seller(), foreign], outcome="x")
        assert [s.name for s in await directory.list_counterparties("supplier", country="in")] == ["Jashn Foods"]
        assert [s.name for s in await directory.list_counterparties("supplier", country="TH")] == ["Thai Rice Co"]
        assert len(await directory.list_counterparties("supplier")) == 2

    @pytest.mark.asyncio
    async def test_identical_concurrent_actor_calls_run_once(self):
        import asyncio
        runs = 0

        async def slow_quote(name, req):
            nonlocal runs
            runs += 1
            await asyncio.sleep(0.05)
            return {"companies": []}

        req = {"keyword": "rice", "countries": ["IN"], "role": "supplier", "max_results": 5}
        with patch.object(svc, "_quote_once", slow_quote):
            out = await asyncio.gather(*[svc._safe_quote("apify_company_search", dict(req)) for _ in range(5)])
        assert runs == 1 and all(o == {"companies": []} for o in out)


class TestOneOpportunityPerCorridor:
    @pytest.mark.asyncio
    async def test_rescan_refreshes_the_open_opportunity_and_lists_it_once(self):
        from datetime import timedelta

        from services.trade_min import repository
        from services.trade_min.schemas import EnrichedOpportunity
        repo = repository.InMemoryFallbackRepository()
        now = datetime.now(timezone.utc)

        def opp(oid, status="ACTIVE", age_h=0):
            return EnrichedOpportunity(
                opportunity_id=oid, hs_code="090422", market="DE", status=status, source="apify_export_markets",
                scanned_at=now - timedelta(hours=age_h), demand_signals=[], demand_index=60.0,
                price_gap_pct=20.0, buyer_signals=[], urgency_tier=1, net_price_gap_pct=15.0,
                tariff_viable=True, seller_candidates=[], arbitrage_rank=50.0, confidence_score=60,
                expires_at=now + timedelta(days=7))

        async def get_repo():
            return repo

        with patch.object(svc, "get_repository", get_repo):
            assert (await svc._opportunity_id_for("090422", "DE")).startswith("OPP-")   # nothing yet → new id
            await repo.save(opp("OPP-OLD", age_h=5))
            await repo.save(opp("OPP-NEW", age_h=1))                                    # legacy duplicate
            assert await svc._opportunity_id_for("090422", "DE") == "OPP-NEW"
            assert [o.opportunity_id for o in await svc.list_opportunities()] == ["OPP-NEW"]
            await repo.mark_converted("OPP-NEW")
            await repo.mark_converted("OPP-OLD")
            assert await svc._opportunity_id_for("090422", "DE") not in ("OPP-NEW", "OPP-OLD")


class TestEndpoints:
    def test_supplier_country_filter_endpoint(self):
        import asyncio

        from services.trade_min.main import create_app
        foreign = SellerCandidate(candidate_id="thairice", company_name="Thai Rice Co", location_city="",
                                  location_state="", hs_code="100630", source="apify_company_search",
                                  website="https://thairice.co.th", country="TH")
        asyncio.run(directory.record_scan("100630", "AE", [], [_seller(), foreign], outcome="x"))
        with TestClient(create_app()) as client:
            india = client.get("/trade-min/suppliers", params={"country": "IN"}).json()
            everyone = client.get("/trade-min/suppliers").json()
        assert [s["name"] for s in india] == ["Jashn Foods"] and len(everyone) == 2

    def test_buyers_and_suppliers_endpoints(self):
        import asyncio

        from services.trade_min.main import create_app
        asyncio.run(directory.record_scan("100630", "AE", [_buyer()], [_seller()], outcome="x"))
        with TestClient(create_app()) as client:
            buyers = client.get("/trade-min/buyers", params={"market": "AE"}).json()
            suppliers = client.get("/trade-min/suppliers", params={"q": "jashn"}).json()
        assert buyers[0]["name"] == "Al Turab Foods" and buyers[0]["email"] == "purchase@alturabfood.com"
        assert suppliers[0]["website"] == "https://www.jashnfoods.com/"
