"""Directory / scan data items (todo 2026-11-19 batch): CIN company age, directory role clean-up,
operator notes scrubbed, reply quantities in containers / bags, UK HMRC + Eurostat Comext,
registration-number duplicates, multi-source score, per-source yield, RFQ widening on capacity,
harvest calendar, QIMA inspection-day poll."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def _rec(**kw) -> CounterpartyRecord:
    base = dict(id="buyer:x", role="buyer", name="X", source="apify_company_search",
                sources=["apify_company_search"], first_seen_at=NOW, last_seen_at=NOW)
    base.update(kw)
    return CounterpartyRecord(**base)


@pytest.fixture(autouse=True)
def _fresh_directory():
    directory.reset_directory()
    yield
    directory.reset_directory()


# ---------------------------------------------------------------------------
# Indian ids: CIN → company age, Udyam
# ---------------------------------------------------------------------------

def test_cin_year_and_age():
    from shared import indian_ids
    text = "Registered office Pune. CIN: U01100MH2005PTC123456 · GSTIN 27AAACA1234A1Z5"
    cin = indian_ids.find_cin(text)
    assert cin == "U01100MH2005PTC123456"
    assert indian_ids.incorporation_year(cin) == 2005
    assert indian_ids.company_age_years(cin, NOW) == 21
    assert indian_ids.find_cin("U01100MH2999PTC123456") is None  # a year in the future is no CIN
    assert indian_ids.find_udyam("Udyam reg. udyam-mh-12-0012345") == "UDYAM-MH-12-0012345"
    assert indian_ids.is_listed("L15100GJ1990PLC012345") is True


def test_supplier_row_reads_gstin_cin_and_udyam():
    from services.trade_min.service import _seller_from_raw
    s = _seller_from_raw({"company_name": "Acme Agro", "vat_id": "27AAACA1234A1Z5",
                          "id_text": "CIN U01100MH2019PTC123456, UDYAM-MH-12-0012345"},
                         "apify_company_search", "070310")
    assert s.gstin == "27AAACA1234A1Z5" and s.cin == "U01100MH2019PTC123456"
    assert s.incorporation_year == 2019 and s.udyam == "UDYAM-MH-12-0012345"


def test_young_company_is_a_reason_not_a_failure(monkeypatch):
    from services.trade_min.validation import decide
    monkeypatch.setenv("COMPLIANCE_MIN_COMPANY_AGE_YEARS", "2")
    v = decide(CounterpartyValidation(sanctions="clear", email="ok", company_age_years=0, gst="active"),
               role="supplier", country="IN")
    assert v.status == "verified" and any("younger than 2" in r for r in v.reasons)


# ---------------------------------------------------------------------------
# Directory: roles and operator notes
# ---------------------------------------------------------------------------

async def test_indian_maker_found_as_buyer_becomes_a_supplier():
    store = await directory.get_directory()
    await store.upsert(_rec(id="buyer:acmeagro.in", name="Acme Agro Exporters Pvt Ltd", country="IN",
                            website="https://acmeagro.in", markets=["AE"], flags=["role_suspect"]))
    await store.upsert(_rec(id="buyer:trader.in", name="Some Trading", country="IN",
                            website="https://trader.in", markets=["AE"], flags=["role_suspect"]))
    await store.upsert(_rec(id="buyer:gulf.ae", name="Gulf Foods LLC", country="AE", markets=["AE"]))
    counts = await directory.clean_roles()
    assert counts == {"moved_to_suppliers": 1, "excluded_from_outreach": 1}
    suppliers = await store.list("supplier")
    assert [s.name for s in suppliers] == ["Acme Agro Exporters Pvt Ltd"]
    assert "moved_from_buyer" in suppliers[0].flags and "role_suspect" not in suppliers[0].flags
    assert await store.get("buyer:acmeagro.in") is None
    assert await store.get("buyer:gulf.ae") is not None


async def test_a_staff_held_buyer_is_never_moved():
    store = await directory.get_directory()
    await store.upsert(_rec(id="buyer:held.in", name="Held Exporters", country="IN", website="https://held.in",
                            markets=["AE"], flags=["role_suspect"], do_not_contact=True))
    assert (await directory.clean_roles())["moved_to_suppliers"] == 0


async def test_operator_notes_are_scrubbed_from_the_directory():
    store = await directory.get_directory()
    await store.upsert(_rec(id="buyer:a", fit_reason="Write to info@a.com — the general inbox"))
    await store.upsert(_rec(id="buyer:b", name="B", fit_reason="Imports basmati rice into Dubai"))
    assert await directory.scrub_notes() == 1
    assert (await store.get("buyer:a")).fit_reason is None
    assert (await store.get("buyer:b")).fit_reason == "Imports basmati rice into Dubai"


def _staff_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.trade_min.router import _staff, router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@x", is_portless=True)
    return TestClient(app)


def test_clean_route_reports_roles_and_notes():
    body = _staff_client().post("/trade-min/directory/clean").json()
    assert {"changed", "moved_to_suppliers", "excluded_from_outreach", "notes_scrubbed"} <= set(body)


# ---------------------------------------------------------------------------
# Reply quantity in containers / bags, echoed to confirm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "mt", "confirm"), [
    ("We need 2 x 20' containers CIF Jebel Ali", 40.0, "2 × 20' ≈ 40 MT — correct?"),
    ("3 x 40ft HC per month", 66.0, "3 × 40' HC ≈ 66 MT — correct?"),
    ("2000 bags of 25 kg", 50.0, "2,000 bags × 25 kg = 50.0 MT — correct?"),
    ("500 MT please", 500.0, None),
])
def test_quantity_detail(text, mt, confirm):
    from shared.orchestration.extraction import parse_buyer
    out = parse_buyer(text)
    assert out["quantity_mt"] == mt and out.get("quantity_confirm") == confirm


def test_container_size_from_the_catalogue():
    from shared.orchestration.extraction import quantity_detail
    assert quantity_detail("2 x 20ft reefer", container_mt=26)["quantity_mt"] == 52.0


def test_moq_is_not_the_order_quantity():
    from shared.orchestration.extraction import quantity_detail
    assert quantity_detail("MOQ 2 x 20' containers") == {}


def test_brief_asks_the_buyer_to_confirm_a_converted_quantity():
    from agents.negotiation_agent.briefing import buyer_missing
    out = buyer_missing({"quantity_to_confirm": "2 × 20' ≈ 40 MT — correct?", "quantity_mt": 40,
                         "destination_port": "Jebel Ali", "grade": "1121", "delivery_window": "March",
                         "certifications_required": ["phyto"], "packing": "std"},
                        qc_selected=True, quantity_known=True)
    assert out[0] == "confirm the quantity as we read it: 2 × 20' ≈ 40 MT — correct?"


# ---------------------------------------------------------------------------
# National trade statistics
# ---------------------------------------------------------------------------

async def test_hmrc_unit_price_and_india_share(monkeypatch):
    from shared.connectors import national_trade as nt
    monkeypatch.setenv("NATIONAL_TRADE_SOURCES_ENABLED", "true")
    monkeypatch.setenv("FX_RATE_GBP", "0.8")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["filter"] = request.url.params.get("$filter")
        return httpx.Response(200, json={"value": [
            {"MonthId": 202608, "Value": 80000, "NetMass": 100000, "Country": {"CountryCodeAlpha": "IN"}},
            {"MonthId": 202609, "Value": 160000, "NetMass": 100000, "Country": {"CountryCodeAlpha": "PK"}},
        ]})
    out = await nt.UkHmrcTradeConnector(transport=httpx.MockTransport(handler)).quote({"hs_code": "100630"})
    assert "CommodityId ge 10063000 and CommodityId le 10063099" in seen["filter"]
    assert out["quantity_mt"] == 200.0 and out["value_usd"] == 300000.0
    assert out["unit_value_usd_per_mt"] == 1500.0 and out["india_unit_value_usd_per_mt"] == 1000.0
    assert out["data_period"] == "2026-09"


async def test_comext_jsonstat_rows(monkeypatch):
    from shared.connectors import national_trade as nt
    monkeypatch.setenv("NATIONAL_TRADE_SOURCES_ENABLED", "true")
    monkeypatch.setenv("FX_RATE_EUR", "0.9")
    cube = {
        "id": ["partner", "indicators", "time"], "size": [2, 2, 1],
        "dimension": {
            "partner": {"category": {"index": {"IN": 0, "EXT_EU27_2020": 1}}},
            "indicators": {"category": {"index": {"VALUE_IN_EUROS": 0, "QUANTITY_IN_100KG": 1}}},
            "time": {"category": {"index": {"2026-08": 0}}},
        },
        # IN: 90,000 EUR / 1,000 x 100 kg; extra-EU: 900,000 EUR / 5,000 x 100 kg
        "value": {"0": 90000, "1": 1000, "2": 900000, "3": 5000},
    }
    out = await nt.EurostatComextConnector(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=cube))).quote({"hs_code": "100630", "market": "DE"})
    assert out["quantity_mt"] == 500.0 and out["unit_value_usd_per_mt"] == 2000.0
    assert out["india_quantity_mt"] == 100.0 and out["india_unit_value_usd_per_mt"] == 1000.0


async def test_national_sources_are_off_in_tests_and_pick_the_market():
    from shared.connectors import national_trade as nt
    assert nt.source_for("GB") == "uk_hmrc" and nt.source_for("DE") == "eurostat_comext"
    assert nt.source_for("AE") is None
    out = await nt.UkHmrcTradeConnector().quote({"hs_code": "100630"})
    assert out["records"] == 0 and out["disabled"] is True


async def test_phase1_uses_the_national_quantity_as_live_demand(monkeypatch):
    from services.trade_min import service as svc
    from services.trade_min.config import DEFAULT_CONFIG
    monkeypatch.setenv("NATIONAL_TRADE_SOURCES_ENABLED", "true")

    async def fake_quote(name, req):
        if name == "uk_hmrc":
            return {"source": "uk_hmrc", "quantity_mt": 2500.0, "months": ["2026-08"], "data_period": "2026-08",
                    "unit_value_usd_per_mt": 1400.0, "india_unit_value_usd_per_mt": 1250.0}
        return None
    monkeypatch.setattr(svc, "_safe_quote", fake_quote)
    import dataclasses
    cfg = dataclasses.replace(DEFAULT_CONFIG, phase1_min_connectors=1)
    r = await svc._phase1_historical_demand("100630", "GB", cfg)
    assert r.active_connectors == 1 and r.price_benchmark_usd_per_mt == 1250.0
    assert r.price_benchmark_source == "uk_hmrc:india" and r.demand_index > 0


# ---------------------------------------------------------------------------
# Duplicates by registration number, multi-source score, per-source yield
# ---------------------------------------------------------------------------

def test_same_gstin_is_a_duplicate():
    from services.trade_min.dedupe import suggestions
    a = _rec(id="supplier:a", role="supplier", name="Acme Agro", gstin="27AAACA1234A1Z5")
    b = _rec(id="supplier:b", role="supplier", name="Totally Different Name", gstin="27aaaca1234a1z5")
    groups = suggestions([a, b])
    assert groups and "registration number" in groups[0]["reasons"]


def test_more_sources_and_register_evidence_score_higher():
    from services.trade_min.relationships import score_of
    one = _rec(sources=["apify_company_search"])
    three = _rec(sources=["apify_company_search", "manual", "trade_fair:gulfood"], cin="U01100MH2005PTC123456")
    s1, _ = score_of(one, {}, None)
    s3, basis = score_of(three, {}, None)
    assert s3 - s1 == 6 + 5
    assert "found by 3 sources (+6)" in basis and "company register evidence (+5)" in basis


def test_source_yield_tally():
    from services.trade_min.source_yield import tally
    rows = [
        _rec(id="b1", sources=["apify_company_search"], relationship={"stage": "traded"},
             validation=CounterpartyValidation(status="verified")),
        _rec(id="b2", sources=["apify_company_search", "manual"], relationship={"stage": "contacted"}),
        _rec(id="b3", sources=["manual"], relationship={"stage": "new"}),
    ]
    t = tally(rows)
    assert t["apify_company_search"]["found"] == 2 and t["apify_company_search"]["traded"] == 1
    assert t["apify_company_search"]["contacted"] == 2 and t["apify_company_search"]["reply_pct"] == 50.0
    assert t["manual"]["found"] == 2 and t["manual"]["verified"] == 0


async def test_source_yield_report_splits_actor_spend(monkeypatch):
    from services.trade_min import source_yield
    store = await directory.get_directory()
    await store.upsert(_rec(id="buyer:a", sources=["apify_company_search"]))
    await store.upsert(_rec(id="buyer:b", name="B", sources=["apify_company_search"]))

    async def spend(days, now):
        return {"COMPANY_CONTACT_FINDER": 4.0}
    monkeypatch.setattr(source_yield, "_actor_spend", spend)
    r = await source_yield.report("buyer", 30)
    row = next(s for s in r["sources"] if s["source"] == "apify_company_search")
    assert row["found"] == 2 and row["apify_usd"] == 4.0


def test_yield_route_registered():
    c = _staff_client()
    assert c.get("/trade-min/directory/yield?role=buyer").status_code == 200
    assert c.get("/trade-min/directory/yield?role=nobody").status_code == 422


# ---------------------------------------------------------------------------
# RFQ widens while stated capacity does not cover the buyers' quantity
# ---------------------------------------------------------------------------

def _deal(qty=None, offers=(), candidates=()):
    return SimpleNamespace(deal_id="D", quantity_mt=qty, supplier_offers=list(offers), buyer_candidates=list(candidates))


def test_rfq_cap_from_capacity(monkeypatch):
    from shared.orchestration import continuations as c
    monkeypatch.setattr(c, "_RFQ_MAX_SUPPLIERS", 5)
    ranked = [{"supplier_id": f"s{i}", "capacity_mt": 100.0} for i in range(6)]
    assert c.rfq_cap(_deal(qty=250), ranked) == 3  # 3 × 100 covers 250
    assert c.rfq_cap(_deal(qty=450), ranked) == 5  # needs 5
    assert c.rfq_cap(_deal(qty=5000), ranked) == 5  # never above RFQ_MAX_SUPPLIERS
    unknown = [{"supplier_id": f"s{i}"} for i in range(6)]
    assert c.rfq_cap(_deal(qty=100), unknown) == 5  # nobody states capacity: ask the most allowed
    assert c.rfq_cap(_deal(qty=None), ranked) == 5


def test_buyers_quantity_from_candidates():
    from shared.orchestration import continuations as c
    cands = [SimpleNamespace(status="interested", requirement=SimpleNamespace(quantity_mt=300)),
             SimpleNamespace(status="pending", requirement=SimpleNamespace(quantity_mt=200)),
             SimpleNamespace(status="declined", requirement=SimpleNamespace(quantity_mt=900))]
    assert c._buyers_quantity_mt(_deal(qty=50, candidates=cands)) == 500


def test_offer_capacity_counts():
    from shared.orchestration import continuations as c
    offer = SimpleNamespace(supplier_id="s1", capacity_mt_per_month=400.0)
    assert c._capacity_mt({"supplier_id": "s1"}, _deal(offers=[offer])) == 400.0


# ---------------------------------------------------------------------------
# Harvest calendar + mandi backfill
# ---------------------------------------------------------------------------

def test_season_phases():
    from services.trade_min.mandi_history import season_of
    assert season_of("Turmeric", date(2026, 3, 10))["phase"] == "harvest"
    assert season_of("Turmeric", date(2026, 1, 10))["phase"] == "lean"
    assert season_of("Turmeric", date(2026, 8, 10))["phase"] == "between"
    assert season_of("Banana", date(2026, 8, 10))["phase"] == "year_round"
    assert season_of("Unknown crop") is None


def test_season_override(monkeypatch):
    from services.trade_min.mandi_history import harvest_months
    monkeypatch.setenv("HARVEST_MONTHS_TURMERIC", "5,6")
    assert harvest_months("Turmeric") == (5, 6)


async def test_trend_carries_the_season_before_any_history():
    from services.trade_min import mandi_history
    mandi_history.reset_for_tests()
    t = await mandi_history.trend("Rice")
    assert t["direction"] == "unknown" and t["season"]["harvest_months"] == [10, 11, 12]


async def test_backfill_from_the_historical_dataset(monkeypatch):
    from services.trade_min import mandi_history
    mandi_history.reset_for_tests()
    monkeypatch.setenv("DATA_GOV_IN_API_KEY", "k")
    today = datetime.now(timezone.utc).date()
    d1, d2 = today - timedelta(days=3), today - timedelta(days=200)
    records = [{"Arrival_Date": d1.strftime("%d/%m/%Y"), "Modal_Price": "2000"},
               {"Arrival_Date": d1.strftime("%d/%m/%Y"), "Modal_Price": "2200"},
               {"Arrival_Date": d2.strftime("%d/%m/%Y"), "Modal_Price": "1800"}]
    out = await mandi_history.backfill("Rice", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"records": records})))
    assert out["added"] == 1  # the 200-day-old point is outside the window
    pts = await mandi_history.points("Rice")
    assert pts[0]["modal_inr_per_quintal"] == 2100.0


async def test_backfill_needs_a_key(monkeypatch):
    from services.trade_min import mandi_history
    monkeypatch.delenv("DATA_GOV_IN_API_KEY", raising=False)
    assert (await mandi_history.backfill("Rice"))["added"] == 0


# ---------------------------------------------------------------------------
# QIMA: shorter poll from the inspection day until the report is due
# ---------------------------------------------------------------------------

def test_inspection_day_poll(monkeypatch):
    from shared.orchestration.qc_schedule import poll_minutes_for
    monkeypatch.setenv("QC_POLL_MINUTES", "60")
    monkeypatch.setenv("QC_POLL_MINUTES_INSPECTION_DAY", "15")
    assert poll_minutes_for("2026-11-19", NOW) == 15
    assert poll_minutes_for("2026-11-17", NOW) == 15  # report days after the inspection
    assert poll_minutes_for("2026-11-16", NOW) == 60
    assert poll_minutes_for("2026-11-25", NOW) == 60
    assert poll_minutes_for(None, NOW) == 60 and poll_minutes_for("soon", NOW) == 60


def test_qc_poll_due_uses_the_inspection_day(monkeypatch):
    from services.shield_link import qc
    monkeypatch.setenv("QC_POLL_MINUTES", "60")
    today = datetime.now(timezone.utc)
    tpo = SimpleNamespace(last_polled_at=today - timedelta(minutes=20), inspection_date=today.date().isoformat())
    assert qc._poll_due(tpo, today, False) is True
    tpo.inspection_date = (today + timedelta(days=5)).date().isoformat()
    assert qc._poll_due(tpo, today, False) is False
