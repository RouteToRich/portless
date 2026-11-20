"""Deal composition after negotiation (todo 13.1–13.4): firm offers on each counterparty's own
thread, the composition agent (one buyer × 1..N suppliers, hard rules, close now / wait, the
L0 gate), supplier legs (PO, QC, money, compliance per leg), keeping a qualifying prospect
open, and privacy with N parties per prospect."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.negotiation_agent.repository  # noqa: F401
import services.deals.service  # noqa: F401
import services.hitl_approval.service  # noqa: F401
import shared.orchestration.continuations  # noqa: F401
from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import (
    BuyerCandidate, BuyerRequirement, DealRecord, DealStatus, FirmOffer, SupplierLeg, SupplierOffer,
)
from shared.domain.enums import PartyRole
from shared.domain.models import Party
from shared.orchestration import composition, firm_offers, supplier_legs

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEAL_COMPOSITION_ENABLED", "true")
    monkeypatch.setenv("OUTCOMES_MONGO", "false")
    monkeypatch.setenv("PRICEBOOK_MONGO", "false")
    for v in ("LANDED_FREIGHT_USD_PER_MT", "COMPOSITION_MAX_SUPPLIERS", "COMPOSITION_QTY_TOLERANCE_PCT",
              "COMPOSITION_REQUIRE_VERIFIED", "COMPOSITION_ALLOW_MULTI_PORT", "MATCH_MIN_PROBABILITY",
              "COMPOSITION_MIN_SCORE"):
        monkeypatch.delenv(v, raising=False)
    from shared.orchestration import outcomes
    outcomes.reset_for_tests()
    # lane freight / incentives off: the regional estimate applies
    monkeypatch.setattr("shared.orchestration.matchmaking._costs", AsyncMock(return_value=(None, None, 0.0)))
    monkeypatch.setattr("shared.orchestration.matchmaking.offers_for",
                        AsyncMock(side_effect=lambda deal: list(deal.supplier_offers)))
    yield


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
        return SimpleNamespace(approval_id=f"APPR-{len(created)}", callback_payload=req.callback_payload)

    async def _list(status=None, deal_id=None):
        return []

    monkeypatch.setattr("services.hitl_approval.service.create_approval", _create)
    monkeypatch.setattr("services.hitl_approval.service.list_approvals", _list)
    monkeypatch.setattr("services.hitl_approval.service.withdraw_pending", AsyncMock(return_value=[]))
    return created


@pytest.fixture
def published(monkeypatch):
    events = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


@pytest.fixture
def mails(monkeypatch):
    sent = []

    async def _send(req):
        sent.append(req)
        return SimpleNamespace(outcome="sent")

    monkeypatch.setattr("services.email_gateway.sender.send_email", _send)
    monkeypatch.setattr("agents.negotiation_agent.email_history.record_deal_email", AsyncMock())
    return sent


def _fo(side, cid, price, qty=None, **kw):
    base = dict(offer_id=f"FO-{side[0].upper()}-{cid}", side=side, counterparty_id=cid, company_name=f"{cid} Co",
                email=f"desk@{cid.lower()}.com", price_usd_per_mt=price, quantity_mt=qty,
                incoterm="CIF" if side == "buyer" else "FOB", verified=True, received_at=NOW,
                valid_until=NOW + timedelta(days=10), source="reply")
    base.update(kw)
    return FirmOffer(**base)


def _deal(deal_id="DEAL-C1", **kw):
    base = dict(deal_id=deal_id, operator_id="op", org_id="portless", hs_code="100630", market="AE",
                status=DealStatus.MATCHING, created_at=NOW, updated_at=NOW, quantity_mt=1000.0)
    base.update(kw)
    return DealRecord(**base)


def _cand(cid, **kw):
    base = dict(candidate_id=cid, legal_name=f"{cid} Co", country="AE", email=f"desk@{cid.lower()}.com",
                status="interested", validation_status="verified")
    base.update(kw)
    return BuyerCandidate(**base)


def _sup(sid, fob, **kw):
    base = dict(offer_id=f"OFR-{sid}", supplier_id=sid, company_name=f"{sid} Co", email=f"desk@{sid.lower()}.com",
                fob_price_usd_per_mt=fob, gst_verified=True, iec_active=True, received_at=NOW, origin_deal_id="DEAL-C1")
    base.update(kw)
    return SupplierOffer(**base)


# The example from the to-do: buyer needs 1000 MT, A offers 800 MT, B 200 MT.
BUYER = _fo("buyer", "B1", 700.0, 1000.0)
SUP_A = _fo("supplier", "SA", 500.0, 800.0, port="Mundra")
SUP_B = _fo("supplier", "SB", 510.0, 200.0, port="Mundra")


# ---------------------------------------------------------------------------
# Firm offers (13.2)
# ---------------------------------------------------------------------------

class TestFirmOfferReading:
    def test_validity_until_a_date(self):
        end = firm_offers.parse_validity("Our offer is valid until 5 December 2026.", NOW)
        assert end is not None and (end.year, end.month, end.day) == (2026, 12, 5)

    def test_validity_for_days_and_words(self):
        assert firm_offers.parse_validity("validity: 10 days", NOW).date() == (NOW + timedelta(days=10)).date()
        assert firm_offers.parse_validity("valid for one week", NOW).date() == (NOW + timedelta(days=7)).date()

    def test_no_validity(self):
        assert firm_offers.parse_validity("price is USD 500 FOB", NOW) is None

    def test_ready_by_and_shipment_by(self):
        d = firm_offers.parse_date_by("Goods ready by 15 Jan 2027, loading at Mundra", NOW)
        assert (d.year, d.month, d.day) == (2027, 1, 15)
        assert firm_offers.parse_date_by("shipment by 2027-02-01", NOW).day == 1

    def test_date_without_year_is_the_next_one(self):
        past = NOW - timedelta(days=40)
        d = firm_offers.parse_date(past.strftime("%d %B"), NOW)
        assert d is not None and d > NOW - timedelta(days=1)


class TestLiveOffers:
    @pytest.mark.asyncio
    async def test_derived_from_board_and_stored_override(self):
        req = BuyerRequirement(quantity_mt=1000, target_price_usd_per_mt=700, target_price_basis="stated",
                               updated_at=NOW)
        deal = _deal(buyer_candidates=[_cand("B1", requirement=req)],
                     supplier_offers=[_sup("SA", 500, capacity_mt_per_month=800)],
                     firm_offers=[_fo("supplier", "SA", 495.0, 800.0)])
        offers = await firm_offers.live_offers(deal)
        by = {(o.side, o.counterparty_id): o for o in offers}
        assert by[("buyer", "B1")].source == "derived" and by[("buyer", "B1")].quantity_mt == 1000
        assert by[("supplier", "SA")].price_usd_per_mt == 495.0  # the stored (newer) firm offer wins

    @pytest.mark.asyncio
    async def test_expired_offer_never_enters(self):
        deal = _deal(firm_offers=[_fo("supplier", "SA", 495.0, 800.0, valid_until=NOW - timedelta(hours=1))])
        assert await firm_offers.live_offers(deal) == []

    @pytest.mark.asyncio
    async def test_an_expired_stored_offer_hides_the_older_derived_quote(self):
        old = NOW - timedelta(days=2)
        deal = _deal(supplier_offers=[_sup("SA", 500, received_at=old)],
                     firm_offers=[_fo("supplier", "SA", 495.0, 800.0, status="expired", expired_at=NOW - timedelta(hours=1),
                                      received_at=old)])
        assert await firm_offers.live_offers(deal) == []

    @pytest.mark.asyncio
    async def test_record_and_withdraw(self, deals_repo, monkeypatch):
        monkeypatch.setattr(composition, "run", AsyncMock(return_value={}))
        await deals_repo.save(_deal())
        await firm_offers.record("DEAL-C1", _fo("buyer", "B1", 700.0, 1000.0))
        await firm_offers.record("DEAL-C1", _fo("buyer", "B1", 710.0, 1000.0))
        deal = await deals_repo.get("DEAL-C1")
        assert [o.price_usd_per_mt for o in deal.firm_offers] == [710.0]
        deal = await firm_offers.withdraw("DEAL-C1", "buyer", "B1", "bought elsewhere")
        assert deal.firm_offers[0].status == "withdrawn"
        assert composition.run.await_count == 3  # every change re-runs the composition


class TestPricingPerThread:
    @pytest.mark.asyncio
    async def test_buyer_band_from_best_supply_mix(self):
        offers = [BUYER, SUP_A, SUP_B, _fo("supplier", "SC", 600.0, 1000.0)]
        b = await firm_offers.thread_bounds(_deal(), "buyer", "B1", offers=offers)
        assert b["supply_mix_fob_usd_per_mt"] == 502.0  # 800 × 500 + 200 × 510, not the 600 single source
        assert b["min_offer_usd_per_mt"] > 502.0

    @pytest.mark.asyncio
    async def test_supplier_ceiling_from_best_buyer(self):
        offers = [BUYER, _fo("buyer", "B2", 650.0, 500.0), SUP_A]
        s = await firm_offers.thread_bounds(_deal(), "supplier", "SA", offers=offers)
        assert s["best_buyer_cif_usd_per_mt"] == 700.0
        assert 400 < s["max_fob_usd_per_mt"] < 700

    def test_supply_mix_needs_cover(self):
        assert firm_offers.supply_mix_fob([SUP_B], 1000) is None


# ---------------------------------------------------------------------------
# Composition (13.3)
# ---------------------------------------------------------------------------

class TestCombinations:
    def test_the_todo_example_one_buyer_two_suppliers(self):
        combos, _ = composition.combinations(_deal(), [BUYER, SUP_A, SUP_B])
        best = combos[0]
        assert best.buyer_id == "B1" and len(best.legs) == 2
        assert {(leg.supplier_id, leg.quantity_mt) for leg in best.legs} == {("SA", 800.0), ("SB", 200.0)}
        assert best.quantity_mt == 1000.0 and best.margin_pct >= 8.0

    def test_single_supplier_too_small_alone(self):
        combos, rejected = composition.combinations(_deal(), [BUYER, SUP_A])
        assert combos == [] and any("below the buyer" in r for r in rejected)

    def test_tolerance_band(self, monkeypatch):
        monkeypatch.setenv("COMPOSITION_QTY_TOLERANCE_PCT", "25")
        combos, _ = composition.combinations(_deal(), [BUYER, SUP_A])
        assert combos and combos[0].quantity_mt == 800.0

    def test_moq_respected(self):
        ok = _fo("supplier", "SB", 510.0, 300.0, min_quantity_mt=300.0)  # 800 + 300 = 1100: inside 10 %
        best = composition.combinations(_deal(), [BUYER, SUP_A, ok])[0][0]
        assert {leg.supplier_id: leg.quantity_mt for leg in best.legs} == {"SA": 800.0, "SB": 300.0}
        big_moq = _fo("supplier", "SB", 510.0, 400.0, min_quantity_mt=350.0)  # 800 + 350 = 1150
        combos, rejected = composition.combinations(_deal(), [BUYER, SUP_A, big_moq])
        assert all(len(c.legs) == 1 or "SB" not in {leg.supplier_id for leg in c.legs} for c in combos)
        assert any("above the buyer" in r for r in rejected)

    def test_grade_conflict_blocks(self):
        b = _fo("buyer", "B1", 700.0, 200.0, grade="food grade")
        s = _fo("supplier", "SA", 500.0, 800.0, grade="crude technical")
        combos, rejected = composition.combinations(_deal(), [b, s])
        assert combos == [] and any("grade" in r for r in rejected)

    def test_unverified_party_blocked_by_default(self, monkeypatch):
        s = _fo("supplier", "SA", 500.0, 1000.0, verified=None)
        combos, rejected = composition.combinations(_deal(), [BUYER, s])
        assert combos == [] and any("not verified" in r for r in rejected)
        monkeypatch.setenv("COMPOSITION_REQUIRE_VERIFIED", "false")
        assert composition.combinations(_deal(), [BUYER, s])[0]

    def test_related_suppliers_never_combined(self):
        a = _fo("supplier", "SA", 500.0, 800.0, email="a@samegroup.in")
        b = _fo("supplier", "SB", 510.0, 200.0, email="b@samegroup.in")
        combos, rejected = composition.combinations(_deal(), [BUYER, a, b])
        assert combos == [] and any("related" in r for r in rejected)

    def test_ready_too_late_for_the_shipment(self):
        b = _fo("buyer", "B1", 700.0, 1000.0, date_by=NOW + timedelta(days=12))
        late = _fo("supplier", "SA", 500.0, 1000.0, date_by=NOW + timedelta(days=10))  # + 10 days transit (Gulf)
        combos, rejected = composition.combinations(_deal(), [b, late])
        assert combos == [] and any("too late" in r for r in rejected)

    def test_leg_margin_floor(self):
        dear = _fo("supplier", "SB", 690.0, 200.0)
        combos, rejected = composition.combinations(_deal(), [BUYER, SUP_A, dear])
        assert combos == [] and any("leg margin" in r for r in rejected)

    def test_max_legs(self, monkeypatch):
        monkeypatch.setenv("COMPOSITION_MAX_SUPPLIERS", "1")
        combos, _ = composition.combinations(_deal(), [BUYER, SUP_A, SUP_B])
        assert combos == []

    def test_ports_must_match_when_multi_port_is_off(self, monkeypatch):
        monkeypatch.setenv("COMPOSITION_ALLOW_MULTI_PORT", "false")
        b = SUP_B.model_copy(update={"port": "Kandla"})
        combos, rejected = composition.combinations(_deal(), [BUYER, SUP_A, b])
        assert combos == [] and any("ports" in r for r in rejected)
        monkeypatch.setenv("COMPOSITION_ALLOW_MULTI_PORT", "true")
        best = composition.combinations(_deal(), [BUYER, SUP_A, b])[0][0]
        assert any("ports of loading" in r for r in best.risks)

    def test_inspection_is_per_facility(self):
        one, _, _ = composition.leg_economics(500.0, 1000.0, 700.0, "CIF", "AE")
        small, _, _ = composition.leg_economics(500.0, 100.0, 700.0, "CIF", "AE")
        assert small > one  # a small leg carries its own inspection


class TestDecide:
    def _best(self, **kw):
        combos, _ = composition.combinations(_deal(), [BUYER, SUP_A, SUP_B])
        return combos[0].model_copy(update=kw)

    def test_close_now_when_an_offer_expires_soon(self):
        best = self._best(earliest_validity=NOW + timedelta(hours=10))
        assert composition.decide(best, now=NOW, pending=3, expected_gain=99999)[0] == "close_now"

    def test_wait_when_pending_quotes_could_beat_it(self):
        best = self._best(earliest_validity=NOW + timedelta(days=9))
        decision, why = composition.decide(best, now=NOW, pending=2, expected_gain=best.margin_usd)
        assert decision == "wait" and "pending" in why

    def test_close_now_when_waiting_is_not_worth_it(self):
        best = self._best(earliest_validity=NOW + timedelta(days=9))
        assert composition.decide(best, now=NOW, pending=1, expected_gain=1.0)[0] == "close_now"

    def test_buyer_deadline_close(self):
        best = self._best(earliest_validity=NOW + timedelta(days=9), shipment_by=NOW + timedelta(days=3))
        assert composition.decide(best, now=NOW, pending=5, expected_gain=1e9)[0] == "close_now"

    def test_none(self):
        assert composition.decide(None, now=NOW, pending=0, expected_gain=0)[0] == "none"


def _composed_deal():
    req = BuyerRequirement(quantity_mt=1000, target_price_usd_per_mt=700, target_price_basis="stated",
                           target_price_incoterm="CIF", updated_at=NOW)
    return _deal(buyer_candidates=[_cand("B1", requirement=req)],
                 supplier_offers=[_sup("SA", 500, capacity_mt_per_month=800, port_of_loading="Mundra"),
                                  _sup("SB", 510, capacity_mt_per_month=200, port_of_loading="Mundra"),
                                  _sup("SC", 520, capacity_mt_per_month=300, port_of_loading="Mundra")],
                 firm_offers=[_fo("buyer", "B1", 700.0, 1000.0, valid_until=NOW + timedelta(hours=20),
                                  email="desk@b1.com")])


class TestRunAndGate:
    @pytest.mark.asyncio
    async def test_run_opens_one_l0_gate_with_runner_ups(self, deals_repo, gates, mails):
        await deals_repo.save(_composed_deal())
        out = await composition.run("DEAL-C1", reason="test")
        assert out["decision"] == "close_now" and out["approval_id"]
        gate = [g for g in gates if g.action == "CONFIRM_DEAL_COMPOSITION"][0]
        assert gate.autonomy_level == "L0" and "money_related_composition" in gate.risk_flags
        combo = gate.callback_payload["combination"]
        assert {leg["supplier_id"] for leg in combo["legs"]} == {"SA", "SB"}
        assert gate.callback_payload["best_single"] is None or gate.callback_payload["best_single"]["legs"]
        deal = await deals_repo.get("DEAL-C1")
        assert deal.composition["latest"]["decision"] == "close_now"

    @pytest.mark.asyncio
    async def test_a_rejected_combination_is_not_proposed_again(self, deals_repo, gates, mails):
        await deals_repo.save(_composed_deal())
        await composition.run("DEAL-C1")
        sig = gates[-1].callback_payload["signature"]
        await composition.reject("DEAL-C1", {"signature": sig})
        n = len([g for g in gates if g.action == "CONFIRM_DEAL_COMPOSITION"])
        await composition.run("DEAL-C1")
        assert len([g for g in gates if g.action == "CONFIRM_DEAL_COMPOSITION"]) == n

    @pytest.mark.asyncio
    async def test_off_switch(self, deals_repo, monkeypatch):
        monkeypatch.setenv("DEAL_COMPOSITION_ENABLED", "false")
        assert await composition.run("DEAL-C1") == {"enabled": False}

    @pytest.mark.asyncio
    async def test_matchmaking_proposes_no_confirm_match_with_composition_on(self, deals_repo, gates, mails, monkeypatch):
        from shared import pricebook
        from shared.orchestration import matchmaking
        monkeypatch.setattr(pricebook, "public_priors", AsyncMock(return_value={"buyer": 700, "supplier": 500}))
        await deals_repo.save(_composed_deal())
        out = await matchmaking.evaluate("DEAL-C1")
        assert "composition" in out
        assert not [g for g in gates if g.action == "CONFIRM_MATCH"]


@pytest.fixture
def neg_repo(monkeypatch):
    from agents.negotiation_agent.repository import InMemoryNegotiationRepository
    repo = InMemoryNegotiationRepository()
    monkeypatch.setattr("agents.negotiation_agent.repository.get_repository", lambda: repo)
    return repo


async def _approve(deals_repo, gates, monkeypatch):
    await deals_repo.save(_composed_deal())
    monkeypatch.setattr("services.deals.service.update_party", AsyncMock())
    monkeypatch.setattr("services.deals.service.set_supplier_party", AsyncMock())
    monkeypatch.setattr("services.deals.service.record_match_result", AsyncMock())
    monkeypatch.setattr("services.deals.service.update_financials", AsyncMock())
    statuses = []

    async def _status(deal_id, req):
        d = await deals_repo.get(deal_id)
        statuses.append(req.target_status)
        await deals_repo.save(d.model_copy(update={"status": req.target_status}))
        return d

    monkeypatch.setattr("services.deals.service.update_status", _status)
    await composition.run("DEAL-C1")
    payload = [g for g in gates if g.action == "CONFIRM_DEAL_COMPOSITION"][-1].callback_payload
    out = await composition.approve("DEAL-C1", payload)
    return out, statuses


class TestApproveAndLegs:
    @pytest.mark.asyncio
    async def test_approve_sets_legs_session_and_sends_one_request_per_leg(
            self, deals_repo, gates, mails, neg_repo, published, monkeypatch):
        out, statuses = await _approve(deals_repo, gates, monkeypatch)
        assert out["composed"] and len(out["legs"]) == 2
        deal = await deals_repo.get("DEAL-C1")
        assert {x.supplier_id for x in deal.supplier_legs} == {"SA", "SB"}
        assert all(x.status == "requested" for x in deal.supplier_legs)
        assert deal.quantity_mt == 1000.0 and statuses == [DealStatus.NEGOTIATING]
        session = await neg_repo.get_session_by_deal("DEAL-C1")
        assert session.agreed_price == 700.0 and session.quantity_mt == 1000.0
        leg_mails = [m for m in mails if "[LEG:DEAL-C1:" in m.subject]
        assert len(leg_mails) == 2
        for m in leg_mails:  # each supplier sees only its own leg
            other = "SB" if "sa.com" in m.to_address else "SA"
            assert f"{other} Co" not in m.body and "B1" not in m.body
        assert all(o.status == "used" for o in deal.firm_offers if o.counterparty_id == "B1")

    @pytest.mark.asyncio
    async def test_stale_combination_recomposes(self, deals_repo, gates, mails, monkeypatch):
        await deals_repo.save(_composed_deal())
        await composition.run("DEAL-C1")
        payload = gates[-1].callback_payload
        deal = await deals_repo.get("DEAL-C1")
        await deals_repo.save(deal.model_copy(update={"firm_offers": [
            o.model_copy(update={"price_usd_per_mt": 650.0}) for o in deal.firm_offers]}))
        out = await composition.approve("DEAL-C1", payload)
        assert out.get("stale")

    @pytest.mark.asyncio
    async def test_leg_confirm_reply_gates_and_last_confirmation_issues_the_agreement(
            self, deals_repo, gates, mails, neg_repo, published, monkeypatch):
        await _approve(deals_repo, gates, monkeypatch)
        deal = await deals_repo.get("DEAL-C1")
        a, b = sorted(deal.supplier_legs, key=lambda x: -x.quantity_mt)
        res = await composition.handle_leg_reply("DEAL-C1", a.leg_id, a.email, "We confirm the order.", "IN-1")
        assert res["verdict"] == "confirmed"
        gate = [g for g in gates if g.action == "CONFIRM_SUPPLIER_ORDER"][-1]
        assert gate.callback_payload["leg_id"] == a.leg_id
        from services.hitl_approval.callbacks import _cb_confirm_supplier_order
        first = await _cb_confirm_supplier_order("DEAL-C1", gate.callback_payload)
        assert first["all_confirmed"] is False
        second = await composition.confirm_leg("DEAL-C1", b.leg_id)
        assert second["all_confirmed"] is True and second["quantity_mt"] == 1000.0
        agreed = [e for e in published if str(getattr(e.topic, "value", e.topic)) == "negotiation.agreed"]
        assert agreed and agreed[-1].payload["agreed_price"] == 700.0

    @pytest.mark.asyncio
    async def test_reply_from_someone_else_goes_to_a_person(self, deals_repo, gates, mails, neg_repo, published, monkeypatch):
        await _approve(deals_repo, gates, monkeypatch)
        leg = (await deals_repo.get("DEAL-C1")).supplier_legs[0]
        res = await composition.handle_leg_reply("DEAL-C1", leg.leg_id, "x@stranger.com", "we confirm", "IN-2")
        assert res["reason"] == "sender_not_verified"
        assert [g for g in gates if g.action == "PARSE_INBOUND_EMAIL"]

    @pytest.mark.asyncio
    async def test_declined_leg_is_recomposed_for_its_quantity_only(
            self, deals_repo, gates, mails, neg_repo, published, monkeypatch):
        await _approve(deals_repo, gates, monkeypatch)
        deal = await deals_repo.get("DEAL-C1")
        small = min(deal.supplier_legs, key=lambda x: x.quantity_mt)
        res = await composition.handle_leg_reply("DEAL-C1", small.leg_id, small.email,
                                                 "Sorry, we regret we cannot supply this order.", "IN-3")
        assert res["verdict"] == "declined" and res["replaced"]
        deal = await deals_repo.get("DEAL-C1")
        new = [x for x in deal.supplier_legs if x.replaced_leg_id == small.leg_id]
        assert len(new) == 1 and new[0].supplier_id == "SC" and new[0].quantity_mt == 200.0
        assert composition.parse_leg_token(mails[-1].subject) == ("DEAL-C1", new[0].leg_id)

    @pytest.mark.asyncio
    async def test_no_cover_raises_the_exception(self, deals_repo, gates, mails, neg_repo, published, monkeypatch):
        await _approve(deals_repo, gates, monkeypatch)
        deal = await deals_repo.get("DEAL-C1")
        await deals_repo.save(deal.model_copy(update={"supplier_offers": [o for o in deal.supplier_offers if o.supplier_id != "SC"]}))
        big = max(deal.supplier_legs, key=lambda x: x.quantity_mt)
        res = await composition.replace_leg("DEAL-C1", big.leg_id, "no_reply")
        assert res["exception"] == "COMPOSITION:no_cover"
        assert any(e.payload.get("stage") == "COMPOSITION:no_cover" for e in published)

    @pytest.mark.asyncio
    async def test_silent_leg_replaced_by_the_sweep(self, deals_repo, gates, mails, neg_repo, published, monkeypatch):
        await _approve(deals_repo, gates, monkeypatch)
        deal = await deals_repo.get("DEAL-C1")
        legs = [x.model_copy(update={"requested_at": NOW - timedelta(hours=30)}) if x.quantity_mt == 200 else
                x.model_copy(update={"status": "confirmed"}) for x in deal.supplier_legs]
        await deals_repo.save(deal.model_copy(update={"supplier_legs": legs}))
        counts = await composition.sweep(NOW)
        assert counts["legs_replaced"] == 1

    @pytest.mark.asyncio
    async def test_composition_recorded_for_calibration(self, deals_repo, gates, mails, neg_repo, published, monkeypatch):
        await _approve(deals_repo, gates, monkeypatch)
        from shared.orchestration import outcomes
        rows = await outcomes.all_outcomes(kind="composition", deal_id="DEAL-C1")
        assert rows and rows[0].legs == 2 and rows[0].predicted_margin_usd > 0
        deal = (await deals_repo.get("DEAL-C1")).model_copy(update={"realized_margin_usd": 12345.0})
        await outcomes.on_status(deal, "PI_ACCEPTED")
        await outcomes.on_status(deal, "PAID")
        rows = await outcomes.all_outcomes(kind="composition", deal_id="DEAL-C1")
        assert rows[0].realized_margin_usd == 12345.0 and rows[0].completion == "completed"


class TestCallbacksRegistered:
    def test_gate_has_approve_and_soft_reject(self):
        from services.hitl_approval import callbacks
        from shared.domain.enums import ApprovalAction
        from shared.orchestration import autonomy_policy
        assert ApprovalAction.CONFIRM_DEAL_COMPOSITION.value in callbacks._CALLBACK_MAP
        assert "CONFIRM_DEAL_COMPOSITION" in callbacks.SOFT_REJECT_ACTIONS
        assert any(a.action == "CONFIRM_DEAL_COMPOSITION" for a in autonomy_policy.action_classes())

    def test_leg_token_round_trip(self):
        from services.email_processor.mapper import MappingResult  # noqa: F401 — the field exists
        assert composition.parse_leg_token("Re: [LEG:DEAL-AB12:LEG-1A2B3C4D] Firm order") == ("DEAL-AB12", "LEG-1A2B3C4D")
        assert composition.parse_leg_token("no token") is None


# ---------------------------------------------------------------------------
# Keeping a qualifying prospect open (13.1)
# ---------------------------------------------------------------------------

class TestKeepOpen:
    def test_live_offers_on_both_sides_keep_it_open(self):
        deal = _deal(composition={"latest": {"offers": {"buyers": 1, "suppliers": 2}}})
        assert composition.keeps_open(deal)

    def test_interested_buyer_and_quote(self):
        deal = _deal(buyer_candidates=[_cand("B1")], supplier_offers=[_sup("SA", 500)])
        assert composition.keeps_open(deal)

    def test_nothing_in_play(self, monkeypatch):
        assert not composition.keeps_open(_deal())
        monkeypatch.setenv("DEAL_COMPOSITION_ENABLED", "false")
        assert not composition.keeps_open(_deal(buyer_candidates=[_cand("B1")], supplier_offers=[_sup("SA", 500)]))

    @pytest.mark.asyncio
    async def test_watcher_does_not_call_it_a_stall(self, deals_repo, published, monkeypatch):
        from shared.orchestration import deal_watcher
        old = NOW - timedelta(days=30)
        await deals_repo.save(_deal(updated_at=old, buyer_candidates=[_cand("B1")], supplier_offers=[_sup("SA", 500)]))
        monkeypatch.setattr(deal_watcher, "_outreach_owns", lambda deal: False)
        await deal_watcher.sweep_stalled_deals(NOW)
        assert not [e for e in published if str(e.payload.get("stage", "")).endswith(":stalled")]

    @pytest.mark.asyncio
    async def test_outreach_running_out_does_not_end_it(self, deals_repo, published, monkeypatch):
        from shared.orchestration import outreach
        cands = [BuyerCandidate(candidate_id="B9", legal_name="B9", country="AE", email="b9@x.ae", status="declined")]
        await deals_repo.save(_deal(buyer_candidates=cands,
                                    composition={"latest": {"best": {"probability": 0.9}}}))
        res = await outreach.advance("DEAL-C1")
        assert res.get("kept_open") and not published


# ---------------------------------------------------------------------------
# Privacy with N parties (13.2)
# ---------------------------------------------------------------------------

class TestPrivacy:
    def _deal(self):
        return _deal(buyer_candidates=[_cand("B1", legal_name="Al Noor Foodstuff LLC"),
                                       _cand("B2", legal_name="Gulf Pantry Trading LLC")],
                     supplier_offers=[_sup("SA", 500, company_name="Shree Ganesh Rice Mills"),
                                      _sup("SB", 510, company_name="Kaveri Agro Exports")])

    def test_buyer_mail_naming_a_supplier_or_counting_them(self):
        from agents.negotiation_agent.email_safety import prospect_leak_findings
        deal = self._deal()
        assert prospect_leak_findings("Shree Ganesh Rice Mills quotes well", "desk@b1.com", "buyer", deal)
        assert prospect_leak_findings("we have three suppliers ready", "desk@b1.com", "buyer", deal)
        assert prospect_leak_findings("Gulf Pantry Trading pays more", "desk@b1.com", "buyer", deal)
        assert prospect_leak_findings("Dear Al Noor Foodstuff team, our price is 700", "desk@b1.com", "buyer", deal) == []

    def test_supplier_mail_never_mentions_buyers_or_other_suppliers(self):
        from agents.negotiation_agent.email_safety import prospect_leak_findings
        deal = self._deal()
        assert prospect_leak_findings("Kaveri Agro offered less", "desk@sa.com", "supplier", deal)
        assert prospect_leak_findings("for our client at desk@b1.com", "desk@sa.com", "supplier", deal)
        assert prospect_leak_findings("two other suppliers quoted", "desk@sa.com", "supplier", deal)
        assert prospect_leak_findings("Dear Shree Ganesh Rice Mills team, please confirm", "desk@sa.com", "supplier",
                                      deal) == []

    def test_preflight_blocks_with_composition_on(self):
        from agents.negotiation_agent.email_safety import preflight
        deal = self._deal().model_copy(update={"parties": [
            Party(role=PartyRole.BUYER, legal_name="Al Noor Foodstuff LLC", country="AE", email="desk@b1.com", party_id="B1"),
            Party(role=PartyRole.SUPPLIER, legal_name="Shree Ganesh Rice Mills", country="IN", email="desk@sa.com",
                  party_id="SA")]})
        session = SimpleNamespace(buyer_email="desk@b1.com", seller_email="desk@sa.com", deal_id="DEAL-C1",
                                  status="ROUND", seller_id="SA", supplier_quote_price=500, supplier_last_quote=500,
                                  last_offer_to_supplier=None, buyer_target_price=700, buyer_last_offer=700,
                                  last_offer_to_buyer=None)
        record = SimpleNamespace(party="buyer", to_address="desk@b1.com", deal_id="DEAL-C1", kind="notification",
                                 email_id="EMAIL-N1", body="We also talk to Gulf Pantry Trading about this lot.",
                                 created_at=NOW)
        reasons = preflight(record, session, deal, [])
        assert any("another buyer" in r for r in reasons)

    def test_memory_about_another_company_of_the_prospect_is_filtered(self):
        from agents.negotiation_agent.email_safety import knowledge_filter
        accept = knowledge_filter("buyer", {"desk@b1.com"}, {"desk@b1.com", "desk@b2.com", "desk@sa.com"})
        assert not accept({"metadata": {"kind": "note", "subject_type": "buyer", "subject_address": "desk@b2.com"}})
        assert accept({"metadata": {"kind": "note", "subject_type": "buyer", "subject_address": "desk@b1.com"}})
        assert accept({"metadata": {"kind": "product"}})


# ---------------------------------------------------------------------------
# Supplier legs (13.4)
# ---------------------------------------------------------------------------

def _legged(**kw):
    legs = [SupplierLeg(leg_id="LEG-A", supplier_id="SA", company_name="SA Co", email="desk@sa.com",
                        quantity_mt=800, price_usd_per_mt=500, status="confirmed", port_of_loading="Mundra",
                        facility_address="Plot 4, GIDC, Mundra, Gujarat 370421"),
            SupplierLeg(leg_id="LEG-B", supplier_id="SB", company_name="SB Co", email="desk@sb.com",
                        quantity_mt=200, price_usd_per_mt=510, status="confirmed", port_of_loading="Mundra")]
    return _deal(supplier_legs=legs, **kw)


class TestLegsModel:
    def test_old_deal_derives_one_leg(self):
        deal = _deal(winning_supplier_id="SA", quantity_mt=50, parties=[
            Party(role=PartyRole.SUPPLIER, legal_name="SA Co", country="IN", email="desk@sa.com", party_id="SA")])
        legs = supplier_legs.legs_for(deal)
        assert len(legs) == 1 and legs[0].supplier_id == "SA" and legs[0].quantity_mt == 50
        assert not supplier_legs.is_multi(deal)

    @pytest.mark.asyncio
    async def test_migrate_is_idempotent(self, deals_repo):
        await deals_repo.save(_deal(winning_supplier_id="SA"))
        assert (await supplier_legs.migrate_all())["migrated"] == 1
        assert (await supplier_legs.migrate_all())["skipped"] == 1
        assert len((await deals_repo.get("DEAL-C1")).supplier_legs) == 1

    def test_site_facts_are_the_legs_facility(self):
        facts = supplier_legs.site_facts(_legged(), "LEG-A")
        assert facts["name"] == "SA Co" and facts["postcode"] == "370421"


class TestLegMoney:
    def test_released_funds_split_pro_rata_never_above_a_legs_due(self):
        owed = {"LEG-A": 400000.0, "LEG-B": 102000.0}
        plan = supplier_legs.payout_plan(owed, {"LEG-B": (100000.0, 0.0)}, 250000.0)
        assert plan["LEG-A"] == round(250000 * 400000 / 502000, 2)
        assert plan["LEG-B"] == 2000.0

    def test_paid_only_when_every_leg_is(self):
        deal = _legged()
        owed = {"LEG-A": 400000.0, "LEG-B": 102000.0}
        pay = [SimpleNamespace(payee_id="SUPPLIER-SA", amount=400000.0, status="COMPLETED"),
               SimpleNamespace(payee_id="SUPPLIER-SA", amount=102000.0, status="COMPLETED")]
        assert not supplier_legs.every_leg_paid(deal, pay, owed)  # the total is there, leg B is not paid
        pay[1] = SimpleNamespace(payee_id="SUPPLIER-SB", amount=102000.0, status="COMPLETED")
        assert supplier_legs.every_leg_paid(deal, pay, owed)

    @pytest.mark.asyncio
    async def test_payout_above_a_legs_due_refused(self, monkeypatch):
        from services.escrow_guard import service as eg
        deal = _legged()
        monkeypatch.setattr(supplier_legs, "owed_by_leg", AsyncMock(return_value={"LEG-A": 400000.0, "LEG-B": 102000.0}))
        req = SimpleNamespace(deal_id="DEAL-C1", payee_id="SUPPLIER-SB", amount=110000.0)
        with pytest.raises(eg.InvalidDealState):
            await eg._leg_check(deal, req, [])
        with pytest.raises(eg.InvalidDealState):
            await eg._leg_check(deal, SimpleNamespace(deal_id="DEAL-C1", payee_id="SUPPLIER-ZZ", amount=1.0), [])
        await eg._leg_check(deal, SimpleNamespace(deal_id="DEAL-C1", payee_id="SUPPLIER-SB", amount=100000.0), [])


class TestLegQc:
    @pytest.mark.asyncio
    async def test_deal_passes_only_when_every_leg_passed(self, deals_repo, published):
        await deals_repo.save(_legged(status=DealStatus.SUPPLIER_DISPATCH_READY))
        first = await supplier_legs.on_leg_qc_result("DEAL-C1", "LEG-A", "PASS", loading={
            "container_numbers": ["MSKU1"], "seal_numbers": ["S1"], "loaded_quantity_mt": 800})
        assert first["proceed"] is False and first["summary"] == "QC 1 of 2 passed"
        second = await supplier_legs.on_leg_qc_result("DEAL-C1", "LEG-B", "PASS", loading={
            "container_numbers": ["MSKU2"], "seal_numbers": ["S2"], "loaded_quantity_mt": 200})
        assert second["proceed"] is True
        assert second["loading"]["container_numbers"] == ["MSKU1", "MSKU2"]  # CI / PL list every leg
        assert second["loading"]["loaded_quantity_mt"] == 1000

    @pytest.mark.asyncio
    async def test_failed_leg_holds_the_deal_without_dispute(self, deals_repo, published):
        await deals_repo.save(_legged(status=DealStatus.SUPPLIER_DISPATCH_READY))
        res = await supplier_legs.on_leg_qc_result("DEAL-C1", "LEG-B", "FAIL", notes="moisture")
        assert res["proceed"] is False
        assert any(e.payload.get("stage") == "QC_FAILED:leg" for e in published)

    @pytest.mark.asyncio
    async def test_cancel_a_failed_leg_needs_the_buyers_consent(self, deals_repo, published, monkeypatch):
        await deals_repo.save(_legged(status=DealStatus.SUPPLIER_DISPATCH_READY))
        await supplier_legs.on_leg_qc_result("DEAL-C1", "LEG-A", "PASS")
        await supplier_legs.on_leg_qc_result("DEAL-C1", "LEG-B", "FAIL")
        with pytest.raises(ValueError):
            await supplier_legs.cancel_leg("DEAL-C1", "LEG-B", by="op", reason="failed", buyer_consent=False)
        moved = AsyncMock()
        monkeypatch.setattr("services.shield_link.service._transition_deal", moved)
        monkeypatch.setattr("services.shield_link.service._emit", AsyncMock())
        monkeypatch.setattr("shared.audit_log.record", AsyncMock())
        out = await supplier_legs.cancel_leg("DEAL-C1", "LEG-B", by="op", reason="failed", buyer_consent=True)
        assert out["qc_passed"] and moved.await_args.args[1] == "QC_PASSED"

    @pytest.mark.asyncio
    async def test_qc_booked_per_leg_once_acknowledged(self, deals_repo, monkeypatch):
        monkeypatch.setenv("PO_ACK_REQUIRED", "true")
        legs = _legged(status=DealStatus.PO_ISSUED).supplier_legs
        legs[0] = legs[0].model_copy(update={"po_ack": "acknowledged"})
        await deals_repo.save(_legged(status=DealStatus.PO_ISSUED).model_copy(update={"supplier_legs": legs}))
        booked = []

        async def _book(req, **kw):
            booked.append(req)
            return SimpleNamespace(tpo_id=f"QC-{len(booked)}")

        monkeypatch.setattr("services.shield_link.service.book_qc", _book)
        assert await supplier_legs.book_qc("DEAL-C1") == ["QC-1"]
        assert booked[0].leg_id == "LEG-A" and booked[0].quantity_mt == 800
        assert booked[0].location.startswith("Plot 4")


class TestLegCompliance:
    def test_related_legs_and_failed_supplier(self):
        deal = _legged(supplier_offers=[_sup("SB", 510, verification_failed=True)])
        legs = [x.model_copy(update={"email": "x@group.in"}) for x in deal.supplier_legs]
        codes = {f.code for f in supplier_legs.compliance_findings(deal.model_copy(update={"supplier_legs": legs}))}
        assert codes == {"leg_supplier_failed", "leg_related_parties"}

    def test_quantity_tolerance_per_leg(self):
        legs = _legged().supplier_legs
        legs = [legs[0].model_copy(update={"loaded_mt": 800}), legs[1].model_copy(update={"loaded_mt": 150})]
        findings = supplier_legs.shipment_findings(_deal(supplier_legs=legs), 0.05)
        assert [f.code for f in findings] == ["shipment_leg_qty_mismatch"] and findings[0].data["leg_id"] == "LEG-B"

    def test_one_leg_deal_unchanged(self):
        deal = _deal(supplier_legs=_legged().supplier_legs[:1])
        assert supplier_legs.compliance_findings(deal) == [] and supplier_legs.shipment_findings(deal, 0.05) == []


class TestPoPerLeg:
    @pytest.mark.asyncio
    async def test_one_po_per_leg_first_moves_the_deal(self, deals_repo, tmp_path, monkeypatch):
        from services.dealmaker import service as dm
        from services.dealmaker.repository import InMemoryDealMakerRepository
        from services.dealmaker.schemas import PiRecord
        repo = InMemoryDealMakerRepository()
        monkeypatch.setattr("services.dealmaker.service.get_repository", lambda: repo)
        monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: repo)
        monkeypatch.setattr("services.dealmaker.service.settings.dealmaker_storage_dir", str(tmp_path))
        monkeypatch.setattr("services.dealmaker.service.get_drive_connector",
                            lambda: SimpleNamespace(is_configured=lambda: False))
        deal = _legged(status=DealStatus.ESCROW_FUNDED)
        await deals_repo.save(deal)

        async def _fetch(deal_id):
            return await deals_repo.get(deal_id)

        moves = []

        async def _transition(deal_id, status, reason):
            moves.append(status)
            d = await deals_repo.get(deal_id)
            await deals_repo.save(d.model_copy(update={"status": DealStatus(status)}))

        monkeypatch.setattr(dm, "_fetch_deal", _fetch)
        monkeypatch.setattr(dm, "_transition_deal", _transition)
        monkeypatch.setattr(dm, "_emit", AsyncMock())
        await repo.save_pi(PiRecord(pi_id="PI-1", deal_id="DEAL-C1", status="ACCEPTED", agreed_price=700.0, currency="USD",
                                    quantity_mt=1000.0, total_value=700000.0, incoterm="CIF", port_of_destination="Jebel Ali",
                                    payment_terms="escrow", validity_days=7, buyer_name="B1 Co", buyer_address="Dubai",
                                    buyer_vat_number=None,
                                    issued_at=NOW, expires_at=NOW + timedelta(days=7)))
        from services.hitl_approval.callbacks import _cb_issue_po
        out = await _cb_issue_po("DEAL-C1", {"deal_id": "DEAL-C1", "agreed_price": 505.0, "quantity_mt": 1000.0,
                                             "port_of_loading": "Mundra, IN", "delivery_deadline": "2027-01-31",
                                             "payment_terms": "escrow"})
        assert len(out["pos"]) == 2 and moves == ["PO_ISSUED"]
        pos = await repo.list_pos_by_deal("DEAL-C1")
        assert {(p.leg_id, p.quantity_mt, p.agreed_price, p.supplier_name) for p in pos} == {
            ("LEG-A", 800, 500, "SA Co"), ("LEG-B", 200, 510, "SB Co")}
        from services.escrow_guard.service import _supplier_owed
        assert await _supplier_owed("DEAL-C1") == 800 * 500 + 200 * 510
        legs = (await deals_repo.get("DEAL-C1")).supplier_legs
        assert all(x.po_id and x.po_ack == "awaiting" for x in legs)

    @pytest.mark.asyncio
    async def test_total_margin_guard(self, deals_repo, monkeypatch):
        from services.dealmaker.repository import InMemoryDealMakerRepository
        repo = InMemoryDealMakerRepository()
        monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: repo)
        await deals_repo.save(_legged(status=DealStatus.ESCROW_FUNDED, buyer_target_price_usd=530.0))
        with pytest.raises(ValueError, match="total margin"):
            await supplier_legs.issue_pos("DEAL-C1", {"delivery_deadline": "2027-01-31", "payment_terms": "x"})
