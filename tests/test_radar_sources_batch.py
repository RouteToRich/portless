"""2026-11-19 batch 6: radar sources — profit gate, demand signals as opportunities, list actors into the
directory (A.2 / A.5 / A.9 – A.15 / A.18), India's export statistics + mirror data (A.1), buyer potential
from import records, freight procurement by e-mail, country risk from direct feeds + A.32, requirement
lines and attribute filters on Buyers & Suppliers."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import DealRecord, DealStatus
from services.trade_min import demand_opportunities, directory_sources, india_exports
from services.trade_min.schemas import BuyerSignal, CounterpartyRecord, SellerCandidate
from shared import catalogue, country_feeds, country_names, country_profiles
from shared.orchestration import compliance, freight_procurement
from tests.factories import make

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
STAFF = SimpleNamespace(user_id="u-ops", email="ops@portless.test", name="Ops", org_id=None, is_portless=True)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for mod in (country_profiles, country_feeds, india_exports, directory_sources, freight_procurement, compliance):
        mod.reset_for_tests()
    monkeypatch.setattr("shared.mongo.collection", lambda name: None, raising=False)
    from services.trade_min import directory
    monkeypatch.setattr(directory, "_directory", directory.InMemoryDirectory())
    monkeypatch.setattr("shared.telegram.send_message", AsyncMock(), raising=False)
    monkeypatch.setattr("shared.audit_log.record", AsyncMock())
    yield
    catalogue.reset_for_tests()


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", r)
    return r


@pytest.fixture
def opp_repo(monkeypatch):
    from services.trade_min.repository import InMemoryFallbackRepository as OppRepo
    r = OppRepo()
    monkeypatch.setattr("services.trade_min.repository.get_repository", AsyncMock(return_value=r))
    return r


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def send_email(req):
        calls.append(req)
        return SimpleNamespace(outcome=SimpleNamespace(value="sent"))
    monkeypatch.setattr("services.email_gateway.sender.send_email", send_email)
    return calls


def _connector(monkeypatch, results: dict):
    async def quote(request):
        return results
    monkeypatch.setattr("shared.connectors.registry.get_connector", lambda name: SimpleNamespace(quote=quote))


def _line(**kw):
    base = dict(hs_code="090411", name="black pepper", synonyms=["pepper"], markets=["AE"], hs8_codes=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _buyer(**kw) -> BuyerSignal:
    base = dict(buyer_name="Gulf Foods", buyer_country="AE", signal_type="web_search", source="apify_company_search",
                urgency_tier=1)
    base.update(kw)
    return BuyerSignal(**base)


def _seller(**kw) -> SellerCandidate:
    base = dict(candidate_id="s1", company_name="Kerala Spices", location_city="Kochi", location_state="Kerala",
                hs_code="090411", source="apify_company_search", price_unit="USD/MT")
    base.update(kw)
    return SellerCandidate(**base)


# ---------------------------------------------------------------------------
# Profit gate
# ---------------------------------------------------------------------------

async def test_profit_gate_prices_and_reasons(monkeypatch):
    from services.trade_min import service as tm
    est = SimpleNamespace(median=None)
    monkeypatch.setattr("shared.pricebook.estimate", AsyncMock(return_value=est))
    p1 = tm._Phase1Result()
    out = await tm._profit_gate("090411", "AE", p1, [_buyer()], [_seller()])
    assert not out["clears"] and out["missing"] == ["buyer price", "supplier price"] and "no buyer price" in out["reason"]
    out = await tm._profit_gate("090411", "AE", p1, [_buyer(unit_price_usd=7000, price_unit="USD/MT")],
                                [_seller(estimated_fob_price_usd=5000)])
    assert out["clears"] and out["flags"] == ["no_market_statistics"] and out["buyer_basis"] == "buyer's own price"
    p1.source, p1.price_benchmark_usd_per_mt = "comtrade", 5100.0
    out = await tm._profit_gate("090411", "AE", p1, [_buyer()], [_seller(estimated_fob_price_usd=5000)])
    assert not out["clears"] and "under the" in out["reason"] and out["buyer_basis"] == "market unit value"
    monkeypatch.setattr("shared.pricebook.estimate", AsyncMock(side_effect=[SimpleNamespace(median=7200.0),
                                                                            SimpleNamespace(median=5000.0)]))
    out = await tm._profit_gate("090411", "AE", tm._Phase1Result(source="comtrade"), [], [])
    assert out["clears"] and out["flags"] == ["price_check_needed"] and out["supplier_basis"] == "price book (supplier side)"
    monkeypatch.setattr("shared.pricebook.estimate", AsyncMock(side_effect=RuntimeError("db")))
    assert (await tm._profit_gate("090411", "AE", tm._Phase1Result(), [], []))["missing"]


async def test_scan_one_keeps_a_corridor_without_a_measured_gap(monkeypatch):
    from services.trade_min import service as tm
    from services.trade_min.config import TradeMinConfig
    monkeypatch.setenv("RADAR_PROFIT_GATE", "true")
    monkeypatch.setattr(tm.start_check, "export_policy", AsyncMock(return_value={"severity": "info"}))
    monkeypatch.setattr(tm, "_phase1_historical_demand", AsyncMock(return_value=tm._Phase1Result()))
    monkeypatch.setattr(tm, "_phase2_live_signals", AsyncMock(return_value=tm._Phase2Result(
        buyer_signals=[_buyer(unit_price_usd=7000, price_unit="USD/MT")])))
    monkeypatch.setattr(tm, "_phase4_seller_discovery", AsyncMock(return_value=[_seller(estimated_fob_price_usd=5000)]))
    monkeypatch.setattr(tm.directory, "record_scan", AsyncMock())
    from services.trade_min.schemas import TariffData
    monkeypatch.setattr(tm, "_phase3_tariff_gate", AsyncMock(return_value=TariffData(
        hs_code="090411", destination_country="AE", applied_duty_pct=0, total_landed_cost_add_pct=0, fetched_at=NOW)))
    monkeypatch.setattr(tm.validation, "validate_scan", AsyncMock())
    p5 = AsyncMock(return_value=None)
    monkeypatch.setattr(tm, "_phase5_assemble_and_persist", p5)
    await tm._scan_one("090411", "AE", TradeMinConfig())
    assert p5.await_args.kwargs["gate"]["clears"]
    # no supplier price anywhere: dropped, the scan report says what is missing
    monkeypatch.setattr(tm, "_phase4_seller_discovery", AsyncMock(return_value=[_seller()]))
    monkeypatch.setattr("shared.pricebook.estimate", AsyncMock(return_value=SimpleNamespace(median=None)))
    p5.reset_mock()
    assert await tm._scan_one("090411", "AE", TradeMinConfig()) is None and not p5.await_count


async def test_phase5_with_a_gate_skips_the_gap_rules(monkeypatch, opp_repo):
    from services.trade_min import service as tm
    from services.trade_min.config import TradeMinConfig
    from services.trade_min.schemas import TariffData
    for name in ("attach",):
        monkeypatch.setattr(tm.pair_match, name, lambda opp: opp)
    monkeypatch.setattr(tm.start_check, "annotate", AsyncMock())
    monkeypatch.setattr(tm.freight_lanes, "refresh", AsyncMock())
    monkeypatch.setattr(tm.profit, "attach", AsyncMock())
    monkeypatch.setattr(tm, "get_repository", AsyncMock(return_value=opp_repo))
    tariff = TariffData(hs_code="090411", destination_country="AE", applied_duty_pct=0, total_landed_cost_add_pct=0, fetched_at=NOW)
    gate = {"clears": True, "flags": ["no_market_statistics"], "buyer_price": 7000, "buyer_basis": "x", "supplier_price": 5000,
            "supplier_basis": "y", "landed_cost": 5500, "margin_pct": 21.4, "floor_pct": 8, "source": None}
    opp = await tm._phase5_assemble_and_persist("090411", "AE", tm._Phase1Result(), tm._Phase2Result(), tariff, [],
                                                TradeMinConfig(), gate=gate)
    assert opp is not None and opp.status == "ACTIVE" and "no_market_statistics" in opp.flags
    assert opp.profit_gate["margin_pct"] == 21.4 and opp.source == "apify_company_search"


# ---------------------------------------------------------------------------
# Demand signals → opportunities
# ---------------------------------------------------------------------------

async def test_demand_lead_creates_then_refreshes_an_opportunity(monkeypatch, opp_repo):
    monkeypatch.setattr(catalogue, "active_lines", lambda: [_line()])
    monkeypatch.setattr("services.trade_min.profit.attach", AsyncMock())
    assert demand_opportunities.product_for({"hs_code": "09041100"}, [_line()]) == "090411"
    assert demand_opportunities.product_for({"hs_code": "100630"}, [_line()]) == "100630"
    assert demand_opportunities.product_for({"product": "Whole black pepper 550"}, [_line()]) == "090411"
    assert demand_opportunities.product_for({"product": "steel"}, [_line()]) is None
    from services.trade_min.directory import get_directory
    await (await get_directory()).upsert(CounterpartyRecord(id="supplier:k", role="supplier", name="Kerala Spices",
                                                            source="scan", hs_codes=["090411"], first_seen_at=NOW, last_seen_at=NOW,
                                                            estimated_fob_price_usd=5000))
    lead = {"source": "trade_connect", "kind": "enquiry", "buyer_name": "Al Noor", "country": "AE",
            "product": "black pepper", "email": "p@alnoor.ae", "quantity_mt": 40}
    out = await demand_opportunities.from_lead(lead)
    assert out["action"] == "created"
    opp = await opp_repo.get(out["opportunity_id"])
    assert opp.found_by == "demand_signal" and opp.buyer_signals[0].buyer_name == "Al Noor"
    assert opp.seller_candidates[0].company_name == "Kerala Spices" and "from_demand_signal" in opp.flags
    again = await demand_opportunities.from_lead(lead)
    assert again["action"] == "already_listed"
    second = await demand_opportunities.from_lead({**lead, "buyer_name": "Dubai Spice", "kind": "tender"})
    assert second["action"] == "buyer_added_first"
    assert (await opp_repo.get(out["opportunity_id"])).buyer_signals[0].buyer_name == "Dubai Spice"
    assert await demand_opportunities.from_lead({**lead, "country": None}) is None
    monkeypatch.setattr(catalogue, "active_lines", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert await demand_opportunities.from_lead(lead) is None


async def test_reorders_become_opportunities(monkeypatch):
    monkeypatch.setattr("shared.orchestration.reorders.due", AsyncMock(return_value=[
        {"buyer": "Gulf Foods", "market": "AE", "hs_code": "090411", "deal_id": "D1"}, {"buyer": None}]))
    monkeypatch.setattr(demand_opportunities, "from_lead", AsyncMock(return_value={"opportunity_id": "O1", "action": "created"}))
    out = await demand_opportunities.from_reorders(NOW)
    assert out == [{"opportunity_id": "O1", "action": "created", "deal_id": "D1"}]


# ---------------------------------------------------------------------------
# List actors into the directory
# ---------------------------------------------------------------------------

def test_list_actor_normalisers():
    from shared.connectors import apify_more as am
    assert am.ApifyTradeFairConnector().company({"companyName": "X", "fair": "gulfood", "role": "buyer"})["source"] == "trade_fair:gulfood"
    reg = am.ApifyExporterRegistriesConnector().company({"companyName": "K", "registry": "spices_board", "registrationNumber": "CRES-1"})
    assert reg["role"] == "supplier" and reg["registration_number"] == "CRES-1" and reg["country"] == "IN"
    b2b = am.ApifyIndiaB2BConnector().company({"companyName": "B", "site": "indiamart", "priceUsdPerMt": "4800", "maskedPhone": True})
    assert b2b["price_usd_per_mt"] == 4800.0 and b2b["flags"] == ["masked_phone"]
    assert am.ApifyApmcTradersConnector().company({"traderName": "T"})["name"] == "T"
    assert am.ApifyAssociationMembersConnector().company({"companyName": "A", "association": "asta"})["flags"] == ["association_member"]
    assert am.ApifyImporterListsConnector().company({"companyName": "I", "mentions": 3})["mentions"] == 3
    assert am.ApifyBusinessDirectoryConnector().company({"companyName": "D", "directory": "europages"})["source"] == "directory:europages"
    assert am.ApifyLocalBusinessConnector().company({"name": "L", "rating": "4.5"})["rating"] == 4.5
    cc = am.ApifyCompetitorClientsConnector().company({"clientName": "C", "clientCountry": "DEU", "exporterName": "VN Co"})
    assert cc["country"] == "DE" and cc["buys_from"] == "VN Co"
    us = am.ApifyUsImportRecordsConnector().company({"consigneeName": "US Imp", "totalMt12m": "300", "shipments12m": 12,
                                                    "frequencyDays": 30, "isForwarder": True})
    assert us["annual_volume_mt"] == 300.0 and us["flags"] == ["forwarder"]
    row = am.ApifyIndiaExportStatsConnector.row({"hsCode": "0904.11", "country": "ARE", "period": "2026-09",
                                                 "valueUsd": 10000, "quantityMt": 2})
    assert row["unit_value_usd_per_mt"] == 5000.0 and row["country"] == "AE"
    a32 = am.ApifyCountryRiskConnector.country({"country": "SAU", "ecgcRating": "a2", "escrowProviders": ["tazapay"],
                                                "attestation": {"required": True, "documents": ["invoice attestation"]}})
    assert a32 == {"country": "SA", "ecgc_rating": "A2", "ecgc_rating_date": None, "escrow_providers": ["tazapay"],
                   "attestation_documents": ["invoice attestation"], "fetched_at": None}


async def test_list_actor_quotes(monkeypatch):
    from shared.connectors import apify_more as am
    monkeypatch.setenv("APIFY_TOKEN", "t")
    for cls in (*am.DIRECTORY_CONNECTORS, am.ApifyIndiaExportStatsConnector, am.ApifyCountryRiskConnector):
        c = cls()
        assert (await c.quote({}))["stub"]
        monkeypatch.setenv(f"APIFY_ACTOR_{cls.actor_key}_BUILT", "true")
        monkeypatch.setattr(c, "run", AsyncMock(return_value=[{"companyName": "X", "country": "DE", "period": "2026-01",
                                                               "hsCode": "0904"}]))
        out = await c.quote({"products": ["pepper"]})
        assert out["total"] == 1


async def test_directory_ingest_suppliers_buyers_prices_and_contacts(monkeypatch):
    recorded = AsyncMock(return_value=True)
    monkeypatch.setattr("shared.pricebook.record", recorded)

    async def quote(request):
        return {"contacts": [{"input_name": "Hamburg Spice", "website": "https://hs.de", "email": "i@hs.de"}]}
    monkeypatch.setattr("shared.connectors.registry.get_connector", lambda name: SimpleNamespace(quote=quote))
    companies = [
        {"name": "Kerala B2B", "role": "supplier", "source": "b2b:indiamart", "price_usd_per_mt": 4800, "gstin": "32AAA"},
        {"name": "Spices Reg", "role": "supplier", "source": "registry:spices_board", "registration_number": "CRES-9"},
        {"name": "US Imp", "role": "buyer", "source": "us_customs", "annual_volume_mt": 300, "shipments_12m": 12,
         "frequency_days": 30, "last_import_date": "2026-10-20", "typical_shipment_mt": 25},
        {"name": "Fwd Co", "role": "buyer", "flags": ["forwarder"]},
        {"name": "Hamburg Spice", "role": "buyer", "source": "article"},
        {"name": ""},
    ]
    out = await directory_sources.ingest(companies, "090411", "DE")
    assert out == {"stored": 4, "skipped": 2, "prices": 1}
    from services.trade_min.directory import get_directory
    store = await get_directory()
    sup = {r.name: r for r in await store.list("supplier")}
    assert "registration:CRES-9" in sup["Spices Reg"].flags and sup["Kerala B2B"].estimated_fob_price_usd == 4800
    buy = {r.name: r for r in await store.list("buyer")}
    assert buy["US Imp"].volume_source == "us_customs" and buy["US Imp"].frequency_days == 30
    assert buy["Hamburg Spice"].email == "i@hs.de"
    pot = directory_sources.potential(buy["US Imp"], date(2026, 11, 19))
    assert pot["annual_mt"] == 300 and pot["next_order_due"] == "2026-11-19" and pot["reorder_probability"] == 0.5
    lot = SimpleNamespace(annual_volume_mt=None, typical_shipment_mt=20, frequency_days=73, shipments_12m=None,
                          last_import_date=None, volume_source=None)
    assert directory_sources.potential(lot)["annual_mt"] == 100.0
    assert directory_sources.potential(SimpleNamespace(annual_volume_mt=None, typical_shipment_mt=10, frequency_days=None,
                                                       shipments_12m=4)) ["annual_mt"] == 40.0
    assert directory_sources.potential(SimpleNamespace(annual_volume_mt=None, typical_shipment_mt=None, frequency_days=None,
                                                       shipments_12m=None)) is None


async def test_directory_run_and_weekly(monkeypatch):
    _connector(monkeypatch, {"stub": True})
    assert (await directory_sources.run("apify_trade_fairs", "090411", "AE"))["stub"]
    _connector(monkeypatch, {"companies": [{"name": "Fair Co", "role": "buyer", "source": "trade_fair:gulfood"}]})
    assert (await directory_sources.run("apify_trade_fairs", "090411", "AE"))["stored"] == 1
    monkeypatch.setattr(catalogue, "active_lines", lambda: [_line()])
    assert await directory_sources.weekly(NOW) is None  # pinned off
    monkeypatch.setenv("DIRECTORY_SOURCES_ENABLED", "true")
    out = await directory_sources.weekly(NOW)
    assert len(out["runs"]) == len(directory_sources.LIST_CONNECTORS) and await directory_sources.weekly(NOW) is None


# ---------------------------------------------------------------------------
# India's export statistics + mirror data
# ---------------------------------------------------------------------------

async def test_india_exports_latest_trend_mirror_and_supplier_check(monkeypatch):
    rows = [{"hs_code": "09041100", "country": "AE", "period": f"2026-{m:02d}", "value_usd": 5000.0 * (2 if m > 6 else 1),
             "quantity_mt": 1.0 * (2 if m > 6 else 1)} for m in range(1, 13)]
    assert await india_exports.apply(rows + [{"hs_code": "", "country": "AE", "period": "x"}]) == 12
    stats = india_exports.latest("090411", "AE")
    assert stats["unit_value_usd_per_mt"] == 5000.0 and stats["trend_pct"] == 100.0 and stats["ships_already"]
    assert india_exports.flags_for(stats) == ["india_ships_already", "india_exports_rising"]
    assert india_exports.flags_for(None) == [] and india_exports.latest("1006", "AE") is None
    assert india_exports.stale(None) and india_exports.stale("2023", NOW) and not india_exports.stale("2025-10", NOW)
    from services.trade_min.service import _Phase1Result
    p1 = _Phase1Result(data_period="2023", price_benchmark_usd_per_mt=3000)
    india_exports.apply_to_phase1(p1, "090411", "AE", NOW)
    assert p1.price_benchmark_usd_per_mt == 5000.0 and p1.price_benchmark_source == "india_exports:mirror"
    assert "mirror_data" in p1.market_data_flags
    fresh = _Phase1Result(data_period="2026-09", price_benchmark_usd_per_mt=6000)
    india_exports.apply_to_phase1(fresh, "090411", "AE", NOW)
    assert fresh.price_benchmark_usd_per_mt == 6000 and fresh.india_to_market_usd_per_mt == 5000.0
    chk = india_exports.supplier_check("090411", "AE", 3500)
    assert chk["flag"] == "supplier_far_below_india_average" and india_exports.supplier_check("090411", "AE", None) is None
    assert india_exports.supplier_check("090411", "AE", 8000)["flag"] == "supplier_far_above_india_average"
    falling = india_exports.flags_for({"ships_already": False, "trend_pct": -40})
    assert falling == ["india_not_shipping", "india_exports_falling"]
    monkeypatch.setattr(catalogue, "active_lines", lambda: [_line()])
    assert await india_exports.monthly(NOW) is None
    monkeypatch.setenv("INDIA_EXPORTS_ENABLED", "true")
    _connector(monkeypatch, {"rows": rows[:1]})
    assert (await india_exports.monthly(NOW))["stored"] == 1 and await india_exports.monthly(NOW) is None
    _connector(monkeypatch, {"stub": True})
    assert (await india_exports.monthly(NOW + timedelta(days=31)))["stub"]


# ---------------------------------------------------------------------------
# Freight procurement
# ---------------------------------------------------------------------------

def test_parse_freight_quotes():
    q = freight_procurement.parse_quote("Rate: 20' USD 1,450 and 40'HC USD 2,300 all in. 14 days free at destination. "
                                        "Transit 18 days. Valid till 30/11/2026. Next sailing 25.11.2026")
    assert q["rates"] == {"20GP": 1450.0, "40HC": 2300.0} and q["free_days"] == 14 and q["transit_days"] == 18
    assert q["valid_until"] == "2026-11-30" and q["sailing"] == "2026-11-25"
    assert freight_procurement.parse_quote("USD 2100 per 40ft, free time 10")["rates"] == {"40GP": 2100.0}
    assert freight_procurement.parse_quote("All in USD 3,900 total")["lump_sum_usd"] == 3900.0
    assert freight_procurement.parse_quote("thanks, will revert") is None
    assert freight_procurement.total_usd({"rates": {"40GP": 2000}}, {"40HC": 2}) == 4000
    assert freight_procurement.total_usd({"rates": {"20GP": 1000}}, {"40HC": 1}) is None
    assert freight_procurement.total_usd({"lump_sum_usd": 900}, {}) == 900
    assert freight_procurement.counter_target(2000, 1900, None) == 1900
    assert freight_procurement.counter_target(2000, 1500, None) == 1840.0  # never more than 8 % under
    assert freight_procurement.counter_target(2000, None, None) == 1940.0
    assert freight_procurement._iso("bad") is None and freight_procurement.parse_token("x") is None


async def test_freight_procurement_flow(repo, sent, monkeypatch):
    with pytest.raises(ValueError):
        await freight_procurement.put_forwarder({"name": "", "email": "x"}, by="o")
    with pytest.raises(ValueError):
        await freight_procurement.put_forwarder({"name": "F", "email": "f@f.com", "lanes": []}, by="o")
    a = await freight_procurement.put_forwarder({"name": "Alpha Logistics", "email": "rates@alpha.in", "lanes": ["AE"]}, by="o")
    await freight_procurement.put_forwarder({"name": "Beta Cargo", "email": "q@beta.in", "lanes": ["ALL"]}, by="o")
    await freight_procurement.put_forwarder({"name": "EU Only", "email": "q@eu.de", "lanes": ["DE"]}, by="o")
    assert [f["name"] for f in await freight_procurement.forwarders("AE")] == ["Alpha Logistics", "Beta Cargo"]
    await repo.save(make(DealRecord, deal_id="DEAL-FRT001", hs_code="090411", market="AE", status=DealStatus.PO_ISSUED,
                         org_id="portless", quantity_mt=20.0))
    await repo.save(make(DealRecord, deal_id="DEAL-FRT002", hs_code="090411", market="BR", status=DealStatus.PO_ISSUED,
                         org_id="portless", quantity_mt=20.0))
    assert await freight_procurement.sweep(NOW) is None  # pinned off
    monkeypatch.setenv("FREIGHT_PROCUREMENT_ENABLED", "true")
    out = await freight_procurement.sweep(NOW)
    assert out["asked"] == ["DEAL-FRT001", "DEAL-FRT002"] and len(sent) == 3 and "[FRT:DEAL-FRT001]" in sent[0].subject
    assert (await freight_procurement.sweep(NOW))["asked"] == []
    assert (await freight_procurement.request_quotes("DEAL-FRT002", by="o"))["requests"]  # already asked: unchanged
    assert not (await freight_procurement.handle_reply("DEAL-FRT001", "x@other.com", "20' USD 1000"))["handled"]
    assert not (await freight_procurement.handle_reply("DEAL-FRT001", "rates@alpha.in", "will revert"))["handled"]
    monkeypatch.setattr("services.trade_min.freight_lanes.save", AsyncMock())
    r1 = await freight_procurement.handle_reply("DEAL-FRT001", "sales@alpha.in", "20' USD 1,500, transit 20 days, valid until 2026-12-31")
    assert r1["quote"]["total_usd"] == 1500.0
    with pytest.raises(LookupError):
        await freight_procurement.counter("DEAL-NONE", by="o")
    await freight_procurement.handle_reply("DEAL-FRT001", "q@beta.in", "20ft USD 1,400")
    deal = await repo.get("DEAL-FRT001")
    top = freight_procurement.best(deal.freight_rfq)
    assert top["forwarder"] == "Beta Cargo"
    monkeypatch.setattr("services.trade_min.freight_lanes.lane", AsyncMock(return_value=None))
    c = await freight_procurement.counter("DEAL-FRT001", by="o")
    assert c["target_usd"] == 1358.0 and "USD 1,358" in sent[-1].body
    with pytest.raises(ValueError):
        await freight_procurement.counter("DEAL-FRT001", by="o")
    gate = await freight_procurement.for_gate("DEAL-FRT001", 2000.0)
    assert gate["email_quote_best"]["forwarder"] == "Beta Cargo" and gate["email_quote_cheaper"]
    assert await freight_procurement.for_gate("DEAL-NONE", None) == {}
    monkeypatch.setenv("FREIGHT_MAX_TRANSIT_DAYS", "15")
    assert freight_procurement.best({"quotes": [{"total_usd": 1, "transit_days": 20}]}) is None
    assert freight_procurement.best({"quotes": [{"total_usd": 1, "valid_until": "2026-01-01"}]}, ready=date(2026, 2, 1)) is None
    assert a["forwarder_id"].startswith("FWD-")
    freight_procurement.reset_for_tests()
    await repo.save(make(DealRecord, deal_id="DEAL-FRT003", hs_code="090411", market="JP", status=DealStatus.PO_ISSUED,
                         org_id="portless", quantity_mt=20.0))
    with pytest.raises(LookupError):
        await freight_procurement.request_quotes("DEAL-FRT003", by="o")


# ---------------------------------------------------------------------------
# Country feeds
# ---------------------------------------------------------------------------

UN_XML = "<CONSOLIDATED_LIST><INDIVIDUAL><REFERENCE_NUMBER>KPi.033</REFERENCE_NUMBER></INDIVIDUAL>" \
         "<INDIVIDUAL><REFERENCE_NUMBER>QDi.001</REFERENCE_NUMBER></INDIVIDUAL><ENTITY><REFERENCE_NUMBER>LYe.002</REFERENCE_NUMBER></ENTITY></CONSOLIDATED_LIST>"
OFAC_CSV = '1,"ACME","entity","CUBA] [IRAN",x\n2,"B","entity","SDGT",y\n3,"C","individual","RUSSIA-EO14024",z\n4,"short"\n'
UK_CSV = "Name,Regime\nX,Russia\nY,Democratic People's Republic of Korea\n"
GREY = "<p>Jurisdictions under increased monitoring: Algeria, Côte d'Ivoire, Lao PDR, Viet Nam and South Africa.</p>"
BLACK = "<h2>High-Risk Jurisdictions subject to a Call for Action</h2><li>Democratic People's Republic of Korea</li><li>Iran</li><li>Myanmar</li>"
OECD = "Country,Category\nPakistan,7\nIndia,3\nAtlantis,5\nUAE,x\n"


def test_country_names_and_parsers():
    assert country_names.iso2("Viet Nam") == "VN" and country_names.iso2("Côte d’Ivoire") == "CI"
    assert country_names.iso2("ae") == "AE" and country_names.iso2("Atlantis") is None
    assert country_names.find_all(BLACK) == ["KP", "IR", "MM"]
    assert country_feeds.parse_un_xml(UN_XML) == {"KP", "LY"}
    assert country_feeds.parse_ofac_csv(OFAC_CSV) == {"CU", "IR", "RU"}
    assert country_feeds.parse_uk_csv(UK_CSV) == {"RU", "KP"} and country_feeds.parse_uk_csv("a,b\n1,2") == set()
    assert country_feeds.parse_fatf(GREY) == {"DZ", "CI", "LA", "VN", "ZA"}
    assert country_feeds.parse_oecd_csv(OECD) == {"PK": 7, "IN": 3}
    props = country_feeds.propose({"un": {"KP", "LY"}, "ofac": {"CU"}, "fatf_black": {"MM"}, "fatf_grey": {"VN"},
                                   "oecd": {"PK": 7, "IN": 3}})
    assert props["KP"]["tier"] == "blocked" and props["LY"]["tier"] == "review" and props["CU"]["tier"] == "blocked"
    assert props["MM"]["tier"] == "blocked" and props["VN"]["tier"] == "review" and props["PK"]["tier"] == "review"
    assert "IN" not in props


async def test_country_feeds_apply_block_at_once_and_suggest_review(repo, monkeypatch):
    out = await country_feeds.apply({"MM": {"tier": "blocked", "reasons": ["FATF call for action"]},
                                     "VN": {"tier": "review", "reasons": ["FATF grey list"]},
                                     "AE": {"tier": "standard", "reasons": ["x"]}})
    assert out == {"blocked": ["MM"], "suggested": ["VN"]}
    assert country_profiles.tier("MM") == "blocked" and country_profiles.get("VN").tier_suggestion == "review"
    assert country_profiles.tier("VN") != "review"  # never raised automatically


async def test_country_feeds_run_and_a32(repo, monkeypatch):
    for env, url in (("UN_SANCTIONS_XML_URL", "https://un/x"), ("OFAC_SDN_CSV_URL", "https://ofac/s"),
                     ("UK_SANCTIONS_CSV_URL", "https://uk/c"), ("FATF_BLACK_URL", "https://fatf/b"),
                     ("FATF_GREY_URL", "https://fatf/g"), ("OECD_RISK_CSV_URL", "https://oecd/r")):
        monkeypatch.setenv(env, url)
    pages = {"un": UN_XML, "ofac": OFAC_CSV, "uk": UK_CSV, "fatf/b": BLACK, "fatf/g": GREY}

    def handler(req):
        for k, v in pages.items():
            if k in str(req.url):
                return httpx.Response(200, text=v)
        return httpx.Response(503)
    facts = await country_feeds.read_feeds(transport=httpx.MockTransport(handler))
    assert set(facts["read"]) == {"un", "ofac", "uk", "fatf_black", "fatf_grey"} and facts["failed"] == ["oecd"]
    _connector(monkeypatch, {"countries": [{"country": "SA", "ecgc_rating": "B1", "escrow_providers": ["tazapay"],
                                            "attestation_documents": ["chamber attestation"]}, {"country": None}]})
    a32 = await country_feeds.apply_a32()
    assert a32["changed"] == ["SA"] and "chamber attestation" in country_profiles.get("SA").documents
    _connector(monkeypatch, {"stub": True})
    assert (await country_feeds.apply_a32())["stub"]
    monkeypatch.setattr(country_feeds, "read_feeds", AsyncMock(return_value={"read": ["fatf_grey"], "failed": [],
                                                                             "fatf_grey": {"VN"}}))
    out = await country_feeds.run()
    assert out["suggested"] == ["VN"] and out["a32"]["stub"]
    assert await country_feeds.daily(NOW) is None  # pinned off
    monkeypatch.setenv("COUNTRY_FEEDS_ENABLED", "true")
    assert (await country_feeds.daily(NOW))["read"] == ["fatf_grey"] and await country_feeds.daily(NOW) is None


async def test_fatf_listing_proposes_review_and_expired_fssai_blocks_the_pi(monkeypatch, repo):
    """todo 23.3 QA: a FATF listing proposes review; an expired FSSAI licence blocks the PI."""
    assert country_feeds.propose({"fatf_grey": {"KE"}})["KE"]["tier"] == "review"
    from shared import registrations
    registrations.reset_for_tests()
    await registrations.record("fssai", "100123", "2026-01-31", by="o")
    monkeypatch.setenv("REGISTRATIONS_REQUIRED", "true")
    deal = make(DealRecord, deal_id="D-REG", hs_code="09041100", market="AE", status=DealStatus.NEGOTIATION_AGREED, org_id="p")
    findings = await compliance.check_registrations(deal)
    assert any(f.code == "registration_expired_fssai" and f.severity == "block" for f in findings)
    registrations.reset_for_tests()


# ---------------------------------------------------------------------------
# Requirement lines + attribute filters
# ---------------------------------------------------------------------------

def test_spec_lines_and_attribute_filter():
    from services.trade_min import product_facts
    from shared import product_spec
    pf, _ = product_spec.merge(None, "090411", {"variety": "Malabar", "packing": "25 kg PP bags"}, source="email",
                               at=NOW, by="reply", product="Black pepper")
    rec = CounterpartyRecord(id="buyer:x", role="buyer", name="X", source="scan", first_seen_at=NOW, last_seen_at=NOW,
                             product_facts={"090411": pf.model_dump(mode="json")})
    lines = product_facts.spec_lines(rec)
    assert lines[0]["hs_code"] == "090411" and "Malabar" in lines[0]["line"] and lines[0]["known"] >= 1
    assert product_facts.attr_filter(["variety:malabar", "bad", "packing:25 kg"]) == {"variety": "malabar", "packing": "25 kg"}
    assert product_facts.matches(rec, None, {"variety": "malabar"}) and not product_facts.matches(rec, "090411", {"variety": "tellicherry"})
    assert not product_facts.matches(rec, "100630", {"variety": "malabar"})
    other = rec.model_copy(update={"id": "buyer:y", "product_facts": {}})
    rows = product_facts.annotate([rec, other], attrs={"variety": "malabar"})
    assert [r.id for r in rows] == ["buyer:x"] and rows[0].spec_lines
    assert product_facts.annotate([other])[0].spec_lines == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def deals_api(repo, monkeypatch, sent):
    from services.deals.router import router
    from shared.auth.dependencies import get_current_user, require_portless
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_portless] = lambda: STAFF
    app.dependency_overrides[get_current_user] = lambda: STAFF
    return TestClient(app, raise_server_exceptions=False)


async def test_deals_routes(deals_api, repo, monkeypatch):
    await repo.save(make(DealRecord, deal_id="DEAL-RT0001", hs_code="090411", market="AE", status=DealStatus.PO_ISSUED,
                         org_id="portless", quantity_mt=20.0))
    assert deals_api.put("/deals/freight/forwarders", json={"name": "F", "email": "bad"}).status_code == 422
    assert deals_api.post("/deals/DEAL-RT0001/freight/request", json={}).status_code == 409
    deals_api.put("/deals/freight/forwarders", json={"name": "Alpha", "email": "r@alpha.in", "lanes": ["AE"]})
    assert deals_api.get("/deals/freight/forwarders?market=AE").json()["forwarders"][0]["name"] == "Alpha"
    assert deals_api.post("/deals/DEAL-RT0001/freight/request", json={"ready_date": "2026-12-01"}).json()["requests"]
    assert deals_api.post("/deals/DEAL-RT0001/freight/counter").status_code == 409
    assert deals_api.get("/deals/DEAL-RT0001/freight/quotes").json()["best"] is None
    monkeypatch.setattr("shared.country_feeds.run", AsyncMock(return_value={"blocked": []}))
    assert deals_api.post("/deals/compliance/country-feeds/run").json() == {"blocked": []}
    monkeypatch.setenv("AUTH_COMPLIANCE_OFFICER_EMAILS", "officer@portless.test")
    assert deals_api.post("/deals/compliance/country-feeds/run").status_code == 403


def test_trade_min_routes(monkeypatch):
    from services.trade_min.router import router, _staff
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_staff()] = lambda: STAFF
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/trade-min/india-exports?hs_code=090411&market=AE").json() == {"latest": None, "supplier_check": None}
    assert client.post("/trade-min/directory/sources/run?connector=nope&hs_code=090411").status_code in (401, 403, 422)


async def test_email_processor_routes_freight_quotes(monkeypatch):
    from services.email_processor import service as ep
    from services.email_processor.repository import InMemoryEmailProcessorRepository
    from services.email_processor.schemas import InboundRawEmail
    store = InMemoryEmailProcessorRepository()
    monkeypatch.setattr(ep, "get_repository", lambda: store)
    monkeypatch.setattr(freight_procurement, "handle_reply", AsyncMock(return_value={"handled": True}))
    r = await ep.process_inbound(InboundRawEmail(from_address="q@beta.in", to_address="t@p.test",
                                                 subject="Re: Freight quote request [FRT:DEAL-FRT001]", body_text="20ft USD 1400",
                                                 message_id="f1", received_at=NOW, source="manual"))
    assert r.status.value == "processed" and r.message.startswith("freight_quote")


def test_spec_lines_edges(monkeypatch):
    """Branches of product_facts.py (held at 100 %)."""
    from services.trade_min import product_facts
    from shared import product_spec
    empty = product_spec.ProductFacts(hs_code="090411", facts={})
    rec = CounterpartyRecord(id="buyer:z", role="buyer", name="Z", source="scan", first_seen_at=NOW, last_seen_at=NOW,
                             product_facts={"090411": empty.model_dump(mode="json")})
    assert product_facts.spec_lines(rec) == []
    monkeypatch.setattr(product_facts, "spec_lines", lambda r: (_ for _ in ()).throw(RuntimeError("x")))
    assert product_facts.annotate([rec])[0].spec_lines == []
