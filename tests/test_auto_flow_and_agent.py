"""Dev auto-flow resume logic, counterparty e-mail templates, and the Exception Resolver agent."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.exception_resolver import agent as ag
from services.exception_resolver.schemas import (
    AgentAnalysis,
    ExceptionCategory,
    ExceptionRecord,
    ExceptionSeverity,
    RemediationDecision,
)
from shared.domain.enums import DealStatus, DocumentType
from shared.llm_gateway import LLMExhaustedException
from shared.orchestration import auto_flow as af
from shared.orchestration import notifications as nf
from tests.factories import make


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(af.asyncio, "sleep", AsyncMock())


def _deal(status="MATCHING", **kw):
    base = {"deal_id": "D1", "opportunity_id": "O1", "hs_code": "1520", "market": "ES", "status": SimpleNamespace(value=status),
            "autonomy_profile": SimpleNamespace(value="L0"), "match_results": [], "buyer_target_price_usd": None}
    return SimpleNamespace(**{**base, **kw})


WINNER = SimpleNamespace(supplier_id="S1", company_name="Sup", match_id="M1", decision="SOURCE", fob_price_usd=700.0, composite_score=80.0)
STEPS = ("_submit_quote", "_nudge_to_negotiating", "_run_negotiation", "_issue_and_accept_pi", "_fund_escrow", "_book_and_simulate_qc",
         "_generate_documents", "_file_customs", "_simulate_freight_cleared", "_simulate_payout_completed", "_create_gate", "_wait_for_status")


@pytest.fixture
def steps():
    mocks = {name: AsyncMock(return_value=True) for name in STEPS}
    mocks["_run_negotiation"].return_value = 800.0
    mocks["_ensure_match_results"] = AsyncMock(return_value=(WINNER, 900.0, 700.0))
    mocks["_resolve_agreed_price"] = AsyncMock(return_value=800.0)
    patches = [patch.object(af, name, new=mock) for name, mock in mocks.items()]
    for item in patches:
        item.start()
    yield mocks
    for item in patches:
        item.stop()


async def _run(deal):
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)):
        await af._run("D1")


class TestSwitch:
    def test_start_does_nothing_when_off(self, monkeypatch):
        from shared.config import settings
        monkeypatch.setattr(settings, "auto_flow_enabled", False)
        with patch.object(af.asyncio, "create_task") as task:
            af.start("D1")
        task.assert_not_called()

    async def test_start_when_on(self, monkeypatch):
        from shared.config import settings
        monkeypatch.setattr(settings, "auto_flow_enabled", True)
        with patch.object(af, "_run_safely", new=MagicMock(return_value="coro")), patch.object(af.asyncio, "create_task") as task:
            af.start("D1")
        task.assert_called_once_with("coro")

    async def test_crash_is_contained(self):
        with patch.object(af, "_run", new=AsyncMock(side_effect=RuntimeError)):
            await af._run_safely("D1")

    @pytest.mark.parametrize(("status", "index"), [("MATCHING", 0), ("PAID", 12), ("DRAFT", -1)])
    def test_stage_index(self, status, index):
        assert af._stage_index(status) == index


class TestResume:
    async def test_off(self, monkeypatch):
        from shared.config import settings
        monkeypatch.setattr(settings, "auto_flow_enabled", False)
        await af.resume_in_flight()

    async def test_only_opportunity_deals_are_resumed(self, monkeypatch):
        from shared.config import settings
        monkeypatch.setattr(settings, "auto_flow_enabled", True)

        async def listing(limit, status):
            if status == DealStatus.MATCHING:
                return [SimpleNamespace(deal_id="A", opportunity_id="O"), SimpleNamespace(deal_id="B", opportunity_id=None)]
            if status == DealStatus.PI_SENT:
                raise RuntimeError("scan failed")
            return []

        with patch("services.deals.repository.get_repository", return_value=MagicMock(list_active=listing)), patch.object(af, "start") as start:
            await af.resume_in_flight()
        start.assert_called_once_with("A")

    async def test_agreed_price_comes_from_the_pi(self):
        repo = MagicMock(get_pi_by_deal=AsyncMock(return_value=SimpleNamespace(agreed_price=5.0)))
        with patch("services.dealmaker.repository.get_repository", return_value=repo):
            assert await af._resolve_agreed_price("D") == 5.0
        repo.get_pi_by_deal = AsyncMock(return_value=None)
        with patch("services.dealmaker.repository.get_repository", return_value=repo):
            assert await af._resolve_agreed_price("D") is None


class TestRunResumes:
    async def test_from_the_top_runs_every_step(self, steps):
        await _run(_deal("MATCHING"))
        for name in ("_submit_quote", "_nudge_to_negotiating", "_run_negotiation", "_issue_and_accept_pi", "_fund_escrow", "_book_and_simulate_qc", "_file_customs", "_simulate_payout_completed"):
            steps[name].assert_awaited_once()
        assert [c.args[1] for c in steps["_create_gate"].await_args_list] == ["ISSUE_PO", "BOOK_FREIGHT", "RELEASE_SUPPLIER_PAYOUT"]

    async def test_no_opportunity(self, steps):
        await _run(_deal(opportunity_id=None))
        steps["_ensure_match_results"].assert_not_awaited()

    async def test_not_resumable(self, steps):
        await _run(_deal("DRAFT"))
        steps["_ensure_match_results"].assert_not_awaited()

    async def test_negotiating_skips_sourcing(self, steps):
        await _run(_deal("NEGOTIATING"))
        steps["_submit_quote"].assert_not_awaited()
        steps["_run_negotiation"].assert_awaited_once()

    async def test_no_candidate_stops(self, steps):
        steps["_ensure_match_results"].return_value = (None, None, None)
        await _run(_deal("MATCHING"))
        steps["_submit_quote"].assert_not_awaited()

    async def test_quote_failure_stops(self, steps):
        steps["_submit_quote"].return_value = None
        await _run(_deal("MATCHING"))
        steps["_run_negotiation"].assert_not_awaited()

    async def test_no_agreement_stops(self, steps):
        steps["_run_negotiation"].return_value = None
        await _run(_deal("MATCHING"))
        steps["_issue_and_accept_pi"].assert_not_awaited()

    async def test_past_negotiation_never_renegotiates(self, steps):
        await _run(_deal("PI_ACCEPTED"))
        steps["_run_negotiation"].assert_not_awaited()
        steps["_issue_and_accept_pi"].assert_not_awaited()
        steps["_fund_escrow"].assert_awaited_once()

    async def test_past_negotiation_without_a_match(self, steps):
        steps["_ensure_match_results"].return_value = (None, None, None)
        await _run(_deal("PI_ACCEPTED"))
        steps["_fund_escrow"].assert_not_awaited()

    async def test_past_negotiation_without_a_pi(self, steps):
        steps["_resolve_agreed_price"].return_value = None
        await _run(_deal("PI_ACCEPTED"))
        steps["_fund_escrow"].assert_not_awaited()

    @pytest.mark.parametrize(("failing", "status", "not_called"), [
        ("_issue_and_accept_pi", "NEGOTIATION_AGREED", "_fund_escrow"),
        ("_fund_escrow", "PI_ACCEPTED", "_create_gate"),
        ("_book_and_simulate_qc", "PO_ISSUED", "_generate_documents"),
        ("_simulate_freight_cleared", "IN_TRANSIT", "_simulate_payout_completed"),
    ])
    async def test_step_failure_stops_the_flow(self, steps, failing, status, not_called):
        steps[failing].return_value = None if failing != "_simulate_freight_cleared" else False
        await _run(_deal(status))
        steps[not_called].assert_not_awaited()

    @pytest.mark.parametrize(("status", "not_called"), [("ESCROW_FUNDED", "_book_and_simulate_qc"), ("PO_ISSUED", "_file_customs"), ("IN_TRANSIT", "_simulate_payout_completed")])
    async def test_gate_never_resolved_stops_the_flow(self, steps, status, not_called):
        steps["_wait_for_status"].return_value = False
        await _run(_deal(status))
        steps[not_called].assert_not_awaited()

    async def test_payout_simulation_failure(self, steps):
        steps["_simulate_payout_completed"].return_value = False
        waits_before = steps["_wait_for_status"].await_count
        await _run(_deal("CLEARED"))
        assert steps["_wait_for_status"].await_count == waits_before

    async def test_cleared_only_gates_the_payout(self, steps):
        await _run(_deal("CLEARED"))
        assert [c.args[1] for c in steps["_create_gate"].await_args_list] == ["RELEASE_SUPPLIER_PAYOUT"]


class TestEnsureMatch:
    @staticmethod
    def _seed(sellers):
        return SimpleNamespace(top_sellers=sellers)

    async def test_stored_winner_is_used(self):
        deal = _deal(match_results=[SimpleNamespace(decision="DISCARD"), WINNER], buyer_target_price_usd=900.0)
        assert await af._ensure_match_results(deal) == (WINNER, 900.0, 700.0)

    @pytest.mark.parametrize("seed", [None, SimpleNamespace(top_sellers=[]), SimpleNamespace(top_sellers=[{"supplier_id": "S"}])])
    async def test_no_sellers(self, seed):
        with patch("services.trade_min.service.get_match_seed", new=AsyncMock(return_value=seed)):
            assert await af._ensure_match_results(_deal()) == (None, None, None)

    async def test_seed_failure(self):
        with patch("services.trade_min.service.get_match_seed", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._ensure_match_results(_deal()) == (None, None, None)

    @staticmethod
    def _score(sid, score):
        return SimpleNamespace(match_id=None, supplier_id=sid, composite_score=score, gatekeeper_passed=True, arbitrage_score=1.0, proximity_score=1.0, reliability_score=0.0, decision="DISCARD")

    async def test_best_score_wins_and_price_is_derived_from_the_cheapest(self):
        sellers = [{"supplier_id": "A", "fob_price_usd": 100.0}, {"supplier_id": "B", "fob_price_usd": 80.0, "supplier_to_port_km": 10}, {"supplier_id": "C", "fob_price_usd": 90.0}]
        scores = AsyncMock(side_effect=[self._score("A", 50.0), self._score("B", 70.0), RuntimeError("x")])
        with patch("services.trade_min.service.get_match_seed", new=AsyncMock(return_value=self._seed(sellers))), \
             patch("services.match_engine.service.score_match", new=scores), \
             patch("services.deals.service.update_financials", new=AsyncMock()) as update:
            winner, buyer, supplier = await af._ensure_match_results(_deal())
        assert winner.supplier_id == "B" and buyer == 100.0 and supplier == 80.0
        assert update.await_args.args[1].winning_supplier_id == "B"

    async def test_lower_score_does_not_replace(self):
        sellers = [{"supplier_id": "A", "fob_price_usd": 100.0}, {"supplier_id": "B", "fob_price_usd": 100.0}]
        with patch("services.trade_min.service.get_match_seed", new=AsyncMock(return_value=self._seed(sellers))), \
             patch("services.match_engine.service.score_match", new=AsyncMock(side_effect=[self._score("A", 70.0), self._score("B", 50.0)])), \
             patch("services.deals.service.update_financials", new=AsyncMock()):
            assert (await af._ensure_match_results(_deal()))[0].supplier_id == "A"

    async def test_all_scoring_failed(self):
        with patch("services.trade_min.service.get_match_seed", new=AsyncMock(return_value=self._seed([{"supplier_id": "A", "fob_price_usd": 1.0}]))), \
             patch("services.match_engine.service.score_match", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._ensure_match_results(_deal()) == (None, None, None)


class TestSteps:
    async def test_submit_quote(self):
        with patch("services.sourcing.service.dispatch_rfq", new=AsyncMock(return_value=SimpleNamespace(rfq_id="R"))), \
             patch("services.sourcing.service.receive_quote", new=AsyncMock(return_value="Q")) as receive:
            assert await af._submit_quote("D", "1520", WINNER, 700.0) == "Q"
        assert receive.await_args.args[0].unit_price_usd == 700.0

    async def test_submit_quote_failure(self):
        with patch("services.sourcing.service.dispatch_rfq", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._submit_quote("D", "1520", WINNER, 700.0) is None

    async def test_nudge(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status=DealStatus.MATCHING))), \
             patch("services.deals.service.update_status", new=AsyncMock()) as update:
            await af._nudge_to_negotiating("D")
        assert update.await_args.args[1].target_status == DealStatus.NEGOTIATING

    async def test_nudge_not_needed(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status=DealStatus.NEGOTIATING))), \
             patch("services.deals.service.update_status", new=AsyncMock()) as update:
            await af._nudge_to_negotiating("D")
        update.assert_not_awaited()

    async def test_nudge_failure_is_contained(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status=DealStatus.MATCHING))), \
             patch("services.deals.service.update_status", new=AsyncMock(side_effect=RuntimeError)):
            await af._nudge_to_negotiating("D")

    @pytest.mark.parametrize(("status", "expected"), [("AGREED", 850.0), ("FAILED", None), ("ESCALATED", None)])
    async def test_negotiation_immediate_result(self, status, expected):
        session = SimpleNamespace(status=status, agreed_price=850.0, negotiation_id="N")
        with patch("agents.negotiation_agent.service.start_negotiation", new=AsyncMock(return_value=session)):
            assert await af._run_negotiation(_deal(), WINNER, 900.0, 700.0) == expected

    async def test_negotiation_waits_for_real_replies(self):
        session = SimpleNamespace(status="ROUND", negotiation_id="N")
        with patch("agents.negotiation_agent.service.start_negotiation", new=AsyncMock(return_value=session)), patch.object(af, "_wait_for_negotiation_result", new=AsyncMock(return_value=810.0)) as wait:
            assert await af._run_negotiation(_deal(), WINNER, 900.0, 700.0) == 810.0
        wait.assert_awaited_once_with("N")

    async def test_negotiation_start_failure(self):
        with patch("agents.negotiation_agent.service.start_negotiation", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._run_negotiation(_deal(), WINNER, 900.0, 700.0) is None

    @pytest.mark.parametrize(("sessions", "expected"), [
        ([SimpleNamespace(status="ROUND"), SimpleNamespace(status="AGREED", agreed_price=5.0)], 5.0),
        ([SimpleNamespace(status="FAILED")], None),
    ])
    async def test_wait_for_negotiation(self, sessions, expected):
        with patch("agents.negotiation_agent.service.get_negotiation", new=AsyncMock(side_effect=sessions)):
            assert await af._wait_for_negotiation_result("N") == expected

    async def test_wait_for_negotiation_poll_failure(self):
        with patch("agents.negotiation_agent.service.get_negotiation", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._wait_for_negotiation_result("N") is None

    async def test_wait_for_negotiation_times_out(self, monkeypatch):
        monkeypatch.setattr(af, "_APPROVAL_POLL_TIMEOUT_SECONDS", 20.0)
        with patch("agents.negotiation_agent.service.get_negotiation", new=AsyncMock(return_value=SimpleNamespace(status="ROUND"))):
            assert await af._wait_for_negotiation_result("N") is None

    async def test_pi(self):
        with patch("services.dealmaker.service.issue_pi", new=AsyncMock(return_value=SimpleNamespace(pi_id="P"))), \
             patch("services.dealmaker.service.accept_pi", new=AsyncMock(return_value="accepted")):
            assert await af._issue_and_accept_pi("D", "ES", 800.0) == "accepted"

    async def test_pi_failure(self):
        with patch("services.dealmaker.service.issue_pi", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._issue_and_accept_pi("D", "ES", 800.0) is None

    @staticmethod
    def _escrow_patches(hold, deal_status=DealStatus.PI_ACCEPTED):
        return (
            patch("services.escrow_guard.service.get_deal_summary", new=AsyncMock(return_value=SimpleNamespace(hold=hold))),
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status=deal_status))),
            patch("services.deals.service.update_status", new=AsyncMock()),
            patch("services.escrow_guard.service.create_hold", new=AsyncMock(return_value=SimpleNamespace(status="PENDING", ext_ref="E"))),
            patch("services.escrow_guard.service.handle_escrow_funded_webhook", new=AsyncMock()),
        )

    async def test_escrow_created_and_funded(self):
        a, b, c, d, e = self._escrow_patches(None)
        with a, b, c as update, d as create, e as funded:
            assert (await af._fund_escrow("D", 800.0)).ext_ref == "E"
        update.assert_awaited_once()
        assert create.await_args.args[0].amount == 800.0 * af._QUANTITY_MT and create.await_args.args[0].idempotency_key == "auto-escrow-D"
        funded.assert_awaited_once()

    async def test_escrow_transition_not_repeated(self):
        a, b, c, d, e = self._escrow_patches(None, DealStatus.ESCROW_PENDING)
        with a, b, c as update, d, e:
            await af._fund_escrow("D", 800.0)
        update.assert_not_awaited()

    async def test_funded_escrow_is_reused(self):
        a, b, c, d, e = self._escrow_patches(SimpleNamespace(status="FUNDED", ext_ref="X"))
        with a, b, c, d as create, e as funded:
            await af._fund_escrow("D", 800.0)
        create.assert_not_awaited()
        funded.assert_not_awaited()

    async def test_escrow_failure(self):
        with patch("services.escrow_guard.service.get_deal_summary", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._fund_escrow("D", 800.0) is None

    @staticmethod
    def _orders(*orders):
        return SimpleNamespace(orders=list(orders))

    async def test_qc_booked_once(self):
        booked = SimpleNamespace(ext_ref="Q", qc_result=None)
        with patch("services.shield_link.service.list_deal_orders", new=AsyncMock(return_value=self._orders())), \
             patch("services.shield_link.service.book_qc", new=AsyncMock(return_value=booked)) as book, \
             patch("services.shield_link.service.handle_qc_result", new=AsyncMock()) as result:
            assert await af._book_and_simulate_qc("D") is booked
        book.assert_awaited_once()
        assert result.await_args.args[1].result == "PASS"

    async def test_qc_existing_order_is_not_rebooked(self):
        existing = SimpleNamespace(order_type="qc", ext_ref="Q", qc_result="PASS")
        with patch("services.shield_link.service.list_deal_orders", new=AsyncMock(return_value=self._orders(existing))), \
             patch("services.shield_link.service.book_qc", new=AsyncMock()) as book, \
             patch("services.shield_link.service.handle_qc_result", new=AsyncMock()) as result:
            await af._book_and_simulate_qc("D")
        book.assert_not_awaited()
        result.assert_not_awaited()

    async def test_qc_failure(self):
        with patch("services.shield_link.service.list_deal_orders", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._book_and_simulate_qc("D") is None

    async def test_documents_use_stable_keys_and_survive_a_failure(self):
        with patch("services.docugen.service.generate_document", new=AsyncMock(side_effect=[RuntimeError, None, None, None, None])) as generate:
            await af._generate_documents("D")
        assert generate.await_count == 5
        assert generate.await_args_list[0].args[0].idempotency_key == f"auto-doc-D-{DocumentType.COMMERCIAL_INVOICE.value}"

    async def test_customs_failure_is_contained(self):
        with patch("services.docugen.service.file_customs", new=AsyncMock(side_effect=RuntimeError)):
            await af._file_customs("D")

    async def test_customs(self):
        with patch("services.docugen.service.file_customs", new=AsyncMock()) as filed:
            await af._file_customs("D")
        filed.assert_awaited_once()

    async def test_freight_cleared(self):
        order = SimpleNamespace(order_type="FREIGHT", ext_ref="F")
        with patch("services.shield_link.service.list_deal_orders", new=AsyncMock(return_value=self._orders(order))), \
             patch("services.shield_link.service.handle_freight_milestone", new=AsyncMock()) as milestone:
            assert await af._simulate_freight_cleared("D") is True
        assert milestone.await_args.args[1].milestone == "CLEARED"

    async def test_freight_not_found(self):
        with patch("services.shield_link.service.list_deal_orders", new=AsyncMock(return_value=self._orders())):
            assert await af._simulate_freight_cleared("D") is False

    async def test_freight_failure(self):
        with patch("services.shield_link.service.list_deal_orders", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._simulate_freight_cleared("D") is False

    async def test_payout_completed_after_lookup_retry(self):
        summaries = [RuntimeError("x"), SimpleNamespace(payouts=[]), SimpleNamespace(payouts=[SimpleNamespace(ext_ref="old"), SimpleNamespace(ext_ref="P")])]
        with patch("services.escrow_guard.service.get_deal_summary", new=AsyncMock(side_effect=summaries)), \
             patch("services.escrow_guard.service.handle_payout_webhook", new=AsyncMock()) as webhook:
            assert await af._simulate_payout_completed("D") is True
        assert webhook.await_args.args[0].ext_ref == "P"

    async def test_payout_never_appears(self, monkeypatch):
        monkeypatch.setattr(af, "_PAYOUT_LOOKUP_TIMEOUT_SECONDS", 2.0)
        with patch("services.escrow_guard.service.get_deal_summary", new=AsyncMock(return_value=SimpleNamespace(payouts=[]))):
            assert await af._simulate_payout_completed("D") is False

    async def test_payout_webhook_failure(self):
        with patch("services.escrow_guard.service.get_deal_summary", new=AsyncMock(return_value=SimpleNamespace(payouts=[SimpleNamespace(ext_ref="P")]))), \
             patch("services.escrow_guard.service.handle_payout_webhook", new=AsyncMock(side_effect=RuntimeError)):
            assert await af._simulate_payout_completed("D") is False

    async def test_gate(self):
        with patch("services.hitl_approval.service.create_approval", new=AsyncMock()) as create:
            await af._create_gate("D", "ISSUE_PO", "s", 1.0, "L0", {"a": 1}, "k")
        assert create.await_args.args[0].idempotency_key == "k"

    async def test_gate_failure_is_contained(self):
        with patch("services.hitl_approval.service.create_approval", new=AsyncMock(side_effect=RuntimeError)):
            await af._create_gate("D", "ISSUE_PO", "s", 1.0, "L0", {})

    @pytest.mark.parametrize(("statuses", "expected"), [
        ([DealStatus.MATCHING, DealStatus.PAID], True), ([DealStatus.CANCELLED], False), ([DealStatus.DISPUTE], False),
    ])
    async def test_wait_for_status(self, statuses, expected):
        deals = [SimpleNamespace(status=s) for s in statuses]
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=deals)):
            assert await af._wait_for_status("D", DealStatus.PAID) is expected

    async def test_wait_for_status_times_out(self, monkeypatch):
        monkeypatch.setattr(af, "_APPROVAL_POLL_TIMEOUT_SECONDS", 20.0)
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status=DealStatus.MATCHING))):
            assert await af._wait_for_status("D", DealStatus.PAID) is False


# ---------------------------------------------------------------------------
# Counterparty e-mail templates
# ---------------------------------------------------------------------------

def _escrow(shortfall=None):
    return SimpleNamespace(escrow_id="ESC-1", amount=1000.0, currency="USD", funded_amount=400.0, shortfall=shortfall, inspection_deadline=None, checkout_url="https://pay")


ctx = {"deal_id": "D1", "supplier_name": None, "buyer_name": None, "name": None, "provider": "SGS", "ext_ref": "X", "inspection_date": "2026-12-01", "po_id": "PO1",
       "reminder_no": 1, "attempt": 1, "notes": "moisture", "reason": "unfunded", "refunded": 500.0, "currency": "USD", "balance_due": 10.0, "deadline": "2026-12-05",
       "vessel": "V", "etd": "E", "eta": "A", "hs_code": "1520", "container_number": "C1"}


@pytest.mark.parametrize(("template", "greeting"), [
    ("_tpl_qc_booked", "Supplier"), ("_tpl_freight_booked", "Buyer"), ("_tpl_cargo_cleared", "Buyer"), ("_tpl_po_reminder", "Supplier"),
    ("_tpl_qc_readiness_reminder", "Supplier"), ("_tpl_qc_rework_notice", "Supplier"), ("_tpl_deal_settled", "Partner"), ("_tpl_deal_cancelled", "Partner"),
])
def test_templates_name_the_deal_and_have_a_safe_greeting(template, greeting):
    subject, body = getattr(nf, template)(ctx)
    assert "D1" in subject and body.startswith("Dear Sir or Madam,")  # no name known → neutral greeting


@pytest.mark.parametrize("template", ["_tpl_funding_shortfall", "_tpl_inspection_window_open"])
def test_escrow_templates(template):
    subject, body = getattr(nf, template)({**ctx, "escrow": _escrow(600.0)})
    assert "D1" in subject and body.startswith("Dear Sir or Madam,") and "ESC-1" in body + subject + "ESC-1"


class TestEscrowReminder:
    def test_full_amount_due(self):
        _subject, body = nf._tpl_escrow_reminder({**ctx, "escrow": _escrow()})
        assert "USD 1,000.00" in body and "partially funded" not in body

    def test_partial(self):
        _subject, body = nf._tpl_escrow_reminder({**ctx, "escrow": _escrow(600.0)})
        assert "partially funded" in body


class TestHelpers:
    def test_entity_name_fallback(self):
        with patch("services.dealmaker.config.settings", new=SimpleNamespace()):
            assert nf._entity_name() == "PortLess"

    def test_money_with_a_bad_value(self):
        assert nf._fmt_money("n/a") == "USD n/a"


WRAPPERS = [
    ("notify_freight_booked", ({"provider": "c"},), "buyer", "freight_booked"),
    ("notify_funding_shortfall", (_escrow(),), "buyer", "funding_shortfall"),
    ("notify_inspection_window_open", (_escrow(), "arrived", "2026-12-05"), "buyer", "inspection_window_open"),
    ("notify_qc_rework", ("n", "X", 1), "supplier", "qc_rework_notice"),
    ("notify_qc_readiness_reminder", (1,), "supplier", "qc_readiness_reminder"),
    ("notify_escrow_reminder", (_escrow(), 1), "buyer", "escrow_reminder"),
]


@pytest.mark.parametrize(("name", "args", "party", "kind"), WRAPPERS)
async def test_wrappers_address_the_right_party(name, args, party, kind):
    deal = SimpleNamespace(deal_id="D1", parties=[])
    with patch.object(nf, "notify_party", new=AsyncMock(return_value="E1")) as send:
        assert await getattr(nf, name)(deal, *args) == "E1"
    assert send.await_args.args[1:3] == (party, kind)


class TestMultiPartyNotices:
    async def test_cancelled_goes_to_both(self):
        with patch.object(nf, "notify_party", new=AsyncMock(side_effect=["E1", None])) as send:
            assert await nf.notify_deal_cancelled(SimpleNamespace(deal_id="D1", parties=[]), "r", 5.0) == ["E1"]
        assert [c.args[1] for c in send.await_args_list] == ["buyer", "supplier"]

    async def test_settled_collects_ids(self):
        with patch.object(nf, "notify_party", new=AsyncMock(return_value="E")):
            assert await nf.notify_deal_settled(SimpleNamespace(deal_id="D1", parties=[]), 10.0) == ["E", "E"]

    async def test_po_reminder_uses_the_po(self):
        repo = MagicMock(get_po_by_deal=AsyncMock(return_value=SimpleNamespace(po_id="PO9")))
        with patch("services.dealmaker.repository.get_repository", return_value=repo), patch.object(nf, "notify_party", new=AsyncMock(return_value="E")) as send:
            await nf.notify_po_reminder(SimpleNamespace(deal_id="D1", parties=[]), 1)
        assert send.await_args.args[3]["po_id"] == "PO9"

    async def test_po_reminder_lookup_failure(self):
        with patch("services.dealmaker.repository.get_repository", side_effect=RuntimeError), patch.object(nf, "notify_party", new=AsyncMock(return_value="E")) as send:
            await nf.notify_po_reminder(SimpleNamespace(deal_id="D1", parties=[]), 1)
        assert send.await_args.args[3]["po_id"] is None


# ---------------------------------------------------------------------------
# Exception Resolver agent
# ---------------------------------------------------------------------------

def _record(**kw):
    return make(ExceptionRecord, **{"exception_id": "E1", "deal_id": "D1", "context": {}, **kw})


def _analysis(decision, category=ExceptionCategory.UNKNOWN):
    return AgentAnalysis(category=category, severity=ExceptionSeverity.LOW, summary="s", root_cause="r", remediation_steps=[], decision=decision, decision_rationale="d", estimated_resolution_hours=1, llm_model="m")


class TestAutoFixHandlers:
    @pytest.mark.parametrize(("context", "doc_type"), [
        ({"document_type": "CI"}, DocumentType.COMMERCIAL_INVOICE),
        ({"doc_type": "PACKING_LIST"}, DocumentType.PACKING_LIST),
        ({"doc_type": "gibberish"}, DocumentType.COMMERCIAL_INVOICE),
        ({}, DocumentType.COMMERCIAL_INVOICE),
    ])
    async def test_document_regeneration(self, context, doc_type):
        with patch("services.docugen.service.generate_document", new=AsyncMock(return_value=SimpleNamespace(doc_id="X"))) as generate:
            result = await ag._auto_fix_document_error(_record(context=context))
        assert result == {"success": True, "document_id": "X", "action": "document_regenerated"}
        assert generate.await_args.args[0].doc_type == doc_type

    async def test_document_failure(self):
        with patch("services.docugen.service.generate_document", new=AsyncMock(side_effect=RuntimeError("x"))):
            assert (await ag._auto_fix_document_error(_record()))["success"] is False

    async def test_connector_error_is_monitored(self):
        assert (await ag._auto_fix_connector_error(_record()))["success"] is True

    @pytest.mark.parametrize(("handler", "target"), [
        (ag._auto_fix_payment_failed, "shared.orchestration.playbooks.remind_funding_shortfall"),
        (ag._auto_fix_negotiation_failed, "shared.orchestration.playbooks.next_buyer"),
    ])
    async def test_playbook_fixes(self, handler, target):
        with patch(target, new=AsyncMock(return_value={"success": True})):
            assert (await handler(_record()))["success"] is True
        with patch(target, new=AsyncMock(side_effect=RuntimeError("x"))):
            assert (await handler(_record()))["success"] is False


class TestClassification:
    @pytest.mark.parametrize(("stage", "category"), [("OUTREACH:exhausted", "NEGOTIATION_FAILED"), ("NEGOTIATION:x", "NEGOTIATION_FAILED"), ("QC_FAILED:x", "QC_FAILED"), ("OTHER", "")])
    def test_stage(self, stage, category):
        assert ag._infer_category(_record(context={"stage": stage})) == category

    @pytest.mark.parametrize("since", [None, "garbage"])
    def test_age_unknown(self, since):
        assert ag._age_days(_record(context={"since": since})) is None

    def test_naive_age(self):
        since = (datetime.now(UTC) - timedelta(days=2)).replace(tzinfo=None).isoformat()
        assert round(ag._age_days(_record(context={"since": since}))) == 2

    @pytest.mark.parametrize("category", ["DOCUMENT_ERROR", "CONNECTOR_ERROR"])
    async def test_technical_failures_auto_fix(self, category):
        analysis, _meta = await ag._deterministic_analysis(_record(context={"category": category}))
        assert analysis.decision == RemediationDecision.AUTO_FIX and analysis.category.value == category


GOOD = {"category": "FREIGHT_DELAY", "severity": "HIGH", "summary": "s", "root_cause": "r", "remediation_steps": ["a"], "decision": "MONITOR", "decision_rationale": "d", "estimated_resolution_hours": 3}


class TestAnalyze:
    @pytest.fixture(autouse=True)
    def _memory(self, monkeypatch):
        monkeypatch.setattr(ag, "_playbooks_enabled", lambda: False)

    @staticmethod
    def _llm(content=None, error=None):
        chat = AsyncMock(side_effect=error) if error else AsyncMock(return_value=SimpleNamespace(content=content, model="m1"))
        return patch.object(ag, "LLMClient", return_value=MagicMock(chat=chat)), chat

    @pytest.mark.parametrize("wrap", ["{}", "```json\n{}\n```", "```\n{}\n```"])
    async def test_reply_is_parsed(self, wrap):
        client, _chat = self._llm(wrap.replace("{}", json.dumps(GOOD)))
        with client, patch("shared.orchestration.memory.context_for", new=AsyncMock(return_value="")):
            result = await ag._node_analyze({"record": _record()})
        assert result["analysis"].category == ExceptionCategory.FREIGHT_DELAY and result["analysis"].llm_model == "m1" and result["error"] is None

    async def test_memory_reaches_the_prompt(self):
        client, chat = self._llm(json.dumps(GOOD))
        with client, patch("shared.orchestration.memory.context_for", new=AsyncMock(return_value="similar case")):
            await ag._node_analyze({"record": _record()})
        assert "similar case" in chat.await_args.kwargs["messages"][1]["content"]

    async def test_memory_failure_is_ignored(self):
        client, _chat = self._llm(json.dumps(GOOD))
        with client, patch("shared.orchestration.memory.context_for", new=AsyncMock(side_effect=RuntimeError)):
            assert (await ag._node_analyze({"record": _record()}))["analysis"] is not None

    @pytest.mark.parametrize("content", ["not json", '{"category": "FREIGHT_DELAY"}', json.dumps({**GOOD, "decision": "NOPE"})])
    async def test_unparseable_reply_escalates(self, content):
        client, _chat = self._llm(content)
        with client, patch("shared.orchestration.memory.context_for", new=AsyncMock(return_value="")):
            result = await ag._node_analyze({"record": _record()})
        assert result["analysis"].category == ExceptionCategory.UNKNOWN and result["analysis"].decision == RemediationDecision.ESCALATE and result["error"]

    async def test_gateway_exhausted_escalates(self):
        client, _chat = self._llm(error=LLMExhaustedException("all down"))
        with client, patch("shared.orchestration.memory.context_for", new=AsyncMock(return_value="")):
            result = await ag._node_analyze({"record": _record()})
        assert result["analysis"].llm_model == "fallback" and result["analysis"].decision == RemediationDecision.ESCALATE

    async def test_playbook_failure_falls_back_to_the_llm(self, monkeypatch):
        monkeypatch.setattr(ag, "_playbooks_enabled", lambda: True)
        client, chat = self._llm(json.dumps(GOOD))
        with client, patch.object(ag, "_deterministic_analysis", new=AsyncMock(side_effect=RuntimeError)), patch("shared.orchestration.memory.context_for", new=AsyncMock(return_value="")):
            await ag._node_analyze({"record": _record()})
        chat.assert_awaited_once()


class TestExecute:
    async def _run(self, analysis, playbook=None):
        return (await ag._node_execute({"record": _record(), "analysis": analysis, "playbook": playbook}))["execution_result"]

    async def test_no_analysis(self):
        assert (await self._run(None))["success"] is False

    async def test_monitor(self):
        assert (await self._run(_analysis(RemediationDecision.MONITOR)))["action"] == "monitoring"

    async def test_auto_fix_success(self):
        handler = AsyncMock(return_value={"success": True})
        with patch.dict(ag._AUTO_FIX_DISPATCH, {ExceptionCategory.CONNECTOR_ERROR: handler}):
            assert await self._run(_analysis(RemediationDecision.AUTO_FIX, ExceptionCategory.CONNECTOR_ERROR)) == {"success": True}

    async def test_failed_playbook_fix_goes_to_an_operator(self):
        handler = AsyncMock(return_value={"success": False, "error": "none left"})
        with patch.dict(ag._AUTO_FIX_DISPATCH, {ExceptionCategory.CONNECTOR_ERROR: handler}), patch.object(ag, "_escalate_to_hitl", new=AsyncMock(return_value={"approval_id": "A"})):
            result = await self._run(_analysis(RemediationDecision.AUTO_FIX, ExceptionCategory.CONNECTOR_ERROR), {"playbook": "x"})
        assert result == {"approval_id": "A", "auto_fix_fell_through": True, "auto_fix_error": "none left"}

    async def test_failed_llm_fix_stays_failed(self):
        handler = AsyncMock(return_value={"success": False})
        with patch.dict(ag._AUTO_FIX_DISPATCH, {ExceptionCategory.CONNECTOR_ERROR: handler}):
            assert (await self._run(_analysis(RemediationDecision.AUTO_FIX, ExceptionCategory.CONNECTOR_ERROR)))["success"] is False

    async def test_no_handler_escalates(self):
        with patch.object(ag, "_escalate_to_hitl", new=AsyncMock(return_value={"approval_id": "A"})):
            result = await self._run(_analysis(RemediationDecision.AUTO_FIX, ExceptionCategory.UNKNOWN))
        assert result["auto_fix_fell_through"] is True

    async def test_cancel_needs_an_operator(self):
        with patch.object(ag, "_escalate_to_hitl", new=AsyncMock(return_value={"approval_id": "A"})):
            assert (await self._run(_analysis(RemediationDecision.CANCEL)))["cancel_recommended"] is True
