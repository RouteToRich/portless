"""Auto-retry for negotiation sessions dead-ended by an LLM Gateway failure.

Two distinct dead-end shapes, both detected by _is_gateway_failure_deadend and
recovered by _retry_gateway_failed_round:

1. Draft failed mid-round: start_negotiation/run_round's graph invocation
   raises (e.g. every model in the LLM Gateway's fallback chain is
   rate-limited/exhausted). The exception is caught and the session is parked
   at HITL_PENDING — but because the graph never produced an outbound draft,
   _post_graph_notify's email-gating step never runs, so NO HITL approval is
   created either. Signature: HITL_PENDING + zero pending approvals + last
   round has no outbound_message.

2. Process died before the first round: start_negotiation's own coroutine is
   killed (deploy, OOM, system restart) somewhere between its initial
   save_session(status=STARTED, round_no=0) and graph.ainvoke() completing.
   Nothing ever runs the except block or writes a round record, so the
   session sits at STARTED/round_no=0 with ZERO rounds — no draft, no
   approval, and no exception for the caller's own try/except to route to the
   Exception Resolver. Signature: STARTED + round_no==0 + zero round records.

Either way that's a dead end: nothing for a human to act on, and nothing that
looks any different from "waiting on a real draft" to a casual read of
session.status. The 5-min stale-watcher sweep detects both signatures and
auto-retries, bounded by _MAX_GATEWAY_RETRIES.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import agents.negotiation_agent.service as svc
from agents.negotiation_agent.schemas import (
    NegotiationPolicy,
    NegotiationResponse,
    NegotiationRound,
    NegotiationSession,
)

NOW = datetime.now(timezone.utc)
NEG_ID = "neg-gateway-retry-test"
DEAL_ID = "DEAL-GWR-1"


def _session(**overrides) -> NegotiationSession:
    defaults = dict(
        negotiation_id=NEG_ID,
        deal_id=DEAL_ID,
        trade_match_id="M1",
        buyer_id="BUYER-1",
        seller_id="SUP-1",
        product_code="290543",
        buyer_country_code="US",
        buyer_target_price=1255.0,
        supplier_quote_price=1004.0,
        policy=NegotiationPolicy(),
        status="HITL_PENDING",
        round_no=2,
        current_bid=1229.41,
        current_ask=1003.9972,
        hitl_required=True,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(minutes=10),
    )
    defaults.update(overrides)
    return NegotiationSession(**defaults)


_seq = 0


def _round(round_no: int, party: str, outbound_message: str = "", **overrides) -> NegotiationRound:
    global _seq
    _seq += 1
    defaults = dict(
        round_id=f"round-{_seq}",
        negotiation_id=NEG_ID,
        deal_id=DEAL_ID,
        round_no=round_no,
        party=party,
        channel="email",
        inbound_price=1229.41 if party == "buyer" else 1003.99,
        inbound_message="hello" if party != "init" else None,
        outbound_message=outbound_message,
        created_at=NOW - timedelta(minutes=10),
    )
    defaults.update(overrides)
    return NegotiationRound(**defaults)


class FakeApprovalsEmpty:
    @staticmethod
    async def list_approvals(status=None, deal_id=None):
        return []


class FakeApprovalsPending:
    @staticmethod
    async def list_approvals(status=None, deal_id=None):
        return [object()]  # any truthy pending approval


def _patch_approvals(monkeypatch, fake):
    import services.hitl_approval.service as hitl_svc
    monkeypatch.setattr(hitl_svc, "list_approvals", fake.list_approvals)


# ---------------------------------------------------------------------------
# Dead-end detection
# ---------------------------------------------------------------------------

async def test_deadend_detected_when_no_approval_and_empty_outbound(monkeypatch):
    _patch_approvals(monkeypatch, FakeApprovalsEmpty)
    session = _session(round_no=2)
    rounds = [_round(1, "init", outbound_message="hi buyer"), _round(2, "buyer", outbound_message="")]

    async def fake_list_rounds(negotiation_id):
        return rounds
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    assert await svc._is_gateway_failure_deadend(session) is True


async def test_not_deadend_when_pending_approval_exists(monkeypatch):
    _patch_approvals(monkeypatch, FakeApprovalsPending)
    session = _session(round_no=2)
    assert await svc._is_gateway_failure_deadend(session) is False


async def test_deadend_detected_when_start_negotiation_itself_failed(monkeypatch):
    # start_negotiation unconditionally bumps round_no 0 -> 1 before saving,
    # even when the graph raised — so a failed FIRST round lands at round_no=1
    # with party="init" and no outbound, not round_no=0. This must still be
    # detected as a dead-end (previously excluded by a stale round_no==0 guard
    # whose "covered by _renotify_stuck_round0" premise never actually applied,
    # since that path only re-notifies an approval that was never created).
    _patch_approvals(monkeypatch, FakeApprovalsEmpty)
    session = _session(round_no=1)
    rounds = [_round(1, "init", outbound_message="")]

    async def fake_list_rounds(negotiation_id):
        return rounds
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    assert await svc._is_gateway_failure_deadend(session) is True


async def test_deadend_detected_when_process_died_before_first_round(monkeypatch):
    # start_negotiation's own coroutine got killed (system restart) somewhere
    # between the initial save_session(status=STARTED, round_no=0) and
    # graph.ainvoke() completing — no round record was ever written, so
    # list_rounds returns []. This must still be caught: without it, a session
    # in this exact shape is invisible to every other safety net (no draft to
    # gate, no approval, no exception for continuations.py to route).
    _patch_approvals(monkeypatch, FakeApprovalsEmpty)
    session = _session(status="STARTED", round_no=0)

    async def fake_list_rounds(negotiation_id):
        return []
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    assert await svc._is_gateway_failure_deadend(session) is True


async def test_not_deadend_when_zero_rounds_but_not_started_round_zero(monkeypatch):
    # Zero rounds is only a dead-end signature for a fresh STARTED/round_no=0
    # session. Any other status/round_no combination with no rounds is an
    # unrelated or not-yet-understood state — don't touch it.
    _patch_approvals(monkeypatch, FakeApprovalsEmpty)
    session = _session(status="HITL_PENDING", round_no=2)

    async def fake_list_rounds(negotiation_id):
        return []
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    assert await svc._is_gateway_failure_deadend(session) is False


async def test_not_deadend_when_last_round_has_real_draft(monkeypatch):
    _patch_approvals(monkeypatch, FakeApprovalsEmpty)
    session = _session(round_no=2)
    rounds = [_round(1, "init", outbound_message="hi"), _round(2, "buyer", outbound_message="real draft text")]

    async def fake_list_rounds(negotiation_id):
        return rounds
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    assert await svc._is_gateway_failure_deadend(session) is False


# ---------------------------------------------------------------------------
# Retry replay
# ---------------------------------------------------------------------------

async def test_retry_replays_last_round_via_run_round(monkeypatch):
    session = _session(round_no=2)
    rounds = [_round(1, "init", outbound_message="hi"), _round(2, "supplier", outbound_message="", inbound_price=1003.99, inbound_message="their reply", channel="email")]

    async def fake_list_rounds(negotiation_id):
        return rounds
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    captured = {}
    async def fake_run_round(negotiation_id, request):
        captured["negotiation_id"] = negotiation_id
        captured["request"] = request
        return NegotiationResponse(negotiation_id=negotiation_id, deal_id=DEAL_ID, status="ROUND", round_no=3)
    monkeypatch.setattr(svc, "run_round", fake_run_round)

    ok = await svc._retry_gateway_failed_round(session)
    assert ok is True
    assert captured["negotiation_id"] == NEG_ID
    assert captured["request"].party == "supplier"
    assert captured["request"].counter_price == 1003.99
    assert captured["request"].inbound_message == "their reply"
    assert captured["request"].channel == "email"


async def test_retry_round_zero_uses_dedicated_path(monkeypatch):
    session = _session(round_no=1)
    rounds = [_round(1, "init", outbound_message="")]

    async def fake_list_rounds(negotiation_id):
        return rounds
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    called = {}
    async def fake_retry_round_zero(sess):
        called["session"] = sess
    monkeypatch.setattr(svc, "_retry_round_zero", fake_retry_round_zero)

    ok = await svc._retry_gateway_failed_round(session)
    assert ok is True
    assert called["session"].negotiation_id == NEG_ID


async def test_retry_uses_round_zero_path_when_no_rounds_exist(monkeypatch):
    # The process-died-before-first-round case: list_rounds returns [].
    # _retry_gateway_failed_round must not bail out (last is None) — it should
    # fall back to _retry_round_zero, the same recovery path start_negotiation
    # itself would have taken.
    session = _session(status="STARTED", round_no=0)

    async def fake_list_rounds(negotiation_id):
        return []
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    called = {}
    async def fake_retry_round_zero(sess):
        called["session"] = sess
    monkeypatch.setattr(svc, "_retry_round_zero", fake_retry_round_zero)

    ok = await svc._retry_gateway_failed_round(session)
    assert ok is True
    assert called["session"].negotiation_id == NEG_ID


async def test_retry_cap_stops_after_max_attempts(monkeypatch):
    session = _session(round_no=20)
    # Simulate _MAX_GATEWAY_RETRIES + 2 consecutive failed (empty-outbound) rounds
    rounds = [_round(1, "init", outbound_message="hi")]
    rounds += [
        _round(n, "buyer" if n % 2 else "supplier", outbound_message="")
        for n in range(2, 2 + svc._MAX_GATEWAY_RETRIES + 2)
    ]

    async def fake_list_rounds(negotiation_id):
        return rounds
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    called = {"n": 0}
    async def fake_run_round(negotiation_id, request):
        called["n"] += 1
        return NegotiationResponse(negotiation_id=negotiation_id, deal_id=DEAL_ID, status="ROUND", round_no=1)
    monkeypatch.setattr(svc, "run_round", fake_run_round)

    ok = await svc._retry_gateway_failed_round(session)
    assert ok is False
    assert called["n"] == 0  # gave up before ever calling run_round


async def test_retry_returns_false_on_repeat_failure(monkeypatch):
    session = _session(round_no=2)
    rounds = [_round(1, "init", outbound_message="hi"), _round(2, "buyer", outbound_message="")]

    async def fake_list_rounds(negotiation_id):
        return rounds
    monkeypatch.setattr(svc.get_repository(), "list_rounds", fake_list_rounds)

    async def failing_run_round(negotiation_id, request):
        raise RuntimeError("gateway still exhausted")
    monkeypatch.setattr(svc, "run_round", failing_run_round)

    ok = await svc._retry_gateway_failed_round(session)
    assert ok is False


# ---------------------------------------------------------------------------
# Sweep wiring
# ---------------------------------------------------------------------------

async def test_sweep_retries_deadend_before_stale_fail_check(monkeypatch):
    """A dead-ended session must be retried, not funneled into the 30-day
    stale-fail branch just because it's technically 'idle'."""
    session = _session(
        round_no=2,
        updated_at=NOW - timedelta(days=40),  # would trip the 30-day stale-fail
    )

    class FakeRepo:
        async def list_sessions_by_status(self, statuses):
            return [session]
        async def list_rounds(self, negotiation_id):
            return [_round(1, "init", outbound_message="hi"), _round(2, "buyer", outbound_message="")]

    monkeypatch.setattr(svc, "get_repository", lambda: FakeRepo())
    _patch_approvals(monkeypatch, FakeApprovalsEmpty)

    retried = {"n": 0}
    async def fake_retry(sess):
        retried["n"] += 1
        return True
    monkeypatch.setattr(svc, "_retry_gateway_failed_round", fake_retry)

    failed_called = {"n": 0}
    async def fake_fail(sess):
        failed_called["n"] += 1
    monkeypatch.setattr(svc, "_fail_stale_negotiation", fake_fail)

    counts = await svc.sweep_stale_negotiations()
    assert counts["retried"] == 1
    assert retried["n"] == 1
    assert failed_called["n"] == 0  # must NOT also fail it in the same sweep


async def test_sweep_skips_gateway_retry_check_within_backoff_window(monkeypatch):
    """A session updated moments ago (e.g. our own retry just ran) should not
    be immediately re-checked for dead-end retry in the same tick."""
    session = _session(round_no=2, updated_at=NOW - timedelta(seconds=5))

    class FakeRepo:
        async def list_sessions_by_status(self, statuses):
            return [session]

    monkeypatch.setattr(svc, "get_repository", lambda: FakeRepo())

    deadend_checked = {"n": 0}
    async def fake_deadend(sess):
        deadend_checked["n"] += 1
        return True
    monkeypatch.setattr(svc, "_is_gateway_failure_deadend", fake_deadend)
    monkeypatch.setattr(svc.settings, "followup_enabled", False)

    await svc.sweep_stale_negotiations()
    assert deadend_checked["n"] == 0
