"""Money gates approved only from the approved networks / registered devices; REJECT and
non-money gates are never restricted (shared/auth/money_network.py)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.hitl_approval.repository as repo_mod
from services.hitl_approval import service as svc
from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from services.hitl_approval.schemas import CreateApprovalRequest
from shared.auth import money_network


def test_address_rules(monkeypatch):
    monkeypatch.delenv("AUTH_MONEY_APPROVER_NETWORKS", raising=False)
    assert money_network.address_allowed("203.0.113.9")
    monkeypatch.setenv("AUTH_MONEY_APPROVER_NETWORKS", "10.8.0.0/16, 203.0.113.9")
    assert money_network.address_allowed("10.8.4.2") and money_network.address_allowed("203.0.113.9")
    assert not money_network.address_allowed("198.51.100.1") and not money_network.address_allowed("garbage")
    monkeypatch.setenv("AUTH_MONEY_APPROVER_NETWORKS", "not-a-network")
    assert not money_network.address_allowed("10.8.4.2")  # a typo fails closed


def test_device_rules(monkeypatch):
    monkeypatch.delenv("AUTH_MONEY_APPROVER_DEVICE_KEYS", raising=False)
    assert money_network.device_allowed(None)
    monkeypatch.setenv("AUTH_MONEY_APPROVER_DEVICE_KEYS", "laptop-cfo-7f3a,phone-founder-91bc")
    assert money_network.device_allowed("phone-founder-91bc")
    assert not money_network.device_allowed("") and not money_network.device_allowed("other")


@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.hitl_approval import router

    repo_mod._repository = InMemoryHitlApprovalRepository()

    async def none(*a, **k):
        return None

    async def approver(req, request):
        req.resolved_by = "cfo@portless.in"
        return True

    monkeypatch.setattr(svc, "execute_callback", none, raising=False)
    monkeypatch.setattr(router, "_apply_signed_in_approver", approver)
    app = FastAPI()
    app.include_router(router.router)
    from shared.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="cfo@portless.in", user_id="u", is_portless=True, org_id="o")
    yield TestClient(app)
    repo_mod._repository = None


def _gate(client, action):
    import anyio

    async def make():
        return await svc.create_approval(CreateApprovalRequest(deal_id="D1", action=action, summary="t", autonomy_level="L0",
                                                               financial_impact_usd=5000.0, callback_payload={}))
    return client.portal.call(make) if getattr(client, "portal", None) else anyio.run(make)


def test_route_refuses_money_approval_off_network_but_not_reject(client, monkeypatch):
    monkeypatch.setenv("AUTH_MONEY_APPROVER_NETWORKS", "10.8.0.0/16")
    with client:
        gate = _gate(client, "RELEASE_SUPPLIER_PAYOUT")
        r = client.post(f"/hitl-approval/approvals/{gate.approval_id}/resolve", json={"decision": "APPROVE", "resolved_by": "x"})
        assert r.status_code == 403 and "approved networks" in r.json()["detail"]
        other = _gate(client, "SEND_NEGOTIATION_EMAIL")
        assert client.post(f"/hitl-approval/approvals/{other.approval_id}/resolve", json={"decision": "APPROVE", "resolved_by": "x"}).status_code == 200
        r = client.post(f"/hitl-approval/approvals/{gate.approval_id}/resolve", json={"decision": "REJECT", "resolved_by": "x"})
        assert r.status_code == 200 and r.json()["status"] == "REJECTED"


def test_route_allows_money_approval_from_the_network(client, monkeypatch):
    monkeypatch.setenv("AUTH_MONEY_APPROVER_NETWORKS", "testclient, 127.0.0.1")
    monkeypatch.setattr(money_network, "request_address", lambda request: "127.0.0.1")
    with client:
        gate = _gate(client, "RELEASE_SUPPLIER_PAYOUT")
        r = client.post(f"/hitl-approval/approvals/{gate.approval_id}/resolve", json={"decision": "APPROVE", "resolved_by": "x"})
        assert r.status_code == 200
