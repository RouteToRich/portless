"""Tests for the negotiation staleness watcher (sweep_stale_negotiations).

Two cases, per the product rule:
  • round 0 never sent (round_no == 0, non-terminal, idle > round0 threshold)
    → re-notify the pending SEND_NEGOTIATION_EMAIL approval; NEVER auto-send.
  • past round 0, gone quiet > cooling period → mark FAILED + advance the deal.
Terminal sessions and not-yet-stale sessions are left untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.negotiation_agent import service
from agents.negotiation_agent.repository import InMemoryNegotiationRepository
from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession


def _session(neg_id: str, status: str, round_no: int, idle_seconds: float) -> NegotiationSession:
    now = datetime.now(timezone.utc)
    return NegotiationSession(
        negotiation_id=neg_id,
        deal_id=f"DEAL-{neg_id}",
        trade_match_id="MATCH-1",
        buyer_id="BUYER-1",
        seller_id="SELLER-1",
        product_code="090422",
        buyer_country_code="US",
        buyer_target_price=100.0,
        supplier_quote_price=90.0,
        policy=NegotiationPolicy(),
        status=status,
        round_no=round_no,
        created_at=now - timedelta(seconds=idle_seconds),
        updated_at=now - timedelta(seconds=idle_seconds),
    )


@pytest.fixture(autouse=True)
def _repo(monkeypatch) -> InMemoryNegotiationRepository:
    repo = InMemoryNegotiationRepository()
    monkeypatch.setattr("agents.negotiation_agent.service.get_repository", lambda: repo)
    return repo


async def test_stuck_round0_is_renotified_not_sent(_repo, monkeypatch):
    # A round-0 session idle well past the 15-min re-notify threshold.
    await _repo.save_session(_session("N1", "HITL_PENDING", round_no=0, idle_seconds=3600))

    pending = SimpleNamespace(
        action="SEND_NEGOTIATION_EMAIL",
        approval_id="APPR-1",
        model_dump=lambda mode=None: {"approval_id": "APPR-1", "action": "SEND_NEGOTIATION_EMAIL"},
    )
    list_approvals = AsyncMock(return_value=[pending])
    notify = AsyncMock()
    monkeypatch.setattr("services.hitl_approval.service.list_approvals", list_approvals)
    monkeypatch.setattr("services.hitl_approval.notification.notify", notify)
    # If the watcher ever tried to fail/advance a round-0 deal, this would fire.
    sync = AsyncMock()
    monkeypatch.setattr("agents.negotiation_agent.service._sync_deal_status", sync)

    result = await service.sweep_stale_negotiations()

    assert result["renotified"] == 1
    assert result["failed"] == 0
    notify.assert_awaited_once()
    sync.assert_not_awaited()  # round-0 is NEVER auto-sent or auto-failed


async def test_round0_not_yet_stale_is_left_alone(_repo, monkeypatch):
    # Idle only 60s — under the 15-min round-0 threshold.
    await _repo.save_session(_session("N2", "HITL_PENDING", round_no=0, idle_seconds=60))

    list_approvals = AsyncMock(return_value=[])
    notify = AsyncMock()
    monkeypatch.setattr("services.hitl_approval.service.list_approvals", list_approvals)
    monkeypatch.setattr("services.hitl_approval.notification.notify", notify)

    result = await service.sweep_stale_negotiations()

    assert result["renotified"] == 0
    notify.assert_not_awaited()
    list_approvals.assert_not_awaited()  # threshold not reached — no HITL lookup


async def test_post_round0_stale_is_failed_and_deal_advanced(_repo, monkeypatch):
    # Round 3, idle 31 days — past the 30-day cooling period.
    await _repo.save_session(
        _session("N3", "ROUND", round_no=3, idle_seconds=31 * 24 * 3600)
    )
    sync = AsyncMock()
    monkeypatch.setattr("agents.negotiation_agent.service._sync_deal_status", sync)

    result = await service.sweep_stale_negotiations()

    assert result["failed"] == 1
    assert result["renotified"] == 0
    sync.assert_awaited_once()
    # Session persisted as FAILED so it leaves the non-terminal set.
    reloaded = await _repo.get_session("N3")
    assert reloaded.status == "FAILED"


async def test_post_round0_not_yet_cooled_is_left_alone(_repo, monkeypatch):
    # Round 2, idle only 10 days — under the 30-day cooling period.
    await _repo.save_session(
        _session("N4", "ROUND", round_no=2, idle_seconds=10 * 24 * 3600)
    )
    sync = AsyncMock()
    monkeypatch.setattr("agents.negotiation_agent.service._sync_deal_status", sync)

    result = await service.sweep_stale_negotiations()

    assert result["failed"] == 0
    sync.assert_not_awaited()
    reloaded = await _repo.get_session("N4")
    assert reloaded.status == "ROUND"  # untouched


async def test_terminal_sessions_are_never_swept(_repo, monkeypatch):
    # AGREED / FAILED / ESCALATED must not be scanned at all, even if ancient.
    for st in ("AGREED", "FAILED", "ESCALATED"):
        await _repo.save_session(
            _session(f"N-{st}", st, round_no=5, idle_seconds=99 * 24 * 3600)
        )
    sync = AsyncMock()
    monkeypatch.setattr("agents.negotiation_agent.service._sync_deal_status", sync)

    result = await service.sweep_stale_negotiations()

    assert result["scanned"] == 0
    assert result["failed"] == 0
    sync.assert_not_awaited()


async def test_round0_stale_but_no_pending_approval_is_noop(_repo, monkeypatch):
    # Idle past threshold but no SEND_NEGOTIATION_EMAIL approval exists to
    # re-notify (e.g. the draft failed to gate) — must not crash or fail the deal.
    await _repo.save_session(_session("N5", "STARTED", round_no=0, idle_seconds=3600))
    monkeypatch.setattr(
        "services.hitl_approval.service.list_approvals",
        AsyncMock(return_value=[SimpleNamespace(action="SOMETHING_ELSE", approval_id="X")]),
    )
    notify = AsyncMock()
    monkeypatch.setattr("services.hitl_approval.notification.notify", notify)

    result = await service.sweep_stale_negotiations()

    assert result["renotified"] == 0
    notify.assert_not_awaited()
