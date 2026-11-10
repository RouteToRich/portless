"""Status e-mails to the buyer and the supplier, and their delivery log.

- a send with no mailbox is LOGGED_ONLY (never shown as sent), a real SMTP
  send is SENT, a transport error is FAILED with its reason;
- negotiation logic still treats LOGGED_ONLY as issued (dev runs unchanged);
- a notice that cannot be addressed is recorded SKIPPED with the reason and
  retried once the address exists;
- the new notices (escrow funded, QC result, tranche released, payout sent,
  goods delivered, refund) fire from their continuations;
- ``GET /deals/{id}/notifications`` returns the correspondence log.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.negotiation_agent.email_history import (
    ISSUED_STATUSES, EmailStatus, InMemoryEmailHistoryRepository,
)
from agents.negotiation_agent.repository import InMemoryNegotiationRepository
from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import autonomy_policy as ap
from shared.orchestration import notifications
from tests.test_notifications import DEAL, NEG, _deal, _pi, _session, _settle


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


def _ev(topic, payload, deal_id=DEAL):
    return DomainEvent(topic=topic, deal_id=deal_id, payload=payload, source_service="test")


# ---------------------------------------------------------------------------
# Delivery outcome
# ---------------------------------------------------------------------------

class TestDeliveryOutcome:
    @pytest.mark.asyncio
    async def test_no_mailbox_is_logged_only(self, wiring):
        await wiring.neg.save_session(_session())
        eid = await notifications.notify_pi_issued(_deal(), _pi())
        await _settle()
        rec = await wiring.email.get(eid)
        assert rec.status == EmailStatus.LOGGED_ONLY and rec.status in ISSUED_STATUSES
        assert "logged" in rec.delivery_note.lower()

    @pytest.mark.asyncio
    async def test_real_smtp_send_is_sent(self, wiring, monkeypatch):
        from services.email_gateway.schemas import SendEmailResult, SendOutcome
        monkeypatch.setattr("services.email_gateway.sender.send_email",
                            AsyncMock(return_value=SendEmailResult(outcome=SendOutcome.SENT, message_id="<m>")))
        await wiring.neg.save_session(_session())
        eid = await notifications.notify_pi_issued(_deal(), _pi())
        await _settle()
        rec = await wiring.email.get(eid)
        assert rec.status == EmailStatus.SENT and rec.delivery_note is None

    @pytest.mark.asyncio
    async def test_transport_error_is_failed_with_reason(self, wiring, monkeypatch):
        from services.email_gateway.sender import EmailSendError
        monkeypatch.setattr("services.email_gateway.sender.send_email",
                            AsyncMock(side_effect=EmailSendError("535 bad credentials")))
        await wiring.neg.save_session(_session())
        eid = await notifications.notify_pi_issued(_deal(), _pi())
        await _settle()
        rec = await wiring.email.get(eid)
        assert rec.status == EmailStatus.FAILED and "535" in rec.delivery_note

    @pytest.mark.asyncio
    async def test_send_returns_status_and_callback_reports_it(self, wiring):
        from agents.negotiation_agent.service import send_approved_email
        from services.hitl_approval import callbacks as cb
        await wiring.neg.save_session(_session())
        with patch("services.hitl_approval.service.create_approval",
                   new=AsyncMock(return_value=SimpleNamespace(approval_id="A1", autonomy_level="L0"))):
            eid = await notifications.notify_pi_issued(_deal(), _pi())
        out = await cb.execute_callback("SEND_DEAL_NOTIFICATION", DEAL, {"email_id": eid, "kind": "pi_issued"}, "A1")
        assert out["status"] == "logged_only"
        assert await send_approved_email(eid) == EmailStatus.LOGGED_ONLY  # idempotent: not sent again

    def test_logged_only_counts_as_issued_for_negotiation_logic(self):
        from agents.negotiation_agent import email_safety
        from agents.negotiation_agent.email_history import ISSUED_VALUES
        assert "logged_only" in ISSUED_VALUES
        src = open(email_safety.__file__, encoding="utf8").read()
        assert '"logged_only"' in src


# ---------------------------------------------------------------------------
# Skipped notices
# ---------------------------------------------------------------------------

class TestSkipped:
    @pytest.mark.asyncio
    async def test_no_thread_or_address_is_recorded_skipped_then_retried(self, wiring):
        assert await notifications.notify_pi_issued(_deal(), _pi()) is None
        [rec] = await wiring.email.list_by_deal(DEAL)
        assert rec.status == EmailStatus.SKIPPED and rec.notice_kind == "pi_issued"
        assert "No negotiation thread" in rec.delivery_note and rec.to_address == ""
        assert await wiring.hitl.list_by_deal(DEAL) == []

        await wiring.neg.save_session(_session(buyer_email=None))
        assert await notifications.notify_pi_issued(_deal(), _pi()) is None
        [rec] = await wiring.email.list_by_deal(DEAL)
        assert rec.status == EmailStatus.SKIPPED and "No e-mail address" in rec.delivery_note
        assert rec.negotiation_id == NEG

        await wiring.neg.save_session(_session())  # the address arrives → the same notice goes out
        eid = await notifications.notify_pi_issued(_deal(), _pi())
        assert eid == rec.email_id
        await _settle()
        [rec] = await wiring.email.list_by_deal(DEAL)
        assert rec.status == EmailStatus.LOGGED_ONLY and rec.to_address == "buyer@example.com"

    @pytest.mark.asyncio
    async def test_skip_never_overwrites_a_drafted_notice(self, wiring):
        await wiring.neg.save_session(_session())
        eid = await notifications.notify_pi_issued(_deal(), _pi())
        await _settle()
        await notifications._record_skipped(eid, DEAL, "buyer", "pi_issued", {"pi": _pi()},
                                            negotiation_id=NEG, round_no=1, reason="x")
        assert (await wiring.email.get(eid)).status == EmailStatus.LOGGED_ONLY


# ---------------------------------------------------------------------------
# New templates
# ---------------------------------------------------------------------------

class TestNewNotices:
    @pytest.mark.asyncio
    async def test_each_new_notice_reaches_the_right_party(self, wiring):
        await wiring.neg.save_session(_session())
        deal = _deal()
        cases = [
            (notifications.notify_escrow_funded(deal, {"escrow_id": "ESC-1", "amount": 30000, "funded_amount": 30000,
                                                       "currency": "USD"}), "buyer", "30,000.00"),
            (notifications.notify_qc_result(deal, {"provider": "sgs", "ext_ref": "SGS-1",
                                                   "seal_numbers": ["S1"], "container_numbers": ["C1"]}, True),
             "buyer", "SGS-1"),
            (notifications.notify_qc_result(deal, {"ext_ref": "SGS-2", "notes": "moisture 16%"}, False),
             "buyer", "moisture 16%"),
            (notifications.notify_tranche_released(deal, {"tranche_id": "T1", "trigger": "PO_ISSUED", "amount": 9000,
                                                          "released_total": 9000, "currency": "USD"}, 30000.0),
             "buyer", "21,000.00"),
            (notifications.notify_payout_sent(deal, {"payout_id": "PO-1", "amount": 5000, "currency": "USD",
                                                     "completed_total": 5000, "supplier_owed": 25000,
                                                     "balance_due": 20000}), "supplier", "20,000.00"),
            (notifications.notify_goods_delivered(deal, {"location": "Hamburg"}), "supplier", "Hamburg"),
            (notifications.notify_escrow_refunded(deal, "ESC-1", 21000.0, "USD", "QC failed twice"),
             "buyer", "21,000.00"),
        ]
        for coro, party, text in cases:
            eid = await coro
            rec = await wiring.email.get(eid)
            assert rec.party == party, rec.notice_kind
            assert text in rec.body, (rec.notice_kind, rec.body)
            assert rec.notice_kind in notifications.NOTICE_LABELS
        kinds = {r.notice_kind for r in await wiring.email.list_by_deal(DEAL)}
        assert kinds == {"escrow_funded", "qc_passed", "qc_failed", "tranche_released", "payout_sent",
                         "goods_delivered", "escrow_refunded"}

    def test_every_template_has_a_label(self):
        assert set(notifications._TEMPLATES) <= set(notifications.NOTICE_LABELS)


# ---------------------------------------------------------------------------
# Continuations fire them
# ---------------------------------------------------------------------------

class TestContinuationHooks:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")

    @pytest.mark.asyncio
    async def test_escrow_funded_notifies_buyer(self, monkeypatch):
        from shared.orchestration import continuations as c
        n = AsyncMock()
        monkeypatch.setattr(notifications, "notify_escrow_funded", n)
        monkeypatch.setattr(c, "_get_deal_in_status", AsyncMock(return_value=_deal()))
        monkeypatch.setattr(c, "_negotiation_numbers", AsyncMock(return_value=(1000.0, 25.0)))
        monkeypatch.setattr(c, "_create_gate", AsyncMock())
        await c.on_escrow_funded(_ev(EventTopic.ESCROW_FUNDED, {"escrow_id": "E", "amount": 1}))
        n.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_qc_failed_notifies_buyer(self, monkeypatch):
        from shared.orchestration import continuations as c
        n = AsyncMock()
        monkeypatch.setattr(notifications, "notify_qc_result", n)
        monkeypatch.setattr(c, "_get_deal_in_status", AsyncMock(return_value=_deal()))
        monkeypatch.setattr(c, "_publish_exception", AsyncMock())
        with patch("services.deals.service.update_status", new=AsyncMock()):
            await c.on_qc_failed(_ev(EventTopic.QC_FAILED, {"ext_ref": "R", "notes": "bad"}))
        assert n.await_args.kwargs["passed"] is False

    @pytest.mark.asyncio
    async def test_payout_completed_notifies_supplier_even_when_partial(self, monkeypatch):
        from shared.orchestration import continuations as c
        n = AsyncMock()
        monkeypatch.setattr(notifications, "notify_payout_sent", n)
        monkeypatch.setattr(c, "_supplier_payout_gate", AsyncMock())
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal())):
            await c.on_payout_completed(_ev(EventTopic.PAYOUT_COMPLETED,
                                            {"payout_id": "P", "amount": 10.0, "fully_paid": False}))
        n.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tranche_release_notifies_buyer(self, monkeypatch):
        from shared.orchestration import continuations as c
        n = AsyncMock()
        monkeypatch.setattr(notifications, "notify_tranche_released", n)
        deal = _deal()
        deal.status = SimpleNamespace(value="PO_ISSUED")
        repo = SimpleNamespace(get_hold_by_deal=AsyncMock(return_value=SimpleNamespace(amount=30000.0)))
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.escrow_guard.repository.get_repository", return_value=repo),
        ):
            await c.on_escrow_tranche_released(_ev(EventTopic.ESCROW_TRANCHE_RELEASED,
                                                   {"tranche_id": "T1", "trigger": "PO_ISSUED", "amount": 9000.0}))
        assert n.await_args.args[2] == 30000.0

    @pytest.mark.asyncio
    async def test_delivered_milestone_notifies_supplier(self, monkeypatch):
        from shared.orchestration import continuations as c
        n = AsyncMock()
        monkeypatch.setattr(notifications, "notify_goods_delivered", n)
        repo = SimpleNamespace(get_hold_by_deal=AsyncMock(return_value=None))
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal())),
            patch("services.escrow_guard.repository.get_repository", return_value=repo),
        ):
            await c.on_freight_milestone(_ev(EventTopic.FREIGHT_MILESTONE, {"milestone": "ARRIVED"}))
            n.assert_not_awaited()
            await c.on_freight_milestone(_ev(EventTopic.FREIGHT_MILESTONE, {"milestone": "DELIVERED"}))
        n.assert_awaited_once()


class TestRefundNotice:
    @pytest.mark.parametrize("status,expected", [("DISPUTE", True), ("PI_SENT", False), ("CANCELLED", False)])
    @pytest.mark.asyncio
    async def test_refund_notice_only_outside_the_cancellation_path(self, monkeypatch, status, expected):
        from services.escrow_guard import service as eg
        from services.escrow_guard.schemas import RefundRequest
        hold = SimpleNamespace(
            escrow_id="ESC-R", deal_id=DEAL, status="FUNDED", ext_ref="x", amount=30000.0, currency="USD",
            provider="tazapay", provider_payload={}, released_total=0.0,
            model_copy=lambda update: SimpleNamespace(**update),
        )
        repo = SimpleNamespace(get_hold=AsyncMock(return_value=hold), save_hold=AsyncMock())
        connector = SimpleNamespace(refund=AsyncMock(return_value={"ok": True}))
        n = AsyncMock()
        deal = _deal()
        deal.status = SimpleNamespace(value=status)
        monkeypatch.setattr(eg, "get_repository", lambda: repo)
        monkeypatch.setattr(eg, "_assert_platform_running", AsyncMock())
        monkeypatch.setattr(eg, "_undisbursed_or_refuse", lambda h: 21000.0)
        monkeypatch.setattr(eg, "_payment_connector", lambda p: connector)
        monkeypatch.setattr(eg, "_fetch_deal", AsyncMock(return_value=deal))
        monkeypatch.setattr(eg, "_transition_deal", AsyncMock())
        monkeypatch.setattr(eg, "_emit", AsyncMock())
        monkeypatch.setattr(eg.EscrowResponse, "from_record", staticmethod(lambda r: r))
        monkeypatch.setattr(notifications, "notify_escrow_refunded", n)
        await eg.refund("ESC-R", RefundRequest(reason="QC failed"))
        assert n.await_count == (1 if expected else 0)


# ---------------------------------------------------------------------------
# Correspondence log
# ---------------------------------------------------------------------------

class TestCorrespondenceLog:
    @pytest.mark.asyncio
    async def test_log_labels_delivery_and_summary(self, wiring):
        from agents.negotiation_agent.email_history import record_deal_email
        await notifications.notify_pi_issued(_deal(), _pi())          # skipped: no thread yet
        await wiring.neg.save_session(_session())
        await notifications.notify_po_reminder(_deal(), 1)            # logged only (supplier)
        await record_deal_email(deal_id=DEAL, direction="outbound", party="supplier", to_address="s@x.com",
                                from_address="us@x.com", subject="RFQ", body="b", kind="rfq", delivered=True)
        await record_deal_email(deal_id=DEAL, direction="inbound", party="supplier", to_address="us@x.com",
                                from_address="s@x.com", subject="Re: RFQ", body="b", kind="rfq_reply")
        await _settle()
        log = await notifications.deal_correspondence_log(DEAL)
        assert log["mailbox_configured"] is False and log["notifications_enabled"] is True
        by_label = {e["label"]: e for e in log["entries"]}
        assert by_label["Proforma Invoice issued"]["delivery"] == "skipped"
        assert by_label["Proforma Invoice issued"]["stage"] == "PI_SENT"
        assert by_label["PO confirmation reminder"]["delivery"] == "logged_only"
        assert by_label["Request for quotation"]["delivery"] == "delivered"
        assert by_label["Reply to: Request for quotation"]["delivery"] == "received"
        assert log["summary"]["buyer"] == {"skipped": 1}
        assert log["summary"]["supplier"] == {"logged_only": 1, "delivered": 1, "received": 1}
        ats = [e["at"] for e in log["entries"]]
        assert ats == sorted(ats, reverse=True)

    @pytest.mark.asyncio
    async def test_undelivered_pre_negotiation_mail_is_logged_only(self, wiring):
        from agents.negotiation_agent.email_history import record_deal_email
        await record_deal_email(deal_id=DEAL, direction="outbound", party="buyer", to_address="b@x.com",
                                from_address="us@x.com", subject="Intro", body="b", kind="outreach_intro",
                                delivered=False)
        [rec] = await wiring.email.list_by_deal(DEAL)
        assert rec.status == EmailStatus.LOGGED_ONLY and "logged" in rec.delivery_note

    def test_route_is_registered(self):
        from services.deals.router import router
        assert any(getattr(r, "path", "").endswith("/{deal_id}/notifications") for r in router.routes)
