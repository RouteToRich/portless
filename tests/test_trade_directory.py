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
        phase1.gross_price_gap_pct = 0.0
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


class TestEndpoints:
    def test_buyers_and_suppliers_endpoints(self):
        import asyncio

        from services.trade_min.main import create_app
        asyncio.run(directory.record_scan("100630", "AE", [_buyer()], [_seller()], outcome="x"))
        with TestClient(create_app()) as client:
            buyers = client.get("/trade-min/buyers", params={"market": "AE"}).json()
            suppliers = client.get("/trade-min/suppliers", params={"q": "jashn"}).json()
        assert buyers[0]["name"] == "Al Turab Foods" and buyers[0]["email"] == "purchase@alturabfood.com"
        assert suppliers[0]["website"] == "https://www.jashnfoods.com/"
