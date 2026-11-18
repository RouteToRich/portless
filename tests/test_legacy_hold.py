"""The legacy Stripe hold is never offered unless switched on (ESCROW_LEGACY_STRIPE_HOLD), the
provider choice never lands on Stripe, and readiness flags it in production."""
from __future__ import annotations

import pytest

from services.escrow_guard import service
from services.escrow_guard.schemas import EscrowHoldRequest


async def test_hold_refused_when_off(monkeypatch):
    monkeypatch.setenv("ESCROW_LEGACY_STRIPE_HOLD", "false")
    with pytest.raises(service.LegacyHoldDisabled, match="neutral escrow"):
        await service.create_hold(EscrowHoldRequest.model_construct(deal_id="D1"))


def test_route_answers_410(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.escrow_guard import router
    from shared.auth.dependencies import get_current_user

    monkeypatch.setenv("ESCROW_LEGACY_STRIPE_HOLD", "false")
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="ops", user_id="u", is_portless=True, org_id="o")
    r = TestClient(app).post("/escrow-guard/holds", json={"deal_id": "D1", "amount": 1000.0})
    assert r.status_code == 410 and "neutral escrow" in r.json()["detail"]


async def test_provider_choice_never_stripe(monkeypatch):
    monkeypatch.setattr(service, "_fetch_deal", lambda deal_id: _none())
    provider, source = await service.resolve_provider("D-none", "stripe")
    assert provider in ("tazapay", "escrow_com") and source == "default"


async def _none():
    return None


def test_readiness_flags_legacy_hold(monkeypatch):
    from shared import readiness
    monkeypatch.setenv("ENV_PROFILE", "production")
    monkeypatch.setenv("ESCROW_LEGACY_STRIPE_HOLD", "true")
    assert {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}["legacy_stripe_hold"] == "fail"
    monkeypatch.setenv("ESCROW_LEGACY_STRIPE_HOLD", "false")
    assert {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}["legacy_stripe_hold"] == "ok"
