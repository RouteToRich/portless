"""Counter-offer ladder: concession steps from our ask to the walk-away price, never below it
(agents/negotiation_agent/pricing.concession_ladder + GET /deals/{id}/concession-ladder)."""
from __future__ import annotations

import pytest

from agents.negotiation_agent.pricing import concession_ladder


def test_three_shrinking_steps_to_the_buyers_counter():
    out = concession_ladder(ask=1000.0, walk_away=900.0, counter=940.0)
    prices = [s["price"] for s in out["steps"]]
    concessions = [s["concede_usd"] for s in out["steps"]]
    assert prices == [970.0, 952.0, 940.0]
    assert concessions == sorted(concessions, reverse=True)  # each smaller than the last
    assert out["stop"] == 940.0 and "meets the buyer's counter" in out["note"]


def test_a_counter_below_the_floor_stops_at_walk_away():
    out = concession_ladder(ask=1000.0, walk_away=900.0, counter=850.0)
    assert out["steps"][-1]["price"] == 900.0 and min(s["price"] for s in out["steps"]) >= 900.0
    assert "do not go lower" in out["note"]


def test_two_moves_and_nothing_to_give():
    out = concession_ladder(ask=1000.0, walk_away=900.0, moves=2)
    assert [s["price"] for s in out["steps"]] == [940.0, 900.0]
    held = concession_ladder(ask=900.0, walk_away=910.0, counter=880.0)
    assert held["steps"] == [] and "hold the price" in held["note"]
    with pytest.raises(ValueError):
        concession_ladder(ask=0, walk_away=900.0)


@pytest.mark.parametrize("ask,walk,counter", [(1234.56, 1001.01, None), (2000.0, 1999.99, 1500.0), (980.0, 700.0, 960.0)])
def test_never_below_walk_away_and_always_descending(ask, walk, counter):
    out = concession_ladder(ask=ask, walk_away=walk, counter=counter)
    prices = [ask] + [s["price"] for s in out["steps"]]
    assert all(p >= walk for p in prices[1:])
    assert all(a >= b for a, b in zip(prices, prices[1:]))


async def test_the_route_uses_the_sessions_prices_and_costs(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession
    from services.deals import router as deals_router
    from shared.auth.dependencies import get_current_user

    now = datetime.now(UTC)
    session = NegotiationSession(
        negotiation_id="11111111-2222-3333-4444-555555555555", deal_id="D1", trade_match_id="TM", buyer_id="B",
        seller_id="S", product_code="0904", buyer_country_code="AE", buyer_target_price=950.0,
        supplier_quote_price=760.0, policy=NegotiationPolicy(margin_floor_pct=8.0), status="ROUND",
        last_offer_to_buyer=1010.0, buyer_last_offer=930.0, created_at=now, updated_at=now)
    monkeypatch.setattr("agents.negotiation_agent.repository.get_repository",
                        lambda: SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session)))
    monkeypatch.setattr(deals_router, "_org_deal", AsyncMock())
    app = FastAPI()
    app.include_router(deals_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="ops", user_id="u", is_portless=True, org_id="o")
    r = TestClient(app).get("/deals/D1/concession-ladder")
    assert r.status_code == 200
    body = r.json()
    walk = round(760.0 / 0.92, 2)
    assert abs(body["walk_away"] - walk) < 0.02 and body["stop"] == 930.0 and body["steps"][-1]["price"] == 930.0
