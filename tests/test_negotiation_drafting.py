"""First-contact drafting quality: price strategy, briefing, clean e-mails,
RFQ / outreach templates (agents/negotiation_agent/pricing.py, briefing.py,
graph.py drafting nodes, services/sourcing RFQ e-mail, outreach.draft)."""
from __future__ import annotations

import asyncio
import itertools
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.negotiation_agent import briefing
from agents.negotiation_agent.graph import (
    _settle_price,
    buyer_agent_node,
    clean_outbound,
    supplier_agent_node,
)
from agents.negotiation_agent.pricing import buyer_band, supplier_band
from agents.negotiation_agent.schemas import CostEnvelope, NegotiationPolicy, NegotiationSession
from agents.negotiation_agent.service import _email_gate_level, _record_offers, negotiation_subject

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _session(**kw) -> NegotiationSession:
    d = dict(
        negotiation_id="neg-d", deal_id="DEAL-D1", trade_match_id="TM", buyer_id="BUYER-DE",
        seller_id="SUP-1", product_code="1520", buyer_country_code="DE",
        buyer_target_price=980.0, supplier_quote_price=760.0, policy=NegotiationPolicy(),
        cost_envelope=CostEnvelope(travel_cost=12, qc_cost=4, doc_cost=3), quantity_mt=20.0,
        logistics_cost_usd=70.0, round_no=0, current_bid=980.0, current_ask=760.0,
        created_at=NOW, updated_at=NOW,
    )
    d.update(kw)
    return NegotiationSession(**d)


def _state(session, **kw) -> dict:
    s = {"session": session, "logistics_cost": session.logistics_cost_usd or 0.0, "party": "init",
         "inbound_message": None, "buyer_history": "", "supplier_history": "", "usd_inr_rate": 88.0,
         "current_bid": session.current_bid, "current_ask": session.current_ask}
    s.update(kw)
    return s


def _margin(buyer: float, supplier: float, costs: float) -> float:
    return (buyer - supplier - costs) / buyer * 100


# ---------------------------------------------------------------------------
# Price strategy
# ---------------------------------------------------------------------------

class TestBuyerBand:
    kw = dict(supplier_price=760.0, costs=89.0, floor_pct=8.0, target_pct=12.0,
              last_offer_to_buyer=None, round_no=0, max_rounds=5)

    def test_meets_target_when_it_clears_ideal_margin(self):
        band = buyer_band(buyer_offer=980.0, **self.kw)  # ideal ≈ 964.8
        assert band.recommended == 980.0          # was 930 before — 50/MT left on the table
        assert band.low == 980.0                  # never quote a buyer below their own target
        assert band.final_offer is False          # meeting their target is not an ultimatum

    def test_between_floor_and_ideal_opens_halfway(self):
        band = buyer_band(buyer_offer=940.0, **self.kw)  # floor ≈ 922.8, ideal ≈ 964.8
        assert 940.0 < band.recommended < 964.8
        assert band.low == 940.0

    def test_target_below_floor_anchors_at_ideal_with_room(self):
        band = buyer_band(buyer_offer=850.0, **self.kw)
        assert band.recommended == pytest.approx(964.77, abs=0.01)
        assert band.low == pytest.approx(922.83, abs=0.01)

    def test_later_round_splits_gap_and_last_round_is_final(self):
        kw = dict(self.kw, last_offer_to_buyer=990.0, round_no=2)
        band = buyer_band(buyer_offer=950.0, **kw)
        assert band.recommended == pytest.approx(970.0)
        assert band.high == 990.0                 # never go back above our last offer
        final = buyer_band(buyer_offer=950.0, **dict(kw, round_no=4))
        assert final.final_offer and final.recommended == 950.0


class TestSupplierBand:
    def test_opening_is_under_their_quote_never_above(self):
        band = supplier_band(supplier_quote=760.0, ceiling=812.6, last_offer_to_supplier=None,
                             round_no=0, max_rounds=5)
        assert band.recommended == pytest.approx(729.6)   # was 802 before — above their own quote
        assert band.high == 760.0
        assert band.low >= 760.0 * 0.92 - 0.01            # no insulting low-ball

    def test_quote_above_ceiling_counters_under_ceiling(self):
        band = supplier_band(supplier_quote=900.0, ceiling=812.6, last_offer_to_supplier=None,
                             round_no=0, max_rounds=5)
        assert band.recommended < 812.6 and band.high == 812.6

    def test_concessions_only_move_up_toward_their_quote(self):
        band = supplier_band(supplier_quote=760.0, ceiling=812.6, last_offer_to_supplier=730.0,
                             round_no=2, max_rounds=5)
        assert band.low == 730.0 and band.recommended == pytest.approx(745.0)


