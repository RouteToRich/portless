"""services/escrow_guard/service.py to 100 % — the branches the flow tests do not reach: store and
provider failures, refusals for unknown escrows / tranches, the Escrow.com final milestone, the
provider's own payout check, the ledger's cost lines and the closed-deal margin check."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.escrow_guard.repository as repo_mod
from services.escrow_guard import service as svc
from services.escrow_guard.repository import InMemoryEscrowGuardRepository, get_repository
from services.escrow_guard.schemas import EscrowHoldRecord, TrancheSpec
from shared.connectors.payments import ConnectorError

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _repo():
    repo_mod._repository = InMemoryEscrowGuardRepository()
    svc._margin_alerted.clear()
    yield
    repo_mod._repository = None
    svc._margin_alerted.clear()


def _hold(**kw) -> EscrowHoldRecord:
    base = dict(escrow_id="ESC-1", deal_id="D1", provider="escrow_com", amount=10000.0, currency="USD",
                status="FUNDED", ext_ref="T-1", created_at=NOW, updated_at=NOW, funded_amount=10000.0, released_total=9000.0,
                tranches=[TrancheSpec(tranche_id="T1", trigger="PO_ISSUED", pct=0.9, amount=9000.0, status="RELEASED"),
                          TrancheSpec(tranche_id="T4", trigger="ACCEPTED", pct=0.1, amount=1000.0)])
    base.update(kw)
    return EscrowHoldRecord(**base)


# ── small helpers that never raise ─────────────────────────────────────────

async def test_helpers_swallow_failures():
    with patch("shared.orchestration.escrow_status.sync", side_effect=RuntimeError("x")):
        await svc._sync_status("D1")
    with patch("shared.orchestration.chargebacks.net_owed", side_effect=RuntimeError("x")):
        assert await svc._net_owed("D1", 500.0) == 500.0
    with patch("shared.orchestration.escrow_interest.income", side_effect=RuntimeError("x")):
        assert await svc._interest_income("D1") == 0.0
    with patch.object(svc, "_fetch_deal", side_effect=RuntimeError("gone")):
        assert await svc._deal_or_none("D1") is None


async def test_provider_choice_edges(monkeypatch):
    monkeypatch.setenv("ESCROW_COM_FEE_PCT", "not a number")
    assert svc.provider_fee_pct("escrow_com") is None
    monkeypatch.setenv("ESCROW_PROVIDER_BY_COST", "true")
    with patch("agents.negotiation_agent.repository.get_repository", side_effect=RuntimeError("x")), \
            patch.object(svc, "_fetch_deal", side_effect=RuntimeError("x")), \
            patch.object(svc, "cheapest_provider", return_value=None):
        provider, source = await svc.resolve_provider("D-NONE")
    assert source == "default"


async def test_create_escrow_returns_the_idempotent_hold():
    await get_repository().save_hold(_hold(idempotency_key="IK-1"))
    from services.escrow_guard.schemas import CreateEscrowRequest
    with patch.object(svc, "_assert_platform_running", new=AsyncMock()):
        out = await svc.create_escrow(CreateEscrowRequest(deal_id="D1", amount=10000.0, idempotency_key="IK-1"))
    assert out.escrow_id == "ESC-1"


# ── unknown escrows / tranches ─────────────────────────────────────────────

async def test_refusals_for_unknown_escrows_and_tranches():
    from services.escrow_guard.schemas import RefundRequest
    with pytest.raises(svc.HoldNotFoundError):
        await svc.request_refund("NOPE", RefundRequest(reason="buyer asked for the money back"))
    with pytest.raises(svc.HoldNotFoundError):
        await svc.poll_provider("NOPE")
    with pytest.raises(svc.HoldNotFoundError):
        await svc.confirm_tranche_release("NOPE", "T1", source="x")
    with pytest.raises(svc.HoldNotFoundError):
        await svc.confirm_release_by_staff("NOPE", "T1", provider_ref="R", reason="seen on dashboard", confirmed_by="ops")
    await get_repository().save_hold(_hold())
    with pytest.raises(svc.HoldNotFoundError):
        await svc.confirm_tranche_release("ESC-1", "T9", source="x")
    with pytest.raises(svc.HoldNotFoundError):
        await svc.confirm_release_by_staff("ESC-1", "T9", provider_ref="R", reason="seen on dashboard", confirmed_by="ops")
    assert (await svc.confirm_tranche_release("ESC-1", "T1", source="x")).escrow_id == "ESC-1"  # already released
    out = await svc.confirm_release_by_staff("ESC-1", "T1", provider_ref="R", reason="seen on dashboard", confirmed_by="ops")
    assert out.escrow_id == "ESC-1"
    with pytest.raises(svc.HoldNotFoundError):
        await svc.handle_provider_webhook("stripe", {}, b"{}")


# ── provider reads ─────────────────────────────────────────────────────────

async def test_poll_provider_edges():
    await get_repository().save_hold(_hold(provider="stripe"))
    assert (await svc.poll_provider("ESC-1")).provider == "stripe"  # not a neutral escrow: nothing to read
    await get_repository().save_hold(_hold(escrow_id="ESC-2", provider="tazapay"))
    conn = MagicMock(status=AsyncMock(side_effect=ConnectorError("503")))
    with patch.object(svc, "_payment_connector", return_value=conn):
        with pytest.raises(svc.ConnectorCallError):
            await svc.poll_provider("ESC-2")


async def test_poll_provider_closed_unfunded_escrow():
    await get_repository().save_hold(_hold(escrow_id="ESC-3", provider="tazapay", status="PENDING", funded_amount=None,
                                           tranches=[]))
    conn = MagicMock(status=AsyncMock(return_value={}), interpret_status=MagicMock(return_value={"cancelled": True}))
    events = []
    with patch.object(svc, "_payment_connector", return_value=conn), \
            patch.object(svc, "_emit", new=AsyncMock(side_effect=lambda t, d, p: events.append(p))):
        hold = await svc.poll_provider("ESC-3")
    assert hold.status == "FAILED" and events[-1]["reason"] == "escrow_closed_unfunded"


async def test_poll_open_escrows_skips_and_survives():
    repo = get_repository()
    await repo.save_hold(_hold(escrow_id="A", provider="stripe", status="PENDING"))
    await repo.save_hold(_hold(escrow_id="B", provider="tazapay", status="PENDING", ext_ref="TB"))
    await repo.save_hold(_hold(escrow_id="C", provider="tazapay", status="PENDING", ext_ref="TC",
                               provider_checked_at=NOW - timedelta(minutes=1)))
    with patch.object(svc, "poll_provider", new=AsyncMock(side_effect=RuntimeError("down"))):
        assert await svc.poll_open_escrows(NOW) == 0


# ── Escrow.com final milestone ─────────────────────────────────────────────

async def test_submit_final_to_provider_paths():
    done = _hold(tranches=[TrancheSpec(tranche_id="T4", trigger="ACCEPTED", pct=1.0, amount=1000.0, status="RELEASED")])
    assert await svc._submit_final_to_provider(done, "x") is done
    hold = _hold()
    with patch.object(svc, "_release_final_tranches", side_effect=svc.ConnectorCallError("refused")):
        assert await svc._submit_final_to_provider(hold, "x") is hold
    paid = [hold.tranches[0], hold.tranches[1].model_copy(update={"status": "RELEASED"})]
    with patch.object(svc, "_release_final_tranches", new=AsyncMock(return_value=(paid, 10000.0, True))), \
            patch.object(svc, "_apply_final_release", new=AsyncMock(return_value="BOOKED")) as booked:
        assert await svc._submit_final_to_provider(hold, "x") == "BOOKED"
    booked.assert_awaited()
    with patch.object(svc, "_release_final_tranches", new=AsyncMock(return_value=(list(hold.tranches), 9000.0, False))):
        assert await svc._submit_final_to_provider(hold, "x") is hold  # nothing submitted


# ── ledger: PortLess costs and recorded fees ───────────────────────────────

async def test_portless_costs_edges():
    session = SimpleNamespace(quantity_mt=0, cost_sheet={"quantity_mt": 0, "lines": []})
    with patch("agents.negotiation_agent.repository.get_repository",
               return_value=SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session))), \
            patch.object(svc, "_recorded_fees", new=AsyncMock()):
        assert await svc._portless_costs("D1", 10000.0) == ({}, {})
    sheet = {"quantity_mt": 20, "inspection_phases": 2,
             "lines": [{"key": "inspection", "paid_by": "PortLess", "usd_per_mt": 10.0, "pct_of_price": 0.0}]}
    session = SimpleNamespace(quantity_mt=20, cost_sheet=sheet)
    orders = SimpleNamespace(orders=[SimpleNamespace(status="FAILED", order_type="QC", provider_payload={}),
                                     SimpleNamespace(status="BOOKED", order_type="QC", provider_payload={})])
    with patch("agents.negotiation_agent.repository.get_repository",
               return_value=SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session))), \
            patch("services.shield_link.service.list_deal_orders", new=AsyncMock(return_value=orders)), \
            patch("services.dealmaker.repository.get_repository",
                  return_value=SimpleNamespace(get_po_by_deal=AsyncMock(return_value=None))), \
            patch.object(svc, "_recorded_fees", new=AsyncMock()):
        costs, basis = await svc._portless_costs("D1", 8000.0)
    assert costs == {"inspection": 100.0} and basis == {"inspection": "estimate"}  # one of two planned visits
    with patch("agents.negotiation_agent.repository.get_repository",
               return_value=SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session))), \
            patch("services.shield_link.service.list_deal_orders", side_effect=RuntimeError("down")), \
            patch("services.dealmaker.repository.get_repository",
                  return_value=SimpleNamespace(get_po_by_deal=AsyncMock(return_value=SimpleNamespace()))), \
            patch.object(svc, "_recorded_fees", new=AsyncMock()):
        costs, _ = await svc._portless_costs("D1", 8000.0)
    assert costs == {}


async def test_recorded_fees_failure_and_without_bank_or_payout():
    costs, basis = {}, {}
    with patch("shared.orchestration.fee_check.recorded", side_effect=RuntimeError("x")):
        await svc._recorded_fees("D1", 0.0, costs, basis)
    with patch("shared.orchestration.fee_check.recorded", new=AsyncMock(return_value=[{"kind": "escrow", "amount_usd": 5.0}])):
        await svc._recorded_fees("D1", 10.0, costs, basis)
    assert "bank_charges" not in costs and "payout_fees" not in costs


# ── closed-deal margin check ───────────────────────────────────────────────

async def test_closed_margin_check_reads_owed_and_survives_a_missing_deal():
    hold = _hold(tranches=[TrancheSpec(tranche_id="T1", trigger="PO_ISSUED", pct=1.0, amount=10000.0, status="RELEASED")])
    payouts = [SimpleNamespace(total_owed=None, status="COMPLETED", amount=9800.0)]
    with patch.object(svc, "_supplier_owed", new=AsyncMock(return_value=9800.0)), \
            patch.object(svc, "_payout_totals", return_value=(9800.0, 0.0)), \
            patch("services.deals.service.get_deal", side_effect=RuntimeError("gone")), \
            patch.object(svc, "_emit", new=AsyncMock()) as emit:
        assert await svc._check_closed_margin("D1", hold, payouts, 100.0, 1.0, {}) is True
    emit.assert_awaited()


# ── watcher: another instance holds the lease ──────────────────────────────

async def test_inspection_watcher_skips_without_the_lease():
    calls = {"n": 0}

    async def sleep(_):
        calls["n"] += 1
        if calls["n"] > 1:
            raise svc.asyncio.CancelledError()  # the shutdown path ends the loop
    with patch.object(svc.asyncio, "sleep", new=sleep), \
            patch("shared.leader_lock.acquire", new=AsyncMock(return_value=False)), \
            patch.object(svc, "check_inspection_windows", new=AsyncMock()) as check:
        await svc.run_inspection_watcher()
    check.assert_not_awaited()


# ── remaining flow branches ────────────────────────────────────────────────

async def test_create_escrow_with_a_new_idempotency_key_checks_the_deal():
    await get_repository().save_hold(_hold())
    from services.escrow_guard.schemas import CreateEscrowRequest
    with patch.object(svc, "_assert_platform_running", new=AsyncMock()):
        out = await svc.create_escrow(CreateEscrowRequest(deal_id="D1", amount=10000.0, idempotency_key="NEW-KEY"))
    assert out.escrow_id == "ESC-1"  # the deal already has a live escrow


async def test_portless_costs_survive_a_broken_session_store():
    with patch("agents.negotiation_agent.repository.get_repository", side_effect=RuntimeError("down")), \
            patch.object(svc, "_recorded_fees", new=AsyncMock()):
        assert await svc._portless_costs("D1", 10000.0) == ({}, {})


def test_payer_from_escrow_com_without_a_buyer_name():
    with patch.object(svc, "_buyer_details", return_value={}):
        assert svc._payer_from_provider(_hold(), SimpleNamespace()) == {}


async def test_webhook_body_must_be_json():
    conn = MagicMock()
    with patch.object(svc, "_payment_connector", return_value=conn), \
            patch.object(svc, "signature_valid", new=AsyncMock(return_value=True)):
        with pytest.raises(svc.WebhookVerifyError, match="not JSON"):
            await svc.handle_provider_webhook("tazapay", {}, b"not json")


async def test_poll_open_escrows_reads_a_stale_one():
    await get_repository().save_hold(_hold(escrow_id="OLD", provider="tazapay", status="PENDING", ext_ref="TO",
                                           provider_checked_at=NOW - timedelta(hours=5)))
    with patch.object(svc, "poll_provider", new=AsyncMock(return_value=None)) as poll:
        assert await svc.poll_open_escrows(NOW) == 1
    poll.assert_awaited_once()


async def test_payout_never_pays_more_than_owed():
    from services.escrow_guard.schemas import PayoutRequest
    await get_repository().save_hold(_hold(released_total=10000.0, tranches=[
        TrancheSpec(tranche_id="T1", trigger="PO_ISSUED", pct=1.0, amount=10000.0, status="RELEASED")]))
    with patch.object(svc, "_assert_platform_running", new=AsyncMock()), \
            patch.object(svc, "_fetch_deal", new=AsyncMock(return_value=SimpleNamespace(status="CLEARED"))), \
            patch.object(svc, "_refuse_if_compliance_blocked", new=AsyncMock()), \
            patch.object(svc, "_net_owed", new=AsyncMock(return_value=5000.0)):
        with pytest.raises(svc.InvalidDealState, match="more than it is owed"):
            await svc.request_payout(PayoutRequest(deal_id="D1", payee_id="SUP", amount=6000.0, idempotency_key="P1"))


async def test_accept_escrow_when_the_milestone_is_already_with_the_provider():
    from services.escrow_guard.schemas import AcceptEscrowRequest
    hold = _hold(tranches=[TrancheSpec(tranche_id="T1", trigger="PO_ISSUED", pct=0.9, amount=9000.0, status="RELEASED"),
                           TrancheSpec(tranche_id="T4", trigger="ACCEPTED", pct=0.1, amount=1000.0, status="SUBMITTED")])
    await get_repository().save_hold(hold)
    with patch.object(svc, "_assert_platform_running", new=AsyncMock()), \
            patch.object(svc, "_refuse_if_compliance_blocked", new=AsyncMock()), \
            patch.object(svc, "poll_provider", new=AsyncMock(return_value=hold)):
        out = await svc.accept_escrow("ESC-1", AcceptEscrowRequest(accepted_by="buyer"))
    assert out.escrow_id == "ESC-1"
    assert (await get_repository().get_hold("ESC-1")).buyer_accepted_at is not None


async def test_accept_escrow_submits_the_final_milestone_and_reads_the_provider():
    from services.escrow_guard.schemas import AcceptEscrowRequest
    hold = _hold()
    await get_repository().save_hold(hold)
    submitted = [hold.tranches[0], hold.tranches[1].model_copy(update={"status": "SUBMITTED"}),
                 TrancheSpec(tranche_id="T5", trigger="CLEARED", pct=0.0, amount=0.0, status="SUBMITTED")]
    events = []
    with patch.object(svc, "_assert_platform_running", new=AsyncMock()), \
            patch.object(svc, "_refuse_if_compliance_blocked", new=AsyncMock()), \
            patch.object(svc, "_release_final_tranches", new=AsyncMock(return_value=(submitted, 9000.0, False))), \
            patch.object(svc, "_emit", new=AsyncMock(side_effect=lambda t, d, p: events.append(p))), \
            patch.object(svc, "poll_provider", new=AsyncMock(return_value=hold)):
        await svc.accept_escrow("ESC-1", AcceptEscrowRequest(accepted_by="buyer"))
    assert [e["tranche_id"] for e in events] == ["T4"]


async def test_window_lapse_waits_for_the_provider_payout():
    hold = _hold(inspection_deadline=NOW - timedelta(days=1))
    await get_repository().save_hold(hold)
    submitted = [hold.tranches[0], hold.tranches[1].model_copy(update={"status": "SUBMITTED"})]
    events = []
    with patch("shared.orchestration.kill_switch.is_paused", new=AsyncMock(return_value=False)),             patch.object(svc, "_refuse_if_compliance_blocked", new=AsyncMock()),             patch.object(svc, "_release_final_tranches", new=AsyncMock(return_value=(submitted, 9000.0, False))),             patch.object(svc, "_emit", new=AsyncMock(side_effect=lambda t, d, p: events.append(p))):
        assert await svc.check_inspection_windows(NOW) == 0
    assert events[-1]["event"] == "window_lapsed"
    assert (await get_repository().get_hold("ESC-1")).tranches[1].status == "SUBMITTED"


async def test_payout_completed_partly_with_the_retention_lookup_failing():
    from services.escrow_guard.schemas import PayoutRecord, PayoutWebhookPayload
    from tests.factories import make
    repo = get_repository()
    payout = make(PayoutRecord, payout_id="PO-1", deal_id="D1", ext_ref="X-1", amount=4000.0, status="PROCESSING",
                  total_owed=9000.0, processed_events=[])
    await repo.save_payout(payout)
    with patch.object(svc, "signature_valid", new=AsyncMock(return_value=True)), \
            patch.object(svc, "_net_owed", new=AsyncMock(return_value=9000.0)), \
            patch.object(repo, "get_hold_by_deal", new=AsyncMock(side_effect=RuntimeError("store down"))), \
            patch.object(svc, "_emit", new=AsyncMock()) as emit:
        out = await svc.handle_payout_webhook(PayoutWebhookPayload(ext_ref="X-1", status="COMPLETED"), {}, b"{}")
    assert out["payout_id"] == "PO-1" and emit.await_count >= 1


async def test_release_claim_is_given_back_when_the_call_is_cancelled():
    from services.escrow_guard.schemas import ReleaseTrancheRequest
    hold = _hold(released_total=0.0, tranches=[TrancheSpec(tranche_id="T1", trigger="PO_ISSUED", pct=0.9, amount=9000.0),
                                               TrancheSpec(tranche_id="T4", trigger="ACCEPTED", pct=0.1, amount=1000.0)])
    repo = get_repository()
    await repo.save_hold(hold)
    conn = MagicMock(release=AsyncMock(side_effect=KeyboardInterrupt()))
    with patch.object(svc, "_assert_platform_running", new=AsyncMock()), \
            patch.object(svc, "_refuse_if_compliance_blocked", new=AsyncMock()), \
            patch.object(svc, "_deal_status", new=AsyncMock(return_value="PO_ISSUED")), \
            patch.object(svc, "_payment_connector", return_value=conn), \
            patch.object(svc, "_release_note", new=AsyncMock(return_value="note")):
        with pytest.raises(KeyboardInterrupt):
            await svc.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T1", proof_refs=["PO"]))
    assert await repo.claim_tranche("ESC-1", "T1", "again") is True  # the claim was released


async def test_mark_submitted_keeps_the_submit_when_the_provider_read_fails():
    hold = _hold(released_total=0.0, tranches=[TrancheSpec(tranche_id="T1", trigger="PO_ISSUED", pct=1.0, amount=10000.0)])
    await get_repository().save_hold(hold)
    with patch.object(svc, "_emit", new=AsyncMock()), \
            patch.object(svc, "poll_provider", new=AsyncMock(side_effect=RuntimeError("provider down"))):
        out = await svc._mark_submitted(hold, ["T1"], {"documents": ["doc-1"]})
    assert out.tranches[0].status == "SUBMITTED" and out.tranches[0].provider_documents == ["doc-1"]


async def test_last_proof_tranche_opens_the_window_and_delivers_the_final_milestone(monkeypatch):
    monkeypatch.setattr(svc, "_window_trigger", lambda: "proof")
    hold = _hold(released_total=0.0, tranches=[TrancheSpec(tranche_id="T1", trigger="PO_ISSUED", pct=0.9, amount=9000.0),
                                               TrancheSpec(tranche_id="T4", trigger="ACCEPTED", pct=0.1, amount=1000.0)])
    await get_repository().save_hold(hold)
    with patch.object(svc, "_emit", new=AsyncMock()), \
            patch.object(svc, "buyer_accepts_at_provider", return_value=True), \
            patch.object(svc, "_submit_final_to_provider", new=AsyncMock(side_effect=lambda h, r: h)) as final, \
            patch.object(svc, "_settle_if_complete", new=AsyncMock(side_effect=lambda h, n: h)):
        out = await svc._apply_tranche_release(hold, "T1", ext_ref="R1", proof_refs=["PO"], source="provider")
    final.assert_awaited_once()
    assert out.inspection_deadline is not None


async def test_portless_costs_actual_fees_and_estimates():
    sheet = {"quantity_mt": 20, "lines": [
        {"key": "ocean_freight", "paid_by": "PortLess", "usd_per_mt": 50.0, "pct_of_price": 0.0},
        {"key": "marine_insurance", "paid_by": "PortLess", "usd_per_mt": 0.0, "pct_of_price": 0.5}]}
    session = SimpleNamespace(quantity_mt=20, cost_sheet=sheet)
    orders = SimpleNamespace(orders=[SimpleNamespace(status="BOOKED", order_type="freight",
                                                     provider_payload={"total_freight_usd": 1200.0})])
    with patch("agents.negotiation_agent.repository.get_repository",
               return_value=SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session))), \
            patch("services.shield_link.service.list_deal_orders", new=AsyncMock(return_value=orders)), \
            patch("services.dealmaker.repository.get_repository",
                  return_value=SimpleNamespace(get_po_by_deal=AsyncMock(return_value=None))), \
            patch.object(svc, "_recorded_fees", new=AsyncMock()):
        costs, basis = await svc._portless_costs("D1", 10000.0)
    assert costs["ocean_freight"] == 1200.0 and basis["ocean_freight"] == "actual"
    assert basis["marine_insurance"] == "estimate"
