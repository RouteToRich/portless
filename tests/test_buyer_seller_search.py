"""Buyer / seller search: the product name sent to Apify is a searchable trade name
(never the official HS text), junk pages (lead lists, data sites, promotion bodies) are
dropped, exporters posing as importers and buyers from another country are flagged, and
company names read cleanly."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.trade_min import directory as dr
from services.trade_min import quality as q
from services.trade_min import repository as repo_mod
from services.trade_min import service as svc
from services.trade_min.schemas import BuyerSignal, ScanRequest
from shared import hs_names
from shared.connectors import apify as ap


class TestSearchName:
    @pytest.mark.parametrize(("code", "expected"), [
        ("071220", "dried onions"), ("091030", "turmeric"), ("091020", "saffron"), ("090422", "chilli powder"),
        ("030617", "frozen shrimp"), ("100610", "paddy rice"), ("847130", "automatic data processing"),
        ("620342", "trousers"),
    ])
    def test_every_code_gets_a_searchable_name(self, code, expected):
        assert hs_names.search_name(code) == expected
        assert not hs_names.looks_unsearchable(hs_names.search_name(code))

    @pytest.mark.parametrize("name", ["", "100610", "Vegetables; onions, whole, cut, sliced…",
                                      "rice in the husk paddy or rough grade"])
    def test_unsearchable_names(self, name):
        assert hs_names.looks_unsearchable(name)

    def test_operator_name_wins_when_searchable(self):
        assert hs_names.search_keyword("basmati rice", "100630") == "basmati rice"
        assert hs_names.search_keyword("Cereals; rice in the husk (paddy or rough)", "100610") == "paddy rice"

    def test_picker_results_carry_the_search_name(self):
        hit = next(r for r in hs_names.search("onions") if r["code"] == "071220")
        assert hit["search_name"] == "dried onions"


class TestScanUsesTheSearchName:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        repo_mod._repository = repo_mod.InMemoryFallbackRepository()
        dr.reset_directory()
        monkeypatch.setattr(svc, "get_publisher", lambda: AsyncMock())
        yield
        repo_mod._repository = None
        dr.reset_directory()

    async def test_official_text_from_the_picker_searches_the_trade_name(self, monkeypatch):
        asked: list[dict] = []

        async def fake(name, req):
            if name == "apify_company_search":
                asked.append(req)
            return None

        monkeypatch.setattr(svc, "_safe_quote", fake)
        await svc.scan_opportunities(ScanRequest(
            hs_codes=["071220"], target_markets=["DE"], worldwide_search=False,
            product_names={"071220": "Vegetables; onions, whole, cut, sliced, broken or in powder but not…"}))
        assert asked and {r["keyword"] for r in asked} == {"dried onions"}

    def test_keyword_without_curated_name_is_never_a_bare_number(self):
        assert svc._keyword_for("120740") == "sesame seeds"
        assert not svc._keyword_for("847130").isdigit()


class TestJunkFilters:
    @pytest.mark.parametrize(("title", "domain"), [
        ("Seafood Buyers in Spain: 150+ Verified Buyers", "eximhub.com"),
        ("India Inc", "ibef.org"),
        ("Spice importers database Germany", "example.com"),
        ("MURMAN SEAFOOD COMPANY, LLC", "megafishnet.com"),
    ])
    def test_lead_lists_and_data_sites_are_dropped(self, title, domain):
        assert ap._not_a_company({"page_title": title, "domain": domain})

    @pytest.mark.parametrize(("title", "domain"), [("Spanish seafood wholesaler", "asturpesca.com"),
                                                   ("Your Spice & Herb Specialist in Hamburg", "husarich.de")])
    def test_real_companies_stay(self, title, domain):
        assert not ap._not_a_company({"page_title": title, "domain": domain})


class TestBuyerFlags:
    def _b(self, name, country, **kw):
        return BuyerSignal(buyer_name=name, buyer_country=country, signal_type="web_search",
                           source="apify_company_search", urgency_tier=1, **kw)

    def test_exporter_name_and_other_country(self):
        exporter, abroad, local = self._b("Hexa Global Exporters", "DE"), self._b("Ahkrice", "PK"), self._b("Husarich", "DE")
        assert q.check_buyers([exporter, abroad, local], "DE") == 1
        assert q.ROLE_SUSPECT in exporter.flags and abroad.flags == [q.WRONG_MARKET] and local.flags == []

    def test_india_market_never_flags_other_country(self):
        b = self._b("Acme", "AE")
        q.check_buyers([b], "IN")
        assert q.WRONG_MARKET not in b.flags


class TestNames:
    @pytest.mark.parametrize(("label", "expected"), [
        ("magnafoodservice", "Magna Food Service"), ("chhcwholesales", "Chhc Wholesales"),
        ("ricemasterglobal", "Rice Master Global"),
        ("fishandco", "Fish & Co"), ("spice-and-co", "Spice & Co"), ("cocoaexports", "Cocoa Exports"),
        ("ricecorp", "Rice Corp"), ("jimmyandsons", "Jimmy & Sons"),
    ])
    def test_more_run_together_domains(self, label, expected):
        assert ap.split_domain_label(label) == expected

    def test_leading_greeting_is_dropped(self):
        assert ap._clean_name("Today, Punjab Rice Ltd") == "Punjab Rice Ltd"
        assert ap._clean_name("Hello, we sell rice") == "Hello, we sell rice"  # not a company name after the comma
