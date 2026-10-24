"""Tests for the Negotiation Agent.

Coverage:
  • guards.py          — net margin, payout/margin check, convergence, midpoint,
                         near-floor detection (pure functions).
  • graph orchestrator — AGREED / FAILED terminals, and the 2026-07-10 draft-vs-gate
                         fix: near-floor and first-outbound now DRAFT (route to an
                         agent) with hitl_required=True instead of short-circuiting
                         to HITL before any message exists.
  • graph end-to-end   — a below-floor margin now produces an outbound message and
                         the pass ends after one draft.
  • service email      — send / modify / reject / inbound lifecycle
                         (DRAFT→PENDING_APPROVAL→SENT|MODIFIED_SENT|REJECTED|FAILED,
                         inbound→RECEIVED).
  • realized CMS       — the repaired score_match(ScoreRequest) call populates
                         session.realized_cms.

The LLM (`graph._chat`), the SMTP transport (`email_gateway.sender.send_email`),
and Match Engine (`score_match`) are mocked, so these run fully in-memory.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from agents.negotiation_agent.email_history import (
    EmailDirection,
    EmailHistoryRecord,
    EmailStatus,
    InMemoryEmailHistoryRepository,
)
from agents.negotiation_agent.graph import (
    build_negotiation_graph,
    buyer_agent_node,
    orchestrator_node,
    supplier_agent_node,
)
from agents.negotiation_agent.guards import (
    acceptance_price_consistent,
    check_convergence,
    compute_midpoint_price,
    compute_net_margin,
    extract_usd_prices,
    is_explicit_acceptance,
    is_near_margin_floor,
    verify_payout_and_margin,
)
from agents.negotiation_agent.repository import InMemoryNegotiationRepository
from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession
from agents.negotiation_agent.service import (
    _build_conversation_history,
    _compute_realized_cms,
    _graph_state_to_status,
    _post_graph_notify,
    _sync_deal_status,
    finalize_agreement,
    list_email_history,
    record_inbound_email,
    reject_agreement,
    reject_email,
    send_approved_email,
)


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _policy(**overrides) -> NegotiationPolicy:
    defaults = dict(target_margin_pct=12.0, margin_floor_pct=8.0, max_rounds=5, concession_step=0.10)
    defaults.update(overrides)
    return NegotiationPolicy(**defaults)


def _session(**overrides) -> NegotiationSession:
    now = datetime.now(timezone.utc)
    defaults = dict(
        negotiation_id="neg-1",
        deal_id="DEAL-1",
        trade_match_id="TM-1",
        buyer_id="BYR-1",
        seller_id="SUP-1",
        product_code="1006",
        buyer_country_code="IN",
        buyer_target_price=500.0,
        supplier_quote_price=460.0,
        policy=_policy(),
        status="STARTED",
        round_no=0,
        current_bid=500.0,
        current_ask=460.0,
        # Satisfy the agreement completeness checklist by default — these
        # tests exercise price/routing logic, not the checklist (which has
        # its own coverage in tests/test_qc_providers.py).
        qc_provider="sgs",
        quantity_mt=25.0,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return NegotiationSession(**defaults)


def _state(session: NegotiationSession, **overrides) -> dict:
    s = {
        "session": session,
        "logistics_cost": 0.0,
        "is_first_round": False,
        "party": "supplier",
        "inbound_message": None,
        "inbound_price": None,
        "operator_override_price": None,
        "current_bid": session.current_bid,
        "current_ask": session.current_ask,
        "current_margin": 0.0,
        "orchestrator_decision": None,
        "buyer_outbound_message": "",
        "supplier_outbound_message": "",
        "buyer_terms": {},
        "supplier_terms": {},
        "routing": "CALL_BUYER_AGENT",
        "hitl_required": False,
        "agreed_price": None,
        "reasoning": "",
        "error": None,
    }
    s.update(overrides)
    return s


# The verbatim buyer email from the 2026-07-15 incident: a conditional
# counter-offer ("if ... 1900$") ending in a request for OUR confirmation —
# the bare keyword "confirm" wrongly closed the negotiation as AGREED.
_BUYER_CONDITIONAL_COUNTER = (
    "Thanks,\n"
    "I would be interested, if the person unit price would be 1900$.\n"
    "Kindly let me know and confirm"
)


def _intent_chat(
    intent: str,
    mentioned_price: float | None = None,
    routing: str = "CALL_SUPPLIER_AGENT",
    margin: float = 12.0,
):
    """LLM stub answering both the intent classifier and the orchestrator router."""
    async def _chat(system_prompt: str, payload: dict, caller: str) -> str:
        if "intent_classifier" in caller:
            return json.dumps({
                "intent": intent,
                "mentioned_price_usd": mentioned_price,
                "reasoning": "test classification",
            })
        return json.dumps({
            "trade_match_id": "TM-1",
            "current_estimated_margin_pct": margin,
            "action_routing": routing,
            "margin_at_current": margin,
            "hitl_required": False,
            "reasoning": "routing decision",
            "buyer_round": 0,
            "supplier_round": 0,
        })
    return _chat


def _orch_chat(routing: str = "CALL_BUYER_AGENT", margin: float = 8.0) -> AsyncMock:
    return AsyncMock(return_value=json.dumps({
        "trade_match_id": "TM-1",
        "current_estimated_margin_pct": margin,
        "action_routing": routing,
        "margin_at_current": margin,
        "hitl_required": False,
        "reasoning": "routing decision",
        "buyer_round": 0,
        "supplier_round": 0,
    }))


async def _fake_chat(system_prompt: str, payload: dict, caller: str) -> str:
    """Node-aware LLM stub keyed on the caller tag."""
    if "orchestrator" in caller:
        return json.dumps({
            "current_estimated_margin_pct": 8.0, "action_routing": "CALL_BUYER_AGENT",
            "margin_at_current": 8.0, "hitl_required": False, "reasoning": "route to buyer",
        })
    if "buyer_agent" in caller:
        return json.dumps({
            "counter_price_usd": 470.0, "outbound_message": "We propose USD 470/MT CIF.",
            "negotiated_terms": {"unit_price": 470.0, "currency": "USD"}, "reasoning": "buyer counter",
        })
    if "supplier_agent" in caller:
        return json.dumps({
            "counter_price_usd": 455.0, "outbound_message": "We can offer USD 455/MT Ex-Works.",
            "negotiated_terms": {"unit_price": 455.0, "currency": "USD"}, "reasoning": "supplier counter",
        })
    return "{}"


def _email(**overrides) -> EmailHistoryRecord:
    now = datetime.now(timezone.utc)
    defaults = dict(
        email_id="EMAIL-1",
        negotiation_id="neg-1",
        deal_id="DEAL-1",
        round_no=1,
        direction=EmailDirection.OUTBOUND,
        party="buyer",
        to_address="buyer@example.com",
        from_address="portless-trade@portless.io",
        subject="Round 1",
        body="original body",
        status=EmailStatus.PENDING_APPROVAL,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return EmailHistoryRecord(**defaults)


@pytest.fixture(autouse=True)
def _email_repo(monkeypatch) -> InMemoryEmailHistoryRepository:
    """Fresh in-memory email-history repo shared with the service layer."""
    repo = InMemoryEmailHistoryRepository()
    monkeypatch.setattr("agents.negotiation_agent.email_history._email_repo", repo)
    return repo


# ---------------------------------------------------------------------------
# guards.py — pure functions
# ---------------------------------------------------------------------------

def test_compute_net_margin_basic():
    assert compute_net_margin(500.0, 460.0, 0.0, None) == pytest.approx(8.0)


def test_compute_net_margin_with_costs():
    # (500 - 460 - 10 - 0) / 500 = 6%
    assert compute_net_margin(500.0, 460.0, 10.0, None) == pytest.approx(6.0)


def test_verify_payout_and_margin_ok():
    ok, margin = verify_payout_and_margin(_session(), 500.0, 440.0, 0.0)
    assert ok is True
    assert margin == pytest.approx(12.0)


def test_verify_payout_and_margin_below_floor():
    ok, margin = verify_payout_and_margin(_session(), 500.0, 470.0, 0.0)  # 6% < 8%
    assert ok is False


def test_check_convergence_true_when_gap_and_margin_ok():
    session = _session(policy=_policy(margin_floor_pct=0.0))
    assert check_convergence(session, 500.0, 499.95, 0.0) is True


def test_check_convergence_false_when_gap_too_wide():
    assert check_convergence(_session(), 500.0, 460.0, 0.0) is False


def test_compute_midpoint_price():
    assert compute_midpoint_price(500.0, 460.0) == 480.0


def test_is_near_margin_floor():
    session = _session()  # floor 8
    assert is_near_margin_floor(session, 9.0, 2.0) is True    # 9 < 10
    assert is_near_margin_floor(session, 11.0, 2.0) is False  # 11 >= 10


def test_is_explicit_acceptance():
    # Real Gmail reply: new content ("Agreed") above a line-wrapped
    # "On <date>, <name> <email> wrote:" quote attribution.
    real_reply = (
        "Agreed\n\n"
        "On Wed, 15 Jul 2026, 12:20 am karpoora paulpandi, <\n"
        "karpoorapaulpandi@gmail.com> wrote:\n\n"
        "> Hi, yes we. Are ok with this deal\n"
        ">\n"
        "> On Wed, 15 Jul 2026, 12:19 am PortLess Trade Agent, <richminds84@gmail.com>\n"
        "> wrote:\n"
    )
    assert is_explicit_acceptance(real_reply) is True
    assert is_explicit_acceptance("Sounds good, please proceed.") is True
    assert is_explicit_acceptance("We can only do $1150, let us know.") is False
    assert is_explicit_acceptance("We don't agree with this price.") is False
    assert is_explicit_acceptance("No problem, when can you ship?") is False
    assert is_explicit_acceptance(None) is False
    assert is_explicit_acceptance("") is False


def test_is_explicit_acceptance_conditional_counter_regression():
    # 2026-07-15 incident: bare "confirm" in a request-for-confirmation
    # matched the accept pattern and closed a live negotiation.
    assert is_explicit_acceptance(_BUYER_CONDITIONAL_COUNTER) is False
    assert is_explicit_acceptance("Please confirm the unit price.") is False
    assert is_explicit_acceptance("Kindly confirm availability and proceed date.") is False
    assert is_explicit_acceptance("Would you agree to $1900?") is False
    # First-person confirmation statements still count as acceptance.
    assert is_explicit_acceptance("Confirmed, go ahead.") is True
    assert is_explicit_acceptance("We confirm the order at the quoted price.") is True


def test_extract_usd_prices():
    assert extract_usd_prices("if the person unit price would be 1900$") == [1900.0]
    assert extract_usd_prices("$1,900.50 and later USD 1800") == [1900.5, 1800.0]
    assert extract_usd_prices("we can ship 500 units in 30 days") == []
    assert extract_usd_prices(None) == []


def test_acceptance_price_consistent():
    assert acceptance_price_consistent("We accept, please proceed.", 500.0) is True
    assert acceptance_price_consistent("We accept your offer of $500.", 500.0) is True
    assert acceptance_price_consistent("We accept, but at $490.", 500.0) is False
    # Prices inside the quoted reply-chain history are ignored.
    quoted = "Agreed\n\nOn Wed, 15 Jul 2026, PortLess Trade Agent wrote:\n> our offer is $450\n"
    assert acceptance_price_consistent(quoted, 500.0) is True
    assert acceptance_price_consistent(None, 500.0) is True


# ---------------------------------------------------------------------------
# Orchestrator routing
# ---------------------------------------------------------------------------

def test_orchestrator_converged_agrees():
    session = _session(policy=_policy(margin_floor_pct=0.0), current_bid=500.0, current_ask=499.95)
    result = run(orchestrator_node(_state(session, current_bid=500.0, current_ask=499.95)))
    assert result["routing"] == "NEGOTIATION_AGREED"
    assert result["agreed_price"] is not None
    assert result["agreed_price"] == pytest.approx(499.975, abs=0.02)


def test_orchestrator_explicit_acceptance_agrees():
    # bid=500 ask=440 is a healthy 12% margin but the gap (60) is nowhere near
    # the concession step (0.10) — check_convergence() alone would never fire
    # here, since the supplier's price is fixed and the "gap" is just margin.
    # An outright "Agreed" from the buyer (LLM intent = ACCEPT) should still
    # close the pass as NEGOTIATION_AGREED (pending operator confirmation).
    session = _session(round_no=2, current_bid=500.0, current_ask=440.0)
    state = _state(
        session,
        party="buyer",
        inbound_message="Agreed, please proceed.",
        current_bid=500.0,
        current_ask=440.0,
    )
    with patch("agents.negotiation_agent.graph._chat", _intent_chat("ACCEPT")):
        result = run(orchestrator_node(state))
    assert result["routing"] == "NEGOTIATION_AGREED"
    assert result["agreed_price"] == 500.0  # the buyer's accepted price, not a bid/ask midpoint
    assert result["hitl_required"] is True  # agreement still needs operator confirmation


def test_orchestrator_conditional_counter_is_not_agreed():
    # 2026-07-15 regression: the buyer's conditional counter ("...if the unit
    # price would be 1900$. Kindly let me know and confirm") must NOT close the
    # negotiation — the LLM classifier reads it as a counter-offer.
    session = _session(round_no=2, current_bid=1900.0, current_ask=1500.0)
    state = _state(
        session,
        party="buyer",
        inbound_message=_BUYER_CONDITIONAL_COUNTER,
        current_bid=1900.0,
        current_ask=1500.0,
    )
    with patch("agents.negotiation_agent.graph._chat", _intent_chat("COUNTER_OFFER", 1900.0)):
        result = run(orchestrator_node(state))
    assert result["routing"] != "NEGOTIATION_AGREED"
    assert result["agreed_price"] is None


def test_orchestrator_accept_with_mismatched_llm_price_is_not_agreed():
    # Defense in depth: even if the LLM says ACCEPT, a mentioned price that
    # differs from the would-be agreed price vetoes the acceptance.
    session = _session(round_no=2, current_bid=500.0, current_ask=440.0)
    state = _state(
        session,
        party="buyer",
        inbound_message="We accept — final price 490 dollars.",
        current_bid=500.0,
        current_ask=440.0,
    )
    with patch("agents.negotiation_agent.graph._chat", _intent_chat("ACCEPT", 490.0)):
        result = run(orchestrator_node(state))
    assert result["routing"] != "NEGOTIATION_AGREED"


def test_orchestrator_accept_with_mismatched_regex_price_is_not_agreed():
    # Same veto via the deterministic USD-price extraction when the LLM
    # returns ACCEPT but omits the price it saw.
    session = _session(round_no=2, current_bid=500.0, current_ask=440.0)
    state = _state(
        session,
        party="buyer",
        inbound_message="We accept at $490.",
        current_bid=500.0,
        current_ask=440.0,
    )
    with patch("agents.negotiation_agent.graph._chat", _intent_chat("ACCEPT", None)):
        result = run(orchestrator_node(state))
    assert result["routing"] != "NEGOTIATION_AGREED"


def test_orchestrator_intent_llm_down_keyword_fallback_rejects_conditional():
    # Gateway completely down → keyword-guard fallback. The tightened guard
    # must not read the conditional counter as an acceptance.
    async def _raise(system_prompt, payload, caller):
        raise RuntimeError("gateway down")

    session = _session(round_no=2, current_bid=1900.0, current_ask=1500.0)
    state = _state(
        session,
        party="buyer",
        inbound_message=_BUYER_CONDITIONAL_COUNTER,
        current_bid=1900.0,
        current_ask=1500.0,
    )
    with patch("agents.negotiation_agent.graph._chat", _raise):
        result = run(orchestrator_node(state))
    assert result["routing"] != "NEGOTIATION_AGREED"


def test_orchestrator_intent_llm_down_keyword_fallback_accepts_outright():
    # Gateway down but the reply is an unambiguous acceptance → the keyword
    # fallback still closes the pass (deal closure isn't hostage to the LLM).
    async def _raise(system_prompt, payload, caller):
        raise RuntimeError("gateway down")

    session = _session(round_no=2, current_bid=500.0, current_ask=440.0)
    state = _state(
        session,
        party="buyer",
        inbound_message="Agreed, please proceed.",
        current_bid=500.0,
        current_ask=440.0,
    )
    with patch("agents.negotiation_agent.graph._chat", _raise):
        result = run(orchestrator_node(state))
    assert result["routing"] == "NEGOTIATION_AGREED"
    assert result["agreed_price"] == 500.0


def test_orchestrator_explicit_acceptance_below_floor_falls_through():
    # Margin below floor: an "Agreed" reply must NOT auto-close the deal at a
    # losing price — falls through to the normal LLM-routed draft instead.
    session = _session(round_no=2, current_bid=500.0, current_ask=470.0)  # 6% margin, floor is 8%
    state = _state(
        session,
        party="buyer",
        inbound_message="Agreed, please proceed.",
        current_bid=500.0,
        current_ask=470.0,
    )
    with patch("agents.negotiation_agent.graph._chat", _orch_chat("CALL_SUPPLIER_AGENT", 6.0)):
        result = run(orchestrator_node(state))
    assert result["routing"] != "NEGOTIATION_AGREED"


def test_orchestrator_max_rounds_fails():
    session = _session(round_no=5, current_bid=500.0, current_ask=460.0)
    result = run(orchestrator_node(_state(session, current_bid=500.0, current_ask=460.0)))
    assert result["routing"] == "NEGOTIATION_FAILED"


def test_orchestrator_near_floor_drafts_with_hitl():
    # margin 8% (== floor) is within the warning band → previously short-circuited
    # to HITL with no draft; now it routes to an agent AND flags hitl_required.
    session = _session(round_no=1, current_bid=500.0, current_ask=460.0)
    state = _state(session, current_bid=500.0, current_ask=460.0, party="supplier")
    with patch("agents.negotiation_agent.graph._chat", _orch_chat("CALL_BUYER_AGENT", 8.0)):
        result = run(orchestrator_node(state))
    assert result["routing"] in ("CALL_BUYER_AGENT", "CALL_SUPPLIER_AGENT")
    assert result["hitl_required"] is True


def test_orchestrator_first_round_drafts_with_hitl():
    session = _session(round_no=0, current_bid=500.0, current_ask=440.0)  # healthy 12% margin
    state = _state(session, is_first_round=True, party="init", current_bid=500.0, current_ask=440.0)
    with patch("agents.negotiation_agent.graph._chat", _orch_chat("CALL_BUYER_AGENT", 12.0)):
        result = run(orchestrator_node(state))
    assert result["routing"] in ("CALL_BUYER_AGENT", "CALL_SUPPLIER_AGENT")
    assert result["hitl_required"] is True   # first-outbound flag, but a draft is still produced


def test_orchestrator_healthy_margin_routes_without_hitl():
    session = _session(round_no=2, current_bid=500.0, current_ask=440.0)  # 12% margin
    state = _state(session, is_first_round=False, party="buyer", current_bid=500.0, current_ask=440.0)
    with patch("agents.negotiation_agent.graph._chat", _orch_chat("CALL_SUPPLIER_AGENT", 12.0)):
        result = run(orchestrator_node(state))
    assert result["routing"] == "CALL_SUPPLIER_AGENT"
    assert result["hitl_required"] is False


def test_orchestrator_clamps_terminal_llm_routing_to_agent():
    # If the LLM returns a non-agent routing on a non-terminal pass, the orchestrator
    # must still route to an agent so a message gets drafted (party=buyer → supplier).
    session = _session(round_no=2, current_bid=500.0, current_ask=440.0)
    state = _state(session, is_first_round=False, party="buyer", current_bid=500.0, current_ask=440.0)
    with patch("agents.negotiation_agent.graph._chat", _orch_chat("HITL_APPROVAL", 12.0)):
        result = run(orchestrator_node(state))
    assert result["routing"] == "CALL_SUPPLIER_AGENT"


# ---------------------------------------------------------------------------
# Graph end-to-end — the draft-vs-gate fix
# ---------------------------------------------------------------------------

def test_graph_below_floor_drafts_one_message_and_ends():
    # A below-floor margin used to route straight to HITL with no draft. Now the
    # buyer agent drafts a message (and the pass ends after one draft).
    session = _session(round_no=1, current_bid=500.0, current_ask=460.0)
    state = _state(session, current_bid=500.0, current_ask=460.0, party="supplier")
    with patch("agents.negotiation_agent.graph._chat", _fake_chat):
        final = run(build_negotiation_graph().ainvoke(state))
    assert final["buyer_outbound_message"]          # non-empty → agent actually drafted
    assert final["routing"] == "CALL_BUYER_AGENT"
    assert final["hitl_required"] is True           # near floor → flagged for approval


# ---------------------------------------------------------------------------
# Service — email lifecycle
# ---------------------------------------------------------------------------

def test_send_approved_email_marks_sent(_email_repo):
    run(_email_repo.save(_email(email_id="E1")))
    with patch("services.email_gateway.sender.send_email", AsyncMock()):
        run(send_approved_email("E1"))
    rec = run(_email_repo.get("E1"))
    assert rec.status == EmailStatus.SENT
    assert rec.sent_at is not None


def test_send_approved_email_with_modification(_email_repo):
    run(_email_repo.save(_email(email_id="E2", body="orig draft")))
    with patch("services.email_gateway.sender.send_email", AsyncMock()):
        run(send_approved_email("E2", modified_body="operator-edited body"))
    rec = run(_email_repo.get("E2"))
    assert rec.status == EmailStatus.MODIFIED_SENT
    assert rec.original_body == "orig draft"
    assert rec.body == "operator-edited body"


def test_send_approved_email_marks_failed_on_smtp_error(_email_repo):
    run(_email_repo.save(_email(email_id="E3")))
    with patch("services.email_gateway.sender.send_email", AsyncMock(side_effect=RuntimeError("smtp down"))):
        run(send_approved_email("E3"))
    rec = run(_email_repo.get("E3"))
    assert rec.status == EmailStatus.FAILED


def test_reject_email_marks_rejected(_email_repo):
    run(_email_repo.save(_email(email_id="E4")))
    run(reject_email("E4"))
    rec = run(_email_repo.get("E4"))
    assert rec.status == EmailStatus.REJECTED


def test_record_inbound_email_is_received(_email_repo):
    rec = run(record_inbound_email(
        "neg-1", "DEAL-1", 2, "supplier", "supplier@x.th", "Re: quote", "We can do 460",
    ))
    assert rec.status == EmailStatus.RECEIVED
    assert rec.direction == EmailDirection.INBOUND
    history = run(list_email_history("neg-1"))
    assert len(history) == 1
    assert history[0].email_id == rec.email_id


# ---------------------------------------------------------------------------
# Realized CMS — repaired signature
# ---------------------------------------------------------------------------

def test_compute_realized_cms_populates_score():
    session = _session(agreed_price=480.0, current_ask=460.0, status="AGREED")
    repo = InMemoryNegotiationRepository()

    class _Resp:
        composite_score = 87.5

    with patch("services.match_engine.service.score_match", AsyncMock(return_value=_Resp())):
        run(_compute_realized_cms(session, repo))

    assert session.realized_cms == 87.5


# ---------------------------------------------------------------------------
# Conversation history — party-scoped context for "continued" emails
# ---------------------------------------------------------------------------

def test_build_conversation_history_is_party_scoped(_email_repo):
    run(_email_repo.save(_email(email_id="b1", party="buyer", direction=EmailDirection.OUTBOUND,
                                status=EmailStatus.SENT, round_no=1, body="We propose 470 to buyer")))
    run(_email_repo.save(_email(email_id="b2", party="buyer", direction=EmailDirection.INBOUND,
                                status=EmailStatus.RECEIVED, round_no=1, body="Buyer says 460 is our max")))
    run(_email_repo.save(_email(email_id="s1", party="supplier", direction=EmailDirection.OUTBOUND,
                                status=EmailStatus.SENT, round_no=1, body="We ask supplier for 455")))
    run(_email_repo.save(_email(email_id="d1", party="buyer", direction=EmailDirection.OUTBOUND,
                                status=EmailStatus.PENDING_APPROVAL, round_no=2, body="unsent draft body")))

    hist = run(_build_conversation_history("neg-1"))

    # buyer thread: both buyer messages, not the supplier one, not the unsent draft
    assert "470 to buyer" in hist["buyer"]
    assert "Buyer says 460" in hist["buyer"]
    assert "455" not in hist["buyer"]
    assert "unsent draft" not in hist["buyer"]
    # supplier thread: only the supplier message
    assert "455" in hist["supplier"]
    assert "470 to buyer" not in hist["supplier"]


def test_buyer_agent_receives_buyer_history_only():
    state = _state(
        _session(),
        buyer_history="[round 1] buyer → PortLess: 460 is our max",
        supplier_history="[round 1] PortLess → supplier: SUPPLIER-SECRET 455",
    )
    captured: dict = {}

    async def _cap(system_prompt, payload, caller):
        captured["payload"] = payload
        return json.dumps({"counter_price_usd": 470.0, "outbound_message": "ok",
                           "negotiated_terms": {}, "reasoning": "r"})

    with patch("agents.negotiation_agent.graph._chat", _cap):
        run(buyer_agent_node(state))

    assert captured["payload"]["conversation_history"] == "[round 1] buyer → PortLess: 460 is our max"
    # the supplier-side thread must never reach the buyer-facing agent
    assert "SUPPLIER-SECRET" not in json.dumps(captured["payload"])


def test_supplier_agent_receives_supplier_history_only():
    state = _state(
        _session(),
        buyer_history="[round 1] PortLess → buyer: BUYER-SECRET 470",
        supplier_history="[round 1] supplier → PortLess: we want 455",
    )
    captured: dict = {}

    async def _cap(system_prompt, payload, caller):
        captured["payload"] = payload
        return json.dumps({"counter_price_usd": 450.0, "outbound_message": "ok",
                           "negotiated_terms": {}, "reasoning": "r"})

    with patch("agents.negotiation_agent.graph._chat", _cap):
        run(supplier_agent_node(state))

    assert captured["payload"]["conversation_history"] == "[round 1] supplier → PortLess: we want 455"
    assert "BUYER-SECRET" not in json.dumps(captured["payload"])


# ---------------------------------------------------------------------------
# market_context wiring — live web-search color reaches the right drafting node
# ---------------------------------------------------------------------------

def test_buyer_agent_receives_market_context():
    state = _state(_session(), buyer_market_context="Rice CIF prices trending up 3% this quarter.")
    captured: dict = {}

    async def _cap(system_prompt, payload, caller):
        captured["payload"] = payload
        return json.dumps({"counter_price_usd": 470.0, "outbound_message": "ok",
                           "negotiated_terms": {}, "reasoning": "r"})

    with patch("agents.negotiation_agent.graph._chat", _cap):
        run(buyer_agent_node(state))

    assert captured["payload"]["market_context"] == "Rice CIF prices trending up 3% this quarter."


def test_supplier_agent_receives_market_context():
    state = _state(_session(), supplier_market_context="India Ex-Works rice rates steady this month.")
    captured: dict = {}

    async def _cap(system_prompt, payload, caller):
        captured["payload"] = payload
        return json.dumps({"counter_price_usd": 450.0, "outbound_message": "ok",
                           "negotiated_terms": {}, "reasoning": "r"})

    with patch("agents.negotiation_agent.graph._chat", _cap):
        run(supplier_agent_node(state))

    assert captured["payload"]["market_context"] == "India Ex-Works rice rates steady this month."


def test_buyer_market_context_excludes_supplier_market_context():
    state = _state(
        _session(),
        buyer_market_context="PUBLIC-BUYER-INFO",
        supplier_market_context="PUBLIC-SUPPLIER-INFO",
    )
    captured: dict = {}

    async def _cap(system_prompt, payload, caller):
        captured["payload"] = payload
        return json.dumps({"counter_price_usd": 470.0, "outbound_message": "ok",
                           "negotiated_terms": {}, "reasoning": "r"})

    with patch("agents.negotiation_agent.graph._chat", _cap):
        run(buyer_agent_node(state))

    # not a secrecy boundary (market data isn't confidential) — a wiring regression guard
    assert "PUBLIC-SUPPLIER-INFO" not in json.dumps(captured["payload"])


# ---------------------------------------------------------------------------
# Price-secrecy hardening — counterparty price never enters the LLM payload
# ---------------------------------------------------------------------------

def _capture_chat(captured: dict, counter: float):
    async def _cap(system_prompt, payload, caller):
        captured["payload"] = payload
        return json.dumps({"counter_price_usd": counter, "outbound_message": "ok",
                           "negotiated_terms": {}, "reasoning": "r"})
    return _cap


def test_buyer_payload_excludes_supplier_price_and_batna():
    session = _session(supplier_quote_price=461.37, batna="Vietnam fallback at 455.99")
    state = _state(session, current_bid=520.0, current_ask=461.37)
    captured: dict = {}
    with patch("agents.negotiation_agent.graph._chat", _capture_chat(captured, 515.0)):
        run(buyer_agent_node(state))

    payload = captured["payload"]
    dump = json.dumps(payload)
    assert "461.37" not in dump            # raw supplier ask never reaches the buyer LLM
    assert "455.99" not in dump            # BATNA never reaches the buyer LLM
    assert "current_ask" not in payload    # removed
    assert "midpoint_price" not in payload  # removed
    assert "floor_price" in payload        # non-invertible bound present instead
    assert payload["current_bid"] == 520.0  # buyer's own price is fine to keep


def test_supplier_payload_excludes_buyer_price_and_batna():
    session = _session(batna="Vietnam fallback at 455.99")
    state = _state(session, current_bid=477.83, current_ask=440.0)
    captured: dict = {}
    with patch("agents.negotiation_agent.graph._chat", _capture_chat(captured, 445.0)):
        run(supplier_agent_node(state))

    payload = captured["payload"]
    dump = json.dumps(payload)
    assert "477.83" not in dump            # raw buyer bid never reaches the supplier LLM
    assert "455.99" not in dump            # BATNA
    assert "current_bid" not in payload    # removed
    assert "implied_max_supplier_price_usd" in payload  # non-invertible ceiling kept
    assert "implied_max_supplier_price_inr" in payload  # INR equivalent provided, not derived by LLM
    assert payload["current_ask_usd"] == 440.0  # supplier's own price is fine to keep
    # INR fields are the supplier's own ask / the non-invertible ceiling scaled
    # by the rate — they must not reconstruct the raw buyer bid either.
    assert payload["current_ask_inr"] == round(440.0 * payload["usd_inr_rate"], 2)


# ---------------------------------------------------------------------------
# Deal hand-off — AGREED / FAILED advances the parent deal
# ---------------------------------------------------------------------------

class _St:
    def __init__(self, v: str) -> None:
        self.value = v


class _FakeDeal:
    def __init__(self, status: str) -> None:
        self.status = _St(status)


def test_sync_deal_status_agreed_transitions_deal():
    session = _session(status="AGREED", agreed_price=480.0, deal_id="DEAL-1")
    upd = AsyncMock()
    with (
        patch("services.deals.service.get_deal", AsyncMock(return_value=_FakeDeal("NEGOTIATING"))),
        patch("services.deals.service.update_status", upd),
    ):
        run(_sync_deal_status(session))
    upd.assert_awaited_once()
    assert upd.call_args.args[0] == "DEAL-1"
    assert upd.call_args.args[1].target_status.value == "NEGOTIATION_AGREED"


def test_sync_deal_status_failed_transitions_deal():
    session = _session(status="FAILED", deal_id="DEAL-1")
    upd = AsyncMock()
    with (
        patch("services.deals.service.get_deal", AsyncMock(return_value=_FakeDeal("NEGOTIATING"))),
        patch("services.deals.service.update_status", upd),
    ):
        run(_sync_deal_status(session))
    assert upd.call_args.args[1].target_status.value == "NEGOTIATION_FAILED"


def test_sync_deal_status_idempotent_when_already_advanced():
    session = _session(status="AGREED", agreed_price=480.0)
    upd = AsyncMock()
    with (
        patch("services.deals.service.get_deal", AsyncMock(return_value=_FakeDeal("NEGOTIATION_AGREED"))),
        patch("services.deals.service.update_status", upd),
    ):
        run(_sync_deal_status(session))
    upd.assert_not_awaited()   # deal already at target → no-op


def test_sync_deal_status_swallows_errors():
    session = _session(status="AGREED", agreed_price=480.0)
    with patch("services.deals.service.get_deal", AsyncMock(side_effect=RuntimeError("deals down"))):
        run(_sync_deal_status(session))   # must not raise


def test_sync_deal_status_noop_for_nonterminal():
    session = _session(status="ROUND")
    get = AsyncMock()
    with patch("services.deals.service.get_deal", get):
        run(_sync_deal_status(session))
    get.assert_not_called()


# ---------------------------------------------------------------------------
# Agreement HITL gate — AGREED only ever happens via operator confirmation
# ---------------------------------------------------------------------------

def test_graph_state_to_status_gates_agreement():
    # NEGOTIATION_AGREED from the graph is provisional → HITL_PENDING, never AGREED.
    assert _graph_state_to_status("NEGOTIATION_AGREED") == "HITL_PENDING"
    assert _graph_state_to_status("NEGOTIATION_FAILED") == "FAILED"
    assert _graph_state_to_status("HITL_APPROVAL") == "HITL_PENDING"


def test_post_graph_notify_gates_agreement_behind_hitl():
    # A detected agreement must create a CONFIRM_NEGOTIATION_AGREEMENT approval
    # and must NOT advance the deal or compute CMS — that happens only in
    # finalize_agreement() after the operator approves.
    session = _session(status="HITL_PENDING", agreed_price=480.0,
                       margin_at_current=12.0, round_no=3)
    final_state = {"buyer_outbound_message": "", "supplier_outbound_message": "",
                   "routing": "NEGOTIATION_AGREED", "reasoning": "acceptance detected"}

    class _Approval:
        approval_id = "APR-1"

    create = AsyncMock(return_value=_Approval())
    sync = AsyncMock()
    cms = AsyncMock()
    with (
        patch("services.hitl_approval.service.create_approval", create),
        patch("agents.negotiation_agent.service._sync_deal_status", sync),
        patch("agents.negotiation_agent.service._compute_realized_cms", cms),
    ):
        run(_post_graph_notify(session, final_state, InMemoryNegotiationRepository()))

    create.assert_awaited_once()
    req = create.call_args.args[0]
    assert req.action == "CONFIRM_NEGOTIATION_AGREEMENT"
    assert req.autonomy_level == "L0"
    assert req.callback_payload == {"negotiation_id": "neg-1", "agreed_price": 480.0}
    sync.assert_not_awaited()
    cms.assert_not_awaited()


def test_post_graph_notify_still_syncs_deal_on_failed():
    session = _session(status="FAILED")
    final_state = {"buyer_outbound_message": "", "supplier_outbound_message": "",
                   "routing": "NEGOTIATION_FAILED"}
    sync = AsyncMock()
    with patch("agents.negotiation_agent.service._sync_deal_status", sync):
        run(_post_graph_notify(session, final_state, InMemoryNegotiationRepository()))
    sync.assert_awaited_once()


def test_finalize_agreement_sets_agreed_and_syncs_deal():
    repo = InMemoryNegotiationRepository()
    session = _session(status="HITL_PENDING", agreed_price=480.0, hitl_required=True)
    run(repo.save_session(session))
    sync = AsyncMock()
    cms = AsyncMock()
    orgs = AsyncMock()
    with (
        patch("agents.negotiation_agent.service.get_repository", lambda: repo),
        patch("agents.negotiation_agent.service._sync_deal_status", sync),
        patch("agents.negotiation_agent.service._compute_realized_cms", cms),
        patch("agents.negotiation_agent.service._provision_counterparty_orgs", orgs),
    ):
        run(finalize_agreement("neg-1"))
    saved = run(repo.get_session("neg-1"))
    assert saved.status == "AGREED"
    assert saved.agreed_price == 480.0
    assert saved.hitl_required is False
    sync.assert_awaited_once()
    cms.assert_awaited_once()
    orgs.assert_awaited_once()   # buyer/seller organizations provisioned


def test_finalize_agreement_is_idempotent():
    repo = InMemoryNegotiationRepository()
    run(repo.save_session(_session(status="AGREED", agreed_price=480.0)))
    sync = AsyncMock()
    with (
        patch("agents.negotiation_agent.service.get_repository", lambda: repo),
        patch("agents.negotiation_agent.service._sync_deal_status", sync),
        patch("agents.negotiation_agent.service._provision_counterparty_orgs", AsyncMock()),
    ):
        run(finalize_agreement("neg-1"))
    sync.assert_not_awaited()   # already finalized → no-op


def test_provision_counterparty_orgs_links_both_sides():
    # Agreement reached → find-or-create an org per side (registering the
    # negotiation contact emails) and link both org ids onto the deal so
    # buyer/seller users see it in their portal.
    from agents.negotiation_agent.service import _provision_counterparty_orgs

    session = _session(
        buyer_email="purchasing@acme.example",
        seller_email="sales@agrico.example",
    )

    class _Org:
        def __init__(self, org_id: str, name: str) -> None:
            self.org_id = org_id
            self.name = name

    find = AsyncMock(side_effect=[_Org("ORG-B", "Acme"), _Org("ORG-S", "AgriCo")])
    setter = AsyncMock()
    with (
        patch("services.auth.service.find_or_create_organization_for_deal", find),
        patch("services.deals.service.set_counterparty_orgs", setter),
        patch("services.deals.service.get_deal", AsyncMock(side_effect=RuntimeError("deals down"))),
    ):
        run(_provision_counterparty_orgs(session))

    assert find.await_count == 2
    find.assert_any_await("BYR-1", "purchasing@acme.example")   # falls back to ids
    find.assert_any_await("SUP-1", "sales@agrico.example")      # when deal fetch fails
    setter.assert_awaited_once_with(
        "DEAL-1", buyer_org_id="ORG-B", seller_org_id="ORG-S"
    )


def test_provision_counterparty_orgs_never_raises():
    from agents.negotiation_agent.service import _provision_counterparty_orgs

    with patch(
        "services.auth.service.find_or_create_organization_for_deal",
        AsyncMock(side_effect=RuntimeError("auth repo not initialised")),
    ):
        run(_provision_counterparty_orgs(_session()))   # must not raise


def test_reject_agreement_reopens_negotiation():
    repo = InMemoryNegotiationRepository()
    session = _session(status="HITL_PENDING", agreed_price=480.0, hitl_required=True)
    run(repo.save_session(session))
    with patch("agents.negotiation_agent.service.get_repository", lambda: repo):
        run(reject_agreement("neg-1"))
    saved = run(repo.get_session("neg-1"))
    assert saved.status == "ROUND"          # negotiation continues
    assert saved.agreed_price is None       # provisional price cleared
    assert saved.hitl_required is False
