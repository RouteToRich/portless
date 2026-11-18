"""Deals router: the thin routes over the orchestration modules answer and map errors
(coverage of services/deals/router.py — todo: bring product-code coverage back to 95 %)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.deals.repository import InMemoryFallbackRepository
from services.deals.router import router
from services.deals.schemas import DealRecord, DealStatus
from shared.auth.dependencies import get_current_user, require_portless
from tests.factories import make

STAFF = SimpleNamespace(user_id="u-ops", email="ops@portless.test", name="Ops", org_id=None, is_portless=True)


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", r)
    return r


@pytest.fixture
def api(repo):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_portless] = lambda: STAFF
    app.dependency_overrides[get_current_user] = lambda: STAFF
    return TestClient(app, raise_server_exceptions=False)


async def _deal(repo, status=DealStatus.MATCHING, **kw):
    d = make(DealRecord, deal_id=kw.pop("deal_id", "DEAL-R1"), hs_code="10063020", market="AE", status=status,
             org_id="portless", **kw)
    await repo.save(d)
    return d


def test_lists_answer_from_their_modules(api, monkeypatch):
    from shared import deal_costs
    from shared.orchestration import funnel, money_glance, price_alerts, prospect_cleanup, prospect_desk, reorders
    monkeypatch.setattr(money_glance, "rows", AsyncMock(return_value={"rows": []}))
    monkeypatch.setattr(funnel, "build", AsyncMock(return_value={"days": 30}))
    monkeypatch.setattr(prospect_cleanup, "stale", AsyncMock(return_value=[{"deal_id": "D1"}]))
    monkeypatch.setattr(prospect_cleanup, "run", AsyncMock(return_value={"closed": ["D1"], "failed": []}))
    monkeypatch.setattr(deal_costs, "all_summaries", AsyncMock(return_value=[{"deal_id": "D1"}]))
    monkeypatch.setattr(reorders, "due", AsyncMock(return_value=[]))
    monkeypatch.setattr(price_alerts, "alerts", AsyncMock(return_value=[{"hs_code": "1006"}]))
    monkeypatch.setattr(prospect_desk, "backfill_radar_estimates", AsyncMock(return_value={"filled": 0}))
    assert api.get("/deals/board/money").json() == {"rows": []}
    assert api.get("/deals/funnel?days=30").json() == {"days": 30}
    assert api.get("/deals/prospects/cleanup").json() == [{"deal_id": "D1"}]
    assert api.post("/deals/prospects/cleanup").json()["closed"] == ["D1"]
    assert api.get("/deals/run-costs").json() == [{"deal_id": "D1"}]
    assert api.get("/deals/reorders/due").json() == []
    assert api.get("/deals/price-alerts").json() == [{"hs_code": "1006"}]
    assert api.post("/deals/radar-estimate/backfill").json() == {"filled": 0}
    assert prospect_cleanup.run.await_args.kwargs["by"] == "ops@portless.test"


def test_usd_inr_rate(api, monkeypatch):
    import shared.currency as cur
    monkeypatch.setattr(cur, "rate_info", AsyncMock(return_value={"rate": 84.0, "source": "fallback"}))
    assert api.get("/deals/fx/usd-inr").json()["rate"] == 84.0


async def test_reorder_maps_errors(api, monkeypatch):
    from services.deals.service import DealNotFoundError
    from shared.orchestration import reorders
    monkeypatch.setattr(reorders, "reorder", AsyncMock(side_effect=DealNotFoundError("DEAL-X")))
    assert api.post("/deals/DEAL-X/reorder", json={}).status_code == 404
    monkeypatch.setattr(reorders, "reorder", AsyncMock(side_effect=ValueError("only a completed deal")))
    r = api.post("/deals/DEAL-X/reorder", json={"quantity_mt": 20})
    assert r.status_code == 409 and "completed" in r.json()["detail"]


async def test_per_deal_routes(api, repo, monkeypatch):
    await _deal(repo)
    from shared import deal_costs
    from shared.orchestration import send_retry
    monkeypatch.setattr(send_retry, "retry_now", AsyncMock(return_value={"resent": 1}))
    monkeypatch.setattr(deal_costs, "summary", AsyncMock(return_value={"deal_id": "DEAL-R1", "tool_usd": 0.5}))
    assert api.post("/deals/DEAL-R1/retry-sends").json() == {"resent": 1}
    assert api.get("/deals/DEAL-R1/run-costs").json()["tool_usd"] == 0.5
    assert api.post("/deals/DEAL-MISSING/retry-sends").status_code == 404


async def test_concession_ladder_needs_a_negotiation(api, repo, monkeypatch):
    await _deal(repo)
    import agents.negotiation_agent.repository as neg
    monkeypatch.setattr(neg, "get_repository", lambda: SimpleNamespace(get_session_by_deal=AsyncMock(return_value=None)))
    assert api.get("/deals/DEAL-R1/concession-ladder").status_code == 404


async def test_supplier_commitment_relays_need_an_open_request(api, repo, monkeypatch):
    await _deal(repo, status=DealStatus.NEGOTIATION_AGREED)
    from shared.orchestration import supplier_commitment
    monkeypatch.setattr(supplier_commitment, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(supplier_commitment, "mark_and_advance", AsyncMock(return_value="next_supplier_asked"))
    note = {"reason": "called the mill at 10:30, they confirmed"}
    assert api.post("/deals/DEAL-R1/supplier-commitment/confirm", json=note).status_code in (404, 409)
    nxt = api.post("/deals/DEAL-R1/supplier-commitment/next", json=note)
    assert nxt.status_code == 200 and nxt.json()["status"] == "next_supplier_asked"
    assert api.post("/deals/DEAL-R1/supplier-commitment/confirm", json={"reason": "short"}).status_code == 422


async def test_packing_and_market_check(api, repo, monkeypatch):
    await _deal(repo)
    import agents.negotiation_agent.repository as neg
    monkeypatch.setattr(neg, "get_repository", lambda: SimpleNamespace(get_session_by_deal=AsyncMock(return_value=None)))
    body = api.get("/deals/DEAL-R1/packing").json()
    assert body["packing"] is None and body["surcharge_usd_per_mt"] == 0
    from shared.orchestration import market_check
    monkeypatch.setattr(market_check, "for_deal", AsyncMock(return_value=None))
    assert api.get("/deals/DEAL-R1/market-check").status_code == 404
