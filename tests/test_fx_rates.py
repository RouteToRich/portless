"""Reference exchange rates: staff rate, daily feed, setting, freshness, plausible band, and the
currency a party is written to in (shared/fx_rates.py + shared/currency.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from shared import currency, fx_rates


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    fx_rates.reset_for_tests()
    monkeypatch.delenv("FX_RATE_AED", raising=False)
    monkeypatch.delenv("FX_RATE_EUR", raising=False)
    yield
    fx_rates.reset_for_tests()


async def test_staff_rate_recorded_and_used_for_inr_fallback(monkeypatch):
    async def no_live():
        return None

    monkeypatch.setattr("agents.negotiation_agent.market_intel.fetch_usd_inr_rate", no_live)
    row = await fx_rates.record("inr", 95.4, source="RBI reference rate", by="ops@portless")
    assert row["currency"] == "INR" and row["rate"] == 95.4
    assert currency.fallback_rate("INR") == 95.4
    info = await currency.rate_info("INR")
    assert info["source"] == "reference" and info["rate"] == 95.4 and info["reference"] == "RBI reference rate"


async def test_implausible_rate_and_missing_source_refused():
    with pytest.raises(ValueError, match="plausible"):
        await fx_rates.record("INR", 9.6, source="typo", by="ops")
    with pytest.raises(ValueError, match="where the rate comes from"):
        await fx_rates.record("INR", 95.0, source=" ", by="ops")
    with pytest.raises(ValueError, match="3-letter"):
        await fx_rates.record("USD", 1.0, source="x", by="ops")


async def test_country_gets_its_own_currency_only_with_a_rate():
    assert currency.party_currency("AE") == "USD"
    await fx_rates.record("AED", 3.6725, source="pegged", by="ops")
    assert currency.party_currency("AE") == "AED"
    assert await currency.usd_rate("AED") == 3.6725
    assert currency.party_currency("DE") == "USD"  # no EUR rate yet
    assert currency.party_currency("IN") == "INR"


async def test_setting_rate_counts(monkeypatch):
    monkeypatch.setenv("FX_RATE_AED", "3.6725")
    assert currency.party_currency("AE") == "AED"
    assert (await fx_rates.usable("AED"))["source"] == "setting FX_RATE_AED"
    monkeypatch.setenv("FX_RATE_AED", "36.7")  # outside the band → ignored
    assert currency.party_currency("AE") == "USD"


async def test_stale_rate_not_used(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=10)
    await fx_rates.record("EUR", 0.92, source="ECB", by="ops", as_of=old)
    assert await fx_rates.usable("EUR") is None
    assert currency.party_currency("FR") == "USD"
    monkeypatch.setenv("FX_REFERENCE_MAX_AGE_DAYS", "30")
    assert currency.party_currency("FR") == "EUR"


async def test_feed_refresh_records_plausible_rates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"date": "2026-11-18", "rates": {"INR": 95.1, "EUR": 0.93, "GBP": 99.0, "XYZ": 1}})

    out = await fx_rates.refresh_from_source(transport=httpx.MockTransport(handler))
    assert out["ok"] and sorted(out["recorded"]) == ["EUR", "INR"] and out["refused"] == ["GBP"]
    assert (await fx_rates.usable("INR"))["kind"] == "feed"


async def test_feed_down_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    out = await fx_rates.refresh_from_source(transport=httpx.MockTransport(handler))
    assert out["ok"] is False and "unreachable" in out["reason"]


async def test_daily_refresh_once_per_day(monkeypatch):
    calls = []

    async def fake(**kw):
        calls.append(kw)
        return {"ok": True, "recorded": ["INR"]}

    monkeypatch.setattr(fx_rates, "refresh_from_source", fake)
    monkeypatch.setenv("FX_REFERENCE_AUTO_REFRESH", "true")
    now = datetime(2026, 11, 19, 6, tzinfo=timezone.utc)
    assert (await fx_rates.daily_refresh(now))["recorded"] == ["INR"]
    assert await fx_rates.daily_refresh(now + timedelta(hours=3)) is None
    assert await fx_rates.daily_refresh(now + timedelta(hours=25)) is not None
    assert len(calls) == 2
    monkeypatch.setenv("FX_REFERENCE_AUTO_REFRESH", "false")
    assert await fx_rates.daily_refresh(now + timedelta(days=5)) is None


def test_routes_record_and_list():
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.deals import router as deals_router
    from shared.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(deals_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="ops@p", user_id="u", is_portless=True, org_id="o")
    c = TestClient(app)
    assert c.post("/deals/fx/reference", json={"currency": "AED", "rate": 30, "source": "x"}).status_code == 422
    r = c.post("/deals/fx/reference", json={"currency": "AED", "rate": 3.6725, "source": "CBUAE peg"})
    assert r.status_code == 200 and r.json()["recorded_by"] == "ops@p"
    body = c.get("/deals/fx/reference").json()
    assert body["usable"]["AED"]["rate"] == 3.6725 and body["history"][0]["currency"] == "AED"
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="b@org", user_id="v", is_portless=False, org_id="o")
    assert c.post("/deals/fx/reference", json={"currency": "AED", "rate": 3.67, "source": "x"}).status_code == 403


def test_money_line_in_local_currency():
    assert currency.money(100, "AED", 3.6725) == "AED 367 (USD 100.00)"
