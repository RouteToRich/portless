"""Counterparty notifications (shared/orchestration/notifications.py).

Runs fully in-memory: negotiation session + email history + HITL repository
are the in-memory implementations, the SMTP gateway is in log-only mode (no
credentials), and the HITL side effects (SSE / webhook / events) are stubbed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.negotiation_agent.email_history import EmailStatus, InMemoryEmailHistoryRepository
from agents.negotiation_agent.repository import InMemoryNegotiationRepository
from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession
from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from shared.orchestration import autonomy_policy as ap
from shared.orchestration import notifications

DEAL = "DEAL-NOTIFY-1"
NEG = "4c02932b-d7fa-4042-aae9-cf4ac412e212"


def _session(**overrides) -> NegotiationSession:
    now = datetime.now(timezone.utc)
    defaults = dict(
        negotiation_id=NEG, deal_id=DEAL, trade_match_id="TM-1",
        buyer_id="BYR-1", seller_id="SUP-1", product_code="1006",
        buyer_country_code="DE", buyer_target_price=1255.0,
        supplier_quote_price=1004.0, policy=NegotiationPolicy(),
        status="AGREED", round_no=3, agreed_price=1200.0,
        buyer_email="buyer@example.com", seller_email="supplier@example.com",
        created_at=now, updated_at=now,
    )
    defaults.update(overrides)
    return NegotiationSession(**defaults)


def _pi(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        pi_id="PI-1", deal_id=DEAL, buyer_name="Acme Imports GmbH", quantity_mt=25.0,
        agreed_price=1200.0, currency="USD", total_value=30000.0, incoterm="CIF",
        port_of_destination="Hamburg", payment_terms="30/70", storage_uri="https://drive/x",
        expires_at=now + timedelta(days=7), issued_at=now, status="SENT",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _deal():
    return SimpleNamespace(
        deal_id=DEAL, hs_code="1006", market="DE",
        autonomy_profile=SimpleNamespace(value="L0"),
        parties=[SimpleNamespace(role="buyer", legal_name="Acme Imports GmbH"),
                 SimpleNamespace(role="supplier", legal_name="Chennai Rice Mills")],
    )


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def wiring(monkeypatch):
    neg_repo = InMemoryNegotiationRepository()
    email_repo = InMemoryEmailHistoryRepository()
    hitl_repo = InMemoryHitlApprovalRepository()
    monkeypatch.setattr("agents.negotiation_agent.repository._repo", neg_repo)
    monkeypatch.setattr("agents.negotiation_agent.email_history._email_repo", email_repo)
    import services.hitl_approval.repository as hitl_mod
    monkeypatch.setattr(hitl_mod, "_repository", hitl_repo)
    monkeypatch.setattr("services.hitl_approval.service.notify", AsyncMock())
    monkeypatch.setattr("services.hitl_approval.service._emit", AsyncMock())
    monkeypatch.delenv("DEAL_NOTIFICATIONS_ENABLED", raising=False)
    ap.reset_for_tests()
    monkeypatch.setattr(ap, "_mongo_collection", lambda: None)
    monkeypatch.delenv("AUTONOMY_SEND_DEAL_NOTIFICATION", raising=False)
    yield SimpleNamespace(neg=neg_repo, email=email_repo, hitl=hitl_repo)
    ap.reset_for_tests()


class TestNotifyParty:
    @pytest.mark.asyncio
    async def test_pi_issued_is_drafted_gated_and_auto_sent(self, wiring):
        await wiring.neg.save_session(_session())
        email_id = await notifications.notify_pi_issued(_deal(), _pi())
        assert email_id and email_id.startswith("EMAIL-N")

        record = await wiring.email.get(email_id)
        assert record.to_address == "buyer@example.com"
        assert record.party == "buyer"
        assert "PI-1" in record.subject
        assert "30,000.00" in record.body
        assert record.negotiation_id == NEG

        approvals = await wiring.hitl.list_by_deal(DEAL)
        assert len(approvals) == 1
        appr = approvals[0]
        assert appr.action == "SEND_DEAL_NOTIFICATION"
        assert appr.autonomy_level == "L2"
        assert appr.status == "AUTO_APPROVED"
        assert appr.callback_payload["email_id"] == email_id
        assert appr.callback_payload["kind"] == "pi_issued"

        await _settle()  # L2 callback runs as a background task → log-only SMTP
        record = await wiring.email.get(email_id)
        # No mailbox in tests: composed + logged, shown honestly as not delivered.
        assert record.status == EmailStatus.LOGGED_ONLY
        assert "logged" in (record.delivery_note or "").lower() and record.notice_kind == "pi_issued"
        assert record.sent_at is not None

    @pytest.mark.asyncio
    async def test_idempotent_per_deal_and_kind(self, wiring):
        await wiring.neg.save_session(_session())
        first = await notifications.notify_pi_issued(_deal(), _pi())
        second = await notifications.notify_pi_issued(_deal(), _pi())
        assert first == second
        assert len(await wiring.hitl.list_by_deal(DEAL)) == 1
        # a different suffix (reminder #2) is a new email
        third = await notifications.notify_pi_reminder(_deal(), _pi(), 2)
        assert third != first

    @pytest.mark.asyncio
    async def test_policy_l0_keeps_it_pending(self, wiring, monkeypatch):
        monkeypatch.setenv("AUTONOMY_SEND_DEAL_NOTIFICATION", "L0")
        await wiring.neg.save_session(_session())
        email_id = await notifications.notify_po_issued(_deal(), SimpleNamespace(
            po_id="PO-1", supplier_name="Chennai Rice Mills", quantity_mt=25.0,
            agreed_price=1004.0, currency="USD", total_value=25100.0, incoterm="FOB",
            port_of_loading="Chennai", delivery_deadline="2026-11-15",
            payment_terms="30/70", quality_spec=None, storage_uri=None,
        ))
        record = await wiring.email.get(email_id)
        assert record.to_address == "supplier@example.com"
        assert record.status == EmailStatus.PENDING_APPROVAL
        appr = (await wiring.hitl.list_by_deal(DEAL))[0]
        assert appr.status == "PENDING" and appr.autonomy_level == "L0"
        await _settle()
        assert (await wiring.email.get(email_id)).status == EmailStatus.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_no_session_or_address_returns_none(self, wiring):
        assert await notifications.notify_pi_issued(_deal(), _pi()) is None
        await wiring.neg.save_session(_session(buyer_email=None))
        assert await notifications.notify_pi_issued(_deal(), _pi()) is None
        assert await wiring.hitl.list_by_deal(DEAL) == []

    @pytest.mark.asyncio
    async def test_disabled_flag(self, wiring, monkeypatch):
        monkeypatch.setenv("DEAL_NOTIFICATIONS_ENABLED", "false")
        await wiring.neg.save_session(_session())
        assert await notifications.notify_pi_issued(_deal(), _pi()) is None

    @pytest.mark.asyncio
    async def test_unknown_kind_is_rejected(self, wiring):
        await wiring.neg.save_session(_session())
        assert await notifications.notify_party(DEAL, "buyer", "nope", {}) is None

    @pytest.mark.asyncio
    async def test_escrow_opened_includes_checkout_and_tranches(self, wiring):
        await wiring.neg.save_session(_session())
        escrow = SimpleNamespace(
            escrow_id="ESC-1", provider="tazapay", amount=30000.0, currency="USD",
            checkout_url="https://pay.example/abc",
            tranches=[SimpleNamespace(tranche_id="T1", trigger="PO_ISSUED", pct=0.3),
                      SimpleNamespace(tranche_id="T4", trigger="ACCEPTED", pct=0.1)],
        )
        email_id = await notifications.notify_escrow_opened(_deal(), escrow, "PI-1")
        body = (await wiring.email.get(email_id)).body
        assert "https://pay.example/abc" in body
        assert "30% when the purchase order is placed with the manufacturer" in body and "PO_ISSUED" not in body
        assert "Dear Acme Imports GmbH" in body

    @pytest.mark.asyncio
    async def test_deal_completed_goes_to_both_parties(self, wiring):
        await wiring.neg.save_session(_session())
        sent = await notifications.notify_deal_completed(_deal())
        assert len(sent) == 2
        records = [await wiring.email.get(e) for e in sent]
        assert {r.to_address for r in records} == {"buyer@example.com", "supplier@example.com"}

    @pytest.mark.asyncio
    async def test_hitl_failure_never_raises(self, wiring, monkeypatch):
        await wiring.neg.save_session(_session())
        monkeypatch.setattr(
            "services.hitl_approval.service.create_approval",
            AsyncMock(side_effect=RuntimeError("hitl down")),
        )
        assert await notifications.notify_pi_issued(_deal(), _pi()) is None
