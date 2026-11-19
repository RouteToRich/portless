"""Apify spend controls (todo 23.1): run / USD budget counted in Mongo per UTC day, result cache
per actor input, carrier-tracking pause after source_refused, e-mail qualifier rows missing from
an incomplete run, VAT / LEI sent to the scan-time screening, retired actor slots."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from shared.connectors import apify as ap
from shared.connectors import apify_usage as usage

_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ap.reset_run_budget_for_tests()
    monkeypatch.setattr(usage, "_usage_collection", lambda: None)
    monkeypatch.setattr(usage, "_cache_collection", lambda: None)
    yield
    ap.reset_run_budget_for_tests()


class _Actor:
    """Apify API double: start → SUCCEEDED → dataset rows; counts runs started."""

    def __init__(self, rows=None, status="SUCCEEDED", usd=0.25):
        self.rows = rows if rows is not None else [{"name": "A"}]
        self.status, self.usd, self.starts = status, usd, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/runs"):
            self.starts += 1
            return httpx.Response(201, json={"data": {"id": f"R{self.starts}", "status": self.status,
                                                      "defaultDatasetId": "DS", "usageTotalUsd": self.usd}})
        if "/datasets/" in request.url.path:
            return httpx.Response(200, json=self.rows)
        return httpx.Response(404)


@pytest.fixture
def actor(monkeypatch):
    fake = _Actor()
    monkeypatch.setenv("APIFY_TOKEN", "T")
    with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(fake), **kw)):
        yield fake


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

async def test_budget_is_counted_in_mongo_for_every_instance(monkeypatch):
    col = AsyncMongoMockClient()["t"]["platform_costs"]
    monkeypatch.setattr(usage, "_usage_collection", lambda: col)
    monkeypatch.setenv("APIFY_MAX_RUNS_PER_DAY", "2")
    assert await usage.reserve_run("COMPANY_CONTACT_FINDER") is None
    # another instance (fresh process memory) still sees the shared count
    usage._runs_by_day.clear()
    assert await usage.reserve_run("COUNTERPARTY_CHECK") is None
    refusal = await usage.reserve_run("COUNTERPARTY_CHECK")
    assert refusal and "APIFY_MAX_RUNS_PER_DAY" in refusal
    doc = await col.find_one({"_id": usage.day_key()})
    assert doc["apify_runs"] == 2  # the refused reservation was undone
    assert doc["apify_by_actor"]["COUNTERPARTY_CHECK"]["runs"] == 1


async def test_usd_cap_stops_runs(monkeypatch):
    monkeypatch.setenv("APIFY_MAX_USD_PER_DAY", "1.0")
    assert await usage.reserve_run("COMPANY_CONTACT_FINDER") is None
    await usage.record_usd("COMPANY_CONTACT_FINDER", 1.2)
    refusal = await usage.reserve_run("EMAIL_LIST_QUALIFIER")
    assert refusal and "APIFY_MAX_USD_PER_DAY" in refusal
    today = await usage.usage_today()
    assert today["usd_today"] == 1.2 and today["budget_left"] is False
    assert today["by_actor"]["COMPANY_CONTACT_FINDER"] == {"runs": 1, "usd": 1.2}


async def test_run_records_spend_and_a_refused_start_is_released(actor, monkeypatch):
    rows = await ap.ApifyContactFinderConnector().run({"x": 1})
    assert rows == [{"name": "A"}] and ap.runs_today() == 1 and ap.usd_today() == 0.25

    def refuse(request):
        return httpx.Response(402, text="no credit")
    with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(refuse), **kw)):
        with pytest.raises(ap.ApifyError):
            await ap.ApifyContactFinderConnector().run({"x": 2})
    assert ap.runs_today() == 1


async def test_over_the_cap_no_run_starts(actor, monkeypatch):
    monkeypatch.setenv("APIFY_MAX_USD_PER_DAY", "0.2")
    await ap.ApifyContactFinderConnector().run({"x": 1})
    assert await ap.ApifyContactFinderConnector().run({"x": 2}) == []
    assert actor.starts == 1


def test_digest_line_shows_spend_vs_cap_per_actor():
    from shared.orchestration.digest import render_digest_text
    text = render_digest_text({"apify": {"runs_today": 3, "max_runs_per_day": 50, "usd_today": 0.95,
                                         "max_usd_per_day": 1.0, "by_actor": {
                                             "COMPANY_CONTACT_FINDER": {"runs": 2, "usd": 0.9},
                                             "EMAIL_LIST_QUALIFIER": {"runs": 1, "usd": 0.05}}}})
    assert "spend 0.95 / 1.00 USD today" in text and "daily spend cap is nearly used" in text
    assert "company_contact_finder 2 run(s) 0.90 USD" in text


# ---------------------------------------------------------------------------
# Result cache
# ---------------------------------------------------------------------------

async def test_identical_input_is_served_from_the_cache(actor, monkeypatch):
    monkeypatch.setenv("APIFY_CACHE_ENABLED", "true")
    conn = ap.ApifyCompanySearchConnector()
    first = await conn.run({"searchQuery": "Rice importers, AE"})
    again = await ap.ApifyCompanySearchConnector().run({"searchQuery": "  rice   IMPORTERS, ae"})
    assert first == again and actor.starts == 1
    stats = usage.cache_stats("COMPANY_CONTACT_FINDER")
    assert stats["hits"] == 1 and stats["misses"] == 1 and stats["ttl_hours"] == 14 * 24


async def test_cache_ttl_per_actor_and_never_for_tracking(monkeypatch):
    monkeypatch.setenv("TRADE_MIN_VALIDATION_MAX_AGE_DAYS", "10")
    assert usage.cache_hours("COUNTERPARTY_CHECK") == 240
    assert usage.cache_hours("EXPORT_MARKET_FINDER") == 30 * 24
    monkeypatch.setenv("APIFY_CACHE_ENABLED", "true")
    assert usage.caches("CARRIER_TRACKING") is False
    monkeypatch.setenv("APIFY_CACHE_HOURS_CARRIER_TRACKING", "2")
    assert usage.caches("CARRIER_TRACKING") is True


async def test_failed_or_partial_runs_are_not_cached(actor, monkeypatch):
    monkeypatch.setenv("APIFY_CACHE_ENABLED", "true")
    actor.status = "ABORTED"
    await ap.ApifyCompanySearchConnector().run({"q": 1})
    actor.status = "SUCCEEDED"
    await ap.ApifyCompanySearchConnector().run({"q": 1})
    assert actor.starts == 2


async def test_expired_entry_is_a_miss(monkeypatch):
    from datetime import datetime, timedelta, timezone
    usage._cache_memory["k"] = {"rows": [{"a": 1}], "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    assert await usage.cache_get("k") is None


# ---------------------------------------------------------------------------
# Carrier tracking pause
# ---------------------------------------------------------------------------

async def test_tracking_stops_for_the_day_after_source_refused(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "T")
    conn = ap.ApifyCarrierTrackingConnector()
    conn.run = AsyncMock(return_value=[])  # type: ignore[method-assign]
    conn.run_output = AsyncMock(return_value={"ok": False, "sourceError": "plan cannot run public actors"})  # type: ignore[method-assign]
    first = await conn.quote({"container_number": "MSCU1234567"})
    assert first["source_refused"] and not first.get("paused")
    second = await conn.quote({"container_number": "MSCU1234567"})
    assert second["paused"] and "staff milestones only" in second["detail"]
    assert conn.run.await_count == 1
    monkeypatch.setenv("APIFY_CARRIER_TRACKING_RETRY_REFUSED", "true")
    await conn.quote({"container_number": "MSCU1234567"})
    assert conn.run.await_count == 2


async def test_refused_tracking_is_noted_on_the_deal(monkeypatch):
    from services.shield_link import service as sl
    written = {}

    class _Deals:
        async def set_fields(self, deal_id, fields):
            written[deal_id] = fields
            return True
    with patch("services.deals.repository.get_repository", return_value=_Deals()):
        await sl._note_staff_milestones_only("D-1")
    assert written == {"D-1": {"freight_status_detail": sl.TRACKING_STAFF_ONLY}}


# ---------------------------------------------------------------------------
# E-mail qualifier: a missing row is "invalid" only after a complete run
# ---------------------------------------------------------------------------

def _qualifier(rows, status):
    conn = ap.ApifyEmailQualifierConnector()
    conn._token = "T"  # noqa: SLF001

    async def fake_run(actor_input, *, max_items=None):
        conn.last_run = {"status": status}
        return rows
    conn.run = fake_run  # type: ignore[method-assign]
    return conn


@pytest.mark.parametrize("status", ["TIMED-OUT", "ABORTED", "BUDGET_REFUSED"])
async def test_incomplete_run_leaves_missing_addresses_unchecked(status):
    out = await _qualifier([{"email": "ok@a.example", "verifierScore": 90}], status).quote(
        {"emails": ["ok@a.example", "maybe@a.example"]})
    st = {r["email"]: r["status"] for r in out["emails"]}
    assert st == {"ok@a.example": "ok", "maybe@a.example": "unchecked"}


async def test_complete_run_still_marks_missing_invalid():
    out = await _qualifier([{"email": "ok@a.example", "verifierScore": 90}], "SUCCEEDED").quote(
        {"emails": ["ok@a.example", "gone@a.example"]})
    assert {r["email"]: r["status"] for r in out["emails"]}["gone@a.example"] == "invalid"


async def test_rows_cut_at_the_limit_say_nothing_about_the_rest():
    conn = _qualifier([{"email": f"a{i}@x.example"} for i in range(2)], "SUCCEEDED")
    conn._max_items = 2  # noqa: SLF001
    out = await conn.quote({"emails": ["a0@x.example", "a1@x.example", "z@x.example"]})
    assert {r["email"]: r["status"] for r in out["emails"]}["z@x.example"] == "unchecked"


# ---------------------------------------------------------------------------
# Scan-time screening gets the identifiers the search already found
# ---------------------------------------------------------------------------

async def test_screening_sends_vat_lei_and_cin_and_keeps_the_evidence(monkeypatch):
    from services.trade_min import validation as v
    from services.trade_min.schemas import CounterpartyValidation
    conn = SimpleNamespace(quote=AsyncMock(return_value={"results": [
        {"name": "Hamburg Foods GmbH", "verdict": "clear", "lei": "5299001", "lei_status": "ISSUED",
         "vat_number": "DE123456789", "vat_valid": True}]}))
    monkeypatch.setattr(v, "_connector", lambda name: conn)
    s = v._Subject(role="buyer", name="Hamburg Foods GmbH", country="DE", vat_number="DE123456789",
                   target=SimpleNamespace(validation=None))
    await v._screen([s])
    sent = conn.quote.await_args.args[0]["counterparties"][0]
    assert sent == {"name": "Hamburg Foods GmbH", "country": "DE", "vat_number": "DE123456789"}
    assert s.v.lei == "5299001" and s.v.vat_valid is True
    ev = v.existence(s.v, s)
    assert "LEI 5299001 (ISSUED)" in ev and "EU VAT DE123456789 valid (VIES)" in ev
    assert isinstance(s.v, CounterpartyValidation)


# ---------------------------------------------------------------------------
# Retired actor slots
# ---------------------------------------------------------------------------

def test_dormant_slots_are_not_registered():
    from shared.connectors.registry import CONNECTOR_REGISTRY
    for cls in ap.DORMANT_CONNECTORS:
        assert cls.name not in CONNECTOR_REGISTRY
        assert cls not in ap.APIFY_CONNECTORS
    assert {"actor", "cache"} <= set(ap.ApifyCompanySearchConnector().describe())
