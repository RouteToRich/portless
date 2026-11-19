"""2026-11-19 batch 4: safe markets and the export desk's own papers — country profiles and tiers,
PortLess registrations, regulatory watch, Incoterms® 2020 pricing and wording, samples, buyer
import permits, country documents, RoDTEP / drawback credits, partial supply, inbound question agent."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import BuyerCandidate, DealRecord, DealStatus
from shared import catalogue, country_profiles, registrations
from shared.orchestration import compliance
from tests.factories import make

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
STAFF = SimpleNamespace(user_id="u-ops", email="ops@portless.test", name="Ops", org_id=None, is_portless=True)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    country_profiles.reset_for_tests()
    registrations.reset_for_tests()
    compliance.reset_for_tests()
    from shared.orchestration import incentive_credits, regulatory_watch
    incentive_credits.reset_for_tests()
    regulatory_watch.reset_for_tests()
    monkeypatch.setattr("shared.mongo.collection", lambda name: None, raising=False)
    yield
    country_profiles.reset_for_tests()
    registrations.reset_for_tests()
    catalogue.reset_for_tests()


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", r)
    return r


def _deal(**kw) -> DealRecord:
    base = dict(deal_id="D-1", hs_code="10063020", market="AE", status=DealStatus.MATCHING, org_id="portless")
    base.update(kw)
    return make(DealRecord, **base)


# ---------------------------------------------------------------------------
# Country profiles and tiers
# ---------------------------------------------------------------------------

def test_profile_seed_settings_and_unknown():
    assert country_profiles.tier("AE") == "preferred" and country_profiles.known("AE")
    assert country_profiles.tier("KP") == "blocked" and country_profiles.tier("PK") == "review"
    assert country_profiles.tier("BR") == "standard" and not country_profiles.known("BR")
    assert country_profiles.get("X") is None and country_profiles.tier(None) == "standard"
    assert country_profiles.transit_days("SA") == 9 and country_profiles.transit_days("BR") is None
    docs = country_profiles.documents("AE")
    assert any("CEPA" in d for d in docs)
    assert {p.iso2 for p in country_profiles.all_profiles()} >= {"AE", "US", "DE"}


def test_settings_lists_outrank_the_seed(monkeypatch):
    monkeypatch.setenv("COMPLIANCE_REVIEW_COUNTRIES", "AE")
    assert country_profiles.tier("AE") == "review"
    monkeypatch.setenv("COMPLIANCE_BLOCKED_COUNTRIES", "AE")
    assert country_profiles.tier("AE") == "blocked"


def test_escrow_serves():
    assert country_profiles.escrow_serves("AE")  # no list → served
    country_profiles._stored["NG"] = {"iso2": "NG", "escrow_providers": ["escrow_com"]}
    assert country_profiles.escrow_serves("NG", ["escrow_com"])
    assert not country_profiles.escrow_serves("NG", ["tazapay"])


async def test_put_audits_and_a_tier_change_rechecks_open_deals(repo, monkeypatch):
    await repo.save(_deal(deal_id="D-BR", market="BR"))
    await repo.save(_deal(deal_id="D-PAID", market="BR", status=DealStatus.PAID))
    published = AsyncMock()
    monkeypatch.setattr("shared.orchestration.continuations._publish_exception", published)
    monkeypatch.setattr("shared.telegram.send_message", AsyncMock())
    out = await country_profiles.put("br", {"currency": "BRL"}, by="officer", source="central bank")
    assert out["profile"]["currency"] == "BRL" and not out["tier_changed"]
    assert country_profiles.get("BR").sources["currency"] == "central bank"
    out = await country_profiles.put("BR", {"tier": "blocked"}, by="officer")
    assert out["tier_changed"] and out["deals_rechecked"] == ["D-BR"]
    assert published.await_args.args[1] == "COUNTRY:BR:tier_changed"


async def test_risk_feed(monkeypatch):
    monkeypatch.setattr(country_profiles, "tier_changed", AsyncMock(return_value=[]))
    assert (await country_profiles.refresh_from_feed())["ok"] is False
    monkeypatch.setenv("COUNTRY_RISK_FEED_URL", "https://feed.test/risk.json")

    def handler(request):
        return httpx.Response(200, json={"date": "2026-11-01", "countries": {
            "BR": {"tier": "review", "fatf": "grey", "junk": 1}, "AE": {"tier": "preferred"}, "XX": "bad",
            "TR": {"tier": "nonsense"}}})
    out = await country_profiles.refresh_from_feed(transport=httpx.MockTransport(handler))
    assert out["ok"] and out["changed"] == ["BR"]
    assert country_profiles.tier("BR") == "review" and country_profiles.get("BR").fatf == "grey"
    bad = await country_profiles.refresh_from_feed(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    assert bad["reason"] == "feed has no countries"
    down = await country_profiles.refresh_from_feed(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert "unreachable" in down["reason"]


def test_destination_tier_findings():
    country_profiles._stored["BR"] = {"iso2": "BR", "tier": "review"}
    f = compliance.check_destination("BR")
    assert f.code == "destination_review_tier" and f.data["money_approver_required"]
    country_profiles._stored["BR"] = {"iso2": "BR", "tier": "blocked"}
    assert compliance.check_destination("BR").severity == "block"
    assert compliance.check_destination("AE") is None


def test_money_approver_needed_for_a_review_market(monkeypatch):
    from services.hitl_approval import service as hitl
    monkeypatch.setenv("AUTH_MONEY_APPROVER_EMAILS", "cfo@portless.test")
    assert not hitl.may_approve("CONFIRM_TRADE_COMPLIANCE", "ops@portless.test", ["money_approver_required"])
    assert hitl.may_approve("CONFIRM_TRADE_COMPLIANCE", "cfo@portless.test", ["money_approver_required"])
    assert hitl.may_approve("CONFIRM_TRADE_COMPLIANCE", "ops@portless.test", [])


def test_tier_in_radar_scan_plan_and_proposed_markets():
    from services.trade_min import scan_plan
    rows = scan_plan.plan_rows([("100630", "KP"), ("100630", "BR"), ("100630", "AE")], [], None,
                               budget_usd=None, now=NOW)
    by = {r["market"]: r for r in rows}
    assert by["KP"]["action"] == "skip" and "blocked" in by["KP"]["reason"]
    assert [r["market"] for r in rows][:2] == ["AE", "BR"]


async def test_scan_spends_nothing_on_review_markets(monkeypatch):
    from services.trade_min import service as tm
    from services.trade_min.config import DEFAULT_CONFIG
    monkeypatch.setattr(tm.start_check, "export_policy", AsyncMock(return_value={"severity": "ok"}))
    phase1 = AsyncMock()
    monkeypatch.setattr(tm, "_phase1_historical_demand", phase1)
    recorded = []
    monkeypatch.setattr(tm.scan_reports, "record", lambda *a: recorded.append(a))
    assert await tm._scan_one("100630", "PK", DEFAULT_CONFIG) is None
    assert phase1.await_count == 0 and "review market" in recorded[-1][3]


def test_exposure_per_country(monkeypatch):
    from shared.orchestration import exposure as ex
    for n in ex.LIMITS:
        monkeypatch.delenv(f"EXPOSURE_MAX_{n.upper()}_USD", raising=False)
    monkeypatch.setenv("EXPOSURE_MAX_COUNTRY_USD", "100000")
    monkeypatch.setenv("EXPOSURE_MAX_STANDARD_COUNTRY_USD", "30000")
    assert ex.country_limit("AE") == 100000 and ex.country_limit("BR") == 30000

    def d(did, market):
        return SimpleNamespace(deal_id=did, market=market, status=SimpleNamespace(value="PI_SENT"),
                               buyer_target_price_usd=1000.0, quantity_mt=20.0, updated_at=NOW,
                               exposure_accepted_by=None, parties=[])
    out = {b["limit"] for b in ex.breaches(d("D0", "BR"), 20000.0, [d("D1", "BR")], 0.0)}
    assert "country" in out
    assert "country" not in {b["limit"] for b in ex.breaches(d("D0", "AE"), 20000.0, [d("D1", "AE")], 0.0)}


# ---------------------------------------------------------------------------
# Outreach country rules, composition limit
# ---------------------------------------------------------------------------

def test_country_refusal(monkeypatch):
    from shared.orchestration.outreach import country_refusal
    deal = SimpleNamespace(market="AE")
    assert country_refusal(deal, SimpleNamespace(country="AE")) == (None, None)
    assert country_refusal(SimpleNamespace(market=None), SimpleNamespace(country=None)) == (None, None)
    monkeypatch.setenv("COUNTRY_PROFILE_REQUIRED", "true")
    assert country_refusal(deal, SimpleNamespace(country="BR"))[0] == "wait"
    assert country_refusal(deal, SimpleNamespace(country="KP"))[0] == "wait"  # unknown profile → wait first
    monkeypatch.setenv("COUNTRY_PROFILE_REQUIRED", "false")
    assert country_refusal(deal, SimpleNamespace(country="KP"))[0] == "refuse"
    assert "consent" in country_refusal(deal, SimpleNamespace(country="CA"))[1]
    monkeypatch.delenv("PORTLESS_ADDRESS", raising=False)
    assert country_refusal(deal, SimpleNamespace(country="US"))[0] == "wait"
    monkeypatch.setenv("PORTLESS_ADDRESS", "Chennai")
    assert country_refusal(deal, SimpleNamespace(country="US")) == (None, None)
    country_profiles._stored["NG"] = {"iso2": "NG", "escrow_providers": ["nobody"]}
    assert "escrow" in country_refusal(deal, SimpleNamespace(country="NG"))[1]


def test_composition_switches(monkeypatch):
    from shared.orchestration import outreach
    assert not outreach.composition_enabled() and outreach.composition_max_buyers() == 3
    monkeypatch.setenv("DEAL_COMPOSITION_ENABLED", "true")
    monkeypatch.setenv("COMPOSITION_MAX_BUYERS", "x")
    assert outreach.composition_enabled() and outreach.composition_max_buyers() == 3
    monkeypatch.setenv("COMPOSITION_MAX_BUYERS", "5")
    assert outreach.composition_max_buyers() == 5


# ---------------------------------------------------------------------------
# Registrations, buyer permits, HS policy re-confirmation
# ---------------------------------------------------------------------------

def test_required_registrations():
    assert registrations.required_for("09042211") == ["spices_board_cres", "fssai"]
    assert registrations.required_for("03061700") == ["mpeda", "fssai"]
    assert registrations.required_for("10063020") == ["apeda_rcmc", "fssai"]
    assert registrations.required_for("52010000") == []


async def test_record_assess_and_expiry(monkeypatch):
    monkeypatch.setenv("PORTLESS_REGISTRATIONS", "iec:AAACP1234X,lut:AD330426000001:2026-11-30,bogus:1")
    today = date(2026, 11, 19)
    rows = {r["kind"]: r for r in await registrations.check_for("10063020", today)}
    assert rows["iec"]["status"] == "ok" and rows["lut"]["status"] == "expiring"
    assert rows["apeda_rcmc"]["status"] == "missing"
    with pytest.raises(ValueError):
        await registrations.record("nope", "1", None, by="o")
    with pytest.raises(ValueError):
        await registrations.record("fssai", " ", None, by="o")
    with pytest.raises(ValueError):
        await registrations.record("fssai", "1", "31/12/2026", by="o")
    await registrations.record("apeda_rcmc", "RCMC-1", "2026-01-01", by="o")
    assert registrations.cached()["apeda_rcmc"]["number"] == "RCMC-1"
    due = {r["kind"]: r["status"] for r in await registrations.expiring(today)}
    assert due == {"lut": "expiring", "apeda_rcmc": "expired"}
    monkeypatch.setenv("REGISTRATION_WARN_DAYS", "x")
    assert registrations.warn_days() == 30
    assert registrations._days_left({"expires_on": "garbage"}, today) is None


async def test_registration_findings_block_the_pi(monkeypatch):
    monkeypatch.setenv("REGISTRATIONS_REQUIRED", "true")
    monkeypatch.setenv("PORTLESS_REGISTRATIONS", "iec:AAACP1234X:2026-11-25,lut:AD33:2026-01-01")
    found = {f.code: f.severity for f in await compliance.check_registrations(_deal())}
    assert found["registration_expiring_iec"] == "info"
    assert found["registration_expired_lut"] == "block"
    assert found["registration_missing_apeda_rcmc"] == "block"
    monkeypatch.setenv("REGISTRATIONS_REQUIRED", "false")
    assert await compliance.check_registrations(_deal()) == []


async def test_buyer_permit_needed_before_the_pi():
    await catalogue.set_catalogue([{"product_id": "rice", "hs_code": "10063020", "name": "Basmati rice",
                                    "markets": ["SA"], "import_requirements": {"SA": ["buyer: SFDA importer registration",
                                                                                     "Halal certificate"]}}])
    deal = _deal(market="SA")
    f = compliance.check_buyer_permits(deal)
    assert f[0].code == "buyer_import_permit_missing" and f[0].data["missing"] == ["SFDA importer registration"]
    deal = deal.model_copy(update={"buyer_permits": {"SFDA importer registration": {"number": "S-1"}}})
    assert compliance.check_buyer_permits(deal) == []


async def test_hs_policy_reconfirm_and_age(monkeypatch):
    await compliance.set_hs_policy(compliance.HsPolicyEntry(hs_code="100630", status="free", confirmed_by="o"))
    assert await compliance.hs_policy_due() == []
    assert await compliance.mark_reconfirm("100630", "DGFT notification 12/2026")
    assert not await compliance.mark_reconfirm("999999", "no entry")
    due = await compliance.hs_policy_due()
    assert due[0]["reason"] == "named by a new notice"
    monkeypatch.setenv("HS_POLICY_MAX_AGE_DAYS", "x")
    assert compliance.hs_policy_max_age_days() == 30


def test_readiness_registrations(monkeypatch):
    from shared import readiness
    monkeypatch.setenv("PORTLESS_REGISTRATIONS", "iec:AAACP1234X:2020-01-01")
    checks = {c["id"]: c for c in readiness.evaluate()["checks"]}
    assert checks["registrations_valid"]["status"] in ("fail", "warn")
    assert checks["registrations_recorded"]["status"] == "warn"


# ---------------------------------------------------------------------------
# Regulatory watch
# ---------------------------------------------------------------------------

def test_links_and_match():
    from shared.orchestration import regulatory_watch as rw
    html = ('<a href="/notif/12.pdf">Notification 12/2026: export of <b>Basmati rice</b> (1006 30 20)</a>'
            '<a href="https://x/y">tiny</a><a href="/n/13.pdf">Amendment on onions</a>')
    links = rw.links(html, "https://www.dgft.gov.in/CP/?opt=notification")
    assert links[0]["url"] == "https://www.dgft.gov.in/notif/12.pdf" and len(links) == 2
    lines = [SimpleNamespace(hs_code="10063020", name="Basmati rice", synonyms=[], hs8_codes=[]),
             SimpleNamespace(hs_code="070310", name="Onion", synonyms=["shallot"], hs8_codes=None)]
    assert rw.match(links[0]["title"], lines)["hs_codes"] == ["10063020"]
    m = rw.match(links[1]["title"], lines)
    assert m == {"hs_codes": ["070310"], "products": ["Onion"]}
    assert rw.match("Trade notice on steel", lines) == {"hs_codes": [], "products": []}


def test_sources_and_switch(monkeypatch):
    from shared.orchestration import regulatory_watch as rw
    assert len(rw.sources()) == 4 and not rw.enabled()
    monkeypatch.setenv("REGULATORY_WATCH_URLS", "dgft|https://a.test/x,https://b.test/y")
    assert rw.sources() == [("dgft", "https://a.test/x"), ("https://b.test/y", "https://b.test/y")]
    assert rw._iso_date("02.11.2026") == "2026-11-02" and rw._iso_date("2026-13-40") is None


async def test_watch_run_acts_on_a_hit(repo, monkeypatch):
    from shared.orchestration import regulatory_watch as rw
    rw.reset_for_tests()
    await catalogue.set_catalogue([{"product_id": "rice", "hs_code": "10063020", "name": "Basmati rice", "markets": ["AE"]}])
    await compliance.set_hs_policy(compliance.HsPolicyEntry(hs_code="100630", status="free", confirmed_by="o"))
    await repo.save(_deal(deal_id="D-OPEN"))
    await repo.save(_deal(deal_id="D-SEA", status=DealStatus.IN_TRANSIT))
    published = AsyncMock()
    monkeypatch.setattr("shared.orchestration.continuations._publish_exception", published)
    monkeypatch.setattr("shared.telegram.send_message", AsyncMock())
    monkeypatch.setenv("REGULATORY_WATCH_URLS", "dgft|https://dgft.test/n,cbic|https://cbic.test/down")
    monkeypatch.setenv("REGULATORY_DENIED_LIST_URL", "https://dgft.test/denied")

    def handler(request):
        if "down" in str(request.url):
            return httpx.Response(503)
        if "denied" in str(request.url):
            return httpx.Response(200, text="Denied entity list updated 05.11.2026")
        return httpx.Response(200, text='<a href="/n/1.pdf">Minimum export price on Basmati rice</a>'
                                        '<a href="/n/2.pdf">Steel quality control order</a>')
    out = await rw.run(transport=httpx.MockTransport(handler), now=NOW)
    assert out["read"] == ["dgft"] and out["failed"][0]["source"] == "cbic" and out["new"] == 2
    assert out["hits"][0]["hs_codes"] == ["10063020"]
    assert [c.args[0] for c in published.await_args_list] == ["D-OPEN"]
    assert (await compliance.get_hs_policy("100630")).needs_reconfirm
    assert out["denied_list"]["newer"] and out["denied_list"]["published"] == "2026-11-05"
    again = await rw.run(transport=httpx.MockTransport(handler), now=NOW)
    assert again["new"] == 0
    assert (await rw.recent_hits(NOW))[0]["title"].startswith("Minimum export price")
    monkeypatch.setenv("REGULATORY_WATCH_ENABLED", "true")
    monkeypatch.setattr(rw, "run", AsyncMock(return_value={"hits": []}))
    assert await rw.daily(NOW) == {"hits": []} and await rw.daily(NOW) is None


# ---------------------------------------------------------------------------
# Incoterms® 2020 pricing and wording
# ---------------------------------------------------------------------------

def test_incoterm_pricing(monkeypatch):
    from shared.orchestration import landed_cost as lc
    assert lc.term("c&f") == "CFR" and lc.term("xyz") == "CIF" and lc.term("dpu") == "DPU"
    assert lc.ddp_refused("DDP") and not lc.ddp_refused("DAP")
    assert lc.destination_usd_per_mt("CIF") == {}
    dap = lc.destination_usd_per_mt("DAP", 25)
    assert dap == {"dthc": 10.0, "destination_haulage": 12.0}
    assert lc.destination_usd_per_mt("DPU")["unloading"] == 5.0
    cif = lc.landed(500.0, "AE", 25, incoterm="CIF")
    dpu = lc.landed(500.0, "AE", 25, incoterm="DPU")
    assert dpu.destination_usd_per_mt == 27.0 and dpu.min_offer_usd_per_mt > cif.min_offer_usd_per_mt
    fas = lc.landed(500.0, "AE", 25, incoterm="FAS")
    fob = lc.landed(500.0, "AE", 25, incoterm="FOB")
    assert fas.incoterm == "FOB" and fas.term == "FAS" and fas.loading_saved_usd_per_mt == 4.0
    assert fas.cost_usd_per_mt < fob.cost_usd_per_mt


def test_ddp_held_at_l0():
    from agents.negotiation_agent import service as neg
    assert neg.mentions_ddp("We can do Delivered Duty Paid") and not neg.mentions_ddp("CIF Jebel Ali")
    assert "ddp_requested" in neg.draft_risk_flags("Price 900 USD DDP Riyadh")
    assert "ddp_requested" in neg._FORCE_L0_FLAGS


def test_incoterm_wording_helpers():
    from shared.documents.render import icc_clause, incoterm_named
    assert incoterm_named("cif", "Jebel Ali") == "CIF Jebel Ali, Incoterms® 2020"
    assert incoterm_named(None, None, "port to be confirmed") == "FOB port to be confirmed, Incoterms® 2020"
    assert icc_clause("CIF").endswith("(C)") and icc_clause("CIP").endswith("(A)") and icc_clause("FOB") is None


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------

def test_sample_request_reading():
    from shared.orchestration import samples
    assert samples.asks_for_sample("Interested. Can you send us a sample first?")
    assert samples.asks_for_sample("Samples required before we order.")
    assert not samples.asks_for_sample("We do not need a sample, send the price.")
    assert not samples.asks_for_sample("Please quote CIF Dubai.")


async def test_sample_flow_and_reference(repo, monkeypatch):
    from shared import deal_costs
    from shared.orchestration import samples
    monkeypatch.setattr(deal_costs, "_collection", lambda: None)
    await repo.save(_deal())
    row = await samples.capture_from_reply("D-1", "Please send samples before we confirm", buyer="C1")
    assert row["status"] == "requested" and row["source"] == "email"
    assert (await samples.request("D-1", by="ops", buyer="C1"))["sample_id"] == row["sample_id"]
    assert await samples.capture_from_reply("D-1", "price please") is None
    assert await samples.capture_from_reply("NOPE", "send a sample") is None
    with pytest.raises(ValueError):
        await samples.record_courier("D-1", row["sample_id"], courier="pigeon", awb="1", cost_usd=1, by="o")
    with pytest.raises(ValueError):
        await samples.record_courier("D-1", row["sample_id"], courier="dhl", awb=" ", cost_usd=1, by="o")
    with pytest.raises(ValueError):
        await samples.record_courier("D-1", row["sample_id"], courier="dhl", awb="1", cost_usd=-1, by="o")
    with pytest.raises(LookupError):
        await samples.record_courier("D-1", "SMP-X", courier="dhl", awb="1", cost_usd=1, by="o")
    shipped = await samples.record_courier("D-1", row["sample_id"], courier="DHL", awb="123456", cost_usd=45.0, by="o")
    assert shipped["status"] == "shipped"
    assert (await deal_costs.summary("D-1"))["samples_usd"] == 45.0
    with pytest.raises(ValueError):
        await samples.record_courier("D-1", row["sample_id"], courier="dhl", awb="1", cost_usd=1, by="o")
    with pytest.raises(ValueError):
        await samples.record_verdict("D-1", row["sample_id"], verdict="maybe", note=None, by="o")
    await samples.record_verdict("D-1", row["sample_id"], verdict="approved", note="good colour", by="o")
    deal = await repo.get("D-1")
    assert deal.approved_sample_ref == row["sample_id"]
    assert samples.approved_ref(deal) == f"as per approved sample ref {row['sample_id']}"
    with pytest.raises(ValueError):
        await samples.record_verdict("D-1", row["sample_id"], verdict="rejected", note=None, by="o")
    from services.dealmaker.service import _hs_description
    assert row["sample_id"] in _hs_description("10063020", deal)
    with pytest.raises(LookupError):
        await samples.request("NOPE", by="o")


# ---------------------------------------------------------------------------
# Country documents, currency, transit, language
# ---------------------------------------------------------------------------

def test_country_documents_required():
    from shared.orchestration import export_certs
    assert export_certs.country_kinds(None) == [] and export_certs.country_kinds("BR") == []
    assert export_certs.country_kinds("AE") == ["preferential_coo", "attestation"]
    assert "preferential_coo" in export_certs.required("10063020", "AE")
    country_profiles._stored["EG"] = {"iso2": "EG", "documents": ["Consular invoice", "Import licence held by the importer"]}
    assert export_certs.country_kinds("EG") == ["consular_invoice"]
    assert export_certs.country_kinds("US") == []  # FDA prior notice is the importer's


def test_profile_feeds_currency_transit_and_language(monkeypatch):
    from shared import currency, search_terms
    from shared.orchestration import matchmaking
    country_profiles._stored["BR"] = {"iso2": "BR", "currency": "BRL", "convertible": True, "language": "pt",
                                      "main_ports": [{"locode": "BRSSZ", "name": "Santos", "transit_days": 33}]}
    monkeypatch.setattr("shared.fx_rates.has_rate", lambda cur: cur == "BRL")
    assert currency.party_currency("BR") == "BRL"
    assert matchmaking._region_transit("BR") == 33
    assert search_terms.language_of("BR") == "pt"
    assert search_terms.language_of("AE") in (None, "ar") and search_terms.language_of("US") is None


# ---------------------------------------------------------------------------
# RoDTEP / drawback credits
# ---------------------------------------------------------------------------

async def test_incentive_credits(repo, monkeypatch):
    from services.trade_min import incentives
    from shared.orchestration import incentive_credits as ic
    ic.reset_for_tests()
    incentives.reset_for_tests()
    monkeypatch.setattr(incentives, "_collection", AsyncMock(return_value=None))
    await repo.save(_deal(buyer_target_price_usd=1000.0, quantity_mt=20.0))
    assert (await ic.check("D-1"))["status"] == "not_counted"
    await incentives.set_entry(incentives.ExportIncentive(hs_code="100630", rodtep_pct=1.5, drawback_pct=0.5,
                                                          eligible_confirmed=True, confirmed_by="ca"))
    res = await ic.check("D-1")
    assert res["counted_usd"] == 400.0 and res["status"] == "awaiting"
    for bad in (dict(kind="x"), dict(shipping_bill=" "), dict(amount_usd=-1), dict(credited_on="1/1/26")):
        args = dict(shipping_bill="SB-1", kind="rodtep", amount_usd=100.0, credited_on="2026-11-10") | bad
        with pytest.raises(ValueError):
            await ic.record("D-1", by="ops", **args)
    await ic.record("D-1", shipping_bill="SB-1", kind="rodtep", amount_usd=250.0, credited_on="2026-11-10", by="ops")
    res = await ic.check("D-1")
    assert res["status"] == "short" and res["short_usd"] == 150.0
    assert (await ic.short_all())[0]["deal_id"] == "D-1"
    await ic.record("D-1", shipping_bill="SB-1", kind="drawback", amount_usd=140.0, credited_on=None, by="ops")
    assert (await ic.check("D-1"))["status"] == "ok"
    with pytest.raises(LookupError):
        await ic.check("NOPE")
    monkeypatch.setenv("INCENTIVE_CREDIT_TOLERANCE_PCT", "x")
    assert ic.tolerance_pct() == 5.0


# ---------------------------------------------------------------------------
# Partial supply
# ---------------------------------------------------------------------------

def test_partial_supply_keeps_the_supplier_and_widens(monkeypatch):
    from shared.orchestration import sourcing_agent as sa
    buyer = SimpleNamespace(status="interested", requirement=SimpleNamespace(quantity_mt=1000.0), quantity_mt=None)
    offer = SimpleNamespace(fob_price_usd_per_mt=500.0, verification_failed=False, capacity_mt_per_month=300.0,
                            price_basis="quoted")
    deal = SimpleNamespace(status="MATCHING", buyer_candidates=[buyer], supplier_offers=[offer],
                           sourcing_widened_at=None, created_at=NOW - timedelta(days=30))
    assert sa.supply_short(deal) == 0.0 and not sa.stalled(deal, NOW)
    monkeypatch.setenv("DEAL_COMPOSITION_ENABLED", "true")
    assert sa.supply_short(deal) == 700.0 and sa.stalled(deal, NOW)
    offer.capacity_mt_per_month = None
    assert sa.supply_short(deal) == 0.0


# ---------------------------------------------------------------------------
# Inbound question agent
# ---------------------------------------------------------------------------

def test_question_reading():
    from shared.orchestration import question_agent as qa
    assert qa.is_question("How does the payment work?") and qa.is_question("Please confirm the packing")
    assert not qa.is_question("Thanks, noted.")
    assert qa.topics("Which certificates and what packing?") == ["certificates", "packing"]
    assert qa.answerable("How does the escrow payment work?")
    assert not qa.answerable("Can you lower the price?") and not qa.answerable("Hello?")


async def test_question_answered_from_facts(repo, monkeypatch):
    from shared.orchestration import question_agent as qa
    deal = _deal(approved_sample_ref="SMP-1")
    await repo.save(deal)
    session = SimpleNamespace(deal_id="D-1", status="ROUND", packing=None)
    body, asked = qa.compose("How does payment work, which certificates and what packing? Lead time? Sample?",
                             deal, session, "buyer")
    assert asked == ["certificates", "packing", "payment", "lead_time", "samples"]
    assert "escrow" in body.lower() and "Certificate of origin" in body
    sbody, _ = qa.compose("Inspection and payment and certificates and samples and lead time?", deal, session, "supplier")
    assert "your facility" in sbody and "goods-ready date" in sbody
    spec, _ = qa.compose("What specification?", deal, session, "buyer")
    assert "SMP-1" in spec
    nothing, _ = qa.compose("What specification?", _deal(approved_sample_ref=None), session, "buyer")
    assert nothing is None
    assert qa.compose("Hi?", deal, session, "buyer")[0] is None
    gate = AsyncMock(return_value="EMAIL-1")
    monkeypatch.setattr("agents.negotiation_agent.service._create_and_gate_email", gate)
    neg = SimpleNamespace(get_session=AsyncMock(return_value=session))
    monkeypatch.setattr("agents.negotiation_agent.repository.get_repository", lambda: neg)
    assert await qa.answer("N-1", "buyer", "b@gulf.ae", "Order", "How does payment work?") is None  # switched off
    monkeypatch.setenv("QUESTION_AGENT_ENABLED", "true")
    assert await qa.answer("N-1", "buyer", "b@gulf.ae", "Order", "How does payment work?") == "EMAIL-1"
    assert gate.await_args.args[3] == "Re: Order" and "question_answer" in gate.await_args.kwargs["extra_flags"]
    assert await qa.answer("N-1", "buyer", "b@gulf.ae", "Order", "What is your best price?") is None
    session.status = "FAILED"
    assert await qa.answer("N-1", "buyer", "b@gulf.ae", "Re: x", "How does payment work?") is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def api(repo, monkeypatch):
    from services.deals.router import router
    from shared.auth.dependencies import get_current_user, require_portless
    monkeypatch.setattr("shared.audit_log.staff_relay", AsyncMock())
    monkeypatch.setattr("shared.audit_log.record", AsyncMock())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_portless] = lambda: STAFF
    app.dependency_overrides[get_current_user] = lambda: STAFF
    return TestClient(app, raise_server_exceptions=False)


async def test_routes(api, repo, monkeypatch):
    await repo.save(_deal())
    assert api.get("/deals/compliance/countries").json()["countries"]
    assert api.get("/deals/compliance/countries/ae").json()["tier"] == "preferred"
    assert api.get("/deals/compliance/countries/X").status_code == 404
    assert api.put("/deals/compliance/countries/BRA", json={"currency": "BRL"}).status_code == 422
    assert api.put("/deals/compliance/countries/BR", json={"source": "x"}).status_code == 422
    assert api.put("/deals/compliance/countries/BR", json={"tier": "bogus"}).status_code == 422
    assert api.put("/deals/compliance/countries/BR", json={"currency": "BRL", "source": "bcb"}).json()["profile"]["currency"] == "BRL"
    assert api.put("/deals/compliance/registrations/iec", json={"number": "AAACP"}).status_code == 200
    assert api.put("/deals/compliance/registrations/zzz", json={"number": "1"}).status_code == 422
    assert "iec" in api.get("/deals/compliance/registrations").json()["held"]
    assert api.get("/deals/compliance/hs-policy-due").json()["due"] == []
    assert api.get("/deals/incentive-credits/short").json() == {"short": []}
    monkeypatch.setenv("AUTH_COMPLIANCE_OFFICER_EMAILS", "officer@portless.test")
    assert api.put("/deals/compliance/registrations/iec", json={"number": "AAACP"}).status_code == 403
    monkeypatch.delenv("AUTH_COMPLIANCE_OFFICER_EMAILS")

    sample = api.post("/deals/D-1/samples", json={"note": "for Gulfood"}).json()
    assert api.post("/deals/NOPE/samples", json={}).status_code == 404
    assert api.post(f"/deals/D-1/samples/{sample['sample_id']}/courier", json={"courier": "x", "awb": "1"}).status_code == 422
    assert api.post("/deals/D-1/samples/SMP-X/courier", json={"courier": "dhl", "awb": "1"}).status_code == 404
    assert api.post(f"/deals/D-1/samples/{sample['sample_id']}/courier",
                    json={"courier": "fedex", "awb": "77", "cost_usd": 30}).json()["status"] == "shipped"
    assert api.post(f"/deals/D-1/samples/{sample['sample_id']}/verdict", json={"verdict": "x"}).status_code == 422
    assert api.post("/deals/D-1/samples/SMP-X/verdict", json={"verdict": "approved"}).status_code == 404
    assert api.post(f"/deals/D-1/samples/{sample['sample_id']}/verdict", json={"verdict": "approved"}).status_code == 200
    assert api.get("/deals/D-1/samples").json()["approved_sample_ref"] == sample["sample_id"]

    assert api.put("/deals/D-1/buyer-permits", json={"name": "SFDA", "number": "1", "valid_until": "soon"}).status_code == 422
    out = api.put("/deals/D-1/buyer-permits", json={"name": "SFDA importer", "number": "S-9", "valid_until": "2027-06-30"}).json()
    assert out["buyer_permits"]["SFDA importer"]["number"] == "S-9" and out["still_missing"] == []

    assert api.post("/deals/D-1/incentive-credits", json={"shipping_bill": "SB", "kind": "x", "amount_usd": 1}).status_code == 422
    monkeypatch.setattr("services.trade_min.incentives.entry_for", AsyncMock(return_value=None))
    assert api.post("/deals/D-1/incentive-credits",
                    json={"shipping_bill": "SB", "kind": "rodtep", "amount_usd": 10}).json()["status"] == "not_counted"
    assert api.get("/deals/D-1/incentive-credits").json()["credited_usd"] == 10.0


# ---------------------------------------------------------------------------
# Mongo storage paths
# ---------------------------------------------------------------------------

async def test_stores_in_mongo(repo, monkeypatch):
    from mongomock_motor import AsyncMongoMockClient
    from shared.orchestration import incentive_credits as ic, regulatory_watch as rw
    db = AsyncMongoMockClient()["portless_test"]
    monkeypatch.setattr("shared.mongo.collection", lambda name: db[name], raising=False)
    monkeypatch.setattr(country_profiles, "tier_changed", AsyncMock(return_value=[]))
    await country_profiles.put("BR", {"currency": "BRL"}, by="o")
    country_profiles.reset_for_tests()
    await country_profiles.load()
    assert country_profiles.get("BR").currency == "BRL"
    await registrations.record("fssai", "F-1", "2027-01-01", by="o")
    registrations.reset_for_tests()
    assert (await registrations.held())["fssai"]["number"] == "F-1"
    await repo.save(_deal())
    await ic.record("D-1", shipping_bill="SB", kind="rodtep", amount_usd=5.0, credited_on=None, by="o")
    assert (await ic.recorded("D-1"))[0]["amount_usd"] == 5.0
    monkeypatch.setattr("services.trade_min.incentives.entry_for", AsyncMock(return_value=None))
    assert await ic.short_all() == []
    item = {"_id": "i1", "source": "dgft", "title": "t", "url": "u", "hs_codes": ["100630"], "products": [],
            "seen_at": NOW.isoformat(), "hit": True}
    await rw._store(item)
    rw.reset_for_tests()
    assert await rw._known("i1") and not await rw._known("i2")
    assert (await rw.recent_hits(NOW))[0]["_id"] == "i1"


async def test_ratchet_edges(monkeypatch):
    """Branches the coverage ratchet holds at 100 %: settings fallbacks, stale confirmations,
    an unreadable HS policy store, partial-supply edge cases."""
    from services.trade_min import markets
    from shared.orchestration import sourcing_agent as sa
    monkeypatch.setenv("WORLDWIDE_MARKET_STANDARD_EXTRA", "x")
    assert markets._standard_extra() == 2
    old = compliance.HsPolicyEntry(hs_code="070310", status="free", confirmed_by="o",
                                   confirmed_at=NOW - timedelta(days=400))
    await compliance.set_hs_policy(old)
    assert (await compliance.hs_policy_due())[0]["reason"] == "older than the re-confirmation period"
    status, note = await compliance.hs_policy_status("07031010")
    assert status == "unverified" and "more than" in note
    await compliance.mark_reconfirm("07031010", "notice")
    status, note = await compliance.hs_policy_status("070310")
    assert status == "unverified" and "(notice)" in note

    class Broken:
        def find(self, *a, **k):
            raise RuntimeError("down")

        async def find_one(self, *a, **k):
            return None

        async def replace_one(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(compliance, "_collection", lambda name: Broken())
    assert (await compliance.hs_policy_due())[0]["hs_code"] == "070310"
    assert await compliance.mark_reconfirm("070310", "again")

    monkeypatch.setenv("REGISTRATIONS_REQUIRED", "true")
    monkeypatch.setenv("PORTLESS_REGISTRATIONS", "iec:AAACP1234X:2030-01-01,lut:AD33:2030-01-01")
    found = {f.code for f in await compliance.check_registrations(_deal(hs_code="52010000"))}
    assert found == set()

    monkeypatch.setenv("DEAL_COMPOSITION_ENABLED", "true")
    declined = SimpleNamespace(status="declined", requirement=None, quantity_mt=50.0)
    deal = SimpleNamespace(buyer_candidates=[declined], supplier_offers=[])
    assert sa.supply_short(deal) == 0.0
    buyer = SimpleNamespace(status="interested", requirement=None, quantity_mt=100.0)
    unpriced = SimpleNamespace(fob_price_usd_per_mt=None, verification_failed=False, capacity_mt_per_month=10.0)
    priced = SimpleNamespace(fob_price_usd_per_mt=500.0, verification_failed=False, capacity_mt_per_month=40.0)
    deal = SimpleNamespace(buyer_candidates=[declined, buyer], supplier_offers=[unpriced, priced])
    assert sa.supply_short(deal) == 60.0
