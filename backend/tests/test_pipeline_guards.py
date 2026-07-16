"""Tests for the three pipeline-hardening gaps (2026-07-17):

  P3-1 — InProcessEventBus + NEGOTIATION_AGREED → PI continuation
  P3-3 — inbound price plausibility guard
  P3-4 — degraded-draft / placeholder risk flags on email approvals
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from agents.negotiation_agent.guards import is_price_plausible
from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession
from agents.negotiation_agent.service import draft_risk_flags
from shared.bus.publisher import InProcessEventBus
from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration.continuations import (
    on_customs_cleared,
    on_escrow_funded,
    on_negotiation_agreed,
    on_pi_accepted,
    on_po_issued,
    on_qc_passed,
)


def _session(**overrides) -> NegotiationSession:
    now = datetime.now(timezone.utc)
    defaults = dict(
        negotiation_id="neg-1", deal_id="DEAL-1", trade_match_id="TM-1",
        buyer_id="BYR-1", seller_id="SUP-1", product_code="1006",
        buyer_country_code="DE", buyer_target_price=1255.0,
        supplier_quote_price=1004.0, policy=NegotiationPolicy(),
        status="ROUND", round_no=1, current_bid=1255.0, current_ask=1004.0,
        created_at=now, updated_at=now,
    )
    defaults.update(overrides)
    return NegotiationSession(**defaults)


# ---------------------------------------------------------------------------
# P3-3 — price plausibility
# ---------------------------------------------------------------------------

class TestPricePlausibility:
    def test_corridor_prices_accepted(self):
        s = _session()
        assert is_price_plausible(s, 1255.0)
        assert is_price_plausible(s, 1004.0)
        assert is_price_plausible(s, 1100.0)

    def test_aggressive_but_sane_counters_accepted(self):
        s = _session()
        assert is_price_plausible(s, 400.0)    # ~2.5× below the corridor min
        assert is_price_plausible(s, 3000.0)   # ~2.4× above the corridor max

    def test_magnitude_errors_rejected(self):
        s = _session()
        assert not is_price_plausible(s, 102123.71)  # the real incident: 81× band
        assert not is_price_plausible(s, 12.0)       # ~84× below
        assert not is_price_plausible(s, 0.0)
        assert not is_price_plausible(s, -5.0)

    def test_band_factor_respected(self):
        s = _session()
        assert is_price_plausible(s, 5000.0, band_factor=5.0)
        assert not is_price_plausible(s, 5000.0, band_factor=3.0)

    def test_no_references_cannot_judge(self):
        s = _session(buyer_target_price=0.0001, supplier_quote_price=0.0001,
                     current_bid=None, current_ask=None)
        s.buyer_target_price = 0
        s.supplier_quote_price = 0
        assert is_price_plausible(s, 999999.0)  # nothing to compare against


class TestRunRoundPriceVeto:
    @pytest.mark.asyncio
    async def test_implausible_buyer_price_never_becomes_bid(self, monkeypatch):
        from agents.negotiation_agent import service as svc
        from agents.negotiation_agent.repository import InMemoryNegotiationRepository
        from agents.negotiation_agent.schemas import RoundRequest

        repo = InMemoryNegotiationRepository()
        session = _session()
        await repo.save_session(session)
        monkeypatch.setattr("agents.negotiation_agent.repository._repo", repo)

        class _FakeGraph:
            async def ainvoke(self, state):
                return {**state, "routing": "CALL_SUPPLIER_AGENT",
                        "hitl_required": False, "reasoning": "route"}

        monkeypatch.setattr(svc, "build_negotiation_graph", lambda: _FakeGraph())
        monkeypatch.setattr(svc, "_build_conversation_history",
                            AsyncMock(return_value={"buyer": "", "supplier": ""}))
        monkeypatch.setattr(svc, "fetch_both",
                            AsyncMock(return_value={"buyer": "", "supplier": ""}))
        monkeypatch.setattr(svc, "get_usd_inr_rate", AsyncMock(return_value=96.0))
        monkeypatch.setattr(svc, "_post_graph_notify", AsyncMock())

        resp = await svc.run_round("neg-1", RoundRequest(
            counter_price=102123.71, party="buyer",
            inbound_message="We agree at 102123.71 total.",
        ))
        saved = await repo.get_session("neg-1")
        assert saved.current_bid == 1255.0            # unchanged
        assert saved.hitl_required is True             # flagged for review
        assert "PRICE GUARD" in (resp.reasoning or "")

    @pytest.mark.asyncio
    async def test_operator_override_bypasses_guard(self, monkeypatch):
        from agents.negotiation_agent import service as svc
        from agents.negotiation_agent.repository import InMemoryNegotiationRepository
        from agents.negotiation_agent.schemas import RoundRequest

        repo = InMemoryNegotiationRepository()
        await repo.save_session(_session())
        monkeypatch.setattr("agents.negotiation_agent.repository._repo", repo)

        class _FakeGraph:
            async def ainvoke(self, state):
                return {**state, "routing": "CALL_SUPPLIER_AGENT",
                        "hitl_required": False, "reasoning": "route"}

        monkeypatch.setattr(svc, "build_negotiation_graph", lambda: _FakeGraph())
        monkeypatch.setattr(svc, "_build_conversation_history",
                            AsyncMock(return_value={"buyer": "", "supplier": ""}))
        monkeypatch.setattr(svc, "fetch_both",
                            AsyncMock(return_value={"buyer": "", "supplier": ""}))
        monkeypatch.setattr(svc, "get_usd_inr_rate", AsyncMock(return_value=96.0))
        monkeypatch.setattr(svc, "_post_graph_notify", AsyncMock())

        resp = await svc.run_round("neg-1", RoundRequest(
            counter_price=1.0, party="buyer",
            operator_override_price=99999.0,
        ))
        saved = await repo.get_session("neg-1")
        assert saved.current_bid == 99999.0
        assert "PRICE GUARD" not in (resp.reasoning or "")


# ---------------------------------------------------------------------------
# P3-4 — draft quality flags
# ---------------------------------------------------------------------------

class TestDraftRiskFlags:
    def test_clean_draft(self):
        assert draft_risk_flags("Dear Sir, we offer USD 470/MT CIF.") == ["email_content_review"]

    def test_degraded_flag(self):
        flags = draft_risk_flags("Thank you for your offer.", degraded=True)
        assert "degraded_llm_fallback" in flags

    def test_placeholder_flag(self):
        flags = draft_risk_flags("Dear [Buyer Name],\nregarding [Product Name]...")
        assert "unresolved_placeholders" in flags

    def test_negotiation_token_not_a_placeholder(self):
        # The [NEG:uuid] threading token must not trigger the flag
        assert "unresolved_placeholders" not in draft_risk_flags(
            "Subject token [NEG:4c02932b-d7fa-4042-aae9-cf4ac412e212]"
        )


# ---------------------------------------------------------------------------
# P3-1 — event bus + AGREED continuation
# ---------------------------------------------------------------------------

class TestInProcessEventBus:
    @pytest.mark.asyncio
    async def test_publish_reaches_subscriber(self):
        bus = InProcessEventBus()
        seen: list[DomainEvent] = []

        async def handler(event: DomainEvent) -> None:
            seen.append(event)

        bus.subscribe(EventTopic.NEGOTIATION_AGREED, handler)
        await bus.publish(DomainEvent(topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1"))
        await asyncio.sleep(0)  # let the detached task run
        assert len(seen) == 1 and seen[0].deal_id == "DEAL-1"

    @pytest.mark.asyncio
    async def test_handler_exception_is_isolated(self):
        bus = InProcessEventBus()
        seen: list[str] = []

        async def bad(event: DomainEvent) -> None:
            raise RuntimeError("boom")

        async def good(event: DomainEvent) -> None:
            seen.append("ok")

        bus.subscribe(EventTopic.NEGOTIATION_AGREED, bad)
        bus.subscribe(EventTopic.NEGOTIATION_AGREED, good)
        await bus.publish(DomainEvent(topic=EventTopic.NEGOTIATION_AGREED, deal_id="D"))
        await asyncio.sleep(0.01)
        assert seen == ["ok"]

    @pytest.mark.asyncio
    async def test_unsubscribed_topic_is_noop(self):
        bus = InProcessEventBus()
        await bus.publish(DomainEvent(topic=EventTopic.QC_PASSED, deal_id="D"))  # no error


def _fake_deal(status: str = "NEGOTIATION_AGREED"):
    class _S:
        def __init__(self, v): self.value = v
    class _D:
        def __init__(self):
            self.deal_id = "DEAL-1"
            self.status = _S(status)
            self.market = "GB"
            self.hs_code = "290543"
            self.autonomy_profile = _S("L0")
            self.winning_supplier_id = "IM-1"
    return _D()


def _neg_repo_mock(current_ask: float | None = 1004.0, quantity_mt: float | None = 25.0):
    session = _session(current_ask=current_ask, quantity_mt=quantity_mt)
    repo = AsyncMock()
    repo.get_session_by_deal = AsyncMock(return_value=session)
    return repo


class TestAgreedContinuation:
    @pytest.mark.asyncio
    async def test_issues_pi_with_idempotency_key(self):
        issue = AsyncMock(return_value=type("PI", (), {"pi_id": "PI-1"})())
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_fake_deal())),
            patch("services.dealmaker.service.issue_pi", new=issue),
        ):
            await on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1",
                payload={"agreed_price": 1200.0},
            ))
        issue.assert_awaited_once()
        req = issue.await_args.args[0]
        assert req.deal_id == "DEAL-1"
        assert req.agreed_price == 1200.0
        assert req.idempotency_key == "auto-pi-DEAL-1"

    @pytest.mark.asyncio
    async def test_skips_when_deal_already_advanced(self):
        issue = AsyncMock()
        with (
            patch("services.deals.service.get_deal",
                  new=AsyncMock(return_value=_fake_deal("PI_SENT"))),
            patch("services.dealmaker.service.issue_pi", new=issue),
        ):
            await on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1",
                payload={"agreed_price": 1200.0},
            ))
        issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_on_missing_or_invalid_price(self):
        issue = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_fake_deal())),
            patch("services.dealmaker.service.issue_pi", new=issue),
        ):
            await on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1", payload={},
            ))
            await on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1",
                payload={"agreed_price": 0},
            ))
        issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_flag(self, monkeypatch):
        monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "false")
        issue = AsyncMock()
        with patch("services.dealmaker.service.issue_pi", new=issue):
            await on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1",
                payload={"agreed_price": 1200.0},
            ))
        issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_session_quantity(self):
        issue = AsyncMock(return_value=type("PI", (), {"pi_id": "PI-1"})())
        session = _session(quantity_mt=50.0)
        repo = AsyncMock()
        repo.get_session_by_deal = AsyncMock(return_value=session)
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_fake_deal())),
            patch("services.dealmaker.service.issue_pi", new=issue),
            patch("agents.negotiation_agent.repository.get_repository", return_value=repo),
        ):
            await on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-1",
                payload={"agreed_price": 1200.0},
            ))
        assert issue.await_args.args[0].quantity_mt == 50.0


class TestRemainingContinuationLegs:
    @pytest.mark.asyncio
    async def test_pi_accepted_opens_escrow_sized_by_pi(self):
        pi = type("PI", (), {"pi_id": "PI-1", "total_value": 31375.0})()
        dm_repo = AsyncMock()
        dm_repo.get_pi = AsyncMock(return_value=pi)
        dm_repo.get_pi_by_deal = AsyncMock(return_value=pi)
        create = AsyncMock(return_value=type("E", (), {"escrow_id": "ESC-1"})())
        with (
            patch("services.deals.service.get_deal",
                  new=AsyncMock(return_value=_fake_deal("PI_ACCEPTED"))),
            patch("services.dealmaker.repository.get_repository", return_value=dm_repo),
            patch("services.escrow_guard.service.create_escrow", new=create),
            patch("agents.negotiation_agent.repository.get_repository",
                  return_value=_neg_repo_mock()),
        ):
            await on_pi_accepted(DomainEvent(
                topic=EventTopic.PI_ACCEPTED, deal_id="DEAL-1",
                payload={"pi_id": "PI-1"},
            ))
        req = create.await_args.args[0]
        assert req.amount == 31375.0
        assert req.idempotency_key == "auto-escrow-DEAL-1"

    @pytest.mark.asyncio
    async def test_pi_accepted_skips_when_deal_advanced(self):
        create = AsyncMock()
        with (
            patch("services.deals.service.get_deal",
                  new=AsyncMock(return_value=_fake_deal("ESCROW_FUNDED"))),
            patch("services.escrow_guard.service.create_escrow", new=create),
        ):
            await on_pi_accepted(DomainEvent(
                topic=EventTopic.PI_ACCEPTED, deal_id="DEAL-1", payload={},
            ))
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_escrow_funded_creates_po_gate(self):
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal",
                  new=AsyncMock(return_value=_fake_deal("ESCROW_FUNDED"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("agents.negotiation_agent.repository.get_repository",
                  return_value=_neg_repo_mock(current_ask=1004.0, quantity_mt=25.0)),
        ):
            await on_escrow_funded(DomainEvent(
                topic=EventTopic.ESCROW_FUNDED, deal_id="DEAL-1", payload={},
            ))
        req = gate.await_args.args[0]
        assert req.action == "ISSUE_PO"
        assert req.financial_impact_usd == 25100.0  # 1004 × 25
        assert req.callback_payload["agreed_price"] == 1004.0
        assert req.idempotency_key == "auto-po-gate-DEAL-1"

    @pytest.mark.asyncio
    async def test_po_issued_books_qc(self):
        book = AsyncMock(return_value=type("T", (), {"tpo_id": "TPO-1", "provider": "qima"})())
        with (
            patch("services.deals.service.get_deal",
                  new=AsyncMock(return_value=_fake_deal("PO_ISSUED"))),
            patch("services.shield_link.service.book_qc", new=book),
            patch("agents.negotiation_agent.repository.get_repository",
                  return_value=_neg_repo_mock()),
        ):
            await on_po_issued(DomainEvent(
                topic=EventTopic.PO_ISSUED, deal_id="DEAL-1", payload={},
            ))
        req = book.await_args.args[0]
        assert req.deal_id == "DEAL-1"
        assert req.provider is None          # buyer-opted resolution decides
        assert req.idempotency_key == "auto-qc-DEAL-1"

    @pytest.mark.asyncio
    async def test_qc_passed_creates_freight_gate(self):
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal",
                  new=AsyncMock(return_value=_fake_deal("QC_PASSED"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await on_qc_passed(DomainEvent(
                topic=EventTopic.QC_PASSED, deal_id="DEAL-1", payload={},
            ))
        req = gate.await_args.args[0]
        assert req.action == "BOOK_FREIGHT"
        assert req.idempotency_key == "auto-freight-gate-DEAL-1"

    @pytest.mark.asyncio
    async def test_cleared_creates_payout_gate(self):
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal",
                  new=AsyncMock(return_value=_fake_deal("CLEARED"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("agents.negotiation_agent.repository.get_repository",
                  return_value=_neg_repo_mock(current_ask=1004.0, quantity_mt=25.0)),
        ):
            await on_customs_cleared(DomainEvent(
                topic=EventTopic.CUSTOMS_CLEARED, deal_id="DEAL-1", payload={},
            ))
        req = gate.await_args.args[0]
        assert req.action == "RELEASE_SUPPLIER_PAYOUT"
        assert req.callback_payload["payee_id"] == "SUPPLIER-IM-1"
        assert req.callback_payload["amount"] == 25100.0
        assert req.idempotency_key == "auto-payout-gate-DEAL-1"

    @pytest.mark.asyncio
    async def test_all_legs_respect_disabled_flag(self, monkeypatch):
        monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "false")
        gate = AsyncMock()
        with patch("services.hitl_approval.service.create_approval", new=gate):
            for handler, topic in (
                (on_pi_accepted, EventTopic.PI_ACCEPTED),
                (on_escrow_funded, EventTopic.ESCROW_FUNDED),
                (on_po_issued, EventTopic.PO_ISSUED),
                (on_qc_passed, EventTopic.QC_PASSED),
                (on_customs_cleared, EventTopic.CUSTOMS_CLEARED),
            ):
                await handler(DomainEvent(topic=topic, deal_id="DEAL-1", payload={}))
        gate.assert_not_awaited()
