"""Roles inside staff: money approver and compliance officer (todo: roles inside staff)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.auth import roles


def test_everyone_holds_both_roles_until_the_lists_are_set(monkeypatch):
    monkeypatch.delenv("AUTH_MONEY_APPROVER_EMAILS", raising=False)
    monkeypatch.delenv("AUTH_COMPLIANCE_OFFICER_EMAILS", raising=False)
    assert roles.roles_of("ops@portless.in") == ["trade_desk", "money_approver", "compliance_officer"]


def test_named_roles(monkeypatch):
    monkeypatch.setenv("AUTH_MONEY_APPROVER_EMAILS", "cfo@portless.in")
    monkeypatch.setenv("AUTH_COMPLIANCE_OFFICER_EMAILS", "co@portless.in")
    assert roles.roles_of("cfo@portless.in") == ["trade_desk", "money_approver"]
    assert roles.roles_of("CO@portless.in") == ["trade_desk", "compliance_officer"]
    assert roles.roles_of("ops@portless.in") == ["trade_desk"]
    with pytest.raises(roles.RoleRequired, match="compliance officer"):
        roles.require_compliance_officer(SimpleNamespace(email="ops@portless.in"), "Clearing a hold")
    roles.require_compliance_officer(SimpleNamespace(email="co@portless.in"), "Clearing a hold")


def test_compliance_gates_need_an_officer(monkeypatch):
    from services.hitl_approval.service import may_approve
    monkeypatch.setenv("AUTH_COMPLIANCE_OFFICER_EMAILS", "co@portless.in")
    monkeypatch.delenv("AUTH_MONEY_APPROVER_EMAILS", raising=False)
    assert not may_approve("CONFIRM_TRADE_COMPLIANCE", "ops@portless.in")
    assert may_approve("CONFIRM_COUNTERPARTY_RISK", "co@portless.in")
    assert may_approve("ISSUE_PO", "ops@portless.in")


def test_clearing_a_sanctions_match_needs_an_officer_failing_does_not(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.trade_min.router import _staff, router
    monkeypatch.setenv("AUTH_COMPLIANCE_OFFICER_EMAILS", "co@portless.in")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@portless.in", is_portless=True)
    c = TestClient(app)
    r = c.post("/trade-min/buyers/buyer:x.ae/screening-review", json={"decision": "clear", "reason": "not listed"})
    assert r.status_code == 403 and "compliance officer" in r.json()["detail"]
    r = c.post("/trade-min/buyers/buyer:x.ae/screening-review", json={"decision": "fail", "reason": "listed entity"})
    assert r.status_code == 404  # passes the role check; the company simply is not in this directory
