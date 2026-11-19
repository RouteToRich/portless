"""Full line + branch coverage of the modules added on 2026-11-19 (part 2): Apify usage, national
trade statistics, source yield, product facts, scan plan, sourcing agent, agent scorecard,
inspection options, freight comparison, reorder agent, document check."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord
from shared import catalogue

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def _rec(**kw) -> CounterpartyRecord:
    base = dict(id="buyer:gulf.ae", role="buyer", name="Gulf Foods", country="AE", email="buy@gulf.ae",
                website="https://gulf.ae", source="apify_company_search", sources=["apify_company_search"],
                hs_codes=["070310"], first_seen_at=NOW, last_seen_at=NOW)
    base.update(kw)
    return CounterpartyRecord(**base)


class _Broken:
    """A Mongo collection whose every call fails."""

    def find(self, *a, **k):
        raise RuntimeError("store down")

    def __getattr__(self, name):
        async def fail(*a, **k):
            raise RuntimeError("store down")
        return fail

    @property
    def database(self):
        return {"freight_lane_history": self}


@pytest.fixture(autouse=True)
async def _fresh():
    from shared.connectors import apify_usage
    directory.reset_directory()
    apify_usage.reset_for_tests()
    await catalogue.set_catalogue([{"product_id": "onion", "hs_code": "070310", "name": "Onion", "markets": ["AE"],
                                    "attributes": [{"key": "variety", "values": ["red"]}]}])
    yield
    apify_usage.reset_for_tests()
    directory.reset_directory()
    catalogue.reset_for_tests()


# ── shared/connectors/apify_usage.py ───────────────────────────────────────

async def test_apify_usage_with_mongo_and_with_a_failing_store(monkeypatch):
    from shared.connectors import apify_usage as u
    monkeypatch.setenv("APIFY_MAX_USD_PER_DAY", "oops")
    assert u.max_usd_per_day() == 0.0
    col = AsyncMongoMockClient()["t"]["platform_costs"]
    monkeypatch.setattr(u, "_usage_collection", lambda: col)
    assert await u.reserve_run("A") is None
    await u.release_run("A")
    await u.record_usd("A", 0.4)
    await u.record_usd("A", 0.0)  # nothing to add
    today = await u.usage_today()
    assert today["runs_today"] == 0 and today["usd_today"] == 0.4 and today["by_actor"]["A"]["usd"] == 0.4
    monkeypatch.setattr(u, "_usage_collection", lambda: _Broken())
    u.reset_for_tests()
    assert await u.reserve_run("B") is None  # counted in memory when the store fails
    await u.release_run("B")
    await u.record_usd("B", 0.1)
    assert (await u.usage_today())["usd_today"] == 0.1
    monkeypatch.setenv("APIFY_MAX_RUNS_PER_DAY", "1")
    assert await u.reserve_run("B") is None
    assert "APIFY_MAX_RUNS_PER_DAY" in (await u.reserve_run("B"))


def test_apify_usage_store_and_cost_meter_failures_are_quiet():
    from shared.connectors import apify_usage as u
    with patch("shared.mongo.collection", side_effect=RuntimeError("no mongo")):
        assert u._usage_collection() is None and u._cache_collection() is None
    with patch("shared.cost_meter.add_memory_only", side_effect=RuntimeError("x")):
        u._cost_memory(apify_runs=1)


async def test_apify_cache_edges(monkeypatch):
    from shared.connectors import apify_usage as u
    monkeypatch.setenv("APIFY_CACHE_HOURS_COMPANY_CONTACT_FINDER", "not a number")
    assert u.cache_hours("COMPANY_CONTACT_FINDER") == 14 * 24
    assert u._normalise({"q": ["A  b", ("C",)]}) == {"q": ["a b", ["c"]]}
    assert u._aware("bad date") is None and u._aware(42) is None
    assert u._aware("2026-01-01T00:00:00").tzinfo is not None
    col = AsyncMongoMockClient()["t"]["apify_result_cache"]
    monkeypatch.setattr(u, "_cache_collection", lambda: col)
    await u.cache_put("COMPANY_CONTACT_FINDER", "k1", [{"a": 1}], {"id": "R"})
    u._cache_memory.clear()
    assert (await u.cache_get("k1"))["rows"] == [{"a": 1}]  # read back from Mongo
    monkeypatch.setattr(u, "_cache_collection", lambda: _Broken())
    await u.cache_put("COMPANY_CONTACT_FINDER", "k2", [], {})
    u._cache_memory.clear()
    assert await u.cache_get("k2") is None


# ── shared/connectors/national_trade.py ────────────────────────────────────

async def test_hmrc_paging_errors_and_refusals(monkeypatch):
    from shared.connectors import national_trade as nt
    monkeypatch.setenv("NATIONAL_TRADE_SOURCES_ENABLED", "true")
    pages = iter([
        {"value": [{"MonthId": 202608, "Value": 10, "NetMass": 1000, "CountryCodeAlpha": "IN"}, {"MonthId": None}],
         "@odata.nextLink": "https://api.uktradeinfo.com/OTS?page=2"},
        {"value": []},
    ])
    out = await nt.UkHmrcTradeConnector(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(pages)))).quote(
        {"hs_code": "070310"})
    assert out["records"] == 2 and out["india_quantity_mt"] == 1.0
    out = await nt.UkHmrcTradeConnector(transport=httpx.MockTransport(lambda r: httpx.Response(503))).quote({"hs_code": "070310"})
    assert out["error"] == "HTTP 503"

    def boom(request):
        raise httpx.ConnectError("offline", request=request)
    assert "offline" in (await nt.UkHmrcTradeConnector(transport=httpx.MockTransport(boom)).quote({"hs_code": "070310"}))["error"]
    with pytest.raises(ValueError):
        await nt.UkHmrcTradeConnector().quote({"hs_code": "07"})
    c = nt.UkHmrcTradeConnector()
    assert await c.book({}) == {"status": "not_supported"} and await c.status("x") == {"status": "not_supported"}
    assert await c.webhook_verify({}, b"") is False


async def test_comext_errors_and_refusals(monkeypatch):
    from shared.connectors import national_trade as nt
    with pytest.raises(ValueError):
        await nt.EurostatComextConnector().quote({"hs_code": "1", "market": "DE"})
    assert (await nt.EurostatComextConnector().quote({"hs_code": "070310", "market": "AE"}))["error"] == "not an EU market"
    monkeypatch.setenv("NATIONAL_TRADE_SOURCES_ENABLED", "false")
    assert (await nt.EurostatComextConnector().quote({"hs_code": "070310", "market": "DE"}))["disabled"] is True
    monkeypatch.setenv("NATIONAL_TRADE_SOURCES_ENABLED", "true")
    bad = nt.EurostatComextConnector(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert (await bad.quote({"hs_code": "070310", "market": "DE"}))["error"] == "HTTP 500"

    def boom(request):
        raise httpx.ConnectError("offline", request=request)
    assert "offline" in (await nt.EurostatComextConnector(transport=httpx.MockTransport(boom)).quote(
        {"hs_code": "070310", "market": "DE"}))["error"]
    c = nt.EurostatComextConnector()
    assert await c.book({}) == {"status": "not_supported"} and await c.status("x") == {"status": "not_supported"}
    assert await c.webhook_verify({}, b"") is False


def test_jsonstat_list_index_values_and_gaps():
    from shared.connectors.national_trade import EurostatComextConnector as E, _usd
    assert E.rows_from_jsonstat({}) == []
    cube = {"id": ["partner", "indicators"], "size": [1, 2],
            "dimension": {"partner": {"category": {"index": ["IN"]}},
                          "indicators": {"category": {"index": ["VALUE_IN_EUROS", "OTHER"]}}},
            "value": [500, None]}
    assert E.rows_from_jsonstat(cube) == [{"month": "", "partner": "IN", "value": 500.0, "kg": None}]
    with patch("shared.fx_rates.cached", side_effect=RuntimeError("x")):
        assert _usd(100.0, "EUR") == (None, None)


# ── services/trade_min/source_yield.py ─────────────────────────────────────

async def test_source_yield_spend_from_mongo_memory_and_failure(monkeypatch):
    from services.trade_min import source_yield as sy
    col = AsyncMongoMockClient()["t"]["platform_costs"]
    await col.insert_one({"_id": NOW.date().isoformat(), "apify_by_actor": {"COMPANY_CONTACT_FINDER": {"usd": 2.5}}})
    with patch("shared.mongo.collection", return_value=col):
        assert await sy._actor_spend(1, NOW) == {"COMPANY_CONTACT_FINDER": 2.5}
    with patch("shared.mongo.collection", return_value=_Broken()):
        from shared.connectors import apify_usage
        apify_usage._by_actor_day[apify_usage.day_key(NOW)] = {"X": {"runs": 1, "usd": 0.3}}
        assert await sy._actor_spend(1, NOW) == {"X": 0.3}
    with patch("shared.mongo.collection", side_effect=RuntimeError("no mongo")), \
            patch("shared.connectors.apify_usage.day_key", side_effect=RuntimeError("x")):
        assert await sy._actor_spend(1, NOW) == {}


async def test_source_yield_report_role_filter_old_rows_and_directory_failure():
    from services.trade_min import source_yield as sy
    store = await directory.get_directory()
    await store.upsert(_rec(first_seen_at=NOW - timedelta(days=400)))
    await store.upsert(_rec(id="supplier:s", role="supplier", name="S", country="IN", email="s@s.in", website="https://s.in",
                            sources=["manual"], source="manual"))
    with patch.object(sy, "_actor_spend", new=AsyncMock(return_value={})):
        r = await sy.report("supplier", 30)
    assert [s["source"] for s in r["sources"]] == ["manual"] and r["sources"][0]["apify_usd"] is None
    with patch("services.trade_min.directory.get_directory", side_effect=RuntimeError("down")), \
            patch.object(sy, "_actor_spend", new=AsyncMock(return_value={})):
        assert (await sy.report(None, 30))["companies"] == 0
    assert sy.tally([_rec(first_seen_at=NOW.replace(tzinfo=None) - timedelta(days=5))], since=NOW - timedelta(days=1)) == {}


# ── services/trade_min/product_facts.py ────────────────────────────────────

async def test_product_facts_edges():
    from services.trade_min import product_facts as pf
    store = await directory.get_directory()
    await store.upsert(_rec())
    assert pf.facts_of(None, "070310") is None
    assert await pf.find_record("buyer") is None
    assert await pf.record("buyer", "07", {"variety": "red"}, source="email", name="X") == []
    assert await pf.record("buyer", "070310", {}, source="email", name="X") == []
    assert await pf.record("buyer", "070310", {"variety": "red"}, source="email", email="nobody@nowhere.com",
                           create=False) == []
    with patch("shared.catalogue.product_name_for", side_effect=RuntimeError("x")):
        assert await pf.record("buyer", "070310", {"variety": "red"}, source="email", name="Gulf Foods",
                               website="https://gulf.ae") == ["variety"]
    assert await pf.record("buyer", "070310", {"variety": "red"}, source="email", name="Gulf Foods",
                           website="https://gulf.ae") == []  # nothing new
    with patch("services.trade_min.directory.get_directory", side_effect=RuntimeError("down")):
        assert await pf.record("buyer", "070310", {"variety": "red"}, source="email", name="X") == []
        assert await pf.facts_for("buyer", "070310", name="X") is None
        assert await pf.note_person("buyer", {"email": "a@b.c"}, name="X") is False
    assert await pf.note_person("buyer", {}, name="Gulf Foods", website="https://gulf.ae") is False
    with pytest.raises(LookupError):
        await pf.view("supplier", "buyer:gulf.ae")


async def test_product_facts_supplier_offer_and_deal_without_facts():
    from services.trade_min import product_facts as pf
    offer = SimpleNamespace(company_name="Nashik Agro", email="s@nashik.in", quote_id="Q1", grade="A",
                            capacity_mt_per_month=300, moq_mt=None, fob_price_usd_per_mt=310, incoterm="FOB",
                            port_of_loading=None, certifications=[], lead_time_days=None, payment_terms=None, spec={})
    assert "price_usd_per_mt" in await pf.record_supplier_offer(SimpleNamespace(hs_code="070310", deal_id="D"), offer)
    deal = SimpleNamespace(deal_id="D", hs_code="070310", quantity_mt=20, parties=[
        SimpleNamespace(role="supplier", legal_name="Nashik Agro", website=None, email="s@nashik.in", country="IN"),
        SimpleNamespace(role="other", legal_name="Bank")])
    with patch("shared.documents.facts.deal_facts", side_effect=RuntimeError("no facts")):
        out = await pf.record_from_deal(deal)
    assert out == {"supplier": ["incoterm", "traded"]}
    spec = SimpleNamespace(bag_type="jute")
    facts = {"pi": SimpleNamespace(packing_spec=spec), "po": SimpleNamespace(agreed_price=300.0, packing_spec=None)}
    with patch("shared.orchestration.packing.describe", side_effect=RuntimeError("x")):
        out = await pf.record_from_deal(deal, facts)
    assert "price_usd_per_mt" in out["supplier"]


async def test_staff_edit_survives_an_audit_failure():
    from services.trade_min import product_facts as pf
    store = await directory.get_directory()
    await store.upsert(_rec())
    with patch("shared.audit_log.record", side_effect=RuntimeError("audit down")):
        out = await pf.staff_edit("buyer", "buyer:gulf.ae", "070310", {"variety": "red"}, by="ops")
    assert out["products"][0]["facts"]["variety"]["source"] == "staff"
    await store.upsert(_rec(id="buyer:x", name="X", email="x@x.ae", website="https://x.ae", hs_codes=["", "070310"]))
    assert len((await pf.view("buyer", "buyer:x"))["products"]) == 1


# ── services/trade_min/scan_plan.py ────────────────────────────────────────

def test_scan_plan_settings_and_rows(monkeypatch):
    from services.trade_min import scan_plan as sp
    monkeypatch.setenv("SCAN_PLAN_RESCAN_DAYS", "x")
    assert sp._f("SCAN_PLAN_RESCAN_DAYS", 7) == 7
    assert sp._aware("x") is None and sp._aware(NOW.replace(tzinfo=None)).tzinfo is not None
    newer = SimpleNamespace(hs_code="070310", market="AE", scanned_at=NOW, opportunity_id="O2", scan_cost=None, profit=None)
    older = SimpleNamespace(hs_code="070310", market="AE", scanned_at=NOW - timedelta(days=30), opportunity_id="O1",
                            scan_cost=None, profit=None)
    nodemand = SimpleNamespace(corridors=[SimpleNamespace(hs_code="100630", market="GB", result="dropped",
                                                          reason="no live demand data")])
    rows = {(r["hs_code"], r["market"]): r for r in sp.plan_rows(
        [("070310", "AE"), ("100630", "GB"), ("090422", "DE")], [older, newer], nodemand, budget_usd=None, now=NOW)}
    assert rows[("070310", "AE")]["opportunity_id"] == "O2" and rows[("070310", "AE")]["reason"] == "scanned recently"
    assert rows[("100630", "GB")]["action"] == "skip" and rows[("090422", "DE")]["action"] == "scan"
    stale = SimpleNamespace(hs_code="070310", market="AE", scanned_at=NOW - timedelta(days=30), opportunity_id="O3",
                            scan_cost=None, profit=None)
    assert sp.plan_rows([("070310", "AE")], [stale], None, budget_usd=None, now=NOW)[0]["action"] == "rescan"


async def test_scan_plan_build(monkeypatch):
    from services.trade_min import scan_plan as sp
    from services.trade_min.repository import repository
    repository.clear()
    monkeypatch.setenv("APIFY_MAX_USD_PER_DAY", "5")
    with patch("services.trade_min.scan_reports.latest", new=AsyncMock(return_value=None)):
        plan = await sp.build_plan(NOW)
    assert plan["scan_request"] == {"hs_codes": ["070310"], "target_markets": ["AE"]} and plan["budget_left_usd"] == 5.0
    catalogue.reset_for_tests()
    monkeypatch.delenv("APIFY_MAX_USD_PER_DAY")
    with patch("services.trade_min.scan_reports.latest", new=AsyncMock(return_value=None)), \
            patch("services.trade_min.scan_plan.plan_rows", return_value=[]):
        plan = await sp.build_plan(NOW)
    assert plan["scan_request"] is None and plan["budget_left_usd"] is None


# ── shared/orchestration/sourcing_agent.py ─────────────────────────────────

def _prospect(**kw):
    base = dict(deal_id="D1", status="MATCHING", hs_code="070310", created_at=NOW - timedelta(days=10),
                sourcing_widened_at=None, supplier_offers=[], match_results=[], parties=[],
                buyer_candidates=[SimpleNamespace(status="interested")])
    base.update(kw)
    return SimpleNamespace(**base)


def test_sourcing_agent_settings_and_stall_rules(monkeypatch):
    from shared.orchestration import sourcing_agent as sa
    monkeypatch.setenv("SOURCING_AGENT_MAX_NEW", "x")
    assert sa.max_new() == 3
    assert sa._aware("x") is None
    assert not sa.stalled(_prospect(status="NEGOTIATING"), NOW)
    assert not sa.stalled(_prospect(created_at=None), NOW)


async def test_sourcing_candidates_skip_short_codes_and_asked_mailboxes():
    from shared.orchestration import sourcing_agent as sa
    store = await directory.get_directory()
    await store.upsert(_rec(id="supplier:short", role="supplier", name="Short", country="IN", email="a@short.in",
                            website="https://short.in", hs_codes=["07"]))
    await store.upsert(_rec(id="supplier:asked", role="supplier", name="Asked", country="IN", email="a@asked.in",
                            website="https://asked.in"))
    await store.upsert(_rec(id="supplier:traded", role="supplier", name="Traded", country="IN", email="t@traded.in",
                            website="https://traded.in", product_facts={"070310": {"facts": {"traded": {"value": True}}}}))
    deal = _prospect(match_results=[SimpleNamespace(supplier_id="x")],
                     parties=[SimpleNamespace(email="a@asked.in"), SimpleNamespace(email=None)])
    assert [r.name for r in await sa.candidates(deal)] == ["Traded"]


async def test_sourcing_widen_and_sweep(monkeypatch):
    from shared.orchestration import sourcing_agent as sa
    deal = _prospect()
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=_prospect(buyer_candidates=[]))):
        assert (await sa.widen("D1", now=NOW))["skipped"] == "not stalled"
    repo = SimpleNamespace(set_fields=AsyncMock(), list_active=AsyncMock(return_value=[deal, _prospect(deal_id="D2", status="PAID")]))
    with patch("services.deals.repository.get_repository", return_value=repo), \
            patch.object(sa, "widen", new=AsyncMock(side_effect=[{"asked": [1, 2]}])):
        assert await sa.sweep(NOW) == {"widened": 1, "asked": 2}
    with patch("services.deals.repository.get_repository", return_value=repo), \
            patch.object(sa, "widen", new=AsyncMock(side_effect=RuntimeError("x"))):
        assert await sa.sweep(NOW) == {"widened": 0, "asked": 0}
    with patch("services.deals.repository.get_repository", side_effect=RuntimeError("down")):
        assert await sa.sweep(NOW) == {"widened": 0, "asked": 0}
    monkeypatch.setenv("SOURCING_AGENT_ENABLED", "false")
    assert await sa.sweep(NOW) == {"widened": 0, "asked": 0}
    monkeypatch.delenv("SOURCING_AGENT_ENABLED")
    rfq = AsyncMock(return_value=None)
    store = await directory.get_directory()
    await store.upsert(_rec(id="supplier:a", role="supplier", name="A", country="IN", email="a@a.in", website="https://a.in"))
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
            patch("shared.orchestration.continuations.rfq_for_supplier", new=rfq), \
            patch("services.deals.repository.get_repository", return_value=repo):
        assert (await sa.widen("D1", now=NOW, force=True))["asked"] == []


# ── shared/orchestration/agent_scorecard.py ────────────────────────────────

async def test_scorecard_full(monkeypatch):
    from shared.orchestration import agent_scorecard as sc
    monkeypatch.setenv("SCORECARD_MIN_DECISIONS", "x")
    assert sc.agent_of("UNKNOWN_ACTION") == "other" and sc._aware("x") is None
    rows = [SimpleNamespace(action=a, status=s, created_at=NOW, modified_body=m, financial_impact_usd=f)
            for a, s, m, f in [("SEND_BUYER_OUTREACH", "AUTO_APPROVED", None, 100.0),
                               ("SEND_BUYER_OUTREACH", "TIMED_OUT", None, None),
                               ("SEND_BUYER_OUTREACH", "PENDING", None, None),
                               ("SEND_BUYER_OUTREACH", "APPROVED", "edited text", None),
                               ("SEND_BUYER_OUTREACH", "APPROVED", None, None)]]
    rows.append(SimpleNamespace(action="DISPATCH_RFQ", status="APPROVED", created_at=NOW - timedelta(days=90),
                                modified_body=None, financial_impact_usd=None))
    t = sc.tally(rows, since=NOW - timedelta(days=30))
    r = t["buyer_outreach"]
    assert (r["auto_approved"], r["timed_out"], r["pending"], r["edited"], r["money_moved_usd"]) == (1, 1, 1, 1, 100.0)
    assert r["recommended_level"] == "L0" and "sourcing" not in t
    monkeypatch.setenv("SCORECARD_MIN_DECISIONS", "2")
    assert sc.recommend({"approved": 8, "rejected": 2, "edited": 0}, "x")[0] == "L0"
    assert sc.recommend({"approved": 9, "rejected": 1, "edited": 0}, "x")[0] == "L1"
    repo = SimpleNamespace(list_by_status=AsyncMock(return_value=rows))
    with patch("services.hitl_approval.repository.get_repository", return_value=repo):
        out = await sc.scorecard(30, now=NOW)
    assert "buyer_outreach" in out["agents"] and out["platform_costs"] is not None
    with patch("services.hitl_approval.repository.get_repository", return_value=repo), \
            patch("shared.cost_meter.report", side_effect=RuntimeError("x")):
        assert (await sc.scorecard(30, now=NOW))["platform_costs"] is None


# ── shared/orchestration/inspection_options.py ─────────────────────────────

async def test_inspection_options_end_to_end(monkeypatch):
    from shared.orchestration import inspection_options as io
    monkeypatch.setenv("INSPECTION_PSI_ONLY_MAX_MT", "x")
    assert io.psi_only_max_mt() == 100.0
    deal = SimpleNamespace(deal_id="D", quantity_mt=50, parties=[SimpleNamespace(role="supplier", legal_name="Nashik Agro")])
    good = MagicMock(quote=AsyncMock(return_value={"fee_usd": 300.0, "price_source": "catalogue_estimate"}))
    bad = MagicMock(quote=AsyncMock(side_effect=RuntimeError("quote failed")))
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
            patch("shared.connectors.qc_providers.enabled_qc_providers", return_value=["qima", "sgs"]), \
            patch("shared.connectors.qc_providers.get_qc_connector", side_effect=lambda n: good if n == "qima" else bad), \
            patch("shared.orchestration.inspection_plan.plan_for", new=AsyncMock(return_value=["DUPRO", "PSI"])), \
            patch("shared.orchestration.supplier_performance.cached", new=AsyncMock(
                return_value={"supplier:nashik agro": {"qc_passed": 3, "qc_failed": 0}})):
        out = await io.options("D")
    assert out["cheapest_inspector"] == "qima" and out["inspectors"][-1]["provider"] == "sgs"
    assert out["offer_buyer"]["plan"] == ["PSI"] and out["offer_buyer"]["saving_usd"] == 300.0
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
            patch("shared.connectors.qc_providers.enabled_qc_providers", return_value=[]), \
            patch("shared.orchestration.inspection_plan.plan_for", new=AsyncMock(return_value=["PSI"])), \
            patch("shared.orchestration.supplier_performance.cached", side_effect=RuntimeError("x")):
        out = await io.options("D")
    assert out["cheapest_inspector"] is None and out["offer_buyer"] is None


# ── services/shield_link/freight_compare.py ────────────────────────────────

async def test_freight_compare_helpers_and_flow(monkeypatch):
    from services.shield_link import freight_compare as fc
    assert fc._rate({"rate_usd": "bad", "total_usd": 0, "price_usd": 1200}) == 1200
    assert fc._rate({"rates": [{"x": 1}]}) is None and fc._transit({"transit_days": "x", "transitTime": "21"}) == 21
    assert fc._transit({}) is None
    with patch("shared.catalogue.line_for", side_effect=RuntimeError("x")):
        equip, m3 = fc._equipment("070310")
    assert m3 is None and equip[0]["code"] == "20DRY"
    with patch("shared.connectors.shields.get_freight_connector",
               return_value=MagicMock(quote=AsyncMock(side_effect=RuntimeError("down")))):
        assert (await fc._one("cogoport", {}))["error"] == "down"
    with patch("shared.connectors.shields.get_freight_connector",
               return_value=MagicMock(quote=AsyncMock(return_value={"rate_usd": 1000, "transit_days": 20}))):
        assert (await fc._one("cogoport", {}))["rate_usd"] == 1000
    monkeypatch.setenv("FREIGHT_COMPARE_QUOTES", "false")
    assert (await fc.compare(origin=None, destination=None, market=None, hs_code=None))["enabled"] is False
    monkeypatch.delenv("FREIGHT_COMPARE_QUOTES")
    monkeypatch.setenv("COGOPORT_API_KEY", "real")
    monkeypatch.setenv("FREIGHT_MAX_TRANSIT_DAYS", "x")

    async def one(provider, request):
        return {"provider": provider, "rate_usd": 1500.0, "transit_days": 25}
    monkeypatch.setattr(fc, "_one", one)
    with patch("services.trade_min.freight_lanes.check_quote", new=AsyncMock(return_value={"verdict": "in_line"})):
        out = await fc.compare(origin="A", destination="B", market="AE", hs_code="070310")
    assert out["chosen"] == "cogoport" and out["quotes"][0]["lane_check"]["verdict"] == "in_line"
    assert out["max_transit_days"] is None
    assert fc.cheapest_plan([{"provider": "x", "equipment": "20DRY", "rate_usd": 100, "transit_days": 60}], 10, None,
                            max_transit=30) is None


# ── shared/orchestration/reorder_agent.py ──────────────────────────────────

async def test_reorder_prices_from_pi_po_and_price_book():
    from shared.orchestration import reorder_agent as ra
    dm = SimpleNamespace(get_pi_by_deal=AsyncMock(return_value=SimpleNamespace(agreed_price=500.0)),
                         get_po_by_deal=AsyncMock(return_value=SimpleNamespace(agreed_price=300.0)))
    old = SimpleNamespace(deal_id="D", hs_code="070310", market="AE", buyer_target_price_usd=None)
    est = SimpleNamespace(n_observations=3, median=320.0)
    with patch("services.dealmaker.repository.get_repository", return_value=dm), \
            patch("shared.pricebook.estimate", new=AsyncMock(return_value=est)):
        assert await ra._prices(old) == {"buyer": 500.0, "supplier_then": 300.0, "supplier_now": 320.0}
    with patch("services.dealmaker.repository.get_repository", side_effect=RuntimeError("x")), \
            patch("shared.pricebook.estimate", side_effect=RuntimeError("x")):
        p = await ra._prices(SimpleNamespace(deal_id="D", hs_code="070310", market="AE", buyer_target_price_usd=480.0))
    assert p == {"buyer": 480.0, "supplier_then": None, "supplier_now": None}
    assert ra.repeat_price(None, None, None, 400.0) == 400.0


async def test_reorder_offer_refusals_and_sweep(monkeypatch):
    from shared.orchestration import reorder_agent as ra
    nobuyer = SimpleNamespace(deal_id="D", parties=[])
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=nobuyer)):
        assert (await ra.offer_repeat("D"))["reason"] == "the buyer has no e-mail"
    old = SimpleNamespace(deal_id="D", hs_code="070310", market="AE", quantity_mt=None, margin_floor_pct=None,
                          parties=[SimpleNamespace(role="buyer", legal_name="G", email="g@g.ae", country="AE")])
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=old)), \
            patch.object(ra, "_prices", new=AsyncMock(return_value={"buyer": None, "supplier_then": None, "supplier_now": None})):
        assert (await ra.offer_repeat("D"))["reason"] == "no price to repeat"
    new = SimpleNamespace(deal_id="N", buyer_candidates=[SimpleNamespace(candidate_id="REORDER-D", legal_name="G",
                                                                           contact_name=None)])
    repo = SimpleNamespace(set_fields=AsyncMock())
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=old)), \
            patch.object(ra, "_prices", new=AsyncMock(return_value={"buyer": 500.0, "supplier_then": None, "supplier_now": None})), \
            patch("shared.orchestration.reorders.reorder", new=AsyncMock(return_value=new)), \
            patch("services.deals.service.add_buyer_candidate", new=AsyncMock(side_effect=ValueError("already there"))), \
            patch("shared.orchestration.matchmaking._create_gate", new=AsyncMock(return_value="G1")), \
            patch("services.deals.repository.get_repository", return_value=repo):
        assert (await ra.offer_repeat("D"))["approval_id"] == "G1"
    monkeypatch.setenv("REORDER_AGENT_ENABLED", "true")
    recent = SimpleNamespace(reorder_offered_at=NOW - timedelta(days=2))
    never = SimpleNamespace(reorder_offered_at=None)
    with patch("shared.orchestration.reorders.due", new=AsyncMock(return_value=[{"deal_id": "A"}, {"deal_id": "B"}, {"deal_id": "C"}])), \
            patch("services.deals.service.get_deal", new=AsyncMock(side_effect=[recent, never, RuntimeError("gone")])), \
            patch.object(ra, "offer_repeat", new=AsyncMock(return_value={"offered": True})):
        assert await ra.sweep(NOW) == {"offered": 1}
    monkeypatch.setenv("REORDER_AGENT_ENABLED", "false")
    assert await ra.sweep(NOW) == {"offered": 0}


# ── shared/orchestration/document_check.py ─────────────────────────────────

def test_document_check_every_branch(monkeypatch):
    from shared.orchestration import document_check as dc
    monkeypatch.setenv("COMPLIANCE_QTY_TOLERANCE_PCT", "x")
    assert dc._tolerance() == 5.0 and dc._num("x") is None
    facts = {"hs_code": "070310", "product": "Onion", "quantity_mt": 50.0, "container_numbers": [], "seal_numbers": ["S1"],
             "buyer": {"legal_name": "Gulf Foods Trading"}, "supplier": {"legal_name": "Nashik Agro Exports"},
             "port_of_loading": "Nhava Sheva", "port_of_destination": None, "unit_price": 400.0}
    res = {f["field"]: f for f in dc.check_text("commercial_invoice", "HS 1006.30 rice 50,000 KGS seal S1", facts)}
    assert res["hs_code"]["status"] == "mismatch" and res["quantity_mt"]["status"] == "ok"
    assert res["buyer"]["status"] == "not_found" and res["invoice_value_usd"]["status"] == "not_found"
    res = {f["field"]: f for f in dc.check_text("packing_list", "no numbers at all", facts)}
    assert res["hs_code"]["status"] == "not_found" and res["quantity_mt"]["status"] == "not_found"
    assert res["seals"]["status"] == "mismatch"
    res = {f["field"]: f for f in dc.check_text("commercial_invoice", "Total USD 1,000.00 for 50 MT", facts)}
    assert res["invoice_value_usd"]["status"] == "mismatch"
    assert dc.check_text("inspection_report", "", {}) == []
    res = {f["field"]: f for f in dc.check_text("draft_bl", "x", {"buyer": SimpleNamespace(legal_name=None),
                                                                  "supplier": SimpleNamespace(legal_name="")})}
    assert "buyer" not in res and "supplier_named" not in res


async def test_document_check_service_paths():
    from shared.orchestration import document_check as dc
    with pytest.raises(ValueError):
        await dc.check("D", "passport", "x")
    facts = {"hs_code": "070310", "quantity_mt": 50.0}
    pub = AsyncMock()
    with patch("shared.documents.facts.deal_facts", new=AsyncMock(return_value=facts)), \
            patch("shared.orchestration.continuations._publish_exception", new=pub):
        out = await dc.check("D", "draft_bl", "HS 1006.30 10 MT")
        assert out["exception_raised"] is True and pub.await_count == 1
        out = await dc.check("D", "other", "HS 1006.30 10 MT")
        assert out["exception_raised"] is False and out["ok"] is False
        out = await dc.check("D", "draft_bl", "HS 0703.10 50 MT")
        assert out["ok"] is True


# ── remaining branches ─────────────────────────────────────────────────────

async def test_remaining_branches(monkeypatch):
    from services.shield_link import freight_compare as fc
    from services.trade_min import product_facts as pf
    from services.trade_min import scan_plan as sp
    from shared.connectors import apify_usage as u
    from shared.connectors.national_trade import EurostatComextConnector as E
    from shared.orchestration import agent_scorecard as sc
    from shared.orchestration import inspection_options as io
    from shared.orchestration import reorder_agent as ra
    from shared.orchestration import sourcing_agent as sa

    assert fc._equipment("999999")[1] is None  # product not in the catalogue
    assert fc.cheapest_plan([{"provider": "x", "equipment": "20DRY", "rate_usd": 100}], 0, None) is None
    monkeypatch.setenv("COGOPORT_API_KEY", "real")
    asked = []

    async def one(provider, request):
        asked.append(request["container_type"])
        return {"provider": provider, "rate_usd": None}
    monkeypatch.setattr(fc, "_one", one)
    with patch("services.trade_min.freight_lanes.check_quote", new=AsyncMock()) as chk:
        await fc.compare(origin="A", destination="B", market="AE", hs_code="070310", quantity_mt=60)
    assert "LCL" not in asked and chk.await_count == 0

    store = await directory.get_directory()
    await store.upsert(_rec(people=[{"name": "Ali", "email": "ali@gulf.ae"}]))
    assert (await pf.find_record("buyer", email="buy@gulf.ae")).id == "buyer:gulf.ae"
    assert await pf.record("buyer", "100630", {"grade": "1121"}, source="email", name="Gulf Foods",
                           website="https://gulf.ae") == ["grade"]
    assert "100630" in (await store.get("buyer:gulf.ae")).hs_codes
    assert await pf.note_person("buyer", {"email": "sara@gulf.ae", "written_at": "x"}, name="Gulf Foods",
                                website="https://gulf.ae")

    newer = SimpleNamespace(hs_code="070310", market="AE", scanned_at=NOW, opportunity_id="N", scan_cost=None, profit=None)
    older = SimpleNamespace(hs_code="070310", market="AE", scanned_at=NOW - timedelta(days=1), opportunity_id="O",
                            scan_cost=None, profit=None)
    assert sp.plan_rows([("070310", "AE")], [newer, older], None, budget_usd=None, now=NOW)[0]["opportunity_id"] == "N"

    monkeypatch.setattr(u, "_usage_collection", lambda: AsyncMongoMockClient()["t"]["empty"])
    assert (await u.usage_today())["runs_today"] == 0

    cube = {"id": ["indicators"], "size": [1], "dimension": {"indicators": {"category": {"index": ["OTHER"]}}},
            "value": [5]}
    assert E.rows_from_jsonstat(cube) == [{"month": "", "partner": "", "value": None, "kg": None}]

    assert sc.tally([SimpleNamespace(action="DISPATCH_RFQ", status="WITHDRAWN", created_at=NOW, modified_body=None,
                                     financial_impact_usd=5.0)])["sourcing"]["gates"] == 1

    deal = SimpleNamespace(deal_id="D", quantity_mt=10, parties=[])
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),             patch("shared.connectors.qc_providers.enabled_qc_providers", return_value=[]),             patch("shared.orchestration.inspection_plan.plan_for", new=AsyncMock(return_value=["PSI"])),             patch("shared.orchestration.supplier_performance.cached", new=AsyncMock(return_value={})):
        assert (await io.options("D"))["plans"][0]["current"] is True

    dm = SimpleNamespace(get_pi_by_deal=AsyncMock(return_value=None), get_po_by_deal=AsyncMock(return_value=None))
    with patch("services.dealmaker.repository.get_repository", return_value=dm),             patch("shared.pricebook.estimate", new=AsyncMock(return_value=SimpleNamespace(n_observations=0, median=None))):
        assert (await ra._prices(SimpleNamespace(deal_id="D", hs_code="070310", market="AE",
                                                 buyer_target_price_usd=None)))["supplier_now"] is None

    ids, mails = sa._asked(SimpleNamespace(supplier_offers=[SimpleNamespace(supplier_id="s1", email=None)],
                                           match_results=[], parties=[]))
    assert ids == {"s1"} and mails == set()