def test_any_pair_of_quotable_prices_keeps_the_margin_floor():
    """The joint guarantee: buyer bound from the supplier's own quote, supplier
    bound from the buyer's own offer ⇒ every pair the agents may quote ≥ floor."""
    costs, floor = 89.0, 8.0
    for buyer_offer, supplier_quote in itertools.product((850, 930, 980, 1100), (700, 760, 820, 900)):
        b = buyer_band(buyer_offer=buyer_offer, supplier_price=supplier_quote, costs=costs, floor_pct=floor,
                       target_pct=12.0, last_offer_to_buyer=None, round_no=0, max_rounds=5)
        ceiling = buyer_offer * (1 - floor / 100) - costs
        s = supplier_band(supplier_quote=supplier_quote, ceiling=ceiling, last_offer_to_supplier=None,
                          round_no=0, max_rounds=5)
        # the deal closes at some buyer price in [b.low, b.high] and supplier price ≤ s.high
        assert _margin(b.low, supplier_quote, costs) >= floor - 1e-6
        assert _margin(buyer_offer, s.high, costs) >= floor - 1e-6 or s.high < 0


# ---------------------------------------------------------------------------
# Draft hygiene
# ---------------------------------------------------------------------------

def test_clean_outbound_strips_markdown_and_subject_line():
    raw = "Subject: Request for Quotation\n\nDear team,\n\n**USD 930 per MT**\n* one\n\n\n\nBest"
    out = clean_outbound(raw)
    assert "Subject:" not in out and "**" not in out
    assert "USD 930 per MT" in out and "- one" in out and "\n\n\n" not in out


def test_out_of_band_price_is_corrected_and_forces_human_review():
    band = supplier_band(supplier_quote=760.0, ceiling=812.6, last_offer_to_supplier=None, round_no=0, max_rounds=5)
    price, flags = _settle_price({"counter_price_usd": 802.3}, band, "supplier")
    assert price == band.recommended and flags == ["price_out_of_band"]
    session = _session(round_no=3)
    with patch("agents.negotiation_agent.service._resolve_gate_level", return_value="L2"):
        assert _email_gate_level(session, ["email_content_review", "price_out_of_band"]) == "L0"
        assert _email_gate_level(session, ["email_content_review"]) == "L2"


# ---------------------------------------------------------------------------
# Briefing → drafting nodes
# ---------------------------------------------------------------------------

def _deal_after_intro():
    from services.deals.schemas import BuyerCandidate, BuyerRequirement, SupplierOffer
    req = BuyerRequirement(grade="USP 99.7%", quantity_mt=20, destination_port="Hamburg",
                           target_price_usd_per_mt=980.0, delivery_window="January")
    cand = BuyerCandidate(candidate_id="c1", legal_name="Glaconchemie", country="DE",
                          email="m.turre@glaconchemie.de", status="selected", contacted_at=NOW,
                          reply_preview="Interested. We need 20 MT USP grade to Hamburg in January, target 980.",
                          requirement=req)
    offer = SupplierOffer(offer_id="o1", supplier_id="SUP-1", company_name="Swadesh India Chemical Pvt Ltd",
                          grade="Refined 99.7%", fob_price_usd_per_mt=760.0, lead_time_days=21,
                          port_of_loading="Nhava Sheva", gst_verified=True, iec_active=True, received_at=NOW)
    return SimpleNamespace(deal_id="DEAL-D1", parties=[], buyer_candidates=[cand], supplier_offers=[offer])


def _capture(captured: dict, price: float):
    async def _chat(system_prompt, payload, caller):
        captured["payload"] = payload
        return json.dumps({"counter_price_usd": price, "outbound_message": "**Dear team**\nSubject: x\nok",
                           "negotiated_terms": {}, "reasoning": "r"})
    return _chat


def test_buyer_who_answered_the_introduction_is_not_cold():
    captured: dict = {}
    session = _session(buyer_email="m.turre@glaconchemie.de")
    with patch("agents.negotiation_agent.briefing._load_deal", AsyncMock(return_value=_deal_after_intro())), \
         patch("agents.negotiation_agent.graph._chat", _capture(captured, 980.0)):
        out = run(buyer_agent_node(_state(session)))
    p = captured["payload"]
    assert p["stage"] == briefing.AFTER_INTRO and p["is_first_contact"] is False
    assert p["what_buyer_told_us"]["destination_port"] == "Hamburg"
    assert not any("destination port" in m for m in p["missing_information"])   # never asked again
    assert any("inspection agency" in m for m in p["missing_information"])
    assert p["product_name"] == "crude glycerine" and p["pricing"]["recommended_price_usd_per_mt"] == 980.0
    assert p["pricing"]["meets_buyer_target"] is True and p["pricing"]["is_final_offer"] is False
    assert "760" not in json.dumps(p)          # supplier price never reaches the buyer agent
    assert out["buyer_outbound_message"] == "Dear team\nok"
    assert out["buyer_stage"] == briefing.AFTER_INTRO and out["buyer_draft_flags"] == []


