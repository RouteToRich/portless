"""Platform-wide running costs per day (shared/cost_meter.py, GET /health/costs)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared import cost_meter, deal_costs

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean():
    cost_meter.reset_for_tests()
    deal_costs.set_collection_for_tests(None)
    yield
    cost_meter.reset_for_tests()


async def test_days_totals_and_fixed(monkeypatch):
    monkeypatch.setenv("COST_FIXED_MONTHLY_USD", "atlas:60.83,hosting:30.415, junk")
    cost_meter.add(now=NOW, llm_calls=3, llm_tokens=3000, llm_usd=0.006, apify_runs=2, apify_usd=0.8)
    cost_meter.add(now=NOW - timedelta(days=1), apify_runs=1, apify_usd=0.4)
    monkeypatch.setenv("TAVILY_USD_PER_SEARCH", "0.01")
    cost_meter.tavily_search()  # today, real clock — outside a 2-day window ending NOW only if dates differ
    r = await cost_meter.report(2, now=NOW)
    assert r["fixed_monthly"] == {"atlas": 60.83, "hosting": 30.415}
    assert [d["day"] for d in r["per_day"]] == ["2026-11-18", "2026-11-19"]
    assert r["totals"]["apify_runs"] == 3 and r["totals"]["apify_usd"] == 1.2
    assert r["totals"]["fixed_usd"] == pytest.approx(2 * 91.245 * 12 / 365, abs=0.01)
    assert r["totals"]["total_usd"] == pytest.approx(r["totals"]["variable_usd"] + r["totals"]["fixed_usd"], abs=0.01)


async def test_deal_cost_also_counts_platform_wide():
    deal_costs.add("D1", llm_calls=1, llm_tokens=500, llm_usd=0.001, decisions=1)
    deal_costs.add(None, apify_runs=1, apify_usd=0.3)  # outside a deal: platform only
    today = datetime.now(timezone.utc)
    r = await cost_meter.report(1, now=today)
    assert r["totals"]["llm_calls"] == 1 and r["totals"]["apify_runs"] == 1
    assert r["booked_to_deals_usd"] == pytest.approx(0.0, abs=0.01)


def test_health_route_registered():
    from app import app
    assert "/health/costs" in {getattr(r, "path", None) for r in app.routes}
