"""Prospect matching: price book, landed cost, pair scoring, indicative offers,
buyer replies (counter / accept), confirm match (prospect → deal), quotes → offers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agents.negotiation_agent.repository  # noqa: F401
import services.deals.service  # noqa: F401
import services.hitl_approval.service  # noqa: F401
import shared.orchestration.continuations  # noqa: F401
from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import (
    BuyerCandidate, BuyerRequirement, DealRecord, DealStatus, IndicativeOffer, SupplierOffer,
)
from shared import pricebook
from shared.orchestration import landed_cost as lc
from shared.orchestration import matchmaking as mm

NOW = datetime.now(timezone.utc)
PRIORS = {"buyer": 900.0, "supplier": 700.0, "source": "comtrade", "tariff_pct": 1.7}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MATCHMAKING_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_ON_DEAL_CREATED", "false")
    monkeypatch.setenv("PRICEBOOK_MONGO", "false")
    for v in ("LANDED_FREIGHT_USD_PER_MT", "LANDED_FREIGHT_USD_PER_MT_ES", "MATCH_MIN_PROBABILITY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("OUTCOMES_MONGO", "false")
    pricebook.reset_for_tests()
    from shared.orchestration import outcomes
    outcomes.reset_for_tests()
    monkeypatch.setattr(pricebook, "public_priors", AsyncMock(return_value=dict(PRIORS)))
    yield
    pricebook.reset_for_tests()


@pytest.fixture
def deals_repo(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    return repo


@pytest.fixture
def gates(monkeypatch):
    created = []

    async def _create(req):
        created.append(req)
        return SimpleNamespace(approval_id=f"APPR-{len(created)}")

    monkeypatch.setattr("services.hitl_approval.service.create_approval", _create)
    return created


@pytest.fixture
def published(monkeypatch):
    events = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


def _cand(cid, status="contacted", **kw):
    return BuyerCandidate(candidate_id=cid, legal_name=f"Buyer {cid}", country="ES", email=f"buy@{cid}.es",
                          status=status, **kw)


def _offer(sid, fob=650.0, **kw):
    base = dict(offer_id=f"OFR-{sid}", supplier_id=sid, company_name=f"Supplier {sid}", email=f"sales@{sid}.in",
                fob_price_usd_per_mt=fob, gst_verified=True, iec_active=True, received_at=NOW, origin_deal_id="DEAL-M1")
    base.update(kw)
    return SupplierOffer(**base)


def _deal(cands=(), offers=(), deal_id="DEAL-M1", **kw):
    return DealRecord(deal_id=deal_id, operator_id="op", org_id="portless", hs_code="290543", market="ES",
                      status=DealStatus.MATCHING, created_at=NOW, updated_at=NOW, quantity_mt=25.0,
                      buyer_candidates=list(cands), supplier_offers=list(offers), **kw)


# ---------------------------------------------------------------------------
class TestPriceBook:
    def test_prior_only(self):
        est = pricebook.estimate_from([], hs_code="290543", side="buyer", prior=900.0, prior_source="comtrade")
        assert est.confidence == "prior" and est.median == 900.0 and est.p25 < 900 < est.p75

    def test_observations_pull_the_estimate_and_old_ones_count_less(self):
        obs = [pricebook.PriceObservation(hs_code="290543", side="supplier", kind="supplier_quote",
                                          price_usd_per_mt=600.0) for _ in range(4)]
        obs.append(pricebook.PriceObservation(hs_code="290543", side="supplier", kind="supplier_quote",
                                              price_usd_per_mt=2000.0, observed_at=NOW - timedelta(days=720)))
        est = pricebook.estimate_from(obs, hs_code="290543", side="supplier", prior=700.0)
        assert est.median == 600.0 and est.n_observations == 5 and est.confidence == "high"

    def test_our_own_offers_are_not_evidence(self):
        obs = [pricebook.PriceObservation(hs_code="290543", side="buyer", kind="indicative_offer", price_usd_per_mt=5000.0)]
        assert pricebook.estimate_from(obs, hs_code="290543", side="buyer").confidence == "none"

    @pytest.mark.asyncio
    async def test_record_and_read(self):
        await pricebook.record(pricebook.PriceObservation(hs_code="290543", side="buyer", kind="buyer_target",
                                                          price_usd_per_mt=880.0, market="es"))
        rows = await pricebook.observations("290543")
        assert len(rows) == 1 and rows[0].market == "ES"


class TestLandedCost:
    def test_min_offer_keeps_the_margin_floor_after_fees(self):
        c = lc.landed(650.0, "ES", 25.0, incoterm="CIF", margin_floor_pct=8.0)
        assert c.freight_usd_per_mt == 95.0 and c.inspection_usd_per_mt == 24.0
        # margin left at the minimum offer = floor
        assert round((c.min_offer_usd_per_mt - c.cost_usd_per_mt) / c.min_offer_usd_per_mt * 100, 1) == 8.0

    def test_fob_buyer_pays_no_freight(self):
        assert lc.landed(650.0, "ES", 25.0, incoterm="FOB").freight_usd_per_mt == 0.0

    def test_incoterm_conversion_round_trip(self):
        assert abs(lc.from_cif(lc.to_cif(700.0, "FOB", "ES"), "FOB", "ES") - 700.0) < 0.05


# ---------------------------------------------------------------------------
def _est(side, median, sigma):
    return pricebook.PriceEstimate(hs_code="290543", side=side, median=median, sigma=sigma, p25=median * 0.9,
                                   p75=median * 1.1, confidence="prior")


class TestScoring:
    B, S = _est("buyer", 900.0, 60.0), _est("supplier", 700.0, 50.0)

    def test_firm_profitable_pair(self):
        cand = _cand("a", "interested", requirement=BuyerRequirement(target_price_usd_per_mt=950.0, incoterm="CIF",
                                                                       quantity_mt=25, grade="refined USP"))
        p = mm.score_pair(_deal(), cand, _offer("s1", 650.0, grade="refined 99.7% USP"), buyer_est=self.B, supplier_est=self.S)
        assert p.feasible and p.firm and p.buyer_price.basis == "stated" and p.supplier_price.basis == "quoted"
        assert p.margin_pct > 8 and p.p_margin > 0.9 and p.spec_fit == 1.0
        assert p.next_step.startswith("Confirm this match")

    def test_moq_and_grade_conflict_block(self):
        cand = _cand("a", "interested", requirement=BuyerRequirement(quantity_mt=10, grade="refined USP"))
        p = mm.score_pair(_deal(), cand, _offer("s1", moq_mt=20, grade="crude 80%"), buyer_est=self.B, supplier_est=self.S)
        assert not p.feasible and p.probability == 0.0
        assert any("minimum" in b for b in p.blockers) and any("grade mismatch" in b for b in p.blockers)

    def test_unknown_buyer_price_uses_the_estimate_and_is_not_firm(self):
        p = mm.score_pair(_deal(), _cand("a", "interested"), _offer("s1"), buyer_est=self.B, supplier_est=self.S)
        assert p.buyer_price.basis == "estimate" and not p.firm and "buyer target price" in p.missing
        assert p.next_step.startswith("Send an indicative offer")

    def test_capacity_and_timing(self):
        cand = _cand("a", "interested", requirement=BuyerRequirement(quantity_mt=500, delivery_by_days=30))
        p = mm.score_pair(_deal(), cand, _offer("s1", capacity_mt_per_month=100, lead_time_days=20),
                          buyer_est=self.B, supplier_est=self.S)
        assert any("cannot deliver" in b for b in p.blockers)

    def test_engaged_buyers_rank_higher(self):
        hot = mm.score_pair(_deal(), _cand("a", "interested"), _offer("s1"), buyer_est=self.B, supplier_est=self.S)
        cold = mm.score_pair(_deal(), _cand("b", "pending"), _offer("s1"), buyer_est=self.B, supplier_est=self.S)
        assert hot.probability > cold.probability


# ---------------------------------------------------------------------------
class TestFlow:
    @pytest.mark.asyncio
    async def test_interested_buyer_gets_an_indicative_offer_gate(self, deals_repo, gates):
        await deals_repo.save(_deal([_cand("a", "interested")], [_offer("s1", 650.0, lead_time_days=15)]))
        out = await mm.evaluate("DEAL-M1")
        assert [g["action"] for g in out["gates"]] == ["SEND_INDICATIVE_OFFER"]
        req = gates[0]
        assert req.action == "SEND_INDICATIVE_OFFER" and req.autonomy_level == "L0"
        p = req.callback_payload
        assert p["price_low_usd_per_mt"] >= p["min_offer_usd_per_mt"] and "Lead time about 15 days" in p["body"]
        assert "[BUY:DEAL-M1:a]" in p["subject"]

    @pytest.mark.asyncio
    async def test_counter_reply_makes_the_pair_firm_and_proposes_the_match(self, deals_repo, gates):
        io = IndicativeOffer(offer_id="OFR-s1", price_low_usd_per_mt=905, price_high_usd_per_mt=935, sent_at=NOW)
        cand = _cand("a", "interested", indicative_offer=io)
        await deals_repo.save(_deal([cand], [_offer("s1", 650.0)]))
        deal = await deals_repo.get("DEAL-M1")
        res = await mm.handle_buyer_reply(deal, deal.buyer_candidates[0], "maria@a.es",
                                          "Too high for us. We can do USD 890/MT CIF Valencia for 25 MT.", "unclear")
        assert res["indicative_offer_response"] == "countered"
        cand = (await deals_repo.get("DEAL-M1")).buyer_candidates[0]
        assert cand.requirement.target_price_usd_per_mt == 890.0 and cand.requirement.target_price_basis == "counter"
        assert cand.reply_address == "maria@a.es" and cand.requirement.destination_port == "Valencia"
        assert [g.action for g in gates] == ["CONFIRM_MATCH"]
        obs = await pricebook.observations("290543", "buyer")
        assert obs[0].kind == "buyer_counter" and obs[0].price_usd_per_mt == 890.0

    @pytest.mark.asyncio
    async def test_acceptance_takes_the_offered_price(self, deals_repo, gates):
        io = IndicativeOffer(offer_id="OFR-s1", price_low_usd_per_mt=905, price_high_usd_per_mt=935, sent_at=NOW)
        await deals_repo.save(_deal([_cand("a", "interested", indicative_offer=io)], [_offer("s1", 650.0)]))
        deal = await deals_repo.get("DEAL-M1")
        await mm.handle_buyer_reply(deal, deal.buyer_candidates[0], "maria@a.es",
                                    "Ok, we accept. Please send the proforma.", "interested")
        cand = (await deals_repo.get("DEAL-M1")).buyer_candidates[0]
        assert cand.indicative_offer.response == "accepted"
        assert cand.requirement.target_price_usd_per_mt == 905 and cand.requirement.target_price_basis == "accepted_offer"
        assert gates and gates[-1].action == "CONFIRM_MATCH"

    @pytest.mark.asyncio
    async def test_confirm_match_turns_the_prospect_into_a_deal(self, deals_repo, published):
        cand = _cand("a", "interested", reply_address="maria@a.es",
                     requirement=BuyerRequirement(target_price_usd_per_mt=950.0, target_price_basis="stated", quantity_mt=30))
        await deals_repo.save(_deal([cand, _cand("b")], [_offer("s1", 650.0), _offer("s2", 690.0)]))
        with patch("services.sourcing.service.list_rfqs", new=AsyncMock(return_value=[])):
            res = await mm.confirm_match("DEAL-M1", {"candidate_id": "a", "offer_id": "OFR-s1"})
        assert res["matched"] is True
        deal = await deals_repo.get("DEAL-M1")
        assert deal.status == DealStatus.NEGOTIATING
        roles = {getattr(p.role, "value", p.role): p for p in deal.parties}
        assert roles["buyer"].email == "maria@a.es" and roles["buyer"].party_id == "a"
        assert roles["supplier"].party_id == "s1" and roles["supplier"].email == "sales@s1.in"
        assert deal.winning_supplier_id == "s1" and deal.buyer_target_price_usd == 950.0 and deal.quantity_mt == 30
        assert deal.selected_pair["offer_id"] == "OFR-s1"
        assert deal.buyer_candidates[0].status == "selected"
        assert deal.status_history[-1].reason.startswith("Match confirmed: Buyer a")

    @pytest.mark.asyncio
    async def test_offers_from_other_prospects_of_the_same_hs_code_are_matched(self, deals_repo, gates):
        await deals_repo.save(_deal([], [_offer("s9", 640.0, origin_deal_id="DEAL-OTHER")], deal_id="DEAL-OTHER"))
        await deals_repo.save(_deal([_cand("a", "interested")], []))
        board = await mm.build_board(await deals_repo.get("DEAL-M1"))
        assert [o["supplier_id"] for o in board.offers] == ["s9"]
        assert board.pairs[0].origin_deal_id == "DEAL-OTHER"

    @pytest.mark.asyncio
    async def test_expired_offers_are_dropped(self, deals_repo):
        old = _offer("s1", received_at=NOW - timedelta(days=60), validity_days=30)
        await deals_repo.save(_deal([_cand("a", "interested")], [old]))
        assert (await mm.build_board(await deals_repo.get("DEAL-M1"))).offers == []

    @pytest.mark.asyncio
    async def test_send_indicative_offer_callback_records_the_offer(self, deals_repo):
        await deals_repo.save(_deal([_cand("a", "interested")], [_offer("s1")]))
        send = AsyncMock(return_value=SimpleNamespace(outcome=SimpleNamespace(value="sent")))
        with patch("services.email_gateway.sender.send_email", new=send):
            res = await mm.send_indicative_offer("DEAL-M1", {
                "candidate_id": "a", "offer_id": "OFR-s1", "to_address": "buy@a.es", "subject": "S", "body": "B",
                "price_low_usd_per_mt": 900, "price_high_usd_per_mt": 930, "incoterm": "CIF", "quantity_mt": 25})
        assert res["delivered"] is True
        cand = (await deals_repo.get("DEAL-M1")).buyer_candidates[0]
        assert cand.indicative_offer.price_low_usd_per_mt == 900 and cand.indicative_offer.sent_at


class TestQuoteBecomesOffer:
    @pytest.mark.asyncio
    async def test_receive_quote_records_offer_and_price_and_does_not_jump_to_negotiation(self, deals_repo, monkeypatch):
        from services.sourcing.repository import InMemorySourcingRepository
        from services.sourcing.schemas import QuoteIntakeRequest, VerificationResult
        from services.sourcing import service as sourcing
        monkeypatch.setattr("services.sourcing.service.get_repository", lambda: InMemorySourcingRepository())
        monkeypatch.setattr(sourcing, "_emit_event", AsyncMock())
        monkeypatch.setattr(sourcing, "_verify_supplier", AsyncMock(return_value=VerificationResult(
            supplier_id="s1", verified_at=NOW, raw={"gstn": {}, "dgft_iec": {}})))
        monkeypatch.setattr(sourcing, "_rescore", AsyncMock(return_value={"match_id": "M", "composite_score": 90,
                                                                         "decision": "SOURCE"}))
        moved = AsyncMock()
        monkeypatch.setattr(sourcing, "_transition_to_negotiating", moved)
        await deals_repo.save(_deal([_cand("a")], [], buyer_target_price_usd=900.0))
        await sourcing.receive_quote(QuoteIntakeRequest(
            rfq_id="RFQ-1", deal_id="DEAL-M1", supplier_id="s1", company_name="Supplier s1", unit_price_usd=640.0,
            grade="refined", moq_mt=20, lead_time_days=15, supplier_email="sales@s1.in"))
        deal = await deals_repo.get("DEAL-M1")
        offer = deal.supplier_offers[0]
        assert (offer.fob_price_usd_per_mt, offer.moq_mt, offer.grade, offer.email) == (640.0, 20, "refined", "sales@s1.in")
        assert offer.gst_verified is True and offer.verification_failed is False
        moved.assert_not_awaited()  # the match board decides now
        assert (await pricebook.observations("290543", "supplier"))[0].price_usd_per_mt == 640.0
