"""The supplier's last payment waits for the buyer's acceptance.

Before this, a healthy margin let the supplier be paid in full at customs
clearance, with the goods still at sea and only the last tranche left to answer
a dispute. The holdback mirrors the buyer's acceptance tranche and is enforced
by Escrow-Guard itself, so no gate or API caller can pay past it.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.escrow_guard.repository import InMemoryEscrowGuardRepository
from services.escrow_guard.schemas import (
    AcceptEscrowRequest,
    CreateEscrowRequest,
    EscrowFundedWebhookPayload,
    PayoutRequest,
    ReleaseTrancheRequest,
)
from services.escrow_guard.service import (
    InvalidDealState,
    accept_escrow,
    create_escrow,
    get_deal_ledger,
    handle_escrow_funded_webhook,
    release_tranche,
    request_payout,
)
from shared.orchestration import continuations as c

DEAL = "DEAL-R1"
OWED = 40000.0  # principal 50,000 → T4 is 10 % → 4,000 held back


@pytest.fixture(autouse=True)
def _retention_on(monkeypatch):
    monkeypatch.setenv("SUPPLIER_RETENTION_ENABLED", "true")


@pytest.fixture(autouse=True)
def _repo():
    import services.escrow_guard.repository as repo_mod
    repo_mod._repository = InMemoryEscrowGuardRepository()
    repo_mod._motor_client = None
    yield
    repo_mod._repository = None


@pytest.fixture(autouse=True)
def _quiet_events():
    with patch("services.escrow_guard.service._emit", new=AsyncMock()):
        yield


def _deal(status):
    return SimpleNamespace(deal_id=DEAL, status=SimpleNamespace(value=status))


async def _escrow_released_to_t3():
    with (
        patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=_deal("PI_ACCEPTED"))),
        patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
    ):
        esc = await create_escrow(CreateEscrowRequest(deal_id=DEAL, provider="tazapay", amount=50000.0))
        await handle_escrow_funded_webhook(
            EscrowFundedWebhookPayload(ext_ref=esc.ext_ref, status="FUNDED", amount=esc.amount), {}, b"{}",
        )
    for tid in ("T1", "T2", "T3"):
        await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id=tid))
    return esc


async def _pay(amount, key, owed=OWED, kind="supplier_payout"):
    with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=_deal("CLEARED"))):
        return await request_payout(PayoutRequest(
            deal_id=DEAL, payee_id="SUP", amount=amount, idempotency_key=key, total_owed=owed, kind=kind,
        ))


class TestEscrowGuardEnforcement:
    @pytest.mark.asyncio
    async def test_full_payment_before_acceptance_is_refused(self):
        await _escrow_released_to_t3()
        with pytest.raises(InvalidDealState) as e:
            await _pay(OWED, "k1")  # the 44,550 net released could cover it
        assert "held back until the buyer accepts" in str(e.value)

    @pytest.mark.asyncio
    async def test_paying_up_to_the_holdback_works_one_cent_more_does_not(self):
        await _escrow_released_to_t3()
        paid = await _pay(36000.0, "k1")
        assert paid.status == "PROCESSING"
        with pytest.raises(InvalidDealState):
            await _pay(1.0, "k2", kind="supplier_balance")

    @pytest.mark.asyncio
    async def test_acceptance_frees_the_holdback(self):
        esc = await _escrow_released_to_t3()
        await _pay(36000.0, "k1")
        with patch("services.escrow_guard.service._patch_deal_realized_margin", new=AsyncMock()):
            await accept_escrow(esc.escrow_id, AcceptEscrowRequest())
        balance = await _pay(4000.0, "k2", kind="supplier_balance")
        assert balance.status == "PROCESSING"

    @pytest.mark.asyncio
    async def test_ledger_shows_the_holdback_then_releases_it(self):
        esc = await _escrow_released_to_t3()
        before = await get_deal_ledger_with_owed()
        assert before.retained_until_acceptance == pytest.approx(4000.0)
        assert before.available_for_payout == pytest.approx(36000.0)  # not the 44,550 net released
        with patch("services.escrow_guard.service._patch_deal_realized_margin", new=AsyncMock()):
            await accept_escrow(esc.escrow_id, AcceptEscrowRequest())
        after = await get_deal_ledger_with_owed()
        assert after.retained_until_acceptance == 0.0
        assert after.available_for_payout == pytest.approx(49500.0)

    @pytest.mark.asyncio
    async def test_unknown_po_value_cannot_be_held_back(self):
        await _escrow_released_to_t3()
        with patch("services.escrow_guard.service._supplier_owed", new=AsyncMock(return_value=None)):
            paid = await _pay(44550.0, "k1", owed=None)
        assert paid.status == "PROCESSING"

    @pytest.mark.asyncio
    async def test_switch_off_restores_full_payment(self, monkeypatch):
        monkeypatch.setenv("SUPPLIER_RETENTION_ENABLED", "false")
        await _escrow_released_to_t3()
        assert (await _pay(OWED, "k1")).status == "PROCESSING"


async def get_deal_ledger_with_owed():
    with patch("services.escrow_guard.service._supplier_owed", new=AsyncMock(return_value=OWED)):
        return await get_deal_ledger(DEAL)


def _gate_deal():
    return SimpleNamespace(
        deal_id=DEAL, status=SimpleNamespace(value="CLEARED"), market="DE", hs_code="1006",
        autonomy_profile=SimpleNamespace(value="L0"), winning_supplier_id="SUP-1", parties=[],
    )


async def _open_gate(**ledger):
    gate = AsyncMock()
    fields = dict(payouts_total=0.0, payouts_pending=0.0, released_total=45000.0, supplier_owed=OWED,
                  reconciled=True, available_for_payout=0.0, retained_until_acceptance=4000.0)
    fields.update(ledger)
    neg = AsyncMock()
    neg.get_session_by_deal = AsyncMock(return_value=SimpleNamespace(current_ask=1000.0, quantity_mt=40.0))
    with (
        patch("services.escrow_guard.service.get_deal_ledger",
              new=AsyncMock(return_value=SimpleNamespace(**fields))),
        patch("services.hitl_approval.service.create_approval", new=gate),
        patch("agents.negotiation_agent.repository.get_repository", return_value=neg),
        patch("services.dealmaker.repository.get_repository", return_value=AsyncMock(
            get_po_by_deal=AsyncMock(return_value=SimpleNamespace(total_value=OWED)))),
    ):
        created = await c._supplier_payout_gate(_gate_deal(), stage="t", first=True)
    return created, gate


class TestPayoutGate:
    @pytest.mark.asyncio
    async def test_holdback_limits_the_gate_and_says_so(self):
        created, gate = await _open_gate(available_for_payout=36000.0)
        req = gate.await_args.args[0]
        assert created and req.callback_payload["amount"] == 36000.0
        assert "supplier_retention_held" in req.risk_flags
        assert "partial_payout_capped_by_escrow" not in req.risk_flags
        assert "held back until the buyer accepts" in req.summary

    @pytest.mark.asyncio
    async def test_escrow_limit_keeps_its_own_flag(self):
        _, gate = await _open_gate(available_for_payout=15000.0)
        req = gate.await_args.args[0]
        assert "partial_payout_capped_by_escrow" in req.risk_flags
        assert "supplier_retention_held" not in req.risk_flags

    @pytest.mark.asyncio
    async def test_nothing_payable_until_acceptance_means_no_gate(self):
        created, gate = await _open_gate(available_for_payout=0.0, payouts_total=36000.0)
        assert created is False
        gate.assert_not_awaited()
