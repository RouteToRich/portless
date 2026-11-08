"""Trade-Min scan: each connector's data through all five phases, the directory, and reply extraction."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from services.hitl_approval import notification as nt
from services.trade_min import directory as dr
from services.trade_min import repository as repo_mod
from services.trade_min import service as svc
from services.trade_min.config import DEFAULT_CONFIG as CONFIG
from services.trade_min.schemas import (
    BuyerSignal,
    CounterpartyRecord,
    ScanRequest,
    SellerCandidate,
    TariffData,
)
from shared.orchestration import extraction as ex
from tests.factories import make

NOW = datetime.now(UTC)
_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    repo_mod._repository = repo_mod.InMemoryFallbackRepository()
    dr.reset_directory()
    monkeypatch.setattr(svc, "get_publisher", lambda: MagicMock(publish=AsyncMock()))
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


COMTRADE = {"records": [{"fob_value_usd": 100.0, "cif_value_usd": 150.0}], "total_records": 250}
MARKET_ROW = {"markets": [{"market": "AE", "import_value_usd": 1e7, "unit_value_usd_per_mt": 1200.0, "exporter_world_unit_value_usd_per_mt": 800.0, "score": 70}]}


class TestConnectorAccess:
    def test_missing_credential_is_not_cached(self):
        svc._connector_cache.pop("x", None)
        with patch("shared.connectors.registry.get_connector", side_effect=KeyError("K")):
            assert svc._build_connector("x") is None
        assert "x" not in svc._connector_cache

    def test_broken_connector_returns_none(self):
        svc._connector_cache.pop("x", None)
        with patch("shared.connectors.registry.get_connector", side_effect=RuntimeError("no")):
            assert svc._build_connector("x") is None

    def test_cached_company_rows_are_refined_again(self):
        with patch("shared.connectors.apify.refine_companies", return_value=[{"n": 1}]):
            assert svc._refine("apify_company_search", {"companies": [{}, {}]}) == {"companies": [{"n": 1}], "total": 1}

    @pytest.mark.parametrize(("name", "result"), [("other", {"companies": [1]}), ("apify_company_search", {"companies": []}), ("apify_company_search", None)])
    def test_other_results_are_untouched(self, name, result):
        assert svc._refine(name, result) == result

    def test_keyword_survives_a_catalogue_failure(self):
        with patch("shared.catalogue.product_name_for", side_effect=RuntimeError, create=True):
            assert svc._keyword_for("290543")

    def test_products_fall_back_to_the_keyword(self):
        with patch("shared.catalogue.products_for", return_value=[]):
            assert svc._products_for("290543") == [svc._keyword_for("290543")]
        with patch("shared.catalogue.products_for", side_effect=RuntimeError):
            assert svc._products_for("290543") == [svc._keyword_for("290543")]

    def test_seller_profile(self):
        with patch("shared.catalogue.seller_profile_for", return_value="refiner"):
            assert svc._seller_profile_for("1") == "refiner"
        with patch("shared.catalogue.seller_profile_for", side_effect=RuntimeError):
            assert svc._seller_profile_for("1") is None

    @pytest.mark.parametrize(("value", "expected"), [("x", None), (None, None), ("2", 2.0)])
    def test_safe_float(self, value, expected):
        assert svc._safe_float(value) == expected

    def test_inr_price(self):
        assert svc._normalise_price_per_mt(84.0, "INR") == 1.0

    @pytest.mark.parametrize("unit", ["INR/kg", "INR per kg", "Rs/kg", "\u20b9/kg", "INRperkg"])
    def test_rupees_per_kg_are_converted(self, unit):
        assert svc._normalise_price_per_mt(84.0, unit) == 1000.0

    @pytest.mark.parametrize(("value", "unit", "expected"), [(1.5, "USD/kg", 1500.0), (700.0, "USD/MT", 700.0), (2.0, "kg", 2000.0), (None, "INR", None)])
    def test_other_units_are_unchanged(self, value, unit, expected):
        assert svc._normalise_price_per_mt(value, unit) == expected


class TestPhase1:
    async def test_no_source_means_no_demand(self, quotes):
        result = await svc._phase1_historical_demand("290543", "AE", CONFIG)
        assert result.source == "" and result.demand_index == 0

    async def test_comtrade_measures_the_gap(self, quotes):
        quotes["comtrade"] = COMTRADE
        result = await svc._phase1_historical_demand("290543", "AE", CONFIG)
        assert result.source == "comtrade" and result.price_gap_measured and result.gross_price_gap_pct == 50.0
        assert result.demand_index == 50.0

    async def test_census_is_labelled(self, quotes):
        quotes["census"] = COMTRADE
        result = await svc._phase1_historical_demand("290543", "US", CONFIG)
        assert result.source == "census"

    async def test_empty_reply_does_not_count_as_a_source(self, quotes):
        quotes["comtrade"] = {"records": [], "total_records": 0}
        assert (await svc._phase1_historical_demand("1", "AE", CONFIG)).active_connectors == 0

    async def test_market_actor_gap_wins_over_cif_fob(self, quotes):
        quotes["comtrade"] = COMTRADE
        quotes["apify_export_markets"] = MARKET_ROW
        result = await svc._phase1_historical_demand("290543", "AE", CONFIG)
        assert result.gross_price_gap_pct == 50.0 and result.price_benchmark_usd_per_mt == 1200.0
        assert result.price_benchmark_source == "apify_export_markets" and result.active_connectors == 2

    async def test_market_actor_without_gap_keeps_cif_fob(self, quotes):
        quotes["comtrade"] = COMTRADE
        quotes["apify_export_markets"] = {"markets": [{"market": "AE", "score": 10, "unit_value_usd_per_mt": 900.0}]}
        assert (await svc._phase1_historical_demand("1", "AE", CONFIG)).gross_price_gap_pct == 50.0

    def test_comtrade_without_values_has_no_gap(self):
        score, _s, buyers, gap = svc._extract_comtrade({"records": [{}], "total_records": 1000}, "1", "AE", NOW)
        assert (score, gap, buyers) == (100.0, 0.0, [])

    def test_score_from_value_when_the_actor_gives_none(self):
        score, *_ = svc._extract_export_market({"market": "AE", "import_value_usd": 1e7}, "1", "AE", NOW)
        assert score == 50.0

    def test_no_market_rows(self):
        assert svc._pick_market_row(None, "AE") is None
        assert svc._pick_market_row({"markets": [{"market": "DE"}]}, "AE") is None


class TestManifestExtractors:
    def test_volza(self):
        recent = (NOW - timedelta(days=88)).isoformat()
        data = {"shipments": [
            {"buyer_name": "A", "unit_price_usd": 2.0, "shipment_date": recent, "buyer_country": "ES"},
            {"buyer_name": "A", "unit_price_usd": 2.0},
            {"buyer_name": "B", "shipment_date": "not a date"},
            {"buyer_name": ""},
        ], "pagination": {"total_records": 40}}
        score, signals, buyers, gap = svc._extract_volza(data, "1", "ES", NOW, CONFIG)
        assert score == 10.0 and gap == 15.0 and [b.buyer_name for b in buyers] == ["A", "B"] and signals[0].source == "volza"
        assert buyers[1].urgency_tier == 1

    def test_volza_empty(self):
        score, _signals, buyers, gap = svc._extract_volza({}, "1", "ES", NOW, CONFIG)
        assert (score, buyers, gap) == (0.0, [], 0.0)

    def test_importgenius(self):
        data = {"importers": [{"importer_name": "A", "net_weight_kg": "1000"}, {"company_name": "B"}, {}]}
        score, _signals, buyers = svc._extract_importgenius(data, "1", "US", NOW)
        assert score == 6.0 and [b.buyer_name for b in buyers] == ["A", "B"]

    def test_importgenius_data_key(self):
        assert svc._extract_importgenius({"data": []}, "1", "US", NOW)[0] == 0.0

    def test_trademo(self):
        score, _signals, buyers = svc._extract_trademo({"companies": [{"company_name": "A"}, {"name": "B"}, {}]}, "1", "ES", NOW)
        assert score == 10.0 and [b.buyer_name for b in buyers] == ["A", "B"]

    @pytest.mark.parametrize("key", ["buyers", "data"])
    def test_trademo_keys(self, key):
        assert len(svc._extract_trademo({key: [{"name": "A"}]}, "1", "ES", NOW)[2]) == 1


class TestPhase2:
    async def _run(self, market="DE", code="290543"):
        return await svc._phase2_live_signals(code, market, svc._Phase1Result(), CONFIG)

    async def test_nothing_found(self, quotes):
        result = await self._run()
        assert (result.active_rfq_count, result.active_tender_count, result.disruption_detected) == (0, 0, False)

    async def test_rfqs_become_buyers(self, quotes):
        quotes["alibaba_rfq"] = {"rfqs": [{"buyer_country": "DE"}], "total": 3}
        result = await self._run()
        assert result.active_rfq_count == 3 and result.buyer_signals[0].source == "alibaba_rfq"

    async def test_empty_rfq_feed(self, quotes):
        quotes["alibaba_rfq"] = {"rfqs": [], "total": 0}
        assert (await self._run()).buyer_signals == []

    @pytest.mark.parametrize(("strength", "tier"), [("HIGH", 4), ("MEDIUM", 3)])
    async def test_disruption_urgency(self, quotes, strength, tier):
        quotes["gdelt_disruption"] = {"disruption_detected": True, "signal_strength": strength, "article_count": 7}
        result = await self._run()
        assert result.disruption_detected and result.demand_signals[0].urgency_tier == tier

    async def test_no_disruption(self, quotes):
        quotes["gdelt_disruption"] = {"disruption_detected": False}
        assert (await self._run()).disruption_detected is False

    async def test_tenders(self, quotes):
        quotes["eu_ted"] = {"tenders": [{"buyer_name": "Hospital", "buyer_country": "DE"}], "total": 2}
        result = await self._run()
        assert result.active_tender_count == 2 and result.buyer_signals[0].buyer_name == "Hospital"

    async def test_no_tenders(self, quotes):
        quotes["eu_ted"] = {"tenders": []}
        assert (await self._run()).active_tender_count == 0


class TestPhase3:
    async def test_eu_prefers_access2markets(self, quotes):
        quotes["access2markets"] = {"applied_duty_pct": 4.0, "ntb_flags": ["A"], "rules_of_origin": "R"}
        tariff = await svc._phase3_tariff_gate("1", "DE", 30.0, CONFIG)
        assert tariff.applied_duty_pct == 4.0 and tariff.ntb_flags == ["A"]

    async def test_eu_falls_back_to_wits(self, quotes):
        quotes["access2markets"] = {"applied_duty_pct": None}
        quotes["wits_tariff"] = {"applied_duty_pct": 2.5, "year": "2021"}
        assert (await svc._phase3_tariff_gate("1", "DE", 30.0, CONFIG)).applied_duty_pct == 2.5

    async def test_no_tariff_is_zero_and_unverified(self, quotes):
        tariff = await svc._phase3_tariff_gate("1", "AE", 30.0, CONFIG)
        assert tariff.applied_duty_pct == 0.0 and tariff.ntb_flags == []

    async def test_non_eu_skips_access2markets(self, quotes):
        quotes["access2markets"] = {"applied_duty_pct": 99.0}
        quotes["wits_tariff"] = {"applied_duty_pct": 1.0}
        assert (await svc._phase3_tariff_gate("1", "AE", 30.0, CONFIG)).applied_duty_pct == 1.0

    def _parse(self, raw):
        return svc._parse_macmap(raw, "1", "ES", 21.0, NOW)

    def test_macmap_details(self):
        tariff = self._parse({"tariffResult": {"appliedTariffDetails": [{"tariffRate": "7.5%"}]},
                              "ntmResult": [{"ntmCode": "A1", "ntmDesc": "Label"}, {"category": "B"}, {"description": "no code"}],
                              "rooResult": {"generalRule": "WO"}})
        assert (tariff.applied_duty_pct, tariff.ntb_flags, tariff.rules_of_origin) == (7.5, ["A1: Label", "B"], "WO")
        assert tariff.total_landed_cost_add_pct == 28.5

    def test_macmap_applied_block(self):
        assert self._parse({"tariff": {"applied": {"rate": "3%"}}, "roo": [{"rule": "R"}]}).applied_duty_pct == 3.0

    @pytest.mark.parametrize("raw", [{"tariff": {"applied": {"rate": "free"}}}, {"tariff": {"appliedTariffDetails": [{"rate": "free"}]}}, {}])
    def test_macmap_unreadable_rate(self, raw):
        assert self._parse(raw).applied_duty_pct == 0.0

    def test_macmap_roo_list_description(self):
        assert self._parse({"roo": [{"description": "D"}]}).rules_of_origin == "D"

    def test_macmap_ntm_not_a_list(self):
        assert self._parse({"ntm": "none"}).ntb_flags == []


class TestPhase4:
    async def test_directories_are_merged_without_duplicates(self, quotes):
        quotes["indiamart"] = {"sellers": [{"company_id": "1", "company_name": "Acme", "min_price": 1.0, "price_unit": "USD/kg"}]}
        quotes["tradeindia"] = {"sellers": [{"company_id": "2", "company_name": "ACME "}, {"company_id": "3", "company_name": "Beta"}]}
        sellers = await svc._phase4_seller_discovery("290543", CONFIG)
        assert sorted(s.company_name for s in sellers) == ["Acme", "Beta"]


def _phase1(**kw):
    return svc._Phase1Result(**{"demand_index": 80.0, "gross_price_gap_pct": 40.0, "active_connectors": 3, "source": "comtrade", "price_gap_measured": True, **kw})


def _tariff(duty=5.0, ntb=()):
    return TariffData(hs_code="1", destination_country="AE", applied_duty_pct=duty, ntb_flags=list(ntb), total_landed_cost_add_pct=duty, fetched_at=NOW)


def _seller(i=0):
    return make(SellerCandidate, candidate_id=f"S{i}", company_name=f"Seller {i}", source="indiamart")


def _buyer(tier=1, name="Buy"):
    return make(BuyerSignal, buyer_name=name, buyer_country="AE", signal_type="manifest", source="volza", urgency_tier=tier)


class TestScoring:
    @pytest.mark.parametrize(("p1", "p2", "expected"), [
        ({}, {"disruption_detected": True}, 4), ({}, {"active_rfq_count": 1}, 3), ({}, {"active_tender_count": 1}, 3),
        ({"buyer_signals": [_buyer(2)]}, {}, 2), ({"buyer_signals": [_buyer(1)]}, {}, 1), ({}, {}, 1),
    ])
    def test_urgency(self, p1, p2, expected):
        assert svc._determine_urgency(_phase1(**p1), svc._Phase2Result(**p2)) == expected

    def _confidence(self, p1=None, p2=None, tariff=None, sellers=0, gap=40.0):
        return svc._compute_confidence(_phase1(**(p1 or {})), svc._Phase2Result(**(p2 or {})), tariff or _tariff(0.0), [_seller(i) for i in range(sellers)], gap, CONFIG)

    def test_connectors_set_the_base(self):
        assert self._confidence(p1={"active_connectors": 4}) == 50.0
        assert self._confidence(p1={"active_connectors": 9}) == 0.0

    def test_live_signals_are_capped(self):
        all_live = {"active_rfq_count": 1, "active_tender_count": 1, "disruption_detected": True}
        assert self._confidence(p2=all_live) == 40.0 + min(25.0, float(CONFIG.confidence_live_max))

    def test_tariff_points_only_when_data_came_back(self):
        assert self._confidence() == 40.0
        assert self._confidence(tariff=_tariff(5.0)) == 40.0 + CONFIG.confidence_tariff_max
        assert self._confidence(tariff=_tariff(0.0, ["A"]), gap=CONFIG.tariff_marginal_floor) == 40.0 + CONFIG.confidence_tariff_max

    def test_marginal_gap_gets_partial_points(self):
        gap = CONFIG.tariff_hard_discard_floor
        assert self._confidence(tariff=_tariff(5.0), gap=gap) == 45.0

    def test_below_floor_gets_no_tariff_points(self):
        assert self._confidence(tariff=_tariff(5.0), gap=CONFIG.tariff_hard_discard_floor - 1) == 40.0

    @pytest.mark.parametrize(("count", "bonus"), [(0, 0.0), (1, 5.0), (3, float(CONFIG.confidence_sellers_max))])
    def test_sellers(self, count, bonus):
        assert self._confidence(sellers=count) == 40.0 + bonus

    def test_capped_at_100(self):
        p2 = {"active_rfq_count": 1, "active_tender_count": 1}
        assert self._confidence(p1={"active_connectors": 4}, p2=p2, tariff=_tariff(5.0), sellers=3) <= 100.0


class TestPhase5:
    async def _assemble(self, phase1=None, phase2=None, tariff=None, sellers=None):
        return await svc._phase5_assemble_and_persist(
            "290543", "AE", phase1 or _phase1(), phase2 or svc._Phase2Result(), tariff or _tariff(), [_seller()] if sellers is None else sellers, CONFIG,
        )

    async def test_active_opportunity_is_saved_and_published(self, monkeypatch):
        publisher = MagicMock(publish=AsyncMock())
        monkeypatch.setattr(svc, "get_publisher", lambda: publisher)
        opp = await self._assemble()
        assert opp.status == "ACTIVE" and opp.net_price_gap_pct == 35.0
        assert (await svc.get_opportunity(opp.opportunity_id)).opportunity_id == opp.opportunity_id
        assert publisher.publish.await_args.args[0].topic.value == svc.EventTopic.OPPORTUNITY_DETECTED.value

    async def test_below_floor_is_discarded(self):
        assert await self._assemble(phase1=_phase1(gross_price_gap_pct=CONFIG.tariff_hard_discard_floor - 1), tariff=_tariff(0.0)) is None

    async def test_marginal(self):
        gap = (CONFIG.tariff_hard_discard_floor + CONFIG.tariff_marginal_floor) / 2
        assert (await self._assemble(phase1=_phase1(gross_price_gap_pct=gap), tariff=_tariff(0.0))).status == "MARGINAL"

    async def test_low_rank_active_is_dropped(self):
        with patch.object(svc.ranking, "arbitrage_rank", return_value=CONFIG.min_arbitrage_rank - 1):
            assert await self._assemble() is None

    async def test_expiry_follows_urgency(self):
        opp = await self._assemble(phase2=svc._Phase2Result(disruption_detected=True))
        assert opp.urgency_tier == 4
        assert opp.expires_at - opp.scanned_at <= timedelta(hours=CONFIG.urgency_ttl_hours[4], seconds=5)

    async def test_save_failure_still_returns(self):
        broken = MagicMock(save=AsyncMock(side_effect=RuntimeError), list_by_hs_market=AsyncMock(return_value=[]))
        with patch.object(svc, "get_repository", new=AsyncMock(return_value=broken)):
            assert (await self._assemble()) is not None

    async def test_publish_failure_still_returns(self, monkeypatch):
        monkeypatch.setattr(svc, "get_publisher", lambda: MagicMock(publish=AsyncMock(side_effect=RuntimeError)))
        assert (await self._assemble()) is not None

    async def test_rescan_keeps_the_open_opportunity_id(self):
        first = await self._assemble()
        second = await self._assemble()
        assert first.opportunity_id == second.opportunity_id

    async def test_id_lookup_failure_mints_a_new_id(self):
        broken = MagicMock(list_by_hs_market=AsyncMock(side_effect=RuntimeError))
        with patch.object(svc, "get_repository", new=AsyncMock(return_value=broken)):
            assert (await svc._opportunity_id_for("1", "AE")).startswith("OPP-")


class TestScan:
    @staticmethod
    def _pipeline(phase1=None, tariff=None, opp=None, sellers=None):
        return (
            patch.object(svc, "_phase1_historical_demand", new=AsyncMock(return_value=phase1 or _phase1(buyer_signals=[_buyer()]))),
            patch.object(svc, "_phase2_live_signals", new=AsyncMock(return_value=svc._Phase2Result())),
            patch.object(svc, "_phase3_tariff_gate", new=AsyncMock(return_value=tariff or _tariff())),
            patch.object(svc, "_phase4_seller_discovery", new=AsyncMock(return_value=[_seller()] if sellers is None else sellers)),
        )

    async def _scan(self, **kw):
        a, b, c, d = self._pipeline(**kw)
        with a, b, c, d:
            return await svc._scan_one("290543", "AE", CONFIG)

    async def _directory(self, role="buyer"):
        return await dr.list_counterparties(role)

    async def test_full_scan_records_counterparties_with_the_opportunity(self):
        opp = await self._scan()
        assert opp is not None
        buyer = (await self._directory())[0]
        assert buyer.opportunity_ids == [opp.opportunity_id]
        assert len(await self._directory("supplier")) == 1

    async def test_no_live_demand_still_keeps_counterparties(self):
        assert await self._scan(phase1=_phase1(source="", buyer_signals=[_buyer()])) is None
        assert "no live demand" in (await self._directory())[0].last_outcome

    async def test_no_price_gap_still_keeps_counterparties(self):
        assert await self._scan(phase1=_phase1(price_gap_measured=False, buyer_signals=[_buyer()])) is None
        assert "no measured price gap" in (await self._directory())[0].last_outcome

    async def test_tariff_drop_still_keeps_counterparties(self):
        assert await self._scan(tariff=_tariff(60.0), phase1=_phase1(buyer_signals=[_buyer()])) is None
        assert "after duty" in (await self._directory())[0].last_outcome
        assert len(await self._directory("supplier")) == 1

    async def test_rank_drop_says_so(self):
        with patch.object(svc.ranking, "arbitrage_rank", return_value=CONFIG.min_arbitrage_rank - 1):
            assert await self._scan() is None
        assert (await self._directory())[0].opportunity_ids == []

    async def test_crash_in_one_corridor_is_contained(self):
        with patch.object(svc, "_phase1_historical_demand", new=AsyncMock(side_effect=RuntimeError("x"))):
            assert await svc._scan_one("1", "AE", CONFIG) is None

    async def test_scan_filters_by_rank_and_survives_a_crash(self):
        keep = SimpleNamespace(arbitrage_rank=100.0, urgency_tier=1)
        low = SimpleNamespace(arbitrage_rank=1.0, urgency_tier=4)
        side = [keep, low, None, RuntimeError("x")]

        async def one(*_a):
            value = side.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(svc, "_scan_one", new=one), patch.object(svc, "_worldwide_counterparties", new=AsyncMock()):
            result = await svc.scan_opportunities(ScanRequest(hs_codes=["1", "2", "3", "4"], target_markets=["AE"], min_arbitrage_rank=99.0, worldwide_search=False))
        assert result == [keep]

    async def test_worldwide_search_needs_a_product_name(self, quotes):
        with patch.object(svc, "_web_searchable", return_value=False):
            assert await svc._worldwide_counterparties("1") is None

    def test_pairs_when_catalogue_is_unreadable(self):
        with patch("shared.catalogue.active_lines", side_effect=RuntimeError):
            pairs = svc._scan_pairs(ScanRequest(hs_codes=["1"], target_markets=["AE"]), CONFIG)
        assert pairs == [("1", "AE")]

    def test_catalogue_pairs_are_unique(self):
        line = SimpleNamespace(hs_code="1", markets=["AE", "AE"])
        with patch("shared.catalogue.active_lines", return_value=[line]):
            assert svc._scan_pairs(ScanRequest(), CONFIG) == [("1", "AE")]

    async def test_list_filters_urgency_and_keeps_latest_per_corridor(self):
        repo = await repo_mod.get_repository()
        base = {"hs_code": "1", "market": "AE", "status": "ACTIVE", "arbitrage_rank": 5.0}
        await repo.save(make(svc.EnrichedOpportunity, opportunity_id="old", scanned_at=NOW - timedelta(days=1), urgency_tier=2, **base))
        await repo.save(make(svc.EnrichedOpportunity, opportunity_id="new", scanned_at=NOW, urgency_tier=2, **base))
        await repo.save(make(svc.EnrichedOpportunity, opportunity_id="other", scanned_at=NOW, urgency_tier=1, **{**base, "market": "DE"}))
        assert [o.opportunity_id for o in await svc.list_opportunities(limit=10, min_rank=0.0, urgency_tier=2)] == ["new"]

    async def test_match_seed_and_convert(self):
        repo = await repo_mod.get_repository()
        await repo.save(make(svc.EnrichedOpportunity, opportunity_id="O1"))
        seed = await svc.get_match_seed("O1")
        assert seed.buyer_target_price_usd is None
        assert await svc.get_match_seed("nope") is None
        assert await svc.mark_opportunity_converted("O1") is True


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------

def _record(**kw):
    base = {"id": "buyer:acme.com", "role": "buyer", "name": "Acme", "source": "apify", "sources": ["apify"], "first_seen_at": NOW, "last_seen_at": NOW}
    return make(CounterpartyRecord, **{**base, **kw})


class TestDirectory:
    @pytest.fixture(params=["memory", "mongo"])
    def store(self, request):
        return dr.InMemoryDirectory() if request.param == "memory" else dr.MongoDirectory(AsyncMongoMockClient()["d"]["c"])

    @pytest.mark.parametrize(("url", "expected"), [(None, ""), ("https://www.Acme.com/x", "acme.com"), ("acme.com", "acme.com")])
    def test_domain(self, url, expected):
        assert dr._domain(url) == expected

    def test_key_prefers_the_domain(self):
        assert dr.record_key("buyer", "Acme Ltd", "https://acme.com") == "buyer:acme.com"
        assert dr.record_key("buyer", "Acme Ltd", None) == "buyer:acme-ltd"

    def test_union_is_capped(self):
        assert len(dr._union([str(i) for i in range(60)], ["x", "", "x"])) == dr._MAX_LIST

    async def test_merge_keeps_old_values_and_counts(self, store):
        await store.upsert(_record(email="a@b.c", hs_codes=["1"], last_outcome="first"))
        merged = await store.upsert(_record(email=None, hs_codes=["2"], phone="9", last_outcome="second"))
        assert (merged.email, merged.phone, merged.hs_codes, merged.times_seen, merged.last_outcome) == ("a@b.c", "9", ["1", "2"], 2, "second")

    async def test_worldwide_outcome_does_not_replace_a_corridor_outcome(self, store):
        await store.upsert(_record(last_outcome="corridor"))
        assert (await store.upsert(_record(last_outcome=dr.WORLDWIDE_OUTCOME))).last_outcome == "corridor"

    async def test_filters(self, store):
        await store.upsert(_record(country="DE", hs_codes=["1"], markets=["AE"], email="x@y.z"))
        await store.upsert(_record(id="buyer:b", name="Beta", country="IN", hs_codes=["2"], markets=["DE"]))
        await store.upsert(_record(id="supplier:s", role="supplier", name="Sup"))
        assert [r.name for r in await store.list("buyer", country="de")] == ["Acme"]
        assert [r.name for r in await store.list("buyer", hs_code="2")] == ["Beta"]
        assert [r.name for r in await store.list("buyer", market="ae")] == ["Acme"]
        assert [r.name for r in await store.list("buyer", q="BET")] == ["Beta"]
        assert await store.list("buyer", q="zzz") == []
        assert len(await store.list("buyer", limit=1)) == 1

    async def test_clear(self, store):
        await store.upsert(_record())
        await store.clear()
        assert await store.list("buyer") == []

    async def test_malformed_mongo_rows(self):
        col = AsyncMongoMockClient()["d"]["c"]
        store = dr.MongoDirectory(col)
        await col.insert_one({"_id": "buyer:acme.com", "role": "buyer", "last_seen_at": "z"})
        assert await store.list("buyer") == []
        assert (await store.upsert(_record())).times_seen == 1

    def test_placeholder_buyer_without_contact_is_dropped(self):
        assert dr._buyer_record(_buyer(name="Alibaba Buyer (ES)"), "1", "ES", "o", None, NOW) is None
        assert dr._buyer_record(_buyer(name=" "), "1", "ES", "o", None, NOW) is None

    def test_placeholder_buyer_with_contact_is_kept(self):
        buyer = make(BuyerSignal, buyer_name="Unknown importer", buyer_country="es", signal_type="manifest", source="volza", urgency_tier=1, email="a@b.c")
        record = dr._buyer_record(buyer, "1", "es", "o", "O1", NOW)
        assert (record.country, record.markets, record.opportunity_ids) == ("ES", ["ES"], ["O1"])

    def test_nameless_supplier_is_dropped(self):
        assert dr._supplier_record(make(SellerCandidate, candidate_id="C", company_name=" ", source="indiamart"), "1", "", "o", None, NOW) is None

    async def test_record_scan_counts(self):
        assert await dr.record_scan("1", "AE", [_buyer(), _buyer(name=" ")], [_seller()], outcome="o") == (1, 1)

    async def test_record_scan_never_raises(self):
        with patch.object(dr, "get_directory", new=AsyncMock(side_effect=RuntimeError)):
            assert await dr.record_scan("1", "AE", [_buyer()], [], outcome="o") == (0, 0)


class TestDirectorySelection:
    @pytest.fixture(autouse=True)
    def _clear(self):
        dr._directory = None
        yield
        dr._directory = None

    async def test_memory_without_uri_and_cached(self, monkeypatch):
        monkeypatch.setattr(dr.settings, "trade_mongo_uri", "")
        first = await dr.get_directory()
        assert isinstance(first, dr.InMemoryDirectory) and await dr.get_directory() is first

    async def test_mongo(self, monkeypatch):
        monkeypatch.setattr(dr.settings, "trade_mongo_uri", "mongodb://x")
        with patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=lambda *_a, **_k: AsyncMongoMockClient()):
            assert isinstance(await dr.get_directory(), dr.MongoDirectory)

    async def test_failure_falls_back(self, monkeypatch):
        monkeypatch.setattr(dr.settings, "trade_mongo_uri", "mongodb://x")
        with patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=RuntimeError):
            assert isinstance(await dr.get_directory(), dr.InMemoryDirectory)


# ---------------------------------------------------------------------------
# Reply extraction
# ---------------------------------------------------------------------------

class TestPatternHelpers:
    @pytest.mark.parametrize(("value", "unit", "expected"), [
        (None, "mt", None), (2000.0, "kg", 2.0), (2.0, "containers", 40.0), (5.0, None, 5.0), (5.0, "tons", 5.0),
    ])
    def test_to_mt(self, value, unit, expected):
        assert ex._to_mt(value, unit) == expected

    @pytest.mark.parametrize(("value", "unit", "expected"), [
        (None, "mt", None), (1.15, "kg", 1150.0), (1.15, None, 1150.0), (900.0, None, 900.0), (900.0, "MT", 900.0),
    ])
    def test_price_per_mt(self, value, unit, expected):
        assert ex._price_per_mt(value, unit) == expected

    @pytest.mark.parametrize(("n", "unit", "expected"), [("2", "weeks", 14), ("2", "months", 60), ("9", "days", 9), ("x", "days", None)])
    def test_days(self, n, unit, expected):
        assert ex._days(n, unit) == expected

    @pytest.mark.parametrize(("text", "expected"), [
        ("USD 900/MT", 900.0), ("900 USD per ton", 900.0), ("$1.15/kg", 1150.0), ("$5 fee", 5000.0), ("USD 0.001 per kg", None), ("nothing", None),
    ])
    def test_first_price(self, text, expected):
        assert ex._first_price(text) == expected

    @pytest.mark.parametrize(("text", "expected"), [
        ("need 20 MT", 20.0), ("2 containers", 40.0), ("500 kg", 0.5), ("MOQ 5 MT only", None), ("0.01 kg", None),
    ])
    def test_quantity(self, text, expected):
        assert ex._quantity(text) == expected

    @pytest.mark.parametrize(("text", "expected"), [("Grade: USP 99.5", "USP 99.5"), ("refined and premium stock", "refined premium"), ("hello", None)])
    def test_grade(self, text, expected):
        assert ex._grade(text) == expected


class TestPatternParsers:
    def test_buyer(self):
        text = "We need 20 MT, target USD 900/MT CNF Valencia within 6 weeks. Grade: USP. HACCP and Halal. Payment: LC at sight."
        out = ex.parse_buyer(text)
        assert out["incoterm"] == "CFR" and out["target_price_usd_per_mt"] == 900.0 and out["target_price_incoterm"] == "CFR"
        assert out["quantity_mt"] == 20.0 and out["destination_port"] == "Valencia" and out["delivery_by_days"] == 42
        assert "HACCP" in out["certifications"] and out["payment_terms"] == "LC at sight" and out["grade"] == "USP"

    @pytest.mark.parametrize("text", ["", None, "thanks"])
    def test_buyer_nothing(self, text):
        assert ex.parse_buyer(text) == {}

    def test_supplier(self):
        text = "FOB Chennai USD 700/MT. MOQ 10 MT. 200 MT per month. Lead time 3 weeks. ISO 9001. 30% advance T/T. Purity: 99.7"
        out = ex.parse_supplier(text)
        assert (out["fob_price_usd_per_mt"], out["moq_mt"], out["capacity_mt_per_month"], out["lead_time_days"]) == (700.0, 10.0, 200.0, 21)
        assert out["port_of_loading"] == "Chennai" and out["certifications"] == ["ISO 9001"] and out["grade"] == "99.7"
        assert out["payment_terms"].startswith("30% advance")

    def test_supplier_port_label(self):
        assert ex.parse_supplier("port of loading: Mundra")["port_of_loading"] == "Mundra"

    @pytest.mark.parametrize("label", ["Port of loading", "PORT OF LOADING"])
    def test_capitalised_port_label(self, label):
        assert ex.parse_supplier(f"{label}: Mundra")["port_of_loading"] == "Mundra"

    def test_ex_works_is_not_a_port(self):
        assert "port_of_loading" not in ex.parse_supplier("Ex Works price on request")

    @pytest.mark.parametrize("label", ["Port of destination", "Port of discharge", "port of discharge"])
    def test_buyer_port_label(self, label):
        assert ex.parse_buyer(f"{label}: Valencia")["destination_port"] == "Valencia"

    def test_supplier_nothing(self):
        assert ex.parse_supplier(None) == {}


class TestClean:
    def test_types_are_normalised(self):
        out = ex._clean({"quantity_mt": "1,000", "moq_mt": 5, "lead_time_days": "21", "certifications": "ISO", "grade": " x "},
                        {"quantity_mt": "", "moq_mt": "", "lead_time_days": "", "certifications": "", "grade": ""})
        assert out == {"quantity_mt": 1000.0, "moq_mt": 5.0, "lead_time_days": 21, "certifications": ["ISO"], "grade": "x"}

    @pytest.mark.parametrize("data", [{"quantity_mt": "lots"}, {"quantity_mt": 0}, {"quantity_mt": -1}, {"quantity_mt": None}, {"quantity_mt": "null"}, {"quantity_mt": ""}])
    def test_bad_numbers_are_dropped(self, data):
        assert ex._clean(data, {"quantity_mt": ""}) == {}

    def test_bad_days_are_dropped(self):
        assert ex._clean({"lead_time_days": "soon"}, {"lead_time_days": ""}) == {}

    def test_long_text_is_cut(self):
        assert len(ex._clean({"notes": "x" * 500}, {"notes": ""})["notes"]) == 200


class TestLlmExtraction:
    @staticmethod
    def _client(content=None, error=None):
        chat = AsyncMock(side_effect=error) if error else AsyncMock(return_value=SimpleNamespace(content=content))
        return patch("shared.llm_gateway.LLMClient", return_value=MagicMock(chat=chat)), chat

    @pytest.fixture(autouse=True)
    def _no_memory(self):
        with patch("shared.orchestration.memory.context_for", new=AsyncMock(return_value="")):
            yield

    @pytest.mark.parametrize(("content", "expected"), [
        ('{"grade": "USP"}', {"grade": "USP"}),
        ('```json\n{"grade": "USP"}\n```', {"grade": "USP"}),
        ('```\n{"grade": "USP"}\n```', {"grade": "USP"}),
        ('Sure: {"grade": "USP"} done', {"grade": "USP"}),
        ("no json", None), ("{broken", None), ("[1]", None), (None, None),
    ])
    async def test_reply_parsing(self, content, expected):
        client, _chat = self._client(content)
        with client:
            assert await ex._llm("buyer", "text", ex._BUYER_FIELDS, None) == expected

    async def test_brace_block_that_is_not_json(self):
        client, _chat = self._client("before {not json} after")
        with client:
            assert await ex._llm("buyer", "t", {}, None) is None

    async def test_gateway_unavailable(self):
        from shared.llm_gateway import LLMConfigurationError
        client, _chat = self._client(error=LLMConfigurationError("no url"))
        with client:
            assert await ex._llm("buyer", "t", {}, None) is None

    async def test_unexpected_failure(self):
        client, _chat = self._client(error=RuntimeError("x"))
        with client:
            assert await ex._llm("buyer", "t", {}, None) is None

    async def test_product_and_knowledge_reach_the_prompt(self):
        client, chat = self._client("{}")
        with client, patch("shared.orchestration.memory.context_for", new=AsyncMock(return_value="grades list")):
            await ex._llm("buyer", "t", {}, "glycerine")
        system = chat.await_args.kwargs["messages"][0]["content"]
        assert "of glycerine" in system and "grades list" in system

    async def test_memory_failure_is_ignored(self):
        client, _chat = self._client("{}")
        with client, patch("shared.orchestration.memory.context_for", new=AsyncMock(side_effect=RuntimeError)):
            assert await ex._llm("buyer", "t", {}, None) == {}

    async def test_buyer_llm_overrides_patterns(self):
        with patch.object(ex, "_llm", new=AsyncMock(return_value={"quantity_mt": 50})):
            out = await ex.extract_buyer("need 20 MT")
        assert out["quantity_mt"] == 50.0 and out["confidence"] == 0.85 and out["source"] == "email"

    @pytest.mark.parametrize(("text", "confidence"), [("need 20 MT", 0.6), ("thanks", 0.0)])
    async def test_buyer_without_llm(self, text, confidence):
        with patch.object(ex, "_llm", new=AsyncMock(return_value=None)):
            assert (await ex.extract_buyer(text))["confidence"] == confidence

    async def test_supplier_with_and_without_llm(self):
        with patch.object(ex, "_llm", new=AsyncMock(return_value={"moq_mt": 5})):
            assert (await ex.extract_supplier("USD 700/MT"))["moq_mt"] == 5.0
        with patch.object(ex, "_llm", new=AsyncMock(return_value=None)):
            assert (await ex.extract_supplier("USD 700/MT")) == {"fob_price_usd_per_mt": 700.0}


# ---------------------------------------------------------------------------
# HITL notifications
# ---------------------------------------------------------------------------

class TestSse:
    @pytest.fixture(autouse=True)
    def _reset(self):
        nt._sse_clients.clear()
        nt._recent_events.clear()
        yield
        nt._sse_clients.clear()
        nt._recent_events.clear()

    async def test_fan_out(self):
        _a, q1 = nt.register_sse_client()
        _b, q2 = nt.register_sse_client()
        await nt.broadcast({"n": 1})
        assert q1.get_nowait() == {"n": 1} and q2.get_nowait() == {"n": 1}

    async def test_deregister(self):
        client, _queue = nt.register_sse_client()
        nt.deregister_sse_client(client)
        nt.deregister_sse_client(client)
        assert nt._sse_clients == {}

    async def test_replay_buffer_is_bounded(self):
        for i in range(nt._MAX_RECENT + 5):
            await nt.broadcast({"n": i})
        events = nt.get_recent_events()
        assert len(events) == nt._MAX_RECENT and events[0] == {"n": 5}

    async def test_full_queue_drops_only_for_that_client(self):
        _a, slow = nt.register_sse_client()
        for _ in range(slow.maxsize):
            slow.put_nowait({})
        _b, fast = nt.register_sse_client()
        await nt.broadcast({"n": 1})
        assert fast.get_nowait() == {"n": 1}


class TestWebhook:
    @pytest.fixture
    def sent(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(handler.status)

        handler.status = 200
        with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)):
            yield requests, handler

    async def test_signed(self, sent):
        requests, _handler = sent
        await nt._post_webhook("https://x.test/h", "s", {"event_type": "created"})
        request = requests[0]
        assert request.headers["x-portless-signature"] == "sha256=" + hmac.new(b"s", request.content, hashlib.sha256).hexdigest()
        assert request.headers["x-portless-event"] == "created" and json.loads(request.content)["event_type"] == "created"

    async def test_no_secret_and_error_status(self, sent):
        requests, handler = sent
        handler.status = 500
        await nt._post_webhook("https://x.test/h", "", {})
        assert requests[0].headers["x-portless-event"] == "hitl"

    async def test_network_failure_is_contained(self):
        with patch("httpx.AsyncClient", side_effect=RuntimeError("down")):
            await nt._post_webhook("https://x.test/h", "s", {})


class TestNotify:
    @pytest.fixture(autouse=True)
    def _settings(self, monkeypatch):
        from services.hitl_approval.config import settings
        monkeypatch.setattr(settings, "notify_webhook_url", "")
        nt._recent_events.clear()
        self.settings = settings

    async def test_webhook_when_configured(self, monkeypatch):
        monkeypatch.setattr(self.settings, "notify_webhook_url", "https://x.test")
        monkeypatch.setattr(self.settings, "notify_webhook_secret", "s")
        with patch.object(nt, "_post_webhook", new=AsyncMock()) as post:
            await nt.notify("approval_created", {"id": 1})
            await asyncio.sleep(0)
        assert post.await_args.args[0] == "https://x.test" and post.await_args.args[2]["event_type"] == "approval_created"

    async def test_telegram_for_a_waiting_gate(self):
        with patch("shared.telegram.configured", return_value=True), patch("shared.telegram.send_message", new=AsyncMock()) as send:
            await nt.notify("approval_created", {"status": "PENDING", "action": "ISSUE_PO"})
            await asyncio.sleep(0)
        send.assert_awaited_once()

    async def test_no_telegram_for_auto_approved(self):
        with patch("shared.telegram.configured", return_value=True), patch("shared.telegram.send_message", new=AsyncMock()) as send:
            await nt.notify("approval_created", {"status": "APPROVED"})
            await asyncio.sleep(0)
        send.assert_not_awaited()

    async def test_telegram_failure_is_contained(self):
        with patch("shared.telegram.configured", side_effect=RuntimeError):
            await nt.notify("approval_created", {"status": "PENDING"})

    @pytest.mark.parametrize(("kind", "approval", "expected"), [
        ("approval_countersign_needed", {}, True), ("approval_created", {"status": "PENDING"}, True),
        ("approval_created", {"status": "APPROVED"}, False), ("approval_resolved", {"status": "PENDING"}, False),
    ])
    def test_worthy(self, kind, approval, expected):
        assert nt._telegram_worthy(kind, approval) is expected

    def test_full_text(self):
        text = nt._telegram_text("approval_countersign_needed", {
            "action": "ISSUE_PO", "deal_id": "D1", "autonomy_level": "L0", "financial_impact_usd": 1234.5, "summary": "s" * 400,
            "approvals": [{"approved_by": "ops"}, {}], "expires_at": "T", "default_on_timeout": "REJECT", "deep_link": "http://l",
        })
        assert "Second approver needed" in text and "Amount: 1,234.50 USD" in text and "ops, ?" in text and "Auto-reject at T" in text and "http://l" in text

    def test_minimal_text(self):
        text = nt._telegram_text("approval_created", {"action": "A", "deal_id": "D", "autonomy_level": "L0"})
        assert text.startswith("PortLess · Approval needed") and "Amount" not in text
