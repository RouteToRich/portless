"""Prospect stage as a trader works it: walk-away / target prices per pair, quote age and
expiry, leverage from the best quote, the radar's freight and incentives on the match
board, the prospect desk (worth, whose move, next moves, stall / park / drop), and the
outreach fixes (operator notes never reach a buyer, failed sends recorded, only the
inspectors PortLess books are named)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import services.deals.service  # noqa: F401
from services.deals.schemas import (
    BuyerCandidate, BuyerRequirement, DealRecord, DealStatus, IndicativeOffer, SupplierOffer,
)
from shared import pricebook
from shared.orchestration import landed_cost as lc
from shared.orchestration import matchmaking as mm
from shared.orchestration import prospect_desk as pd

NOW = datetime.now(timezone.utc)
PRIORS = {"buyer": 900.0, "supplier": 700.0, "source": "comtrade", "tariff_pct": 1.7}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MATCHMAKING_ENABLED", "true")
    monkeypatch.setenv("PRICEBOOK_MONGO", "false")
    monkeypatch.setenv("OUTCOMES_MONGO", "false")
    for v in ("LANDED_FREIGHT_USD_PER_MT", "LANDED_FREIGHT_USD_PER_MT_ES", "MATCH_MIN_PROBABILITY",
              "MATCH_TARGET_MARGIN_PCT", "PROSPECT_STALL_DAYS", "PROSPECT_DROP_DAYS"):
        monkeypatch.delenv(v, raising=False)
    pricebook.reset_for_tests()
    from shared.orchestration import outcomes
    outcomes.reset_for_tests()
    from services.trade_min import freight_lanes, incentives
    freight_lanes.reset_for_tests()
    incentives.reset_for_tests()
    monkeypatch.setattr(pricebook, "public_priors", AsyncMock(return_value=dict(PRIORS)))
    monkeypatch.setattr("services.deals.service.list_deals", AsyncMock(return_value=[]))
    yield
    pricebook.reset_for_tests()


def _cand(cid, status="contacted", **kw):
    return BuyerCandidate(candidate_id=cid, legal_name=f"Buyer {cid}", country="ES", email=f"buy@{cid}.es",
                          status=status, **kw)


def _offer(sid, fob=650.0, **kw):
    base = dict(offer_id=f"OFR-{sid}", supplier_id=sid, company_name=f"Supplier {sid}", email=f"sales@{sid}.in",
                fob_price_usd_per_mt=fob, gst_verified=True, iec_active=True, received_at=NOW)
    base.update(kw)
    return SupplierOffer(**base)


def _deal(cands=(), offers=(), deal_id="DEAL-P1", **kw):
    base = dict(deal_id=deal_id, operator_id="op", org_id="portless", hs_code="290543", market="ES",
                status=DealStatus.MATCHING, created_at=NOW, updated_at=NOW, quantity_mt=25.0,
                buyer_candidates=list(cands), supplier_offers=list(offers))
    base.update(kw)
    return DealRecord(**base)


def _firm_buyer(cid="b1", price=900.0, **kw):
    return _cand(cid, status="interested",
                 requirement=BuyerRequirement(target_price_usd_per_mt=price, target_price_basis="stated",
                                              target_price_incoterm="CIF"), **kw)


# ── negotiation targets ──────────────────────────────────────────────────────

class TestTargets:
    def test_walk_away_and_target_prices(self):
        t = mm.negotiation_targets(1000.0, 700.0, freight=90.0, inspection=24.0, fee_ins_pct=1.5,
                                   incentive_pct=0.0, floor_pct=8.0, target_pct=12.0)
        # at the max supplier price the margin is exactly the floor
        margin = 1000.0 - (t["max_supplier"] + 90 + 24) - 1000.0 * 0.015
        assert round(margin, 2) == 80.0 and t["target_supplier"] < t["max_supplier"]
        # at the min buyer price the margin is exactly the floor
        b = t["min_buyer"]
        assert round(b - (700 + 90 + 24) - b * 0.015 - b * 0.08, 2) == 0.0 and t["target_buyer"] > b

    def test_incentive_lets_the_supplier_charge_more(self):
        kw = dict(freight=90.0, inspection=24.0, fee_ins_pct=1.5, floor_pct=8.0, target_pct=12.0)
        without = mm.negotiation_targets(1000.0, None, incentive_pct=0.0, **kw)
        with_inc = mm.negotiation_targets(1000.0, None, incentive_pct=2.0, **kw)
        assert with_inc["max_supplier"] > without["max_supplier"] and without["min_buyer"] is None

    async def test_pair_under_floor_says_how_far_to_push(self):
        board = await mm.build_board(_deal([_firm_buyer(price=800.0)], [_offer("s1", fob=720.0)]))
        p = board.pairs[0]
        assert p.supplier_cut_needed_usd_per_mt > 0
        assert round(720.0 - p.max_supplier_fob_usd_per_mt, 2) == p.supplier_cut_needed_usd_per_mt
        assert "push Supplier s1 to ≤" in p.next_step and p.min_buyer_price_usd_per_mt > 800.0

    async def test_pair_above_floor_needs_no_cut(self):
        board = await mm.build_board(_deal([_firm_buyer(price=1100.0)], [_offer("s1", fob=650.0)]))
        assert board.pairs[0].supplier_cut_needed_usd_per_mt == 0.0


# ── quotes: age, expiry, leverage, radar costs ──────────────────────────────

class TestBoard:
    async def test_expiring_and_expired_quotes(self):
        fresh = _offer("s1", received_at=NOW - timedelta(days=28), validity_days=30)
        old = _offer("s2", received_at=NOW - timedelta(days=40), validity_days=30)
        board = await mm.build_board(_deal([_firm_buyer()], [fresh, old]))
        assert [o["supplier_id"] for o in board.expired_offers] == ["s2"]
        p = board.pairs[0]
        assert p.quote_age_days == 28 and p.quote_expires_in_days == 2
        assert any("expires in 2 day" in m for m in p.missing)
        assert any("1 quote(s) expired" in a for a in board.next_actions)

    async def test_best_quote_is_leverage(self):
        board = await mm.build_board(_deal([_firm_buyer()], [_offer("s1", fob=650.0), _offer("s2", fob=700.0)]))
        assert board.best_quote_usd_per_mt == 650.0 and board.quote_spread_pct == round(50 / 650 * 100, 1)
        assert any(a.startswith("Leverage: best quote 650") for a in board.next_actions)

    async def test_board_uses_the_radar_lane_freight_and_incentives(self):
        from services.trade_min import freight_lanes, incentives
        base = await mm.build_board(_deal([_firm_buyer()], [_offer("s1")]))
        await freight_lanes.save(freight_lanes.LaneRate(market="ES", usd_per_container=800.0, container_mt=20.0))
        await incentives.set_entry(incentives.ExportIncentive(hs_code="290543", rodtep_pct=1.0, drawback_pct=1.0,
                                                              eligible_confirmed=True))
        board = await mm.build_board(_deal([_firm_buyer()], [_offer("s1")]))
        p = board.pairs[0]
        assert p.landed.freight_usd_per_mt == 40.0 and p.freight_basis.startswith("lane_quote")
        assert p.incentive_usd_per_mt == 13.0 and board.assumptions["incentive_pct_of_fob"] == 2.0
        assert p.margin_usd_per_mt > base.pairs[0].margin_usd_per_mt


# ── the desk ─────────────────────────────────────────────────────────────────

class TestDesk:
    async def _row(self, deal):
        return pd.summarise(deal, await mm.build_board(deal))

    async def test_firm_pair_is_ours_to_confirm(self):
        row = await self._row(_deal([_firm_buyer(price=1100.0)], [_offer("s1", fob=650.0)]))
        assert row.waiting_on == "us" and row.actions[0].kind == "confirm"
        assert row.firm and row.expected_value_usd > 0 and row.priority == round(row.expected_value_usd * 1.5, 2)
        assert row.best_buyer == "Buyer b1" and row.best_supplier == "Supplier s1"

    async def test_push_supplier_and_best_and_final(self):
        row = await self._row(_deal([_firm_buyer(price=800.0)], [_offer("s1", fob=720.0), _offer("s2", fob=760.0)]))
        kinds = [a.kind for a in row.actions]
        assert "push_supplier" in kinds and "best_and_final" in kinds and row.waiting_on == "us"
        assert row.supplier_cut_needed_usd_per_mt > 0

    async def test_waiting_on_buyers_then_suppliers(self):
        contacted = _deal([_cand("b1", contacted_at=NOW), _cand("b2", status="pending")])
        row = await self._row(contacted)
        assert row.waiting_on == "buyers" and "1 contacted, 1 more" in row.waiting_detail
        yes_no_quote = _deal([_cand("b1", status="interested")])
        row = await self._row(yes_no_quote)
        assert row.waiting_on == "suppliers" and row.actions[0].kind == "chase_quotes"

    async def test_indicative_offer_out_waits_for_the_buyer(self):
        cand = _cand("b1", status="interested", indicative_offer=IndicativeOffer(
            offer_id="OFR-s1", price_low_usd_per_mt=900, price_high_usd_per_mt=950, sent_at=NOW))
        row = await self._row(_deal([cand], [_offer("s1")]))
        assert row.waiting_on == "buyer" and "indicative offer sent" in row.waiting_detail

    async def test_undelivered_introductions_point_at_the_mailbox(self):
        row = await self._row(_deal([_cand("b1", status="pending", send_error="not delivered: no mailbox")]))
        assert row.undelivered == 1 and row.actions[0].kind == "fix_mailbox" and row.waiting_on == "us"
        assert "GMAIL_SENDER_ADDRESS" in row.actions[0].text

    async def test_stall_park_and_drop(self):
        old = NOW - timedelta(days=30)
        stalled = _deal([_cand("b1", contacted_at=old), _cand("b2", status="pending")], created_at=old, updated_at=old)
        row = await self._row(stalled)
        assert row.stalled and row.idle_days == 30
        assert {"contact_buyers", "park"} <= {a.kind for a in row.actions}
        exhausted = _deal([_cand("b1", status="declined"), _cand("b2", status="no_reply")])
        assert (await self._row(exhausted)).actions[-1].kind == "drop"

    async def test_desk_orders_by_our_move_then_priority_and_uses_the_radar(self, monkeypatch):
        ready = _deal([_firm_buyer(price=1100.0)], [_offer("s1", fob=650.0)], deal_id="D-READY")
        waiting = _deal([_cand("b1", contacted_at=NOW)], deal_id="D-WAIT", opportunity_id="OPP-9")
        monkeypatch.setattr(pd, "_opportunity_profit", AsyncMock(return_value={"expected_profit_usd": 5000.0,
                                                                              "margin_usd_per_mt": 200.0}))
        paid = _deal(deal_id="D-PAID", status=DealStatus.PI_SENT)
        rows = await pd.desk([waiting, ready, paid])
        assert [r.deal_id for r in rows] == ["D-READY", "D-WAIT"]
        assert rows[1].radar_expected_profit_usd == 5000.0 and rows[1].priority > 0

    async def test_one_broken_board_never_hides_the_desk(self, monkeypatch):
        monkeypatch.setattr(mm, "build_board", AsyncMock(side_effect=RuntimeError("boom")))
        rows = await pd.desk([_deal([_cand("b1")])])
        assert rows[0].deal_id == "DEAL-P1" and rows[0].best_buyer is None

    def test_endpoint(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from services.deals import router as deals_router
        from shared.auth.dependencies import get_current_user
        app = FastAPI()
        app.include_router(deals_router.router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            user_id="u", email="ops@x", org_id="portless", is_portless=True)
        monkeypatch.setattr("services.deals.service.list_deals",
                            AsyncMock(return_value=[_deal([_cand("b1", status="pending")])]))
        body = TestClient(app).get("/deals/prospects/desk").json()
        assert body[0]["deal_id"] == "DEAL-P1" and body[0]["actions"][0]["kind"] == "contact_buyers"


# ── outreach fixes ───────────────────────────────────────────────────────────

class TestOutreach:
    @pytest.mark.parametrize("note", [
        "Write to einkauf@duepmann.de — the purchasing inbox of Dried onions (MX ok, listed on /kontakt/).",
        "Write to info@elysiumagrico.com — the general inbox of Dehydrated Onion Exporter (MX ok, listed on /).",
        "short",
    ])
    def test_operator_notes_never_open_an_email(self, note):
        from shared.orchestration import outreach
        assert outreach.usable_pitch(note) == ""
        deal = SimpleNamespace(deal_id="D1", hs_code="071220", opportunity_id=None, quantity_mt=None)
        cand = SimpleNamespace(candidate_id="C1", legal_name="Duepmann", pitch_angle=note, quantity_mt=None)
        _, body = outreach.draft(deal, cand)
        assert "MX ok" not in body and "Write to" not in body and "I am reaching out because" in body

    def test_a_real_pitch_is_kept(self):
        from shared.orchestration import outreach
        line = "Your dehydrated onion range for German food makers matches our Gujarat mills."
        assert outreach.usable_pitch(line) == line

    def test_scan_never_stores_the_note_as_pitch(self):
        from services.trade_min import service as svc
        assert svc._pitch("Write to info@x.com — the general inbox (MX ok, listed on /).") is None

    async def test_send_failure_is_recorded_on_the_candidate(self):
        from shared.orchestration import outreach
        update = AsyncMock()
        deal = SimpleNamespace(buyer_candidates=[SimpleNamespace(candidate_id="C1", send_attempts=1)])
        with (patch("services.email_gateway.sender.send_email", AsyncMock(side_effect=ConnectionError("Connection lost"))),
              patch("services.deals.service.get_deal", AsyncMock(return_value=deal)),
              patch("services.deals.service.update_buyer_candidate", update)):
            with pytest.raises(ConnectionError):
                await outreach.send_outreach("D1", "C1", "buy@x.de", "s", "b")
        fields = update.await_args.args[2]
        assert fields["send_error"].startswith("send failed: Connection lost") and fields["send_attempts"] == 2

    def test_only_booked_inspectors_are_named(self, monkeypatch):
        from shared import company_profile as cp
        monkeypatch.setenv("QC_PROVIDERS_ENABLED", "qima")
        line = cp.buyer_reasons()[1]
        assert line.startswith("Independent inspection. QIMA") and "SGS" not in line
        monkeypatch.setenv("QC_PROVIDERS_ENABLED", "qima,sgs")
        assert cp.buyer_reasons()[1].startswith("You choose the inspector.")
