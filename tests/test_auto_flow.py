"""Tests for shared.orchestration.auto_flow: resume_in_flight() and _run()'s
own resume-from-current-status branching.

Covers the restart-recovery gap: auto_flow's per-deal task is a bare
asyncio.create_task with no persistence of its own (see auto_flow.py's module
docstring). resume_in_flight() is the startup-time fix — it re-arms the task
for every opportunity-seeded deal still sitting at any non-terminal pipeline
status (not just MATCHING/NEGOTIATING — see _RESUMABLE_STATUSES), instead of
leaving those deals permanently orphaned after a restart. _run() itself must
then resume from wherever the deal actually is rather than always replaying
from the top, or steps like _submit_quote (mints a fresh RFQ) and
_create_gate (creates a HITL approval) would duplicate real side effects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import DealRecord, DealStatus
from shared.orchestration import auto_flow


def _deal(deal_id: str, status: DealStatus, opportunity_id: str | None) -> DealRecord:
    now = datetime.now(timezone.utc)
    return DealRecord(
        deal_id=deal_id,
        operator_id="OP-1",
        org_id="ORG-1",
        hs_code="090422",
        market="US",
        status=status,
        opportunity_id=opportunity_id,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def _repo(monkeypatch) -> InMemoryFallbackRepository:
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository.get_repository", lambda: repo)
    # services/deals/service.py does `from .repository import get_repository`
    # at import time, binding its own local name — patching the repository
    # module alone doesn't reach that already-bound reference, so get_deal()
    # (used by _run()) needs the service module patched too.
    monkeypatch.setattr("services.deals.service.get_repository", lambda: repo)
    return repo


async def test_resume_starts_matching_and_negotiating_opportunity_deals(_repo):
    await _repo.save(_deal("DEAL-1", DealStatus.MATCHING, "OPP-1"))
    await _repo.save(_deal("DEAL-2", DealStatus.NEGOTIATING, "OPP-2"))

    with patch("shared.orchestration.auto_flow.enabled", return_value=True):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    started = {call.args[0] for call in mock_start.call_args_list}
    assert started == {"DEAL-1", "DEAL-2"}


async def test_resume_starts_deals_stuck_past_negotiation(_repo):
    """A deal orphaned mid-pipeline (e.g. at SUPPLIER_DISPATCH_READY when the
    server restarted) must be re-armed too — not just MATCHING/NEGOTIATING."""
    await _repo.save(_deal("DEAL-ESCROW", DealStatus.ESCROW_FUNDED, "OPP-1"))
    await _repo.save(_deal("DEAL-DISPATCH", DealStatus.SUPPLIER_DISPATCH_READY, "OPP-2"))
    await _repo.save(_deal("DEAL-TRANSIT", DealStatus.IN_TRANSIT, "OPP-3"))

    with patch("shared.orchestration.auto_flow.enabled", return_value=True):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    started = {call.args[0] for call in mock_start.call_args_list}
    assert started == {"DEAL-ESCROW", "DEAL-DISPATCH", "DEAL-TRANSIT"}


async def test_resume_skips_deals_without_opportunity_id(_repo):
    await _repo.save(_deal("DEAL-MANUAL", DealStatus.NEGOTIATING, None))

    with patch("shared.orchestration.auto_flow.enabled", return_value=True):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    mock_start.assert_not_called()


async def test_resume_skips_terminal_and_pre_pipeline_statuses(_repo):
    """PAID (terminal success) and DRAFT (before MATCHING) are never resumed —
    same for the other terminal/exception-routed statuses not in
    _RESUMABLE_STATUSES (NEGOTIATION_FAILED, QC_FAILED, DISPUTE, CANCELLED,
    REFUNDED): those are handled by the Exception Resolver, not auto_flow."""
    await _repo.save(_deal("DEAL-DONE", DealStatus.PAID, "OPP-3"))
    await _repo.save(_deal("DEAL-DRAFT", DealStatus.DRAFT, "OPP-4"))
    await _repo.save(_deal("DEAL-FAILED", DealStatus.NEGOTIATION_FAILED, "OPP-5"))
    await _repo.save(_deal("DEAL-QCFAIL", DealStatus.QC_FAILED, "OPP-6"))

    with patch("shared.orchestration.auto_flow.enabled", return_value=True):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    mock_start.assert_not_called()


async def test_resume_is_noop_when_auto_flow_disabled(_repo):
    await _repo.save(_deal("DEAL-1", DealStatus.NEGOTIATING, "OPP-1"))

    with patch("shared.orchestration.auto_flow.enabled", return_value=False):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    mock_start.assert_not_called()


async def test_resume_continues_past_a_scan_failure(_repo, caplog):
    """A repository error for one status must not block scanning the other."""
    await _repo.save(_deal("DEAL-OK", DealStatus.NEGOTIATING, "OPP-1"))

    original_list_active = _repo.list_active

    async def _flaky_list_active(*args, **kwargs):
        if kwargs.get("status") == DealStatus.MATCHING:
            raise RuntimeError("boom")
        return await original_list_active(*args, **kwargs)

    with patch.object(_repo, "list_active", side_effect=_flaky_list_active):
        with patch("shared.orchestration.auto_flow.enabled", return_value=True):
            with patch("shared.orchestration.auto_flow.start") as mock_start:
                await auto_flow.resume_in_flight()

    mock_start.assert_called_once_with("DEAL-OK")


# ---------------------------------------------------------------------------
# _run() resume-from-current-status branching
# ---------------------------------------------------------------------------

def _match_result(supplier_id: str = "SUP-1") -> "object":
    from services.deals.schemas import MatchResult
    return MatchResult(
        match_id="MATCH-1", supplier_id=supplier_id, company_name="Acme Supplier",
        composite_score=80.0, gatekeeper_passed=True, arbitrage_score=30.0,
        proximity_score=25.0, reliability_score=25.0, decision="SOURCE",
        fob_price_usd=1000.0,
    )


def _dispatch_ready_deal(deal_id: str) -> DealRecord:
    return _deal(deal_id, DealStatus.SUPPLIER_DISPATCH_READY, "OPP-1").model_copy(update={
        "match_results": [_match_result()],
        "buyer_target_price_usd": 1255.0,
    })


def _pi_record(deal_id: str, pi_id: str, agreed_price: float = 1004.0) -> "object":
    from services.dealmaker.schemas import PiRecord
    now = datetime.now(timezone.utc)
    return PiRecord(
        pi_id=pi_id, deal_id=deal_id, agreed_price=agreed_price, currency="USD",
        quantity_mt=25.0, total_value=agreed_price * 25.0, incoterm="FOB",
        port_of_destination="Chennai", payment_terms="30/70", validity_days=30,
        buyer_name="Acme Buyer Inc", buyer_address="123 Trade St", buyer_vat_number=None,
        issued_at=now, expires_at=now,
    )


async def test_run_resumes_past_negotiation_without_replaying_earlier_steps(monkeypatch, _repo):
    """A deal orphaned at SUPPLIER_DISPATCH_READY (its auto_flow task died on
    a restart) must, on resume, skip sourcing/negotiation/PI/escrow/the
    ISSUE_PO gate entirely — those already happened — and only run the
    remaining tail (_book_and_simulate_qc onward; that call is itself
    responsible for detecting QC already passed and no-op'ing — see
    test_book_and_simulate_qc_skips_rebooking_when_already_passed). Regression:
    before this fix _run() always replayed from the top, which would mint a
    duplicate RFQ (_submit_quote) and a duplicate ISSUE_PO approval
    (_create_gate had no idempotency_key)."""
    deal = _dispatch_ready_deal("DEAL-1")
    await _repo.save(deal)

    pi = _pi_record("DEAL-1", "PI-1")

    submit_quote = AsyncMock()
    run_negotiation = AsyncMock()
    issue_and_accept_pi = AsyncMock()
    fund_escrow = AsyncMock()
    book_and_simulate_qc = AsyncMock(return_value={"ext_ref": "QC-1"})
    generate_documents = AsyncMock()
    create_gate = AsyncMock()
    wait_for_status = AsyncMock(return_value=True)
    file_customs = AsyncMock()
    simulate_freight_cleared = AsyncMock(return_value=True)
    simulate_payout_completed = AsyncMock(return_value=True)

    monkeypatch.setattr(auto_flow, "_submit_quote", submit_quote)
    monkeypatch.setattr(auto_flow, "_run_negotiation", run_negotiation)
    monkeypatch.setattr(auto_flow, "_issue_and_accept_pi", issue_and_accept_pi)
    monkeypatch.setattr(auto_flow, "_fund_escrow", fund_escrow)
    monkeypatch.setattr(auto_flow, "_book_and_simulate_qc", book_and_simulate_qc)
    monkeypatch.setattr(auto_flow, "_generate_documents", generate_documents)
    monkeypatch.setattr(auto_flow, "_create_gate", create_gate)
    monkeypatch.setattr(auto_flow, "_wait_for_status", wait_for_status)
    monkeypatch.setattr(auto_flow, "_file_customs", file_customs)
    monkeypatch.setattr(auto_flow, "_simulate_freight_cleared", simulate_freight_cleared)
    monkeypatch.setattr(auto_flow, "_simulate_payout_completed", simulate_payout_completed)

    with patch("services.dealmaker.repository.get_repository") as mock_pi_repo:
        mock_pi_repo.return_value.get_pi_by_deal = AsyncMock(return_value=pi)
        await auto_flow._run("DEAL-1")

    submit_quote.assert_not_awaited()
    run_negotiation.assert_not_awaited()
    issue_and_accept_pi.assert_not_awaited()
    fund_escrow.assert_not_awaited()
    book_and_simulate_qc.assert_awaited_once_with("DEAL-1")
    generate_documents.assert_awaited_once_with("DEAL-1")

    # Only BOOK_FREIGHT and RELEASE_SUPPLIER_PAYOUT gates — NOT ISSUE_PO
    # (that step, and everything before it, is already done).
    gate_actions = [call.args[1] for call in create_gate.call_args_list]
    assert gate_actions == ["BOOK_FREIGHT", "RELEASE_SUPPLIER_PAYOUT"]


async def test_book_and_simulate_qc_skips_rebooking_when_already_passed(monkeypatch):
    """The real _book_and_simulate_qc (not mocked): resuming after a restart
    where QC was already booked AND passed (qc_result is set) must not call
    book_qc or handle_qc_result again."""
    from services.shield_link.schemas import DealOrdersResponse, ThirdPartyOrderResponse
    from datetime import datetime, timezone

    existing_tpo = ThirdPartyOrderResponse(
        tpo_id="TPO-1", deal_id="DEAL-1", order_type="QC", provider="sgs",
        status="COMPLETED", ext_ref="SGS-1", qc_result="PASS",
        created_at=datetime.now(timezone.utc),
    )

    with patch("services.shield_link.service.list_deal_orders") as mock_list, \
         patch("services.shield_link.service.book_qc") as mock_book, \
         patch("services.shield_link.service.handle_qc_result") as mock_handle:
        mock_list.return_value = DealOrdersResponse(deal_id="DEAL-1", orders=[existing_tpo])
        result = await auto_flow._book_and_simulate_qc("DEAL-1")

    mock_book.assert_not_called()
    mock_handle.assert_not_called()
    assert result is existing_tpo


async def test_book_and_simulate_qc_completes_interrupted_webhook(monkeypatch):
    """Resuming where QC was booked (deal already at SUPPLIER_DISPATCH_READY)
    but qc_result is still None — the process died during the simulated
    webhook's sleep — must finish that webhook, not re-book."""
    from services.shield_link.schemas import DealOrdersResponse, ThirdPartyOrderResponse
    from datetime import datetime, timezone

    booked_no_result = ThirdPartyOrderResponse(
        tpo_id="TPO-1", deal_id="DEAL-1", order_type="QC", provider="sgs",
        status="BOOKED", ext_ref="SGS-1", qc_result=None,
        created_at=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(auto_flow, "_WEBHOOK_SIM_DELAY_SECONDS", 0.0)
    with patch("services.shield_link.service.list_deal_orders") as mock_list, \
         patch("services.shield_link.service.book_qc") as mock_book, \
         patch("services.shield_link.service.handle_qc_result") as mock_handle:
        mock_list.return_value = DealOrdersResponse(deal_id="DEAL-1", orders=[booked_no_result])
        await auto_flow._book_and_simulate_qc("DEAL-1")

    mock_book.assert_not_called()
    mock_handle.assert_awaited_once()
    assert mock_handle.await_args.args[0] == "SGS-1"


async def test_run_resumes_at_escrow_pending_reuses_existing_hold(monkeypatch, _repo):
    """Resuming at ESCROW_PENDING must not create a second escrow hold —
    _fund_escrow checks get_deal_summary().hold first."""
    deal = _deal("DEAL-2", DealStatus.ESCROW_PENDING, "OPP-1").model_copy(update={
        "match_results": [_match_result()],
        "buyer_target_price_usd": 1255.0,
    })
    await _repo.save(deal)

    from services.escrow_guard.schemas import DealEscrowSummary, EscrowResponse

    pi = _pi_record("DEAL-2", "PI-2")
    existing_hold = EscrowResponse(
        escrow_id="ESC-1", deal_id="DEAL-2", provider="stripe_treasury",
        amount=25100.0, currency="usd", status="FUNDED", ext_ref="stub-1",
        created_at=datetime.now(timezone.utc),
    )

    create_gate = AsyncMock()
    wait_for_status = AsyncMock(return_value=True)
    monkeypatch.setattr(auto_flow, "_create_gate", create_gate)
    monkeypatch.setattr(auto_flow, "_wait_for_status", wait_for_status)
    monkeypatch.setattr(auto_flow, "_book_and_simulate_qc", AsyncMock(return_value={"ext_ref": "QC-1"}))
    monkeypatch.setattr(auto_flow, "_generate_documents", AsyncMock())
    monkeypatch.setattr(auto_flow, "_file_customs", AsyncMock())
    monkeypatch.setattr(auto_flow, "_simulate_freight_cleared", AsyncMock(return_value=True))
    monkeypatch.setattr(auto_flow, "_simulate_payout_completed", AsyncMock(return_value=True))

    with patch("services.dealmaker.repository.get_repository") as mock_pi_repo, \
         patch("services.escrow_guard.service.create_hold") as mock_create_hold, \
         patch("services.escrow_guard.service.get_deal_summary") as mock_summary, \
         patch("services.escrow_guard.service.handle_escrow_funded_webhook") as mock_webhook:
        mock_pi_repo.return_value.get_pi_by_deal = AsyncMock(return_value=pi)
        mock_summary.return_value = DealEscrowSummary(deal_id="DEAL-2", hold=existing_hold)
        await auto_flow._run("DEAL-2")

    mock_create_hold.assert_not_called()
    mock_webhook.assert_not_called()  # already FUNDED — no need to re-simulate

    gate_actions = [call.args[1] for call in create_gate.call_args_list]
    assert gate_actions == ["ISSUE_PO", "BOOK_FREIGHT", "RELEASE_SUPPLIER_PAYOUT"]


def test_stage_index_rejects_pre_pipeline_status():
    assert auto_flow._stage_index("DRAFT") == -1
    assert auto_flow._stage_index("PAID") == len(auto_flow._STAGE_ORDER) - 1
    assert auto_flow._stage_index("MATCHING") == 0
