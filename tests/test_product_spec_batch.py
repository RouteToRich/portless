"""2026-11-19 batch 2: exact product requirement per buyer / supplier (15.x), volume line and
size band, source merge rule, found-by chip, proposed / supplier-led markets, contact persons,
sourcing agent, scan plan, agent scorecard, mandi prior, market quality rules, freight quote
index and comparison, inspection options, env parity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation
from shared import catalogue, product_spec
from shared.product_spec import ProductFacts

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
ONION = {
    "product_id": "onion", "hs_code": "070310", "name": "Onion", "markets": ["AE"],
    "synonyms": ["shallot", "big onion"], "hs8_codes": ["07031010"], "form": "fresh",
    "quality_requirements": {"AE": ["MRL per GSO 382"], "*": ["phytosanitary certificate"]},
    "attributes": [
        {"key": "variety", "values": ["red", "pink"], "question": "Which variety — red or pink?"},
        {"key": "size", "values": ["40-60 mm", "55 mm+"], "label": "size"},
        {"key": "bag", "values": ["25 kg mesh", "10 kg mesh"], "label": "packing bag", "hard": True},
    ],
}


def _rec(**kw) -> CounterpartyRecord:
    base = dict(id="buyer:gulf.ae", role="buyer", name="Gulf Foods LLC", country="AE", email="buy@gulf.ae",
                website="https://gulf.ae", source="apify_company_search", sources=["apify_company_search"],
                hs_codes=["070310"], first_seen_at=NOW, last_seen_at=NOW)
    base.update(kw)
    return CounterpartyRecord(**base)


@pytest.fixture(autouse=True)
async def _fresh():
    directory.reset_directory()
    await catalogue.set_catalogue([ONION])
    yield
    directory.reset_directory()
    catalogue.reset_for_tests()


# ---------------------------------------------------------------------------
# Template, facts, precedence
# ---------------------------------------------------------------------------

def test_precedence_staff_deal_email_website():
    pf, ch = product_spec.merge(None, "070310", {"variety": "red"}, source="website")
    assert ch == ["variety"]
    pf, ch = product_spec.merge(pf, "070310", {"variety": "pink"}, source="website")
    assert ch == [] and pf.facts["variety"].value == "red"  # a scan only fills what is empty
    pf, _ = product_spec.merge(pf, "070310", {"variety": "pink"}, source="email")
    assert pf.facts["variety"].value == "pink" and pf.facts["variety"].source == "email"
    pf, _ = product_spec.merge(pf, "070310", {"variety": "red"}, source="staff", by="ops@x")
    pf, ch = product_spec.merge(pf, "070310", {"variety": "pink"}, source="deal")
    assert ch == [] and pf.facts["variety"].value == "red"  # staff is never overwritten but by staff


def test_completeness_questions_conflicts_summary():
    line = catalogue.line_for("070310")
    pf, _ = product_spec.merge(None, "070310", {"variety": "red", "quantity_per_order_mt": 40,
                                                "incoterm": "CIF", "destination_port": "Jebel Ali"},
                               source="email", product="Onion")
    c = product_spec.completeness(line, pf)
    assert c["known"] == 4 and "size" in c["missing"]
    assert product_spec.missing_questions(line, pf) == ["size (40-60 mm / 55 mm+)", "packing bag (25 kg mesh / 10 kg mesh)"]
    conflicts, matches = product_spec.conflicts(line, {"variety": "red", "size": "55 mm+"},
                                                {"variety": ["red", "pink"], "size": "40-60 mm"})
    assert matches == 1 and conflicts and "size" in conflicts[0]
    assert product_spec.summary_line(pf, line) == "Onion · red · 40 MT/order · CIF Jebel Ali"
    assert product_spec.missing_questions(None, pf) == []  # no template → no extra questions


def test_attributes_read_only_from_allowed_values():
    line = catalogue.line_for("070310")
    assert product_spec.read_attributes(line, "We want RED onion, 55 mm+ in 25 kg mesh bags") == {
        "variety": "red", "size": "55 mm+", "bag": "25 kg mesh"}
    assert product_spec.read_attributes(line, "something reddish") == {}


def test_catalogue_synonyms_hs8_quality():
    assert catalogue.line_for_name("We buy shallot every month").product_id == "onion"
    assert catalogue.line_for_hs8("0703.10.10").product_id == "onion"
    assert catalogue.quality_requirements_for("070310", "AE") == ["MRL per GSO 382", "phytosanitary certificate"]
    assert "shallot" in catalogue.products_for("070310")


# ---------------------------------------------------------------------------
# Facts on the directory record
# ---------------------------------------------------------------------------

async def test_reply_is_kept_on_the_directory_record():
    from services.trade_min import product_facts
    store = await directory.get_directory()
    await store.upsert(_rec())
    cand = SimpleNamespace(legal_name="Gulf Foods LLC", website="https://gulf.ae", email="buy@gulf.ae", country="AE")
    req = SimpleNamespace(grade=None, packaging="25 kg mesh", quantity_mt=40, destination_port="Jebel Ali",
                          incoterm="CIF", target_price_usd_per_mt=420, certifications=["phyto"], delivery_window=None,
                          payment_terms=None, spec={"variety": "red"})
    changed = await product_facts.record_buyer_requirement(SimpleNamespace(hs_code="070310", deal_id="D1"), cand, req)
    assert set(changed) >= {"variety", "packing", "quantity_per_order_mt"}
    view = await product_facts.view("buyer", "buyer:gulf.ae")
    p = view["products"][0]
    assert p["facts"]["variety"]["source"] == "email" and p["facts"]["variety"]["ref"] == "D1"
    assert p["open_questions"] == ["size (40-60 mm / 55 mm+)", "packing bag (25 kg mesh / 10 kg mesh)"]


async def test_staff_edit_wins_and_is_audited():
    from services.trade_min import product_facts
    store = await directory.get_directory()
    await store.upsert(_rec())
    with patch("shared.audit_log.record", new=AsyncMock()) as audit:
        out = await product_facts.staff_edit("buyer", "buyer:gulf.ae", "070310", {"variety": "pink"},
                                             by="ops@x", reason="phone call")
    assert out["products"][0]["facts"]["variety"]["source"] == "staff"
    audit.assert_awaited()
    await product_facts.record("buyer", "070310", {"variety": "red"}, source="email", name="Gulf Foods LLC",
                               website="https://gulf.ae")
    assert (await product_facts.facts_for("buyer", "070310", name="Gulf Foods LLC", website="https://gulf.ae")
            ).facts["variety"].value == "pink"
    with pytest.raises(LookupError):
        await product_facts.staff_edit("buyer", "buyer:nobody", "070310", {"x": 1}, by="ops@x")


async def test_found_by_email_domain_and_created_when_new():
    from services.trade_min import product_facts
    store = await directory.get_directory()
    await store.upsert(_rec())
    rec = await product_facts.find_record("buyer", email="other.person@gulf.ae")
    assert rec is not None and rec.id == "buyer:gulf.ae"
    assert await product_facts.find_record("buyer", email="x@gmail.com") is None
    await product_facts.record("supplier", "070310", {"price_usd_per_mt": 300}, source="email",
                               name="Nashik Agro", email="sales@nashikagro.in")
    assert any(r.name == "Nashik Agro" for r in await store.list("supplier"))


async def test_completed_deal_becomes_the_proven_requirement():
    from services.trade_min import product_facts
    store = await directory.get_directory()
    await store.upsert(_rec())
    deal = SimpleNamespace(deal_id="D9", hs_code="070310", quantity_mt=50, parties=[
        SimpleNamespace(role="buyer", legal_name="Gulf Foods LLC", website="https://gulf.ae", email="buy@gulf.ae",
                        country="AE")])
    facts = {"hs_code": "070310", "pi": SimpleNamespace(packing_spec=None), "po": None, "shipped_quantity_mt": 48.0,
             "port_of_destination": "Jebel Ali", "incoterm": "CIF", "unit_price": 410.0}
    out = await product_facts.record_from_deal(deal, facts)
    assert "traded" in out["buyer"]
    f = (await store.get("buyer:gulf.ae")).product_facts["070310"]["facts"]
    assert f["quantity_per_order_mt"]["value"] == 48.0 and f["quantity_per_order_mt"]["source"] == "deal"


async def test_directory_merge_keeps_facts_and_people():
    store = await directory.get_directory()
    pf, _ = product_spec.merge(None, "070310", {"variety": "red"}, source="staff")
    await store.upsert(_rec(product_facts={"070310": pf.model_dump(mode="json")},
                            people=[{"name": "Ali", "email": "ali@gulf.ae"}]))
    scan_pf, _ = product_spec.merge(None, "070310", {"variety": "pink", "size": "55 mm+"}, source="website")
    await store.upsert(_rec(product_facts={"070310": scan_pf.model_dump(mode="json")},
                            people=[{"name": "Ali", "email": "ali@gulf.ae", "role": "purchasing"},
                                    {"name": "Sara", "email": "sara@gulf.ae"}]))
    rec = await store.get("buyer:gulf.ae")
    facts = rec.product_facts["070310"]["facts"]
    assert facts["variety"]["value"] == "red" and facts["size"]["value"] == "55 mm+"
    assert [p["name"] for p in rec.people] == ["Ali", "Sara"] and rec.people[0]["role"] == "purchasing"


async def test_note_person_who_replied():
    from services.trade_min import product_facts
    store = await directory.get_directory()
    await store.upsert(_rec())
    assert await product_facts.note_person("buyer", {"email": "sara@gulf.ae", "replied_at": "2026-11-19"},
                                           name="Gulf Foods LLC", website="https://gulf.ae")
    assert (await store.get("buyer:gulf.ae")).people[0]["replied_at"] == "2026-11-19"


def test_people_carried_from_the_search():
    from services.trade_min.service import _people
    rows = _people([{"name": "Ali", "title": "Purchasing manager", "email": "ali@gulf.ae"}, "junk", {}])
    assert rows == [{"name": "Ali", "role": "Purchasing manager", "email": "ali@gulf.ae", "phone": None,
                     "language": None, "source": "website", "at": rows[0]["at"]}]


def test_product_routes_registered():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.trade_min.router import _staff, router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@x", is_portless=True)
    c = TestClient(app)
    assert c.get("/trade-min/buyers/buyer:nobody/products").status_code == 404
    assert c.put("/trade-min/buyers/nobody/products/070310", json={"facts": {"variety": "red"}}).status_code == 404
    assert c.get("/trade-min/robots/x/products").status_code == 404


# ---------------------------------------------------------------------------
# Asking for what is missing; matching on the specification
# ---------------------------------------------------------------------------

def test_introduction_and_rfq_ask_the_unanswered_attributes():
    from services.sourcing.service import rfq_email_body
    from shared.orchestration import outreach
    deal = SimpleNamespace(deal_id="D1", hs_code="070310", quantity_mt=None, market="AE")
    cand = SimpleNamespace(candidate_id="C1", legal_name="Gulf Foods", pitch_angle=None, quantity_mt=None,
                           requirement=SimpleNamespace(spec={"variety": "red"}), contact_name=None, email="b@g.ae")
    _, body = outreach.draft(deal, cand)
    assert "6. For the exact product: size (40-60 mm / 55 mm+); packing bag" in body
    assert "Which variety" not in body  # already answered
    assert "7. For the exact product: Which variety — red or pink?" in rfq_email_body(
        "Acme", "Please quote", questions=outreach.spec_questions("070310"))
    assert "7. For the exact product" not in rfq_email_body("Acme", "Please quote")


def test_brief_puts_the_product_questions_first():
    from agents.negotiation_agent.briefing import buyer_missing
    out = buyer_missing({"spec_questions": ["size (40-60 mm / 55 mm+)"], "destination_port": "x", "grade": "g",
                         "delivery_window": "w", "certifications_required": ["c"], "packing": "p"},
                        qc_selected=True, quantity_known=True)
    assert out[0] == "size (40-60 mm / 55 mm+)"


def test_pair_blocked_on_a_specification_conflict():
    from shared import pricebook
    from shared.orchestration.matchmaking import score_pair
    deal = SimpleNamespace(deal_id="D", hs_code="070310", market="AE", quantity_mt=40)
    req = SimpleNamespace(quantity_mt=40, delivery_by_days=None, grade=None, certifications=[],
                          target_price_usd_per_mt=None, target_price_incoterm=None, incoterm="CIF",
                          spec={"variety": "red", "size": "55 mm+"})
    cand = SimpleNamespace(candidate_id="C", legal_name="Gulf", requirement=req, quantity_mt=None, status="interested")

    def offer(size):
        return SimpleNamespace(offer_id="O", supplier_id="S", verification_failed=False, moq_mt=None,
                               capacity_mt_per_month=None, grade=None, certifications=[], lead_time_days=None,
                               fob_price_usd_per_mt=300.0, incoterm="FOB", port_km=None, price_basis="quoted",
                               spec={"variety": "red", "size": size})
    est = pricebook.PriceEstimate(hs_code="070310", side="buyer")
    bad = score_pair(deal, cand, offer("40-60 mm"), buyer_est=est, supplier_est=est)
    good = score_pair(deal, cand, offer("55 mm+"), buyer_est=est, supplier_est=est)
    assert any("specification mismatch" in b for b in bad.blockers)
    assert not any("specification mismatch" in b for b in good.blockers)


# ---------------------------------------------------------------------------
# Volume, band, lot
# ---------------------------------------------------------------------------

def test_volume_line_band_and_stated_lot():
    from services.trade_min import volume
    assert volume.volume_line(480, 8, "apify_us_importers") == "Buys ~480 MT/yr · ships every ~6 weeks (apify_us_importers)"
    assert volume.order_mt(480, 8) == 60.0
    assert volume.order_size_band(text="20 branches and 3 warehouses", product_lines=6) == "large"
    assert volume.order_size_band(text="family-run shop") == "small"
    assert volume.order_size_band() is None
    buyers = [SimpleNamespace(quantity=40, quantity_unit="MT"),
              SimpleNamespace(quantity=None, annual_volume_mt=600, shipments_12m=10),
              SimpleNamespace(quantity=None)]
    assert volume.stated_order_mt(buyers) == (50.0, "order size stated / recorded by 2 buyer(s)")


# ---------------------------------------------------------------------------
# Sources: merge rule, found-by chip
# ---------------------------------------------------------------------------

async def test_fresher_national_figure_wins_and_disagreement_is_flagged(monkeypatch):
    import dataclasses

    from services.trade_min import service as svc
    from services.trade_min.config import DEFAULT_CONFIG
    monkeypatch.setenv("NATIONAL_TRADE_SOURCES_ENABLED", "true")

    async def fake_quote(name, req):
        if name == "eurostat_comext":
            return {"source": "eurostat_comext", "quantity_mt": 900.0, "months": ["2026-09"], "data_period": "2026-09",
                    "unit_value_usd_per_mt": 1000.0, "india_unit_value_usd_per_mt": None}
        if name == "apify_export_markets":
            return {"markets": [{"market": "DE", "country": "DE", "unit_value_usd_per_mt": 1500.0,
                                 "latest_period": "2025", "demand_score": 50}]}
        return None
    monkeypatch.setattr(svc, "_safe_quote", fake_quote)
    monkeypatch.setattr(svc, "_pick_market_row", lambda r, m: (r or {}).get("markets", [None])[0])
    monkeypatch.setattr(svc, "_extract_export_market", lambda row, hs, m, now: (50.0, [], 1500.0, None))
    cfg = dataclasses.replace(DEFAULT_CONFIG, phase1_min_connectors=1)
    r = await svc._phase1_historical_demand("100630", "DE", cfg)
    assert r.price_benchmark_usd_per_mt == 1000.0 and r.price_benchmark_source == "eurostat_comext"
    assert "sources_disagree" in r.market_data_flags and r.data_period == "2026-09"


def test_period_key_and_found_by():
    from services.trade_min import service as svc
    assert svc._period_key("2025") == "2025-12" and svc._period_key("2026-09") == "2026-09"
    assert svc.found_by_for("uk_hmrc:india") == "national_statistics"
    assert svc.found_by_for("apify_export_markets") == "market_statistics"
    assert svc.count_found_by([SimpleNamespace(found_by="national_statistics"), SimpleNamespace()]) == {
        "national_statistics": 1, "market_statistics": 1}


# ---------------------------------------------------------------------------
# Markets beyond the scanned corridors
# ---------------------------------------------------------------------------

async def test_proposed_market_from_worldwide_buyers_and_accept():
    from services.trade_min import markets
    store = await directory.get_directory()
    for i in range(3):
        await store.upsert(_rec(id=f"buyer:sa{i}", name=f"Riyadh Foods {i}", country="SA", email=f"b{i}@sa{i}.sa",
                                website=f"https://sa{i}.sa", last_outcome=directory.WORLDWIDE_OUTCOME))
    await store.upsert(_rec(id="buyer:sa9", name="Exporter", country="SA", email="x@sa9.sa", flags=["role_suspect"]))
    rows = await markets.proposed_markets()
    assert rows == [{"product_id": "onion", "hs_code": "070310", "product": "Onion", "market": "SA", "buyers": 3,
                     "verified": 0, "worldwide": 3, "examples": ["Riyadh Foods 0", "Riyadh Foods 1", "Riyadh Foods 2"]}]
    out = await markets.accept_market("onion", "sa", by="ops@x")
    assert out["markets"] == ["AE", "SA"] and await markets.proposed_markets() == []
    with pytest.raises(LookupError):
        await markets.accept_market("nothing", "SA", by="ops@x")


async def test_markets_for_a_supplier():
    from services.trade_min import markets
    store = await directory.get_directory()
    await store.upsert(_rec(id="supplier:nashik", role="supplier", name="Nashik Agro", country="IN",
                            email="s@nashik.in", website="https://nashik.in"))
    await store.upsert(_rec(validation=CounterpartyValidation(status="verified")))
    await store.upsert(_rec(id="buyer:dnc", name="DNC", country="QA", email="d@dnc.qa", do_not_contact=True))
    out = await markets.markets_for_supplier("nashik")
    assert [(m["market"], m["contactable"], m["verified"]) for m in out["markets"]] == [("AE", 1, 1)]


# ---------------------------------------------------------------------------
# Sourcing agent
# ---------------------------------------------------------------------------

def _prospect(**kw):
    base = dict(deal_id="D1", status="MATCHING", hs_code="070310", created_at=NOW - timedelta(days=10),
                sourcing_widened_at=None, supplier_offers=[], match_results=[], parties=[],
                buyer_candidates=[SimpleNamespace(status="interested")])
    base.update(kw)
    return SimpleNamespace(**base)


def test_stalled_only_with_an_interested_buyer_and_no_quote():
    from shared.orchestration.sourcing_agent import stalled
    assert stalled(_prospect(), NOW)
    assert not stalled(_prospect(buyer_candidates=[SimpleNamespace(status="pending")]), NOW)
    assert not stalled(_prospect(created_at=NOW - timedelta(days=1)), NOW)
    quoted = SimpleNamespace(fob_price_usd_per_mt=300, price_basis="quoted", verification_failed=False)
    assert not stalled(_prospect(supplier_offers=[quoted]), NOW)


async def test_widen_asks_unasked_directory_suppliers_best_first(monkeypatch):
    from shared.orchestration import sourcing_agent
    store = await directory.get_directory()
    await store.upsert(_rec(id="supplier:a", role="supplier", name="A", country="IN", email="a@a.in", website="https://a.in",
                            validation=CounterpartyValidation(status="verified")))
    await store.upsert(_rec(id="supplier:b", role="supplier", name="B", country="IN", email="b@b.in", website="https://b.in"))
    await store.upsert(_rec(id="supplier:c", role="supplier", name="C", country="IN", email="c@c.in", website="https://c.in",
                            flags=["product_mismatch"]))
    await store.upsert(_rec(id="supplier:d", role="supplier", name="D", country="IN", email="d@d.in", website="https://d.in",
                            hs_codes=["100630"]))
    deal = _prospect(supplier_offers=[SimpleNamespace(supplier_id="supplier:b", email="b@b.in",
                                                      fob_price_usd_per_mt=None)])
    assert [r.name for r in await sourcing_agent.candidates(deal)] == ["A"]
    rfq = AsyncMock(return_value="APR-1")
    repo = SimpleNamespace(set_fields=AsyncMock(return_value=True))
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
            patch("shared.orchestration.continuations.rfq_for_supplier", new=rfq), \
            patch("services.deals.repository.get_repository", return_value=repo):
        out = await sourcing_agent.widen("D1", now=NOW)
    assert out["asked"] == [{"supplier_id": "supplier:a", "name": "A", "approval_id": "APR-1"}]
    repo.set_fields.assert_awaited_with("D1", {"sourcing_widened_at": NOW})


# ---------------------------------------------------------------------------
# Scan plan, scorecard
# ---------------------------------------------------------------------------

def test_scan_plan_rows():
    from services.trade_min.scan_plan import plan_rows
    old = SimpleNamespace(hs_code="070310", market="AE", scanned_at=NOW - timedelta(days=10), opportunity_id="O1",
                          scan_cost={"usd": 0.5}, profit=SimpleNamespace(expected_profit_usd=2000.0))
    weak = SimpleNamespace(hs_code="090422", market="AE", scanned_at=NOW - timedelta(days=10), opportunity_id="O2",
                           scan_cost={"usd": 1.0}, profit=SimpleNamespace(expected_profit_usd=50.0))
    report = SimpleNamespace(corridors=[SimpleNamespace(hs_code="100630", market="IR", result="dropped",
                                                        reason="export policy: embargo")])
    rows = plan_rows([("070310", "AE"), ("090422", "AE"), ("100630", "IR"), ("100630", "GB")], [old, weak], report,
                     budget_usd=1.0, now=NOW)
    by = {(r["hs_code"], r["market"]): r for r in rows}
    assert by[("070310", "AE")]["action"] == "rescan" and by[("070310", "AE")]["profit_per_usd"] == 4000.0
    assert by[("090422", "AE")]["action"] == "skip"
    assert by[("100630", "IR")]["action"] == "skip"
    assert by[("100630", "GB")]["action"] == "skip" and "budget" in by[("100630", "GB")]["reason"]


def test_scorecard_tally_and_recommendation(monkeypatch):
    from shared.orchestration import agent_scorecard as sc
    monkeypatch.setenv("SCORECARD_MIN_DECISIONS", "3")
    rows = [SimpleNamespace(action="SEND_BUYER_OUTREACH", status="APPROVED", created_at=NOW, modified_body=None,
                            financial_impact_usd=None) for _ in range(4)]
    rows += [SimpleNamespace(action="RELEASE_ESCROW_TRANCHE", status="APPROVED", created_at=NOW, modified_body=None,
                             financial_impact_usd=1000.0) for _ in range(3)]
    rows += [SimpleNamespace(action="SEND_NEGOTIATION_EMAIL", status="REJECTED", created_at=NOW, modified_body=None,
                             financial_impact_usd=None)]
    t = sc.tally(rows)
    assert t["buyer_outreach"]["recommended_level"] == "L2" and t["buyer_outreach"]["agreement_pct"] == 100.0
    assert t["money"]["money_moved_usd"] == 3000.0 and t["money"]["recommended_level"] == "L1"  # money ceiling
    assert t["negotiation"]["recommended_level"] == "L0"


# ---------------------------------------------------------------------------
# Mandi prior, quality rules, freight, inspection, env parity
# ---------------------------------------------------------------------------

async def test_mandi_price_joins_the_supplier_prior(monkeypatch):
    from services.trade_min import mandi_history
    from shared import pricebook
    mandi_history.reset_for_tests()
    await mandi_history.record({"commodity": "Onion", "modal_inr_per_quintal": 2100.0})
    monkeypatch.setattr("shared.connectors.mandi._usd_inr", lambda: 84.0)
    monkeypatch.setenv("MANDI_TO_FOB_MARKUP_PCT", "20")
    assert await pricebook.mandi_fob_usd_per_mt("070310") == 300.0
    out = {"supplier": 400.0, "source": "comtrade"}
    await pricebook._mandi_prior("070310", out)
    assert out["supplier"] == 350.0 and out["supplier_source"] == "comtrade+agmarknet"


def test_inspector_note_carries_destination_rules():
    from shared.orchestration.trade_brief import note
    text = note({"deal_id": "D", "hs_code": "070310", "product": "Onion", "desk": "x",
                 "quality_requirements": ["MRL per GSO 382"]}, "inspector")
    assert "check against destination rules: MRL per GSO 382" in text
    assert "destination rules" not in note({"deal_id": "D", "desk": "x", "quality_requirements": ["y"]}, "carrier")


async def test_freight_quote_against_the_lane_index():
    from services.trade_min import freight_lanes as fl
    fl.reset_for_tests()
    assert (await fl.check_quote("AE", 1500.0))["verdict"] == "no_index"
    for usd in (1000.0, 1100.0, 1200.0):
        await fl.save(fl.LaneRate(market="AE", usd_per_container=usd))
    hi = await fl.check_quote("AE", 1500.0)
    assert hi["index_usd"] == 1100.0 and hi["verdict"] == "above_market" and hi["deviation_pct"] == 36.4
    assert (await fl.check_quote("AE", 1150.0))["verdict"] == "in_line"


def test_freight_compare_choose_and_providers(monkeypatch):
    from services.shield_link import freight_compare as fc
    quotes = [{"provider": "maersk", "rate_usd": 1400.0, "transit_days": 20},
              {"provider": "cogoport", "rate_usd": 1200.0, "transit_days": 35},
              {"provider": "freightos", "rate_usd": 900.0, "stub": True}]
    assert fc.choose(quotes)["provider"] == "cogoport"
    assert fc.choose(quotes, max_transit=30)["provider"] == "maersk"
    assert fc.choose([{"provider": "x", "rate_usd": None}]) is None
    monkeypatch.setenv("COGOPORT_API_KEY", "real")
    monkeypatch.setenv("FREIGHTOS_API_KEY", "your_key_here")
    monkeypatch.delenv("MAERSK_CONSUMER_KEY", raising=False)
    assert fc.providers() == ["cogoport"]
    assert fc._rate({"rates": [{"rate_usd": 1300}, {"rate_usd": 1250}]}) == 1250


def test_inspection_plans_and_savings():
    from shared.orchestration.inspection_options import plans_from
    plans = plans_from(["DUPRO", "PSI", "PEO"], 300.0, quantity_mt=50)
    by = {"+".join(p["plan"]): p for p in plans}
    assert by["DUPRO+PSI+PEO"]["current"] and by["DUPRO+PSI+PEO"]["cost_usd"] == 900.0
    assert by["PSI"]["saving_usd"] == 600.0 and by["PSI"]["enough"]
    big = {"+".join(p["plan"]): p for p in plans_from(["DUPRO", "PSI"], 300.0, quantity_mt=500)}
    assert not big["PSI"]["enough"]
    proven = {"+".join(p["plan"]): p for p in plans_from(["DUPRO", "PSI"], 300.0, quantity_mt=500,
                                                         supplier_record={"qc_passed": 3, "qc_failed": 0})}
    assert proven["PSI"]["enough"]


def test_env_parity_report():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("env_parity", Path(__file__).resolve().parent.parent / "scripts" / "env_parity.py")
    ep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ep)
    schema = ep.schema_keys("MONGO_URI=\n# OPTIONAL_KEY=x\nTAZAPAY_API_KEY=your_tazapay_key_here\n")
    assert schema == {"MONGO_URI", "OPTIONAL_KEY", "TAZAPAY_API_KEY"}
    rep = ep.report(schema, {
        "staging": ep.parse("MONGO_URI=x\nTAZAPAY_API_KEY=k\nQC_SANDBOX_PROVIDERS=qima"),
        "production": ep.parse("MONGO_URI=y\nTAZAPAY_API_KEY=your_tazapay_key_here\nINTEGRATION_MODE=sandbox\nTYPO_KEY=1"),
    })
    prod = rep["production"]
    assert prod["placeholders"] == ["TAZAPAY_API_KEY"] and prod["sandbox_in_production"] == ["INTEGRATION_MODE"]
    assert prod["unknown"] == ["INTEGRATION_MODE", "TYPO_KEY"] and prod["differs"] == ["QC_SANDBOX_PROVIDERS"]
