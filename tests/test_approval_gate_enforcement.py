"""Money and gated statuses cannot be reached around their approval.

The saga runs release, payout and acceptance in process from the approved gate.
Over HTTP those routes only carry out a gate that was approved for exactly that
money, and a status recording money or a provider's confirmation cannot be set
by hand.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.deals.router import router as deals_router
from services.deals.schemas import DealRecord
from services.docugen import service as docugen
from services.docugen.schemas import CustomsFilingRequest
from services.escrow_guard import service as escrow
from services.escrow_guard.repository import InMemoryEscrowGuardRepository
from services.escrow_guard.router import router as escrow_router
from services.escrow_guard.schemas import AcceptEscrowRequest, EscrowHoldRecord, EscrowResponse, PayoutResponse, TrancheSpec
from services.hitl_approval.service import ApprovalNotFoundError
from shared.auth.dependencies import AuthUser, get_current_user
from shared.domain.enums import DealStatus
from tests.factories import NOW, make

RELEASE = "/escrow-guard/escrows/ESC-1/release"
ACCEPT = "/escrow-guard/escrows/ESC-1/accept"
PAYOUT = "/escrow-guard/payouts"
RELEASE_BODY = {"tranche_id": "T1"}
ACCEPT_BODY = {}
PAYOUT_BODY = {"deal_id": "D1", "payee_id": "S", "amount": 100.0, "idempotency_key": "k1"}
RELEASE_GATE = ("RELEASE_ESCROW_TRANCHE", {"escrow_id": "ESC-1", "tranche_id": "T1"})
ACCEPT_GATE = ("CONFIRM_BUYER_ACCEPTANCE", {"escrow_id": "ESC-1"})
PAYOUT_GATE = ("RELEASE_SUPPLIER_PAYOUT", {"deal_id": "D1", "amount": 100.0, "idempotency_key": "k1"})


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(escrow_router)
    return TestClient(app, raise_server_exceptions=False)


def _gate(action, payload, status="APPROVED"):
    return AsyncMock(return_value=SimpleNamespace(action=action, status=status, callback_payload=payload))


def _post(api, url, body, gate, target):
    with (
        patch("services.hitl_approval.service.get_approval", new=gate),
        patch(f"services.escrow_guard.router.{target[0]}", new=target[1]) as moved,
    ):
        return api.post(url, json=body), moved


def _targets():
    escrow_result = AsyncMock(return_value=make(EscrowResponse))
    payout_result = AsyncMock(return_value=make(PayoutResponse))
    return {
        "release": (RELEASE, RELEASE_BODY, RELEASE_GATE, ("release_tranche", escrow_result), 200),
        "accept": (ACCEPT, ACCEPT_BODY, ACCEPT_GATE, ("accept_escrow", escrow_result), 200),
        "payout": (PAYOUT, PAYOUT_BODY, PAYOUT_GATE, ("request_payout", payout_result), 201),
    }


@pytest.mark.parametrize("route", ["release", "accept", "payout"])
def test_a_direct_call_is_refused(api, route):
    url, body, (action, payload), target, _ = _targets()[route]
    response, moved = _post(api, url, body, _gate(action, payload), target)
    assert response.status_code == 403
    moved.assert_not_awaited()


@pytest.mark.parametrize("route", ["release", "accept", "payout"])
def test_the_approved_gate_for_this_money_can_be_carried_out(api, route):
    url, body, (action, payload), target, expected = _targets()[route]
    response, moved = _post(api, url, {**body, "approval_id": "APPR-1"}, _gate(action, payload), target)
    assert response.status_code == expected
    moved.assert_awaited_once()


@pytest.mark.parametrize("route", ["release", "accept", "payout"])
@pytest.mark.parametrize("status", ["PENDING", "REJECTED", "TIMED_OUT"])
def test_a_gate_that_is_not_approved_is_refused(api, route, status):
    url, body, (action, payload), target, _ = _targets()[route]
    response, moved = _post(api, url, {**body, "approval_id": "APPR-1"}, _gate(action, payload, status), target)
    assert response.status_code == 403
    moved.assert_not_awaited()


@pytest.mark.parametrize("route", ["release", "accept", "payout"])
def test_a_gate_for_another_action_is_refused(api, route):
    url, body, (_, payload), target, _ = _targets()[route]
    response, moved = _post(api, url, {**body, "approval_id": "APPR-1"}, _gate("ISSUE_PO", payload), target)
    assert response.status_code == 403
    moved.assert_not_awaited()


def test_an_unknown_gate_is_refused(api):
    url, body, _, target, _ = _targets()["release"]
    missing = AsyncMock(side_effect=ApprovalNotFoundError("NOPE"))
    response, moved = _post(api, url, {**body, "approval_id": "NOPE"}, missing, target)
    assert response.status_code == 403
    moved.assert_not_awaited()


@pytest.mark.parametrize(("route", "changed"), [
    ("release", {"tranche_id": "T2"}),
    ("payout", {"amount": 99999.0}),
    ("payout", {"deal_id": "OTHER"}),
    ("payout", {"idempotency_key": "another-key"}),
])
def test_an_approval_covers_only_the_money_it_named(api, route, changed):
    url, body, (action, payload), target, _ = _targets()[route]
    response, moved = _post(api, url, {**body, **changed, "approval_id": "APPR-1"}, _gate(action, payload), target)
    assert response.status_code == 403
    moved.assert_not_awaited()


# ---------------------------------------------------------------------------
# Operator-relayed acceptance
# ---------------------------------------------------------------------------

def _hold(**overrides):
    tranches = [
        TrancheSpec(tranche_id="T3", trigger="CLEARED", pct=0.9, amount=900.0, status="RELEASED"),
        TrancheSpec(tranche_id="T4", trigger="ACCEPTED", pct=0.1, amount=100.0, status="PENDING"),
    ]
    values = {"escrow_id": "ESC-1", "deal_id": "D1", "provider": "tazapay", "status": "FUNDED", "amount": 1000.0,
              "currency": "USD", "tranches": tranches, "idempotency_key": "e1"}
    values.update(overrides)
    return make(EscrowHoldRecord, **values)


@pytest.fixture
async def stored_repo():
    import services.escrow_guard.repository as repo_mod
    repo_mod._repository = InMemoryEscrowGuardRepository()
    yield repo_mod._repository
    repo_mod._repository = None


async def _request(repo, hold, pending=()):
    await repo.save_hold(hold)
    create = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-9", status="PENDING"))
    with (
        patch("services.hitl_approval.service.create_approval", new=create),
        patch("services.hitl_approval.service.list_approvals", new=AsyncMock(return_value=list(pending))),
    ):
        result = await escrow.request_buyer_acceptance("ESC-1", AcceptEscrowRequest(accepted_by="ops@x.invalid"))
    return result, create


async def test_relayed_acceptance_opens_the_gate_and_moves_nothing(stored_repo):
    result, create = await _request(stored_repo, _hold())
    assert result["approval_id"] == "APPR-9"
    req = create.await_args.args[0]
    assert req.action == "CONFIRM_BUYER_ACCEPTANCE" and req.financial_impact_usd == 100.0
    assert req.callback_payload["escrow_id"] == "ESC-1"
    assert (await stored_repo.get_hold("ESC-1")).tranches[1].status == "PENDING"


async def test_relayed_acceptance_reuses_a_waiting_gate(stored_repo):
    waiting = SimpleNamespace(action="CONFIRM_BUYER_ACCEPTANCE", approval_id="APPR-OLD", status="PENDING")
    result, create = await _request(stored_repo, _hold(), pending=[waiting])
    assert result["approval_id"] == "APPR-OLD"
    create.assert_not_awaited()


@pytest.mark.parametrize("change", [
    {"disputed": True},
    {"status": "PENDING"},
])
async def test_relayed_acceptance_needs_a_live_escrow(stored_repo, change):
    with pytest.raises(escrow.InvalidDealState):
        await _request(stored_repo, _hold(**change))


async def test_relayed_acceptance_needs_a_pending_final_tranche(stored_repo):
    released = [TrancheSpec(tranche_id="T4", trigger="ACCEPTED", pct=1.0, amount=1000.0, status="RELEASED")]
    with pytest.raises(escrow.InvalidDealState):
        await _request(stored_repo, _hold(tranches=released))


async def test_relayed_acceptance_for_an_unknown_escrow(stored_repo):
    with pytest.raises(escrow.HoldNotFoundError):
        await escrow.request_buyer_acceptance("NOPE", AcceptEscrowRequest())


# ---------------------------------------------------------------------------
# Status changes by hand
# ---------------------------------------------------------------------------

@pytest.fixture
def deals_api():
    app = FastAPI()
    app.include_router(deals_router)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="U1", email="s@x.invalid", is_portless=True)
    return TestClient(app, raise_server_exceptions=False)


def _set_status(deals_api, target, hold=None):
    record = make(DealRecord, deal_id="D1", org_id="O", status=DealStatus(target), created_at=NOW, updated_at=NOW)
    moved = AsyncMock(return_value=record)
    repo = SimpleNamespace(get_hold_by_deal=AsyncMock(return_value=hold))
    with (
        patch("services.deals.router._org_deal", new=AsyncMock(return_value=record)),
        patch("services.deals.router.service.update_status", new=moved),
        patch("services.escrow_guard.repository.get_repository", return_value=repo),
    ):
        return deals_api.patch("/deals/D1/status", json={"target_status": target}), moved


@pytest.mark.parametrize("target", [
    "NEGOTIATION_AGREED", "PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING", "ESCROW_FUNDED", "PO_ISSUED",
    "SUPPLIER_DISPATCH_READY", "QC_PASSED", "QC_FAILED", "IN_TRANSIT", "CLEARED", "PAID", "REFUNDED",
])
def test_a_gated_status_cannot_be_set_by_hand(deals_api, target):
    response, moved = _set_status(deals_api, target)
    assert response.status_code == 422
    assert "not by hand" in response.json()["detail"]
    moved.assert_not_awaited()


@pytest.mark.parametrize("target", ["MATCHING", "NEGOTIATING", "NEGOTIATION_FAILED", "DISPUTE", "CANCELLED"])
def test_operator_statuses_still_work(deals_api, target):
    response, moved = _set_status(deals_api, target)
    assert response.status_code == 200
    moved.assert_awaited_once()


def test_a_deal_holding_buyer_money_cannot_be_cancelled_by_hand(deals_api):
    response, moved = _set_status(deals_api, "CANCELLED", hold=SimpleNamespace(funded_amount=500.0, refunded_total=0.0))
    assert response.status_code == 422 and "playbook" in response.json()["detail"]
    moved.assert_not_awaited()


def test_a_fully_refunded_deal_can_be_cancelled(deals_api):
    response, moved = _set_status(deals_api, "CANCELLED", hold=SimpleNamespace(funded_amount=500.0, refunded_total=500.0))
    assert response.status_code == 200
    moved.assert_awaited_once()


async def test_customs_filing_for_an_unknown_deal_is_reported_as_not_found():
    repo = SimpleNamespace(get=AsyncMock(return_value=None), get_by_deal_and_type=AsyncMock(return_value=None))
    with (
        patch.object(docugen, "get_repository", return_value=repo),
        patch.object(docugen, "_fetch_deal", new=AsyncMock(side_effect=docugen.DealNotFoundError("NOPE"))),
        pytest.raises(docugen.DealNotFoundError),
    ):
        await docugen.file_customs(CustomsFilingRequest(deal_id="NOPE"))