def test_supplier_draft_above_their_quote_is_corrected_and_buyer_price_hidden():
    captured: dict = {}
    session = _session()
    with patch("agents.negotiation_agent.briefing._load_deal", AsyncMock(return_value=_deal_after_intro())), \
         patch("agents.negotiation_agent.graph._chat", _capture(captured, 802.3)):
        out = run(supplier_agent_node(_state(session)))
    p = captured["payload"]
    assert p["stage"] == briefing.AFTER_INTRO
    assert p["what_supplier_told_us"]["port_of_loading"] == "Nhava Sheva"
    assert not any("GSTIN" in m for m in p["missing_information"])   # already verified
    assert "980" not in json.dumps(p)          # buyer target never reaches the supplier agent
    assert out["current_ask"] == p["pricing"]["recommended_price_usd_per_mt"] < 760.0
    assert out["supplier_draft_flags"] == ["price_out_of_band"]


def test_no_deal_means_cold_introduction():
    captured: dict = {}
    with patch("agents.negotiation_agent.graph._chat", _capture(captured, 980.0)):
        run(buyer_agent_node(_state(_session(deal_id="DEAL-MISSING"))))
    assert captured["payload"]["stage"] == briefing.COLD and captured["payload"]["is_first_contact"] is True
    # the session target here is our scan estimate — the buyer never named it
    assert captured["payload"]["pricing"]["meets_buyer_target"] is False
    assert captured["payload"]["quantity_mt"] == 20 and isinstance(captured["payload"]["quantity_mt"], int)


def test_record_offers_and_subjects():
    s = _session()
    _record_offers(s, {"buyer_outbound_message": "x", "current_bid": 975.0, "supplier_outbound_message": "",
                       "current_ask": 700.0})
    assert s.last_offer_to_buyer == 975.0 and s.last_offer_to_supplier is None
    assert negotiation_subject(s, "buyer", briefing.AFTER_INTRO) == "Crude glycerine — our offer for 20 MT, CIF your port"
    assert negotiation_subject(s, "supplier", briefing.COLD).startswith("Crude glycerine export order — 20 MT")
    assert negotiation_subject(s, "buyer", briefing.CONTINUING).startswith("Re: Crude glycerine")
    assert "Round" not in negotiation_subject(s, "supplier", None)


def test_terms_follow_the_real_escrow_schedule():
    text = briefing.buyer_terms()
    assert "30% when the purchase order is placed" in text and "10% when the buyer accepts" in text
    assert "letter" not in briefing.buyer_terms().lower()
    assert "%" not in briefing.supplier_terms()   # supplier split is agreed on the PO, never promised


# ---------------------------------------------------------------------------
# Templates: RFQ and buyer introduction
# ---------------------------------------------------------------------------

def test_rfq_never_carries_the_buyer_price():
    from services.sourcing.schemas import RfqRequest
    from services.sourcing.service import _build_rfq_message, rfq_email_body, rfq_subject
    req = RfqRequest(deal_id="DEAL-D1", hs_code="1520", quantity_mt=20.0, target_price_usd=980.0, suppliers=[])
    msg = _build_rfq_message(req)
    assert "980" not in msg and "CIF" not in msg and "crude glycerine" in msg and "20 MT" in msg
    body = rfq_email_body("Swadesh India Chemical Pvt Ltd", msg)
    assert body.startswith("Dear Swadesh India Chemical Pvt Ltd team,")
    assert "escrow" in body and "GSTIN" in body and "980" not in body
    assert rfq_subject("RFQ-X", "1520", 20.0) == (
        "Request for quotation — 20 MT crude glycerine (HS 1520), payment secured in escrow [RFQ:RFQ-X]")


def test_outreach_names_the_product_and_describes_escrow_truthfully():
    from shared.orchestration import outreach
    deal = SimpleNamespace(deal_id="DEAL-D1", hs_code="1520", quantity_mt=20.0)
    cand = SimpleNamespace(legal_name="Glaconchemie", pitch_angle="", candidate_id="c1", quantity_mt=None)
    subject, body = outreach.draft(deal, cand)
    assert subject.startswith("Crude glycerine from India") and "Hs " not in subject
    assert body.startswith("Dear Glaconchemie team,")
    assert "production advance" in body and "never pay for goods" not in body
    unknown = SimpleNamespace(deal_id="DEAL-D1", hs_code="9999", quantity_mt=None)
    s2, b2 = outreach.draft(unknown, cand)
    assert s2.startswith("HS 9999 from India") and "the HS 9999 product" in b2
