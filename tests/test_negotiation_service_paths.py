"""Negotiation service: starting, reusing and resuming sessions, QC choices, gates and lookups."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.negotiation_agent import service as svc
from agents.negotiation_agent.briefing import AFTER_INTRO
from agents.negotiation_agent.email_history import EmailHistoryRecord
from agents.negotiation_agent.repository import InMemoryNegotiationRepository
from agents.negotiation_agent.schemas import (
    NegotiationPolicy,
    NegotiationSession,
    RoundRequest,
    StartNegotiationRequest,
)
from tests.factories import NOW, make


@pytest.fixture
def repo(monkeypatch):
    store = InMemoryNegotiationRepository()
    monkeypatch.setattr(svc, "get_repository", lambda: store)
    return store


def _session(**kw):
    base = {
        "negotiation_id": "N1", "deal_id": "D1", "status": "ROUND", "buyer_target_price": 900.0, "supplier_quote_price": 700.0,
        "current_bid": 850.0, "current_ask": 750.0, "policy": NegotiationPolicy(),
    }
    return make(NegotiationSession, **{**base, **kw})


def _start(**kw):
    base = {"deal_id": "D1", "buyer_target_price": 900.0, "supplier_quote_price": 700.0, "policy": NegotiationPolicy(), "buyer_email": "b@x.co", "seller_email": "s@y.co"}
    return make(StartNegotiationRequest, **{**base, **kw})


STATE = {"routing": "CALL_BUYER_AGENT", "current_bid": 860.0, "current_ask": 740.0, "buyer_outbound_message": "Hello", "supplier_outbound_message": "Hi", "hitl_required": False, "current_margin": 12.0}


@pytest.fixture
def pipeline(monkeypatch):
    graph = MagicMock(ainvoke=AsyncMock(return_value=dict(STATE)))
    monkeypatch.setattr(svc, "build_negotiation_graph", lambda: graph)
    monkeypatch.setattr(svc, "fetch_both", AsyncMock(return_value={"buyer": "", "supplier": ""}))
    monkeypatch.setattr(svc, "get_usd_inr_rate", AsyncMock(return_value=84.0))
    monkeypatch.setattr(svc, "_seed_qc_costing", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(svc, "_seed_logistics_cost", AsyncMock(return_value=None))
    notify = AsyncMock()
    monkeypatch.setattr(svc, "_post_graph_notify", notify)
    return SimpleNamespace(graph=graph, notify=notify)


class TestStart:
    async def test_new_session(self, repo, pipeline):
        response = await svc.start_negotiation(_start())
        session = await repo.get_session(response.negotiation_id)
        assert session.round_no == 1 and session.current_bid == 860.0 and session.margin_at_current == 12.0
        rounds = await repo.list_rounds(response.negotiation_id)
        assert len(rounds) == 1
        pipeline.notify.assert_awaited_once()

    async def test_supplier_message_records_the_offer(self, repo, pipeline):
        response = await svc.start_negotiation(_start())
        session = await repo.get_session(response.negotiation_id)
        assert (session.last_offer_to_buyer, session.last_offer_to_supplier) == (860.0, 740.0)

    async def test_existing_session_is_reused(self, repo, pipeline):
        await repo.save_session(_session())
        assert (await svc.start_negotiation(_start())).negotiation_id == "N1"
        pipeline.graph.ainvoke.assert_not_awaited()

    @pytest.mark.parametrize("status", ["FAILED", "ESCALATED"])
    async def test_failed_session_can_restart(self, repo, pipeline, status):
        await repo.save_session(_session(status=status))
        repo._deal_claims.clear()
        assert (await svc.start_negotiation(_start())).negotiation_id != "N1"

    async def test_lost_claim_reuses_the_winner(self, repo, pipeline):
        winner = _session(negotiation_id="W")
        with patch.object(repo, "get_session_by_deal", new=AsyncMock(side_effect=[None, winner])), patch.object(repo, "claim_deal", new=AsyncMock(return_value=False)):
            assert (await svc.start_negotiation(_start())).negotiation_id == "W"
        pipeline.graph.ainvoke.assert_not_awaited()

    async def test_lost_claim_without_a_session_continues(self, repo, pipeline):
        with patch.object(repo, "claim_deal", new=AsyncMock(return_value=False)):
            assert (await svc.start_negotiation(_start())).negotiation_id

    async def test_graph_crash_goes_to_a_human(self, repo, pipeline):
        pipeline.graph.ainvoke.side_effect = RuntimeError("llm down")
        response = await svc.start_negotiation(_start())
        session = await repo.get_session(response.negotiation_id)
        assert session.hitl_required is True


class TestRound:
    async def test_unknown(self, repo):
        with pytest.raises(ValueError, match="not found"):
            await svc.run_round("x", RoundRequest(counter_price=800.0, party="buyer"))

    @pytest.mark.parametrize("status", ["AGREED", "FAILED", "ESCALATED"])
    async def test_terminal(self, repo, status):
        await repo.save_session(_session(status=status))
        with pytest.raises(ValueError, match="terminal"):
            await svc.run_round("N1", RoundRequest(counter_price=800.0, party="buyer"))

    @pytest.fixture
    def _round_env(self, monkeypatch, pipeline):
        monkeypatch.setattr(svc, "_build_conversation_history", AsyncMock(return_value={"buyer": "", "supplier": ""}))
        monkeypatch.setattr(svc, "_sync_deal_status", AsyncMock(), raising=False)
        monkeypatch.setattr(svc, "_apply_buyer_qc_choice", AsyncMock())
        return pipeline

    async def test_supplier_price_updates_the_ask(self, repo, _round_env):
        await repo.save_session(_session())
        graph_state = _round_env.graph.ainvoke
        await svc.run_round("N1", RoundRequest(counter_price=760.0, party="supplier"))
        assert graph_state.await_args.args[0]["session"].supplier_last_quote == 760.0

    async def test_graph_crash_goes_to_a_human(self, repo, _round_env):
        await repo.save_session(_session())
        _round_env.graph.ainvoke.side_effect = RuntimeError("llm down")
        await svc.run_round("N1", RoundRequest(counter_price=800.0, party="buyer"))
        assert (await repo.get_session("N1")).hitl_required is True

    async def test_new_inspector_in_the_reply_is_applied(self, repo, _round_env):
        await repo.save_session(_session())
        with patch("shared.connectors.qc_providers.detect_qc_provider_mention", return_value="qima"):
            await svc.run_round("N1", RoundRequest(counter_price=800.0, party="buyer", inbound_message="use QIMA please"))
        assert svc._apply_buyer_qc_choice.await_args.args[1] == "qima"

    async def test_classifier_single_choice(self, repo, _round_env):
        await repo.save_session(_session())
        _round_env.graph.ainvoke.return_value = {**STATE, "mentioned_qc_providers": ["sgs"]}
        await svc.run_round("N1", RoundRequest(counter_price=800.0, party="buyer"))
        assert svc._apply_buyer_qc_choice.await_args.args[1] == "sgs"

    async def test_several_acceptable_inspectors_picks_the_cheapest(self, repo, _round_env):
        await repo.save_session(_session())
        _round_env.graph.ainvoke.return_value = {**STATE, "mentioned_qc_providers": ["sgs", "qima"]}
        with patch("shared.connectors.qc_providers.cheapest_qc_provider", return_value="qima"):
            await svc.run_round("N1", RoundRequest(counter_price=800.0, party="buyer"))
        assert svc._apply_buyer_qc_choice.await_args.args[1] == "qima"

    async def test_same_inspector_is_not_reapplied(self, repo, _round_env):
        await repo.save_session(_session(qc_provider="sgs"))
        _round_env.graph.ainvoke.return_value = {**STATE, "mentioned_qc_providers": ["sgs"]}
        await svc.run_round("N1", RoundRequest(counter_price=800.0, party="buyer"))
        svc._apply_buyer_qc_choice.assert_not_awaited()


class TestLookups:
    async def test_get(self, repo):
        await repo.save_session(_session())
        assert (await svc.get_negotiation("N1")).deal_id == "D1"
        with pytest.raises(ValueError, match="not found"):
            await svc.get_negotiation("x")

    async def test_lists(self, repo):
        await repo.save_session(_session())
        assert len(await svc.list_negotiations("D1")) == 1
        assert await svc.list_rounds("N1") == []

    async def test_threads_empty(self, repo):
        assert await svc.get_email_threads() == []

    async def test_threads_count_pending_and_sort(self, repo):
        await repo.save_session(_session(updated_at=NOW))
        await repo.save_session(_session(negotiation_id="N2", deal_id="D2", updated_at=NOW.replace(year=2030)))
        email = make(EmailHistoryRecord, email_id="E1", negotiation_id="N1", status="pending_approval")
        emails = MagicMock(list_by_negotiation=AsyncMock(side_effect=lambda nid: [email] if nid == "N1" else []))
        with patch.object(svc, "get_email_repository", return_value=emails):
            threads = await svc.get_email_threads()
        assert [t.negotiation_id for t in threads] == ["N2", "N1"] and threads[1].pending_approval_count == 1

    async def test_safe_call_returns_the_error(self):
        async def boom():
            raise RuntimeError("x")

        assert isinstance(await svc._safe_call(boom()), RuntimeError)

    async def test_safe_call_returns_the_value(self):
        async def fine():
            return 1

        assert await svc._safe_call(fine()) == 1

    def test_response(self):
        assert svc._session_to_response(_session(), "why").negotiation_id == "N1"


class TestDrafts:
    async def test_history_failure(self):
        with patch.object(svc, "get_email_repository", side_effect=RuntimeError):
            assert await svc._build_conversation_history("N") == {"buyer": "", "supplier": ""}

    def test_long_bodies_are_cut(self):
        record = make(EmailHistoryRecord, email_id="E", negotiation_id="N", party="buyer", body="x" * (svc._HISTORY_BODY_CHARS + 50), status="sent")
        assert svc._format_history([record], "buyer").endswith("…")

    def test_gate_level_policy_failure(self):
        with patch("shared.orchestration.autonomy_policy.resolve_level", side_effect=RuntimeError):
            assert svc._resolve_gate_level("X") == "L0"

    def test_first_outbound_is_always_human(self, monkeypatch):
        monkeypatch.setattr(svc.settings, "hitl_on_first_outbound", True)
        assert svc._email_gate_level(_session(round_no=0), []) == "L0"

    async def test_safety_check_failure_is_tolerated(self):
        with patch.object(svc, "_load_deal_quiet", new=AsyncMock(return_value=None)), patch("agents.negotiation_agent.email_safety.leak_findings", side_effect=RuntimeError):
            flags, leaks = await svc._draft_safety(_session(), "buyer", "a@b.co", "body")
        assert leaks == [] and "counterparty_leak" not in flags

    async def test_fake_address_is_flagged(self):
        with patch.object(svc, "_load_deal_quiet", new=AsyncMock(return_value=None)), patch("agents.negotiation_agent.email_safety.leak_findings", return_value=[]), patch("agents.negotiation_agent.email_safety.is_real_address", return_value=False):
            assert (await svc._draft_safety(_session(), "buyer", "x@placeholder", "body"))[0] == ["no_real_address"]

    @pytest.mark.parametrize(("flags", "text"), [
        (["no_real_address"], "No real address"), (["degraded_llm_fallback"], "DEGRADED TEMPLATE"),
        (["unresolved_placeholders"], "unresolved"), (["price_out_of_band"], "outside the allowed range"),
    ])
    async def test_quality_note_reaches_the_approval(self, monkeypatch, flags, text):
        emails = MagicMock(save=AsyncMock(), create=AsyncMock())
        monkeypatch.setattr(svc, "get_email_repository", lambda: emails)
        monkeypatch.setattr(svc, "_draft_safety", AsyncMock(return_value=(list(flags), [])))
        monkeypatch.setattr(svc, "draft_risk_flags", lambda *_a, **_k: [])
        with patch("services.hitl_approval.service.create_approval", new=AsyncMock(return_value=SimpleNamespace(approval_id="A", status="PENDING"))) as create:
            await svc._create_and_gate_email(_session(), "buyer", "a@b.co", "Subject", "body")
        assert text in create.await_args.args[0].summary

    async def test_approval_failure_is_contained(self, monkeypatch):
        monkeypatch.setattr(svc, "get_email_repository", lambda: MagicMock(save=AsyncMock(), create=AsyncMock()))
        monkeypatch.setattr(svc, "_draft_safety", AsyncMock(return_value=([], [])))
        with patch("services.hitl_approval.service.create_approval", new=AsyncMock(side_effect=RuntimeError)):
            await svc._create_and_gate_email(_session(), "buyer", "a@b.co", "Subject", "body")

    async def test_agreement_gate_failure_is_contained(self):
        with patch("services.hitl_approval.service.create_approval", new=AsyncMock(side_effect=RuntimeError)):
            await svc._create_agreement_gate(_session(), 800.0)

    def test_subject_after_introduction(self):
        assert "your quotation" in svc.negotiation_subject(_session(), "supplier", AFTER_INTRO)

    async def test_gating_errors_are_contained(self, repo, monkeypatch):
        monkeypatch.setattr(svc, "_create_and_gate_email", AsyncMock(side_effect=RuntimeError))
        monkeypatch.setattr(svc, "_sync_deal_status", AsyncMock())
        await svc._post_graph_notify(_session(), {**STATE, "routing": "CALL_BUYER_AGENT"}, repo)
        assert svc._create_and_gate_email.await_count == 2

    async def test_realized_cms_failure_is_contained(self, repo):
        with patch("services.match_engine.service.score_match", new=AsyncMock(side_effect=RuntimeError)):
            await svc._compute_realized_cms(_session(agreed_price=800.0), repo)


class TestEmailDecisions:
    async def test_send_unknown_email(self):
        with patch.object(svc, "get_email_repository", return_value=MagicMock(get=AsyncMock(return_value=None))):
            await svc.send_approved_email("x")

    async def test_reject_unknown_email(self):
        with patch.object(svc, "get_email_repository", return_value=MagicMock(get=AsyncMock(return_value=None))):
            await svc.reject_email("x")

    async def test_preflight_survives_a_thread_failure(self, repo):
        await repo.save_session(_session())
        record = make(EmailHistoryRecord, email_id="E", negotiation_id="N1")
        with patch.object(svc, "get_email_repository", side_effect=RuntimeError), patch.object(svc, "_load_deal_quiet", new=AsyncMock(return_value=None)), patch("agents.negotiation_agent.email_safety.preflight", return_value=["x"]) as check:
            assert await svc._send_preflight(record) == ["x"]
        assert check.call_args.args[3] == []


class TestAgreement:
    async def test_finalize_unknown(self, repo):
        await svc.finalize_agreement("x")

    async def test_finalize_sets_the_price(self, repo, monkeypatch):
        monkeypatch.setattr(svc, "_compute_realized_cms", AsyncMock())
        monkeypatch.setattr(svc, "_sync_deal_status", AsyncMock())
        monkeypatch.setattr(svc, "_provision_counterparty_orgs", AsyncMock())
        await repo.save_session(_session(status="HITL_PENDING"))
        await svc.finalize_agreement("N1", agreed_price=800.0)
        session = await repo.get_session("N1")
        assert session.status == "AGREED" and session.agreed_price == 800.0

    async def test_finalize_without_a_price_skips_cms(self, repo, monkeypatch):
        monkeypatch.setattr(svc, "_compute_realized_cms", AsyncMock())
        monkeypatch.setattr(svc, "_sync_deal_status", AsyncMock())
        monkeypatch.setattr(svc, "_provision_counterparty_orgs", AsyncMock())
        await repo.save_session(_session(status="HITL_PENDING", agreed_price=None))
        await svc.finalize_agreement("N1")
        svc._compute_realized_cms.assert_not_awaited()

    async def test_reject_unknown(self, repo):
        await svc.reject_agreement("x")

    async def test_reject_after_agreement_is_ignored(self, repo):
        await repo.save_session(_session(status="AGREED"))
        await svc.reject_agreement("N1")
        assert (await repo.get_session("N1")).status == "AGREED"

    async def test_party_names_are_used_for_org_provisioning(self, monkeypatch):
        party = lambda role, name: SimpleNamespace(role=SimpleNamespace(value=role), legal_name=name)
        deal = SimpleNamespace(parties=[party("BUYER", "Buy Co"), party("SUPPLIER", "Sup Co"), party("BUYER", "")])
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
             patch("services.deals.service.set_counterparty_orgs", new=AsyncMock()), \
             patch("services.auth.service.provision_organization", new=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(org_id=str(a[0]))), create=True):
            await svc._provision_counterparty_orgs(_session())


class TestWatcher:
    def test_naive_timestamps_are_utc(self):
        session = _session(updated_at=datetime(2026, 1, 1))  # noqa: DTZ001
        assert svc._session_idle_seconds(session, datetime(2026, 1, 1, 0, 1, tzinfo=UTC)) == 60.0

    def test_record_time_naive(self):
        record = make(EmailHistoryRecord, email_id="E", negotiation_id="N", created_at=datetime(2026, 1, 1))  # noqa: DTZ001
        assert svc._record_ts(record).tzinfo is not None

    async def test_retry_round_zero(self, repo, pipeline):
        session = _session(round_no=0)
        await repo.save_session(session)
        await svc._retry_round_zero(session)
        assert (await repo.get_session("N1")).round_no == 1
        pipeline.notify.assert_awaited_once()

    async def test_retry_round_zero_graph_crash(self, repo, pipeline):
        pipeline.graph.ainvoke.side_effect = RuntimeError("down")
        session = _session(round_no=0)
        await repo.save_session(session)
        await svc._retry_round_zero(session)
        assert (await repo.get_session("N1")).hitl_required is True

    async def test_followup_uses_the_model(self):
        with patch("agents.negotiation_agent.graph._chat_json", new=AsyncMock(return_value={"outbound_message": "Following up"})):
            assert await svc._draft_followup_body(_session(), "buyer", "h", "S", 3.0) == ("Following up", False)

    async def test_followup_template_when_degraded(self):
        with patch("agents.negotiation_agent.graph._chat_json", new=AsyncMock(return_value={"_degraded": True})):
            body, degraded = await svc._draft_followup_body(_session(), "buyer", "h", "S", 3.0)
        assert degraded is True and body.startswith("Dear Sir or Madam")

    async def test_followup_history_failure(self):
        with patch.object(svc, "get_email_repository", side_effect=RuntimeError):
            assert await svc._sweep_followups_for_session(_session(), NOW) == 0

    async def test_followup_party_failure_is_contained(self):
        emails = MagicMock(list_by_negotiation=AsyncMock(return_value=[]))
        with patch.object(svc, "get_email_repository", return_value=emails), patch.object(svc, "_maybe_followup_party", new=AsyncMock(side_effect=RuntimeError)):
            assert await svc._sweep_followups_for_session(_session(), NOW) == 0

    async def test_disabled_watcher_exits(self, monkeypatch):
        monkeypatch.setattr(svc.settings, "stale_watcher_enabled", False)
        await svc.run_stale_watcher()

    async def test_watcher_keeps_going_after_a_failed_sweep(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(svc.settings, "stale_watcher_enabled", True)
        monkeypatch.setattr(svc.settings, "stale_watcher_interval_seconds", 0)
        sweep = AsyncMock(side_effect=[RuntimeError("x"), asyncio.CancelledError])
        with patch.object(svc, "sweep_stale_negotiations", new=sweep):
            await svc.run_stale_watcher()
        assert sweep.await_count == 2
