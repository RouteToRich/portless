"""Smoke tests: every service app boots and exposes a healthy ``/health``."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

# (module path, health route) for each standalone service app.
SERVICE_APPS = [
    ("services.api_gateway.main", "/health"),
    ("services.deals.main", "/deals/health"),
    ("services.trade_min.main", "/trade-min/health"),
    ("services.match_engine.main", "/match-engine/health"),
    ("services.sourcing.main", "/sourcing/health"),
    ("agents.negotiation_agent.main", "/negotiation-agent/health"),
    ("services.dealmaker.main", "/dealmaker/health"),
    ("services.shield_link.main", "/shield-link/health"),
    ("services.docugen.main", "/docugen/health"),
    ("services.escrow_guard.main", "/escrow-guard/health"),
    ("services.hitl_approval.main", "/hitl-approval/health"),
    ("services.exception_resolver.main", "/exception-resolver/health"),
]


@pytest.mark.parametrize("module_path, health_route", SERVICE_APPS)
def test_service_health(module_path: str, health_route: str) -> None:
    module = importlib.import_module(module_path)
    client = TestClient(module.app)
    response = client.get(health_route)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
