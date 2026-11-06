"""Prospects (MATCHING deals) name no counterparty before one is decided.

- The buyer party is empty while the outreach funnel is open; the candidate who
  replies "interested" becomes the buyer as a *fresh* party (no screening carried
  over from someone else).
- The supplier party is the Match Engine's SOURCE winner only; otherwise it is set
  when a verified quote wins or an operator confirms one (``set_supplier_party``).
- Company-search names: a legal name that names someone else on the page is not
  the company; cached rows get the current filters; HS 290543 searches "glycerine".
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agents.negotiation_agent.repository  # noqa: F401
import services.deals.service  # noqa: F401
import shared.orchestration.continuations  # noqa: F401
from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import BuyerCandidate, DealRecord, DealStatus
from services.deals.service import _seed_parties_from_opportunity, set_supplier_party
from shared.domain.models import Party
from shared.orchestration import outreach

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_ON_DEAL_CREATED", "false")
    monkeypatch.delenv("OUTREACH_ENABLED", raising=False)


@pytest.fixture
def deals_repo(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    return repo


def _opp():
    buyers = [SimpleNamespace(company_id="degrados.es", buyer_name="Degrados", buyer_country="ES", fit_score=80,
                              email="info@degrados.es", website="https://degrados.es", source="apify_company_search")]
    sellers = [SimpleNamespace(candidate_id="almsgroup.in", company_name="Almsgroup", email="sales@almsgroup.in",
                               website="https://almsgroup.in", source="apify_company_search", country="IN")]
    return SimpleNamespace(buyer_signals=buyers, seller_candidates=sellers)


class TestSeeding:
    def test_prospect_with_buyer_funnel_names_nobody(self):
        assert _seed_parties_from_opportunity(_opp(), None, "ES", buyer_funnel=True) == []

    def test_without_funnel_the_top_buyer_is_seeded(self):
        parties = _seed_parties_from_opportunity(_opp(), None, "ES", buyer_funnel=False)
        assert [(p.role.value if hasattr(p.role, "value") else p.role, p.legal_name) for p in parties] == [
            ("buyer", "Degrados")]

    def test_match_engine_winner_is_the_supplier(self):
        parties = _seed_parties_from_opportunity(_opp(), "almsgroup.in", "ES", buyer_funnel=True)
        assert len(parties) == 1 and parties[0].legal_name == "Almsgroup" and parties[0].email == "sales@almsgroup.in"


def _deal(parties=None, candidates=None, **kw) -> DealRecord:
    return DealRecord(deal_id="DEAL-P1", operator_id="op", org_id="portless", hs_code="290543", market="ES",
                      status=DealStatus.MATCHING, created_at=NOW, updated_at=NOW,
                      parties=parties or [], buyer_candidates=candidates or [], **kw)


class TestSupplierParty:
    @pytest.mark.asyncio
    async def test_chosen_supplier_replaces_a_different_one(self, deals_repo):
        old = Party(role="supplier", legal_name="Old Co", country="IN", party_id="old.in",
                    phone="+91 1", sanctions_status="clear")
        await deals_repo.save(_deal(parties=[old]))
        rfqs = [SimpleNamespace(dispatch_results=[SimpleNamespace(supplier_id="almsgroup.in", email="sales@almsgroup.in")])]
        with patch("services.sourcing.service.list_rfqs", new=AsyncMock(return_value=rfqs)):
            deal = await set_supplier_party("DEAL-P1", "almsgroup.in", company_name="Almsgroup", gstin="27AAA")
        sup = next(p for p in deal.parties if (getattr(p.role, "value", p.role)) == "supplier")
        assert (sup.party_id, sup.legal_name, sup.email, sup.gstin) == ("almsgroup.in", "Almsgroup", "sales@almsgroup.in", "27AAA")
        assert sup.sanctions_status == "unchecked" and sup.phone is None  # nothing from Old Co
        assert len(deal.parties) == 1

    @pytest.mark.asyncio
    async def test_operator_set_supplier_is_kept(self, deals_repo):
        op = Party(role="supplier", legal_name="Picked By Hand", country="IN", party_id="hand.in",
                   contact_source="operator")
        await deals_repo.save(_deal(parties=[op]))
        deal = await set_supplier_party("DEAL-P1", "almsgroup.in", company_name="Almsgroup")
        assert deal.parties[0].legal_name == "Picked By Hand"

    @pytest.mark.asyncio
    async def test_confirmed_selection_names_the_supplier(self, deals_repo):
        from services.deals.service import record_match_result
        await deals_repo.save(_deal())
        with patch("services.sourcing.service.list_rfqs", new=AsyncMock(return_value=[])):
            deal = await record_match_result("DEAL-P1", "almsgroup.in", company_name="Almsgroup",
                                             fob_price_usd=900.0, make_winner=True)
        assert deal.winning_supplier_id == "almsgroup.in"
        assert [p.legal_name for p in deal.parties] == ["Almsgroup"]


class TestInterestedBuyer:
    @pytest.mark.asyncio
    async def test_interested_candidate_replaces_a_stale_buyer_party(self, deals_repo, monkeypatch):
        monkeypatch.setenv("MATCHMAKING_ENABLED", "false")  # legacy path sets the party on "interested"
        stale = Party(role="buyer", legal_name="Degrados", country="ES", party_id="degrados.es",
                      phone="+34 1", sanctions_status="clear")
        cand = BuyerCandidate(candidate_id="iberalcohol.es", legal_name="IBERALCOHOL S.L", country="ES",
                              email="iberalcohol@iberalcohol.es", status="contacted")
        await deals_repo.save(_deal(parties=[stale], candidates=[cand]))
        neg_repo = AsyncMock()
        neg_repo.get_session_by_deal = AsyncMock(return_value=None)
        with patch("agents.negotiation_agent.repository.get_repository", return_value=neg_repo):
            res = await outreach.handle_reply("DEAL-P1", "iberalcohol.es", "compras@iberalcohol.es",
                                              "We are interested, please send your price.")
        assert res["buyer_set"] is True
        deal = await deals_repo.get("DEAL-P1")
        buyer = next(p for p in deal.parties if getattr(p.role, "value", p.role) == "buyer")
        assert (buyer.party_id, buyer.legal_name, buyer.email) == ("iberalcohol.es", "IBERALCOHOL S.L",
                                                                   "compras@iberalcohol.es")
        assert buyer.sanctions_status == "unchecked" and buyer.phone is None
        assert len(deal.parties) == 1


class TestCompanyNames:
    @pytest.mark.parametrize("row,expected", [
        # a client listed on the supplier's page, not the supplier (live scan 2026-11-06)
        ({"legal_name": "Ltd, Gas Authority of India Ltd", "domain": "magellanium.com"}, "Magellanium"),
        ({"legal_name": "There are many chemical manufacturers in the UAE. Hazchem Chemicals LLC",
          "domain": "hazchemicals.ae"}, "Hazchem Chemicals LLC"),
        ({"legal_name": "Kerala Spices Pvt. Ltd.", "domain": "keralaspices.in"}, "Kerala Spices Pvt. Ltd"),
        ({"legal_name": "Hindustan Unilever Limited", "domain": "hul.co.in"}, "Hindustan Unilever Limited"),
        ({"name": "https://www.brewmart.co.uk/glycerol-maturing-agent", "domain": "brewmart.co.uk"}, "Brewmart"),
        ({"name": "Alcohol and Beer Exporters, Distillers, Brewers, Bottlers", "domain": "khemanigroup.com"},
         "Khemanigroup"),
    ])
    def test_company_name(self, row, expected):
        from shared.connectors.apify import _company_name
        assert _company_name(row) == expected

    def test_refine_applies_current_rules_to_cached_rows(self):
        from shared.connectors.apify import refine_companies
        cached = [
            {"domain": "magellanium.com", "legal_name": "Ltd, Gas Authority of India Ltd",
             "name": "Ltd, Gas Authority of India Ltd", "page_title": "Ltd, Gas Authority of India Ltd",
             "company_name": "Ltd, Gas Authority of India Ltd"},
            {"domain": "www.chemicalbook.com", "name": "Glycerol manufacturers", "page_title": "Glycerol manufacturers",
             "company_name": "Chemicalbook"},
            {"domain": "www.intechopen.com", "name": "Market Prospecting", "page_title": "Market Prospecting",
             "company_name": "Intechopen"},
        ]
        assert [c["company_name"] for c in refine_companies(cached)] == ["Magellanium"]

    def test_glycerol_keyword(self):
        from shared.connectors.trade_intel import hs_keyword
        assert hs_keyword("290543") == "glycerine"
        assert hs_keyword("290511") == "glycerol alcohol"
        assert hs_keyword("999999") == "999999" and hs_keyword("999999", "HS 999999") == "HS 999999"
