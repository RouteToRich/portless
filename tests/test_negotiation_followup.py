"""No-reply follow-up sweep (agents/negotiation_agent/service.py).

After PortLess sends an email to a party, if that party has not replied within
NEGOTIATION_FOLLOWUP_AFTER_SECONDS (default 2 days) the stale-watcher sweep
drafts a courteous follow-up — routed through the normal L0 SEND_NEGOTIATION_EMAIL
approval, never auto-sent. These tests cover every eligibility rule.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import agents.negotiation_agent.service as svc
from agents.negotiation_agent.email_history import (
    EmailDirection,
    EmailHistoryRecord,
    EmailStatus,
)
from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession

NOW = datetime.now(timezone.utc)
NEG_ID = "neg-followup-test"


def _session(**overrides) -> NegotiationSession:
    defaults = dict(
        negotiation_id=NEG_ID,
        deal_id="DEAL-F1",
        trade_match_id="M1",
        buyer_id="BUYER-1",
        seller_id="SUP-1",
        product_code="290543",
        buyer_country_code="US",
        buyer_target_price=1255.0,
        supplier_quote_price=1004.0,
        policy=NegotiationPolicy(),
        status="ROUND",
        round_no=2,
        created_at=NOW - timedelta(days=5),
        updated_at=NOW - timedelta(days=3),
    )
    defaults.update(overrides)
    return NegotiationSession(**defaults)


_seq = 0


def _email(
    party: str,
    direction: EmailDirection,
    status: EmailStatus,
    age: timedelta,
    to_address: str = "supplier@example.com",
    subject: str = "Sourcing Inquiry — HS 290543",
) -> EmailHistoryRecord:
    global _seq
    _seq += 1
    ts = NOW - age
    return EmailHistoryRecord(
        email_id=f"EMAIL-T{_seq:04d}",
        negotiation_id=NEG_ID,
        deal_id="DEAL-F1",
        round_no=2,
        direction=direction,
        party=party,
        to_address=to_address,
        from_address="portless-trade@portless.io",
        subject=subject,
        body="body",
        status=status,
        sent_at=ts,
        created_at=ts,
        updated_at=ts,
    )


@pytest.fixture
def capture(monkeypatch):
    """Stub the LLM draft + HITL gate; capture gate calls."""
    calls: list[dict] = []

    async def fake_draft(session, party, history_text, last_subject, days):
        return f"follow-up body for {party}", False

    async def fake_gate(session, party, to_address, subject, body, degraded=False, **_kw):
        calls.append({
            "party": party, "to": to_address, "subject": subject,
            "body": body, "degraded": degraded,
        })
        return "EMAIL-FAKE"

    monkeypatch.setattr(svc, "_draft_followup_body", fake_draft)
    monkeypatch.setattr(svc, "_create_and_gate_email", fake_gate)
    return calls


async def test_overdue_unanswered_outbound_triggers_followup(capture):
    records = [_email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=3))]
    created = await svc._maybe_followup_party(_session(), records, "supplier", NOW)
    assert created is True
    assert len(capture) == 1
    assert capture[0]["party"] == "supplier"
    assert capture[0]["subject"].startswith("Re: ")


async def test_recent_outbound_not_followed_up(capture):
    records = [_email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(hours=5))]
    assert await svc._maybe_followup_party(_session(), records, "supplier", NOW) is False
    assert capture == []


async def test_reply_received_after_outbound_suppresses_followup(capture):
    records = [
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=3)),
        _email("supplier", EmailDirection.INBOUND, EmailStatus.RECEIVED, timedelta(days=2)),
    ]
    assert await svc._maybe_followup_party(_session(), records, "supplier", NOW) is False
    assert capture == []


async def test_pending_draft_blocks_new_followup(capture):
    records = [
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=3)),
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.PENDING_APPROVAL, timedelta(days=1)),
    ]
    assert await svc._maybe_followup_party(_session(), records, "supplier", NOW) is False
    assert capture == []


async def test_chain_cap_stops_followups(capture, monkeypatch):
    monkeypatch.setattr(svc.settings, "followup_max_per_party", 2)
    records = [  # original + 2 follow-ups already sent, all unanswered
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=9)),
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=6)),
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=3)),
    ]
    assert await svc._maybe_followup_party(_session(), records, "supplier", NOW) is False
    assert capture == []


async def test_operator_reject_suppresses_renagging(capture):
    records = [
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=5)),
        # operator rejected a previously drafted follow-up for this same silence
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.REJECTED, timedelta(days=1)),
    ]
    assert await svc._maybe_followup_party(_session(), records, "supplier", NOW) is False
    assert capture == []


async def test_placeholder_address_never_followed_up(capture):
    records = [_email(
        "supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=3),
        to_address="SUP-1@portless.placeholder",
    )]
    assert await svc._maybe_followup_party(_session(), records, "supplier", NOW) is False
    assert capture == []


async def test_parties_evaluated_independently(capture):
    records = [
        _email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=3)),
        _email("buyer", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=3),
               to_address="buyer@example.com", subject="Trade Proposal — HS 290543"),
        _email("buyer", EmailDirection.INBOUND, EmailStatus.RECEIVED, timedelta(days=2)),
    ]
    session = _session()
    assert await svc._maybe_followup_party(session, records, "supplier", NOW) is True
    assert await svc._maybe_followup_party(session, records, "buyer", NOW) is False
    assert [c["party"] for c in capture] == ["supplier"]


async def test_sweep_wires_followups_and_reports_count(capture, monkeypatch):
    session = _session()

    class FakeRepo:
        async def list_sessions_by_status(self, statuses):
            return [session]

    class FakeEmailRepo:
        async def list_by_negotiation(self, negotiation_id):
            return [_email("supplier", EmailDirection.OUTBOUND, EmailStatus.SENT, timedelta(days=3))]

    monkeypatch.setattr(svc, "get_repository", lambda: FakeRepo())
    monkeypatch.setattr(svc, "get_email_repository", lambda: FakeEmailRepo())
    monkeypatch.setattr(svc.settings, "followup_enabled", True)

    counts = await svc.sweep_stale_negotiations()
    assert counts["followups"] == 1
    assert counts["failed"] == 0
    assert len(capture) == 1


async def test_sweep_respects_disable_flag(capture, monkeypatch):
    session = _session()

    class FakeRepo:
        async def list_sessions_by_status(self, statuses):
            return [session]

    monkeypatch.setattr(svc, "get_repository", lambda: FakeRepo())
    monkeypatch.setattr(svc.settings, "followup_enabled", False)

    counts = await svc.sweep_stale_negotiations()
    assert counts["followups"] == 0
    assert capture == []
