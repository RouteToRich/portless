"""Actual freight above the estimate raises a margin alert (DEF-19)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.orchestration import cost_overrun as co


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("COST_OVERRUN_PCT", raising=False)
    co.reset_for_tests()
    yield
    co.reset_for_tests()


def test_compare():
    assert co.compare("ocean_freight", 1000.0, 1080.0, 25000.0) is None          # within 10 %
    out = co.compare("ocean_freight", 1000.0, 1500.0, 25000.0)
    assert out["over_usd"] == 500.0 and out["over_pct"] == 50.0 and out["margin_points_lost"] == 2.0
    assert co.compare("ocean_freight", 0, 900.0, None) is None


async def test_booked_freight_above_the_sheet_alerts_once(monkeypatch):
    import agents.negotiation_agent.repository as neg
    import services.escrow_guard.repository as eg
    import services.shield_link.service as sl
    session = SimpleNamespace(quantity_mt=25.0, cost_sheet={"lines": [{"key": "ocean_freight", "usd_per_mt": 40.0}]})
    monkeypatch.setattr(neg, "get_repository", lambda: SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session)))
    orders = [SimpleNamespace(order_type="FREIGHT", status="BOOKED", provider_payload={"total_freight_usd": 1600.0})]
    monkeypatch.setattr(sl, "list_deal_orders", AsyncMock(return_value=SimpleNamespace(orders=orders)))
    monkeypatch.setattr(eg, "get_repository", lambda: SimpleNamespace(get_hold_by_deal=AsyncMock(
        return_value=SimpleNamespace(amount=25000.0))))
    events = []

    class _Pub:
        async def publish(self, e):
            events.append(e)
    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    out = await co.check("D1")
    assert out["estimate_usd"] == 1000.0 and out["actual_usd"] == 1600.0
    await co.check("D1")
    assert len(events) == 1 and events[0].payload["category"] == "MARGIN_BREACH"
    assert events[0].payload["stage"] == "COSTS:ocean_freight_over_estimate"
