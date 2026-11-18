"""Money approvers: only named staff approve money-moving gates (todo: roles)."""
from __future__ import annotations

import pytest

import services.hitl_approval.repository as repo_mod
from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from services.hitl_approval.schemas import CreateApprovalRequest, ResolveApprovalRequest
from services.hitl_approval import service as svc


@pytest.fixture(autouse=True)
def _repo(monkeypatch):
    repo_mod._repository = InMemoryHitlApprovalRepository()
    monkeypatch.setattr(svc, "execute_callback", lambda *a, **k: _none(), raising=False)
    yield
    repo_mod._repository = None


async def _none():
    return None


async def _gate(action, amount=5000.0):
    return await svc.create_approval(CreateApprovalRequest(deal_id="D1", action=action, summary="t", autonomy_level="L0",
                                                           financial_impact_usd=amount, callback_payload={}))


def test_off_without_the_list(monkeypatch):
    monkeypatch.delenv("AUTH_MONEY_APPROVER_EMAILS", raising=False)
    assert svc.may_approve("RELEASE_SUPPLIER_PAYOUT", "ops@portless.in")


def test_only_listed_people_approve_money(monkeypatch):
    monkeypatch.setenv("AUTH_MONEY_APPROVER_EMAILS", "cfo@portless.in, Founder@portless.in")
    assert svc.may_approve("RELEASE_SUPPLIER_PAYOUT", "founder@portless.in")
    assert not svc.may_approve("RELEASE_ESCROW_TRANCHE", "ops@portless.in")
    assert svc.may_approve("SEND_NEGOTIATION_EMAIL", "ops@portless.in")  # not money: any staff
    assert not svc.may_approve("REFUND_ESCROW", None)


async def test_resolve_refuses_an_operator_and_keeps_the_gate_open(monkeypatch):
    monkeypatch.setenv("AUTH_MONEY_APPROVER_EMAILS", "cfo@portless.in")
    gate = await _gate("RELEASE_SUPPLIER_PAYOUT")
    with pytest.raises(svc.NotMoneyApprover):
        await svc.resolve_approval(gate.approval_id, ResolveApprovalRequest(decision="APPROVE", resolved_by="ops@portless.in"))
    assert (await svc.get_approval(gate.approval_id)).status == "PENDING"
    # an operator may still reject
    out = await svc.resolve_approval(gate.approval_id, ResolveApprovalRequest(decision="REJECT", resolved_by="ops@portless.in"))
    assert out.status == "REJECTED"
