"""Organisation isolation (todo: penetration test — one org reading another's deal): an org user
never sees another organisation's deal through any per-deal route, and never reaches a staff route."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.deals.repository import InMemoryFallbackRepository
from services.deals.router import router
from services.deals.schemas import DealRecord, DealStatus
from shared.auth.dependencies import get_current_user
from tests.factories import make

ORG_A_USER = SimpleNamespace(user_id="u-a", email="user@org-a.test", name="A", org_id="org-a", is_portless=False)
PER_DEAL_GETS = ["", "/timeline", "/realisation", "/export-certificates", "/containers", "/packing", "/cost-sheet",
                 "/market-check", "/match-board", "/notifications", "/compliance", "/supplier-commitment",
                 "/concession-ladder", "/profit"]


@pytest.fixture
def client(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: ORG_A_USER
    return TestClient(app, raise_server_exceptions=False), repo


@pytest.mark.parametrize("suffix", PER_DEAL_GETS)
async def test_another_orgs_deal_is_invisible(client, suffix):
    c, repo = client
    await repo.save(make(DealRecord, deal_id="DEAL-B1", hs_code="1006", market="AE", status=DealStatus.NEGOTIATING,
                         org_id="org-b", buyer_org_id=None, seller_org_id=None))
    r = c.get(f"/deals/DEAL-B1{suffix}")
    assert r.status_code in (401, 403, 404), (suffix, r.status_code, r.text[:200])
    assert "org-b" not in r.text


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/deals/DEAL-B1/exposure/accept", {"reason": "trying to accept someone else's risk"}),
    ("POST", "/deals/DEAL-B1/export-certificates", {"kind": "phytosanitary", "number": "X-123"}),
    ("PUT", "/deals/compliance/dgft-denied", {"csv": "name,iec\nA,0400000001\n"}),
    ("POST", "/deals/prearranged", {"hs_code": "10063020", "market": "AE", "quantity_mt": 10,
                                    "buyer": {"legal_name": "Gulf"}, "supplier": {"legal_name": "Mill"},
                                    "buyer_price_usd": 1100, "supplier_price_usd": 900}),
    ("GET", "/deals/realisation/open", None),
])
async def test_staff_routes_refuse_an_org_user(client, method, path, body):
    c, _repo = client
    r = c.request(method, path, json=body)
    assert r.status_code == 403, (path, r.status_code, r.text[:200])  # the staff check, not body validation
