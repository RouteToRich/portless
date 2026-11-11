"""Money + handshake legs added to shared/orchestration/continuations.py and
the sourcing / email-processor RFQ-by-e-mail path.

  NEGOTIATION_AGREED → screening verdict decides PI / CONFIRM_COUNTERPARTY_RISK / block
  CUSTOMS_CLEARED    → supplier payout capped at funds released from escrow
  PAYOUT_COMPLETED   → partial payout opens a balance gate
  ESCROW_TRANCHE_RELEASED → inspection-window notice, next tranche re-gate, payout top-up
  ESCROW_SETTLED     → balance gate + settlement notices
  callbacks          → CONFIRM_COUNTERPARTY_RISK issues the PI, CONFIRM_BUYER_ACCEPTANCE accepts escrow
  sourcing           → RFQ e-mailed with [RFQ:<id>] token; supplier reply → receive_quote
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import autonomy_policy as ap
from shared.orchestration import continuations as c

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_ON_DEAL_CREATED", "false")
    ap.reset_for_tests()
    monkeypatch.setattr(ap, "_mongo_collection", lambda: None)
    for cls in ap.ACTION_CLASSES:
        monkeypatch.delenv(f"AUTONOMY_{cls.action}", raising=False)
    monkeypatch.delenv("AUTONOMY_MONEY_CEILING", raising=False)
    yield
    ap.reset_for_tests()


@pytest.fixture
def published(monkeypatch):
    events: list[DomainEvent] = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


def _party(role, **kw):
    base = dict(role=role, legal_name=f"{role} co", country="DE" if role == "buyer" else "IN",
                sanctions_status=None, sanctions_reason=None, sanctions_checked_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _deal(status="CLEARED", **overrides):
    base = dict(
        deal_id="DEAL-1", status=SimpleNamespace(value=status), market="DE", hs_code="1006",
        autonomy_profile=SimpleNamespace(value="L0"), winning_supplier_id="SUP-1", opportunity_id=None,
        buyer_target_price_usd=1255.0, match_results=[], parties=[_party("buyer"), _party("supplier")],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _ledger(**kw):
    base = dict(payouts_total=0.0, payouts_pending=0.0, available_for_payout=0.0, released_total=0.0,
                supplier_owed=None, supplier_balance_due=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _neg_repo(ask=1004.0, qty=25.0):
    repo = AsyncMock()
    repo.get_session_by_deal = AsyncMock(return_value=SimpleNamespace(current_ask=ask, quantity_mt=qty))
    return repo


# ---------------------------------------------------------------------------
# NEGOTIATION_AGREED → screening decides
# ---------------------------------------------------------------------------

class TestAgreedScreening:
    async def _run(self, deal, issue, gate, enrich_result=None):
        enrich = AsyncMock(return_value=enrich_result)
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.dealmaker.service.issue_pi", new=issue),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("shared.orchestration.enrichment.enrich_deal_parties", new=enrich),
            patch("agents.negotiation_agent.repository.get_repository", return_value=_neg_repo()),
        ):
            await c.on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1", payload={"agreed_price": 1200.0},
            ))
        return enrich

    @pytest.mark.asyncio
    async def test_status_echo_without_price_is_skipped_quietly(self, caplog):
        get = AsyncMock()
        with patch("services.deals.service.get_deal", new=get), caplog.at_level("ERROR"):
            await c.on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1", payload={"status": "NEGOTIATION_AGREED"},
            ))
        get.assert_not_awaited()
        assert not caplog.records

    @pytest.mark.asyncio
    async def test_real_agreement_without_price_is_still_an_error(self, caplog):
        with caplog.at_level("ERROR"):
            await c.on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1", payload={"negotiation_id": "NEG-1"},
            ))
        assert "missing/invalid agreed_price" in caplog.text

    @pytest.mark.asyncio
    async def test_clear_and_fresh_issues_pi_without_rescreen(self):
        issue, gate = AsyncMock(return_value=SimpleNamespace(pi_id="PI-1")), AsyncMock()
        deal = _deal("NEGOTIATION_AGREED",
                     parties=[_party("buyer", sanctions_status="clear", sanctions_checked_at=NOW)])
        enrich = await self._run(deal, issue, gate)
        enrich.assert_not_awaited()
        issue.assert_awaited_once()
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_review_creates_risk_gate_and_holds_pi(self):
        issue, gate = AsyncMock(), AsyncMock()
        deal = _deal("NEGOTIATION_AGREED",
                     parties=[_party("buyer", sanctions_status="review", sanctions_reason="weak UK list match",
                                     sanctions_checked_at=NOW)])
        await self._run(deal, issue, gate)
        issue.assert_not_awaited()
        req = gate.await_args.args[0]
        assert req.action == "CONFIRM_COUNTERPARTY_RISK" and req.autonomy_level == "L0"
        assert req.callback_payload["agreed_price"] == 1200.0 and req.callback_payload["quantity_mt"] == 25.0
        assert req.financial_impact_usd == 30000.0 and "buyer_sanctions_review" in req.risk_flags

    @pytest.mark.asyncio
    async def test_block_withholds_pi_and_raises_exception(self, published):
        issue, gate = AsyncMock(), AsyncMock()
        stale = _deal("NEGOTIATION_AGREED", parties=[_party("buyer", sanctions_status="unchecked")])
        blocked = _deal("NEGOTIATION_AGREED", parties=[_party("buyer", sanctions_status="block",
                                                                sanctions_checked_at=NOW)])
        enrich = await self._run(stale, issue, gate, enrich_result={"deal": blocked, "blocked": True})
        enrich.assert_awaited_once()
        assert enrich.await_args.kwargs["roles"] == ("buyer",) and enrich.await_args.kwargs["contacts"] is False
        issue.assert_not_awaited()
        gate.assert_not_awaited()
        exc = [e for e in published if e.topic == EventTopic.DEAL_EXCEPTION]
        assert exc and exc[0].payload["stage"] == "NEGOTIATION_AGREED→PI_BLOCKED"

    @pytest.mark.asyncio
    async def test_unchecked_after_rescreen_still_issues(self):
        issue, gate = AsyncMock(return_value=SimpleNamespace(pi_id="PI-1")), AsyncMock()
        deal = _deal("NEGOTIATION_AGREED", parties=[_party("buyer")])
        await self._run(deal, issue, gate, enrich_result={"deal": deal, "blocked": False})
        issue.assert_awaited_once()
        gate.assert_not_awaited()


# ---------------------------------------------------------------------------
# Supplier payout: capped, partial, balance
# ---------------------------------------------------------------------------

class TestSupplierPriceForThePo:
    @pytest.mark.parametrize(("session", "expected"), [
        (dict(current_ask=960.0, supplier_quote_price=1000.0, supplier_last_quote=None), 1000.0),
        (dict(current_ask=960.0, supplier_quote_price=1000.0, supplier_last_quote=1010.0), 1010.0),
        (dict(current_ask=960.0, supplier_quote_price=None, supplier_last_quote=None), 960.0),
    ])
    @pytest.mark.asyncio
    async def test_our_own_unanswered_counter_is_not_the_supplier_price(self, session, expected):
        repo = AsyncMock()
        repo.get_session_by_deal = AsyncMock(return_value=SimpleNamespace(quantity_mt=20.0, **session))
        with patch("agents.negotiation_agent.repository.get_repository", return_value=repo):
            price, quantity = await c._negotiation_numbers("DEAL-1", _deal("ESCROW_FUNDED"))
        assert price == expected and quantity == 20.0


class TestSupplierPayoutGate:
    @pytest.mark.asyncio
    async def test_cleared_payout_capped_at_available(self):
        gate = AsyncMock()
        po_repo = AsyncMock()
        po_repo.get_po_by_deal = AsyncMock(return_value=SimpleNamespace(total_value=25100.0))
        with (
            patch("services.dealmaker.repository.get_repository", return_value=po_repo),
            patch("services.escrow_guard.service.get_deal_ledger",
                  new=AsyncMock(return_value=_ledger(available_for_payout=17000.0, released_total=17500.0))),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("agents.negotiation_agent.repository.get_repository", return_value=_neg_repo()),
        ):
            created = await c._supplier_payout_gate(_deal("CLEARED"), stage="t", first=True)
        assert created is True
        req = gate.await_args.args[0]
        assert req.callback_payload["amount"] == 17000.0 and req.callback_payload["total_owed"] == 25100.0
        assert req.callback_payload["kind"] == "supplier_payout" and req.idempotency_key == "auto-payout-gate-DEAL-1"
        assert "partial_payout_capped_by_escrow" in req.risk_flags
        assert req.autonomy_level == "L0"  # follows the deal profile (L0); the money ceiling never raises it

    @pytest.mark.asyncio
    async def test_nothing_released_yet_means_no_gate(self):
        gate = AsyncMock()
        with (
            patch("services.dealmaker.repository.get_repository", return_value=AsyncMock(get_po_by_deal=AsyncMock(return_value=None))),
            patch("services.escrow_guard.service.get_deal_ledger", new=AsyncMock(return_value=_ledger())),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("agents.negotiation_agent.repository.get_repository", return_value=_neg_repo()),
        ):
            created = await c._supplier_payout_gate(_deal("CLEARED"), stage="t", first=True)
        assert created is False
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_balance_gate_after_partial_payout(self):
        gate = AsyncMock()
        with (
            patch("services.dealmaker.repository.get_repository", return_value=AsyncMock(get_po_by_deal=AsyncMock(return_value=SimpleNamespace(total_value=25100.0)))),
            patch("services.escrow_guard.service.get_deal_ledger",
                  new=AsyncMock(return_value=_ledger(payouts_total=17000.0, available_for_payout=9000.0,
                                                     released_total=27500.0, supplier_owed=25100.0))),
            patch("services.hitl_approval.service.list_approvals", new=AsyncMock(return_value=[])),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("agents.negotiation_agent.repository.get_repository", return_value=_neg_repo()),
        ):
            await c.on_payout_completed(DomainEvent(
                topic=EventTopic.PAYOUT_COMPLETED, deal_id="DEAL-1",
                payload={"fully_paid": False, "balance_due": 8100.0},
            )) if False else None
            with patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("CLEARED"))):
                await c.on_payout_completed(DomainEvent(
                    topic=EventTopic.PAYOUT_COMPLETED, deal_id="DEAL-1",
                    payload={"fully_paid": False, "balance_due": 8100.0},
                ))
        req = gate.await_args.args[0]
        assert req.action == "RELEASE_SUPPLIER_PAYOUT" and req.callback_payload["kind"] == "supplier_balance"
        assert req.callback_payload["amount"] == 8100.0 and req.idempotency_key.startswith("auto-payout-balance-DEAL-1-")

    @pytest.mark.asyncio
    async def test_pending_payout_gate_blocks_duplicate(self):
        gate = AsyncMock()
        with (
            patch("services.dealmaker.repository.get_repository", return_value=AsyncMock(get_po_by_deal=AsyncMock(return_value=SimpleNamespace(total_value=25100.0)))),
            patch("services.escrow_guard.service.get_deal_ledger",
                  new=AsyncMock(return_value=_ledger(available_for_payout=9000.0, released_total=27500.0))),
            patch("services.hitl_approval.service.list_approvals",
                  new=AsyncMock(return_value=[SimpleNamespace(action="RELEASE_SUPPLIER_PAYOUT")])),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("agents.negotiation_agent.repository.get_repository", return_value=_neg_repo()),
        ):
            created = await c._supplier_payout_gate(_deal("CLEARED"), stage="t", first=False)
        assert created is False
        gate.assert_not_awaited()


# ---------------------------------------------------------------------------
# ESCROW_TRANCHE_RELEASED / ESCROW_SETTLED
# ---------------------------------------------------------------------------

class TestTrancheReleasedAndSettled:
    @pytest.mark.asyncio
    async def test_window_notice_next_gate_and_topup(self):
        gate = AsyncMock()
        notify = AsyncMock()
        hold = SimpleNamespace(
            escrow_id="ESC-1", status="FUNDED", amount=30000.0, currency="USD",
            tranches=[
                SimpleNamespace(tranche_id="T1", trigger="PO_ISSUED", pct=0.3, amount=9000.0, status="RELEASED"),
                SimpleNamespace(tranche_id="T2", trigger="QC_PASSED", pct=0.4, amount=12000.0, status="PENDING"),
                SimpleNamespace(tranche_id="T4", trigger="ACCEPTED", pct=0.1, amount=3000.0, status="PENDING"),
            ],
        )
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("IN_TRANSIT"))),
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("shared.orchestration.notifications.notify_inspection_window_open", new=notify),
            patch.object(c, "_supplier_payout_gate", new=AsyncMock(return_value=False)) as topup,
        ):
            await c.on_escrow_tranche_released(DomainEvent(
                topic=EventTopic.ESCROW_TRANCHE_RELEASED, deal_id="DEAL-1",
                payload={"tranche_id": "T1", "next_pending_tranche": "T2", "next_pending_trigger": "QC_PASSED",
                         "inspection_window_opened": True, "inspection_deadline": "2026-11-01T00:00:00+00:00"},
            ))
        notify.assert_awaited_once()
        assert notify.await_args.args[2] == 3000.0  # the pending ACCEPTED tranche
        req = gate.await_args.args[0]
        assert req.action == "RELEASE_ESCROW_TRANCHE" and req.callback_payload["tranche_id"] == "T2"
        assert req.idempotency_key == "auto-tranche-gate-DEAL-1-T2-after-T1"
        topup.assert_not_awaited()  # deal is IN_TRANSIT, not CLEARED/PAID

    @pytest.mark.asyncio
    async def test_next_gate_waits_for_trigger_status(self):
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("PO_ISSUED"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_escrow_tranche_released(DomainEvent(
                topic=EventTopic.ESCROW_TRANCHE_RELEASED, deal_id="DEAL-1",
                payload={"tranche_id": "T1", "next_pending_tranche": "T2", "next_pending_trigger": "QC_PASSED"},
            ))
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_settled_creates_balance_gate_and_notifies(self):
        notify = AsyncMock(return_value=["a", "b"])
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("CLEARED"))),
            patch.object(c, "_supplier_payout_gate", new=AsyncMock(return_value=True)) as bal,
            patch("shared.orchestration.notifications.notify_deal_settled", new=notify),
        ):
            await c.on_escrow_settled(DomainEvent(
                topic=EventTopic.ESCROW_SETTLED, deal_id="DEAL-1",
                payload={"supplier_balance_due": 1450.0, "currency": "USD"},
            ))
        bal.assert_awaited_once()
        notify.assert_awaited_once_with(bal.await_args.args[0], 1450.0, "USD")

    def test_registration_covers_new_topics(self):
        subs: dict = {}
        bus = SimpleNamespace(subscribe=lambda topic, handler: subs.__setitem__(topic, handler))
        c.register_continuations(bus)
        assert subs[EventTopic.ESCROW_TRANCHE_RELEASED] is c.on_escrow_tranche_released
        assert subs[EventTopic.ESCROW_SETTLED] is c.on_escrow_settled


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

class TestCallbacks:
    @pytest.mark.asyncio
    async def test_counterparty_risk_approve_issues_pi(self):
        from services.hitl_approval import callbacks as cb
        issue = AsyncMock(return_value=SimpleNamespace(pi_id="PI-9"))
        with patch("services.dealmaker.service.issue_pi", new=issue):
            out = await cb.execute_callback("CONFIRM_COUNTERPARTY_RISK", "DEAL-1",
                                            {"agreed_price": 1200.0, "quantity_mt": 25.0, "market": "DE"}, "APPR-1")
        assert out == {"pi_id": "PI-9", "status": "pi_issued"}
        req = issue.await_args.args[0]
        assert req.idempotency_key == "auto-pi-DEAL-1" and req.quantity_mt == 25.0

    @pytest.mark.asyncio
    async def test_buyer_acceptance_approve_releases_final_tranche(self):
        from services.hitl_approval import callbacks as cb
        accept = AsyncMock(return_value=SimpleNamespace(released_total=50000.0, status="RELEASED"))
        with patch("services.escrow_guard.service.accept_escrow", new=accept):
            out = await cb.execute_callback("CONFIRM_BUYER_ACCEPTANCE", "DEAL-1",
                                            {"escrow_id": "ESC-1", "accepted_by": "buyer@acme.de"}, "APPR-2")
        assert out["status"] == "RELEASED" and accept.await_args.args[0] == "ESC-1"
        assert accept.await_args.args[1].accepted_by == "buyer@acme.de"

    @pytest.mark.asyncio
    async def test_payout_callback_forwards_kind_and_owed(self):
        from services.hitl_approval import callbacks as cb
        pay = AsyncMock(return_value=SimpleNamespace(payout_id="PAY-1", status="PROCESSING", kind="supplier_balance"))
        with patch("services.escrow_guard.service.request_payout", new=pay):
            out = await cb.execute_callback("RELEASE_SUPPLIER_PAYOUT", "DEAL-1", {
                "deal_id": "DEAL-1", "payee_id": "SUP", "amount": 100.0, "idempotency_key": "k",
                "kind": "supplier_balance", "total_owed": 500.0, "unexpected": "dropped",
            }, "APPR-3")
        req = pay.await_args.args[0]
        assert req.kind == "supplier_balance" and req.total_owed == 500.0 and out["kind"] == "supplier_balance"

    def test_action_classes_cover_enum(self):
        from shared.domain.enums import ApprovalAction
        assert {a.value for a in ApprovalAction} == {cls.action for cls in ap.ACTION_CLASSES}
        assert ap.resolve_level("CONFIRM_BUYER_ACCEPTANCE") == "L0"
        assert ap.is_money_moving("CONFIRM_BUYER_ACCEPTANCE") is True


# ---------------------------------------------------------------------------
# RFQ by e-mail (sourcing + email processor)
# ---------------------------------------------------------------------------

class TestRfqByEmail:
    @pytest.fixture(autouse=True)
    def _sourcing_repo(self, monkeypatch):
        from services.sourcing.repository import InMemorySourcingRepository
        repo = InMemorySourcingRepository()
        monkeypatch.setattr("services.sourcing.service.get_repository", lambda: repo)
        return repo

    @pytest.mark.asyncio
    async def test_dispatch_emails_supplier_with_token(self):
        from services.sourcing.schemas import RfqRequest, SupplierRef
        from services.sourcing.service import dispatch_rfq
        send = AsyncMock(return_value=SimpleNamespace(outcome="sent", message_id="<m1@portless.io>"))
        with (
            patch("services.email_gateway.sender.send_email", new=send),
            patch("services.sourcing.service._emit_event", new=AsyncMock()),
        ):
            res = await dispatch_rfq(RfqRequest(
                deal_id="DEAL-1", hs_code="090422", quantity_mt=25.0, target_price_usd=4200.0,
                suppliers=[SupplierRef(supplier_id="SUP-1", source="other", company_name="Kerala Spice Co",
                                       email="sales@kerala.in")],
            ))
        assert res.status == "COMPLETE" and res.dispatch_results[0].channel == "email"
        assert res.dispatch_results[0].email == "sales@kerala.in"
        sent = send.await_args.args[0]
        assert f"[RFQ:{res.rfq_id}]" in sent.subject and sent.to_address == "sales@kerala.in"

    @pytest.mark.asyncio
    async def test_logged_only_rfq_is_not_a_dispatch(self):
        """No mailbox configured: the RFQ is written to the log, not delivered —
        the dispatch says so instead of claiming success."""
        from services.sourcing.schemas import RfqRequest, SupplierRef
        from services.sourcing.service import dispatch_rfq
        with (
            patch("services.email_gateway.sender.send_email",
                  new=AsyncMock(return_value=SimpleNamespace(outcome="logged", message_id=""))),
            patch("services.sourcing.service._emit_event", new=AsyncMock()),
        ):
            res = await dispatch_rfq(RfqRequest(
                deal_id="DEAL-1", hs_code="090422", quantity_mt=25.0,
                suppliers=[SupplierRef(supplier_id="SUP-1", source="other", email="sales@kerala.in")],
            ))
        d = res.dispatch_results[0]
        assert res.status == "FAILED" and d.success is False and d.channel == "email"
        assert d.email == "sales@kerala.in" and "no outbound mailbox" in d.error

    @pytest.mark.asyncio
    async def test_reply_maps_to_rfq_and_records_quote(self):
        from services.sourcing.schemas import RfqRequest, SupplierRef
        from services.sourcing.service import dispatch_rfq, receive_quote_by_email
        with (
            patch("services.email_gateway.sender.send_email",
                  new=AsyncMock(return_value=SimpleNamespace(outcome="logged", message_id="m"))),
            patch("services.sourcing.service._emit_event", new=AsyncMock()),
        ):
            res = await dispatch_rfq(RfqRequest(
                deal_id="DEAL-1", hs_code="090422", quantity_mt=25.0,
                suppliers=[SupplierRef(supplier_id="SUP-1", source="other", email="sales@kerala.in"),
                           SupplierRef(supplier_id="SUP-2", source="other", email="x@other.in")],
            ))
        received = AsyncMock(return_value=SimpleNamespace(quote_id="Q-1", supplier_id="SUP-1", re_scored_decision=None))
        deal = SimpleNamespace(match_results=[SimpleNamespace(supplier_id="SUP-1", company_name="Kerala Spices Pvt Ltd")])
        with patch("services.sourcing.service.receive_quote", new=received),                 patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)):
            await receive_quote_by_email(res.rfq_id, "Ravi <ravi@kerala.in>".split("<")[1].rstrip(">"), 3150.0,
                                         body_text="Our GSTIN is 32AAAAA0000A1Z5, IEC: AAAPL1234A")
        req = received.await_args.args[0]
        assert req.supplier_id == "SUP-1" and req.rfq_id == res.rfq_id and req.deal_id == "DEAL-1"
        assert req.unit_price_usd == 3150.0 and req.gstin == "32AAAAA0000A1Z5" and req.iec_number == "AAAPL1234A"
        assert req.company_name == "Kerala Spices Pvt Ltd"  # the match board never shows a nameless offer

        from services.email_processor.mapper import map_email, rfq_id_from_subject
        assert rfq_id_from_subject(f"Re: Request for quotation [RFQ:{res.rfq_id}]") == res.rfq_id
        mapping = await map_email("ravi@kerala.in", f"Re: quote [RFQ:{res.rfq_id}]", "", [])
        assert mapping.rfq_id == res.rfq_id and mapping.party == "supplier" and mapping.negotiation_id is None

    @pytest.mark.asyncio
    async def test_processor_routes_rfq_reply(self, monkeypatch):
        import services.email_processor.repository as ep_repo_mod
        from services.email_processor.repository import InMemoryEmailProcessorRepository
        from services.email_processor.schemas import InboundEmailSource, InboundEmailStatus, InboundRawEmail
        from services.email_processor.service import process_inbound
        ep_repo_mod._repository = InMemoryEmailProcessorRepository()
        try:
            quote = AsyncMock(return_value=SimpleNamespace(quote_id="Q-7", supplier_id="SUP-1", re_scored_decision="SOURCE"))
            with (
                patch("services.sourcing.service.get_rfq", new=AsyncMock(return_value=SimpleNamespace(deal_id="DEAL-1"))),
                patch("services.sourcing.service.receive_quote_by_email", new=quote),
            ):
                result = await process_inbound(InboundRawEmail(
                    from_address="sales@kerala.in", to_address="portless-trade@portless.io",
                    subject="Re: Request for quotation [RFQ:RFQ-ABC123DEF456]",
                    body_text="We can offer USD 3150 per MT FOB Kochi.", source=InboundEmailSource.MANUAL,
                    received_at=NOW,
                ))
            assert result.status == InboundEmailStatus.PROCESSED and "Q-7" in result.message
            assert quote.await_args.args[0] == "RFQ-ABC123DEF456" and quote.await_args.args[2] == 3150.0
        finally:
            ep_repo_mod._repository = None
