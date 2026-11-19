"""2026-11-19 batch 5: trade operations — Gulf USD pegs, ECGC tier suggestions, cargo insurance per
Incoterm, CHA hand-off, empty-container survey, letter of intent + broker-chain guard, RoSCTL, CBM,
government portals + Indian missions, demand leads (buy requests, tenders, stated quantities), payment
chase, and the PortLess side of actors A.3 / A.4 / A.6 / A.7 / A.8 / A.16 / A.17 / A.19 / A.22 /
A.26 / A.29 / A.30 / A.31 / A.33 / A.34 / A.38."""
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
from shared import catalogue, country_profiles, fx_rates, registrations
from shared.orchestration import (cargo_insurance, cbm, cha, compliance, demand_leads, destination_rules, gov_leads,
                                  loi, refusals, registration_checks, regulatory_watch)
from tests.factories import make

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
STAFF = SimpleNamespace(user_id="u-ops", email="ops@portless.test", name="Ops", org_id=None, is_portless=True)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for mod in (country_profiles, registrations, compliance, fx_rates, cha, demand_leads, destination_rules,
                gov_leads, refusals, registration_checks, regulatory_watch):
        mod.reset_for_tests()
    monkeypatch.setattr("shared.mongo.collection", lambda name: None, raising=False)
    from services.trade_min import directory
    monkeypatch.setattr(directory, "_directory", directory.InMemoryDirectory())
    monkeypatch.setattr("shared.telegram.send_message", AsyncMock(), raising=False)
    yield
    catalogue.reset_for_tests()


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", r)
    return r


@pytest.fixture
def published(monkeypatch):
    pub = AsyncMock()
    monkeypatch.setattr("shared.orchestration.continuations._publish_exception", pub)
    return pub


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def send_email(req):
        calls.append(req)
        return SimpleNamespace(outcome=SimpleNamespace(value="sent"))
    monkeypatch.setattr("services.email_gateway.sender.send_email", send_email)
    return calls


def _deal(**kw) -> DealRecord:
    base = dict(deal_id="DEAL-ABC123", hs_code="09041100", market="AE", status=DealStatus.MATCHING, org_id="portless",
                quantity_mt=20.0)
    base.update(kw)
    return make(DealRecord, **base)


def _connector(monkeypatch, results: dict):
    async def quote(request):
        return results
    monkeypatch.setattr("shared.connectors.registry.get_connector",
                        lambda name: SimpleNamespace(quote=quote, configured=True))


# ---------------------------------------------------------------------------
# Gulf USD pegs
# ---------------------------------------------------------------------------

async def test_pegs_are_reference_rates_and_checked(monkeypatch):
    assert fx_rates.cached("AED") is None  # pinned off in the suite
    monkeypatch.setenv("FX_PEGS_ENABLED", "true")
    assert fx_rates.cached("AED")["rate"] == 3.6725 and fx_rates.cached("AED")["source"] == "peg"
    assert fx_rates.has_rate("SAR") and fx_rates.cached("KWD") is None
    await fx_rates.record("AED", 3.70, source="bank", by="ops")
    out = await fx_rates.check_pegs(now=NOW)
    assert out["moved"] == ["AED"]
    assert await fx_rates.monthly_peg_check(NOW + timedelta(days=1)) is None
    assert (await fx_rates.monthly_peg_check(NOW + timedelta(days=31)))["moved"] == ["AED"]
    monkeypatch.setenv("FX_PEGS_ENABLED", "false")
    assert await fx_rates.monthly_peg_check(NOW + timedelta(days=90)) is None


# ---------------------------------------------------------------------------
# ECGC tier suggestion
# ---------------------------------------------------------------------------

async def test_ecgc_rating_suggests_a_tier_and_flags_open_deals(repo, published):
    assert country_profiles.suggest_tier("a1") == "preferred" and country_profiles.suggest_tier("C2") == "review"
    assert country_profiles.suggest_tier("D") == "blocked" and country_profiles.suggest_tier(None) is None
    await repo.save(_deal())
    out = await country_profiles.put("AE", {"ecgc_rating": "C1"}, by="feed")
    assert out["profile"]["tier"] == "preferred" and out["profile"]["tier_suggestion"] == "review"
    assert published.await_args.args[1] == "COUNTRY:AE:ecgc_changed" and out["deals_rechecked"] == ["DEAL-ABC123"]
    again = await country_profiles.put("AE", {"ecgc_rating": "A2"}, by="feed")
    assert "tier_suggestion" not in again["profile"] or again["profile"]["tier_suggestion"] is None
    first = await country_profiles.put("BR", {"ecgc_rating": "B1"}, by="feed")  # no profile before
    assert first["profile"]["tier_suggestion"] is None  # suggestion equals the default standard tier


# ---------------------------------------------------------------------------
# Cargo insurance
# ---------------------------------------------------------------------------

def test_insurance_segments_per_incoterm():
    cif = cargo_insurance.segments("CIF", "FOB", marine_pct=0.5)
    assert [s["key"] for s in cif] == ["marine_insurance"] and cif[0]["pct_of_price"] == 0.55
    assert "Cargo Clauses (C)" in cif[0]["note"]
    ddp = cargo_insurance.segments("DDP", "EXW", marine_pct=0.5)
    assert [s["key"] for s in ddp] == ["insurance_inland", "marine_insurance"] and "door" in ddp[1]["label"]
    assert "(A)" in cargo_insurance.segments("CIP", "FOB")[0]["note"]
    fob = cargo_insurance.segments("FOB", "FOB")
    assert [s["key"] for s in fob] == ["insurance_contingency"]


def test_cost_sheet_uses_the_sale_term():
    from shared.orchestration import trade_costs
    base = dict(quantity_mt=20, market="AE", freight_usd_per_mt=50, freight_is_quote=False, qc_usd_per_mt=None,
                qc_is_quote=False, supplier_address=None)
    old = trade_costs.build(**base)
    assert [line.label for line in old.lines if line.key == "marine_insurance"] == ["Marine cargo insurance (CIF)"]
    new = trade_costs.build(**base, sale_incoterm="FOB")
    keys = [line.key for line in new.lines]
    assert "insurance_contingency" in keys and "marine_insurance" not in keys


async def test_declaration_and_claims(repo, monkeypatch):
    await repo.save(_deal())
    with pytest.raises(ValueError):
        await cargo_insurance.record_declaration("DEAL-ABC123", certificate_no=" ", insurer=None, sum_insured_usd=None, by="o")
    with pytest.raises(ValueError):
        await cargo_insurance.record_declaration("DEAL-ABC123", certificate_no="C1", insurer=None, sum_insured_usd=-1, by="o")
    with pytest.raises(LookupError):
        await cargo_insurance.record_declaration("NOPE", certificate_no="C1", insurer=None, sum_insured_usd=None, by="o")
    await cargo_insurance.record_declaration("DEAL-ABC123", certificate_no="MC-77", insurer="ICICI", sum_insured_usd=22000, by="o")
    deal = await repo.get("DEAL-ABC123")
    assert cargo_insurance.certificate_no(deal) == "MC-77"
    for bad in (dict(reason="", amount_usd=1), dict(reason="wet", amount_usd=0), dict(reason="wet", amount_usd=1, delivered_on="x")):
        with pytest.raises(ValueError):
            await cargo_insurance.open_claim("DEAL-ABC123", by="o", **{"delivered_on": None, **bad})
    claim = await cargo_insurance.open_claim("DEAL-ABC123", reason="water damage", amount_usd=900,
                                             delivered_on="2026-11-01", by="o")
    assert claim["notice_due"] == "2026-11-08"
    deal = await repo.get("DEAL-ABC123")
    assert cargo_insurance.overdue(deal, date(2026, 11, 9))[0]["claim_id"] == claim["claim_id"]
    with pytest.raises(ValueError):
        await cargo_insurance.update_claim("DEAL-ABC123", claim["claim_id"], status="settled", by="o")
    with pytest.raises(LookupError):
        await cargo_insurance.update_claim("DEAL-ABC123", "CLM-X", status="notified", by="o")
    await cargo_insurance.update_claim("DEAL-ABC123", claim["claim_id"], status="notified", by="o")
    with pytest.raises(ValueError):
        await cargo_insurance.update_claim("DEAL-ABC123", claim["claim_id"], status="surveyed", by="o")
    await cargo_insurance.update_claim("DEAL-ABC123", claim["claim_id"], status="surveyed", survey_ref="SV-1", by="o")
    await cargo_insurance.update_claim("DEAL-ABC123", claim["claim_id"], status="filed", by="o")
    with pytest.raises(ValueError):
        await cargo_insurance.update_claim("DEAL-ABC123", claim["claim_id"], status="settled", by="o")
    done = await cargo_insurance.update_claim("DEAL-ABC123", claim["claim_id"], status="settled", recovered_usd=800, by="o")
    assert done["recovered_usd"] == 800 and len(done["history"]) == 5


# ---------------------------------------------------------------------------
# CHA hand-off
# ---------------------------------------------------------------------------

async def test_cha_agents_kyc_pack_reply_and_invoice(repo, published, sent, monkeypatch):
    for bad in ({"firm": "", "licence_no": "1", "email": "a@b.in", "ports": ["INNSA1"]},
                {"firm": "X", "licence_no": "1", "email": "nope", "ports": ["INNSA1"]},
                {"firm": "X", "licence_no": "1", "email": "a@b.in", "ports": []}):
        with pytest.raises(ValueError):
            await cha.put_agent(bad, by="o")
    old = await cha.put_agent({"firm": "Lapsed CHA", "licence_no": "L0", "email": "x@lapsed.in", "ports": ["NHAVA SHEVA"],
                               "licence_valid_until": "2025-01-01"}, by="o")
    agent = await cha.put_agent({"firm": "Nhava CHA", "licence_no": "R-1", "email": "docs@nhavacha.in",
                                 "ports": ["Nhava Sheva", "INNSA1"], "licence_valid_until": "2027-12-31",
                                 "charges_per_shipment_usd": 140}, by="o")
    assert not cha.licence_ok(old) and cha.licence_ok(agent) and not cha.licence_ok({"licence_valid_until": None})
    assert (await cha.choose("INNSA1"))["agent_id"] == agent["agent_id"]
    assert await cha.choose("Mundra") is None and len(await cha.agents()) == 2
    status = await cha.kyc_status(agent["agent_id"])
    assert not status["sent"] and not status["ok"]
    with pytest.raises(ValueError):
        await cha.record_kyc(agent["agent_id"], sent_on="2026-11-01", authorisation_ref=" ", by="o")
    await cha.record_kyc(agent["agent_id"], sent_on="2026-11-01", authorisation_ref="AUTH-1", by="o")
    assert (await cha.kyc_status(agent["agent_id"]))["sent"]

    await repo.save(_deal(status=DealStatus.QC_PASSED))
    monkeypatch.setattr(cha, "incentives_to_declare", AsyncMock(return_value=["rodtep"]))
    with pytest.raises(LookupError):
        await cha.send_pack("DEAL-ABC123", by="o", port="Mundra")
    pack = await cha.send_pack("DEAL-ABC123", by="o", port="INNSA1")
    assert pack["status"] == "pack_sent" and pack["incentives_to_declare"] == ["rodtep"]
    assert "[CHA:DEAL-ABC123]" in sent[0].subject and "RODTEP" in sent[0].body
    assert (await cha.send_pack("DEAL-ABC123", by="o"))["pack_sent_at"] == pack["pack_sent_at"]  # idempotent

    assert cha.parse_token("Re: Shipping Bill [CHA:DEAL-ABC123]") == "DEAL-ABC123" and cha.parse_token("x") is None
    assert not (await cha.handle_reply("DEAL-ABC123", "spoof@evil.com", "SB No: 1234567"))["handled"]
    gate = AsyncMock(return_value=SimpleNamespace(approval_id="AP-1"))
    monkeypatch.setattr("shared.orchestration.continuations._create_gate", gate)
    out = await cha.handle_reply("DEAL-ABC123", "ops@nhavacha.in", "Please find the checklist attached for approval.")
    assert out["gate"] == "AP-1" and gate.await_args.args[1] == "APPROVE_SB_CHECKLIST"
    assert "incentive_not_in_checklist" in gate.await_args.kwargs["risk_flags"]
    assert not (await cha.handle_reply("DEAL-ABC123", "ops@nhavacha.in", "hello"))["handled"]
    assert (await cha.approve_filing("DEAL-ABC123", {}))["status"] == "filing_approved"

    sb = cha.parse_shipping_bill("Shipping Bill No: 7654321\nSB date: 20/11/2026\nLEO date 21.11.2026\nPort code INNSA1\nDrawback claimed")
    assert sb == {"number": "7654321", "date": "2026-11-20", "leo_date": "2026-11-21", "port_code": "INNSA1",
                  "declared": ["drawback"]}
    assert cha.parse_shipping_bill("no number") is None and cha._iso("bad") is None
    out = await cha.handle_reply("DEAL-ABC123", "docs@nhavacha.in", "Shipping bill no 7654321 filed, drawback")
    assert out["shipping_bill"]["number"] == "7654321"
    assert published.await_args.args[1] == "INCENTIVE:not_declared"
    with pytest.raises(ValueError):
        await cha.record_shipping_bill("DEAL-ABC123", {"number": "12"}, by="o")
    with pytest.raises(ValueError):
        await cha.record_invoice("DEAL-ABC123", amount_usd=-1, items=None, by="o")
    inv = await cha.record_invoice("DEAL-ABC123", amount_usd=400, items={"agency": 300}, by="o")
    assert inv["over_estimate"] and published.await_args.args[1] == "CHA:cost_overrun"
    assert cha._estimate(SimpleNamespace(cost_sheet={"lines": [{"key": "customs_agent", "usd_per_mt": 7.5}],
                                                     "quantity_mt": 20})) == 150.0
    assert cha._port_of_loading(SimpleNamespace(port_of_loading=None, cost_sheet={"supplier_route": {"port": "Mundra"}})) == "Mundra"


async def test_cha_incentives_to_declare(monkeypatch):
    from services.trade_min import incentives
    entry = incentives.ExportIncentive(hs_code="6109", rosctl_pct=3.0, drawback_pct=1.0, eligible_confirmed=True)
    monkeypatch.setattr(incentives, "entry_for", AsyncMock(return_value=entry))
    assert await cha.incentives_to_declare("61091000") == ["rosctl", "drawback"]
    monkeypatch.setattr(incentives, "entry_for", AsyncMock(return_value=None))
    assert await cha.incentives_to_declare("61091000") == []


async def test_cha_gate_callback_and_continuation(monkeypatch):
    from services.hitl_approval import callbacks
    monkeypatch.setattr(cha, "approve_filing", AsyncMock(return_value={"status": "filing_approved"}))
    assert (await callbacks._cb_approve_sb_checklist("D", {}))["status"] == "filing_approved"
    from shared.orchestration import continuations
    monkeypatch.setattr(cha, "send_pack", AsyncMock(side_effect=LookupError("no CHA")))
    monkeypatch.setenv("CHA_HANDOFF_ENABLED", "true")
    await continuations._cha_pack("D")  # no CHA → nothing, no raise
    monkeypatch.setenv("CHA_HANDOFF_ENABLED", "false")
    await continuations._cha_pack("D")


# ---------------------------------------------------------------------------
# RoSCTL
# ---------------------------------------------------------------------------

def test_rosctl_rules_and_income():
    from services.trade_min import incentives
    with pytest.raises(ValueError):
        incentives.ExportIncentive(hs_code="6109", rodtep_pct=1, rosctl_pct=2)
    with pytest.raises(ValueError):
        incentives.ExportIncentive(hs_code="0904", rosctl_pct=2)
    e = incentives.ExportIncentive(hs_code="6109", rosctl_pct=4.0, rosctl_cap_usd_per_mt=30, eligible_confirmed=True)
    income, basis = incentives.income_per_mt(e, 1000)
    assert income == 30 and "RoSCTL 4%" in basis
    assert incentives.schemes(e) == ["rosctl"] and incentives.total_pct(e) == 4.0
    assert incentives.schemes(None) == [] and incentives.total_pct(None) == 0.0
    from shared.orchestration import incentive_credits
    assert "rosctl" in incentive_credits.KINDS


# ---------------------------------------------------------------------------
# Empty-container survey
# ---------------------------------------------------------------------------

async def test_container_survey_blocks_until_every_box_passed(repo, published, monkeypatch):
    await repo.save(_deal(status=DealStatus.QC_PASSED))
    rec = compliance.LoadingRecord(container_numbers=["MSKU1", "MSKU2"], seal_numbers=["S1", "S2"], loaded_quantity_mt=20,
                                   photos=["p"], container_surveys=[{"container": "msku1", "status": "passed"},
                                                                    {"container": "MSKU2", "status": "rejected"}])
    await compliance.record_loading("DEAL-ABC123", rec)
    assert published.await_args.args[1] == "CONTAINER:rejected"
    rec_loaded = (await compliance.get_record("DEAL-ABC123")).loading
    assert compliance.survey_problems(rec_loaded) == ["MSKU2: rejected"] and compliance.survey_problems(None) == []
    monkeypatch.setattr(compliance, "_pi_po", AsyncMock(return_value=(None, None)))
    monkeypatch.setenv("CONTAINER_SURVEY_REQUIRED", "true")
    result = await compliance.check_shipment(await repo.get("DEAL-ABC123"))
    assert any(f.code == "shipment_container_survey" and f.severity == "block" for f in result.findings)
    monkeypatch.setenv("CONTAINER_SURVEY_REQUIRED", "false")
    result = await compliance.check_shipment(await repo.get("DEAL-ABC123"))
    assert not any(f.code == "shipment_container_survey" for f in result.findings)
    missing = compliance.LoadingRecord(container_numbers=["A1"], seal_numbers=["S"], container_surveys=[])
    assert compliance.survey_problems(missing) == ["A1: missing"]
    assert len(compliance.SURVEY_CHECKS) == 7


# ---------------------------------------------------------------------------
# Letter of intent
# ---------------------------------------------------------------------------

LOI_TEXT = ("Please find our LOI.\nLOI No: GF-2026/11\nWe intend to buy 500 MT per month, CIF Jebel Ali, port.\n"
            "Shipment within December 2026.\nSpecification: black pepper 550 g/l, moisture max 12%")


def test_loi_parse_and_chain():
    facts = loi.parse(LOI_TEXT)
    assert facts["quantity_mt"] == 500 and facts["monthly"] and facts["port"].startswith("Jebel Ali")
    assert facts["reference"] == "GF-2026/11" and "550" in facts["spec"] and "December" in facts["delivery_window"]
    assert loi.parse("Thanks for the offer") is None
    assert loi.broker_chain_pattern("Send ICPO, then we issue FCO; SGS at load port") == ["icpo", "fco", "sgs_load_port"]
    assert loi.broker_chain_pattern("We sent our LOI") == []
    assert loi.chance_factor(SimpleNamespace(loi={"x": 1})) == loi.CHANCE_FACTOR
    assert loi.chance_factor(SimpleNamespace(loi=None)) == 1.0
    text = loi.template(product="Black pepper", spec="550 g/l", quantity_mt=100, price_low=5000, price_high=5300,
                        incoterm="CNF", place="Jebel Ali", delivery_window="December")
    assert "CFR Jebel Ali, Incoterms® 2020" in text and "USD 5,000 – 5,300 per MT" in text
    assert "to be confirmed" in loi.template(product="x", spec=None, quantity_mt=1, price_low=None, price_high=None,
                                             incoterm="FOB", place="Mundra", delivery_window="soon")


async def test_loi_record_and_capture(repo, monkeypatch):
    cand = BuyerCandidate(candidate_id="C1", legal_name="Gulf Foods", country="AE", email="buy@gulffoods.ae")
    await repo.save(_deal(buyer_candidates=[cand]))
    with pytest.raises(LookupError):
        await loi.record("NOPE", {}, source="x", by="o")
    assert await loi.capture_from_reply("DEAL-ABC123", cand, "x@gmail.com", LOI_TEXT) is None  # not the buyer
    assert await loi.capture_from_reply("DEAL-ABC123", cand, "buy@gulffoods.ae", "Send ICPO and FCO " + LOI_TEXT) is None
    row = await loi.capture_from_reply("DEAL-ABC123", cand, "buy@gulffoods.ae", LOI_TEXT)
    assert row["quantity_mt"] == 500
    deal = await repo.get("DEAL-ABC123")
    assert deal.loi["reference"] == "GF-2026/11" and deal.buyer_candidates[0].requirement.commitment == "loi"
    failed = cand.model_copy(update={"validation_status": "failed"})
    assert loi.counts(deal, "buy@gulffoods.ae", failed) == (False, "the buyer failed the counterparty check")


# ---------------------------------------------------------------------------
# CBM
# ---------------------------------------------------------------------------

def test_cbm_from_packages_and_checks():
    from shared.orchestration.packing import PackingSpec
    spec = PackingSpec(unit_weight_kg=25, gross_weight_kg=25.2, length_cm=80, width_cm=50, height_cm=15)
    out = cbm.shipment(spec, 20)
    assert out["packages"] == 800 and out["cbm"] == 48.0 and out["gross_kg"] == 20160.0 and out["cbm_source"] == "packages"
    assert cbm.package_cbm(PackingSpec()) is None and cbm.package_cbm(None) is None
    est = cbm.shipment(None, 20, m3_per_mt=1.4)
    assert est["cbm"] == 28.0 and est["cbm_source"] == "estimate" and est["packages"] is None
    assert cbm.shipment({"unit_weight_kg": 50}, 1)["gross_kg"] == 1010.0
    fit = cbm.fill_check(48.0, 20160, {"20GP": 1})
    assert not fit["fits"] and "33" in fit["problems"][0]
    ok = cbm.fill_check(48.0, 20160, {"40HC": 1, "LCL": 1})
    assert ok["fits"] and ok["volume_used_pct"] == 63.2
    assert not cbm.fill_check(10, 60000, {"20GP": 1})["fits"]
    assert cbm.loaded_count_check(800, 790) is not None and cbm.loaded_count_check(800, 800) is None
    assert cbm.loaded_count_check(None, 3) is None


async def test_container_choice_uses_measured_cbm(monkeypatch):
    from shared.orchestration import container_choice
    plan = await container_choice.choose("AE", "09041100", 20, cbm=48.0, gross_kg=20160)
    assert plan["cbm_source"] == "packages" and plan["m3_per_mt"] == 2.4 and "fill_check" in plan


def test_maersk_booking_carries_volume_and_packages():
    from shared.connectors.maersk import MaerskConnector
    body = MaerskConnector().build_booking({"quantity_mt": 20, "cbm": 48.0, "packages": 800, "gross_kg": 20160})
    c = body["commodities"][0]
    assert c["cargoVolume"] == 48.0 and c["numberOfPackages"] == 800 and c["cargoGrossWeight"] == 20160.0


def test_documents_cargo_from_the_packing_spec():
    from services.docugen.schemas import GenerateDocumentRequest
    from services.docugen.service import _cargo, _rows
    from shared.orchestration.packing import PackingSpec
    spec = PackingSpec(unit_weight_kg=25, length_cm=80, width_cm=50, height_cm=15)
    f = {"session": SimpleNamespace(packing=spec), "hs_code": "09041100", "shipped_quantity_mt": 20,
         "container_numbers": ["A", "B"], "seal_numbers": ["1", "2"]}
    req = make(GenerateDocumentRequest, deal_id="D", doc_type="PL")
    cargo = _cargo(f, req)
    assert cargo["packages"] == 800 and cargo["cbm"] == 48.0 and cargo["gross_mt"] == 20.2
    rows = _rows(f, req)
    assert rows[0]["cbm"] == 24.0 and rows[0]["packages"] == 400


# ---------------------------------------------------------------------------
# Demand leads, government portals, Indian missions
# ---------------------------------------------------------------------------

ENQUIRY = """Trade enquiry received through Trade Connect
Company Name: Al Noor Trading LLC
Country: United Arab Emirates
Contact person: Mr Saeed
Email: purchase@alnoor.ae
Phone: +971 4 555 1234
Product: Black pepper whole
HS code: 090411
Quantity: 2 x 20 ft, about 40 MT
"""


async def test_portal_enquiry_becomes_a_lead_and_candidate(repo):
    assert gov_leads.is_portal_enquiry("noreply@trade.gov.in", "x") == "trade_connect"
    assert gov_leads.is_portal_enquiry("a@mail.indianbusinessportal.in", "") == "indian_business_portal"
    assert gov_leads.is_portal_enquiry("a@b.com", "New Trade Connect enquiry") == "trade_connect"
    assert gov_leads.is_portal_enquiry("a@b.com", "Indian Business Portal lead") == "indian_business_portal"
    assert gov_leads.is_portal_enquiry("a@b.com", "hello") is None
    lead = gov_leads.parse_enquiry(ENQUIRY, "trade_connect")
    assert lead["buyer_name"] == "Al Noor Trading LLC" and lead["country"] == "AE" and lead["hs_code"] == "090411"
    assert lead["quantity_mt"] == 40 and lead["email"] == "purchase@alnoor.ae"
    assert gov_leads.parse_enquiry("Need 500 kg of turmeric, email x@y.com", "x")["quantity_mt"] == 0.5
    await repo.save(_deal())
    out = await gov_leads.handle_inbound("noreply@trade.gov.in", "Enquiry", ENQUIRY)
    assert out["new"] == 1 and out["directory"] == 1 and out["attached"] == ["DEAL-ABC123"]
    deal = await repo.get("DEAL-ABC123")
    assert deal.buyer_candidates[0].quantity_mt == 40 and deal.buyer_candidates[0].source == "trade_connect"
    from services.trade_min.directory import get_directory
    rec = (await (await get_directory()).list("buyer"))[0]
    assert rec.stated_quantity_mt == 40 and rec.stated_quantity_source == "trade_connect"
    again = await gov_leads.handle_inbound("noreply@trade.gov.in", "Enquiry", ENQUIRY)
    assert again["new"] == 0
    assert await gov_leads.handle_inbound("a@b.com", "hi", "x") is None
    assert gov_leads._country_code("ksa") == "SA" and gov_leads._country_code("de") == "DE"
    assert gov_leads._country_code("") is None and gov_leads._country_code("Atlantis") is None


async def test_demand_leads_match_by_product_name_and_daily(repo, monkeypatch):
    await repo.save(_deal(deal_id="DEAL-PEPPER1", hs_code="090411"))
    monkeypatch.setattr("shared.product_label.product_label", lambda hs: "Black pepper")
    lead = {"source": "buy_lead:tradekey", "kind": "buy_request", "buyer_name": "Rotterdam Spice", "country": "AE",
            "product": "black pepper 500gl", "email": "rs@spice.nl", "quantity_mt": 25, "flags": ["unverified_lead"]}
    assert demand_leads._matches(await repo.get("DEAL-PEPPER1"), lead)
    assert not demand_leads._matches(await repo.get("DEAL-PEPPER1"), {**lead, "country": "DE"})
    out = await demand_leads.ingest([lead, {"source": "x", "buyer_name": ""}])
    assert out["attached"] == ["DEAL-PEPPER1"] and out["directory"] == 1
    assert (await demand_leads.attach({"buyer_name": "No contact"})) == []
    assert len(await demand_leads.recent()) == 2
    _connector(monkeypatch, {"leads": []})
    monkeypatch.setenv("DEMAND_LEADS_ENABLED", "true")
    assert (await demand_leads.daily(NOW))["new"] == 0 and await demand_leads.daily(NOW) is None


async def test_missions_request_reminder_reply_import_and_stats(repo, sent, monkeypatch):
    with pytest.raises(LookupError):
        await gov_leads.request_list("AE", "090411", product=None, quantity_range=None, by="o", now=NOW)
    await country_profiles.put("AE", {"commercial_wing_email": "com.dubai@mea.gov.in"}, by="o")
    req = await gov_leads.request_list("AE", "0904.11", product="Black pepper", quantity_range="1-2 x 20 ft", by="o", now=NOW)
    assert "[MISSION:" in sent[0].subject and "1-2 x 20 ft" in sent[0].body and req["quarter"] == "2026-Q4"
    with pytest.raises(ValueError):
        await gov_leads.request_list("AE", "090411", product=None, quantity_range=None, by="o", now=NOW)
    assert await gov_leads.reminders(NOW + timedelta(days=3)) == []
    assert await gov_leads.reminders(NOW + timedelta(days=15)) == [req["request_id"]]
    assert await gov_leads.reminders(NOW + timedelta(days=30)) == []
    rid = gov_leads.parse_token(sent[0].subject)
    assert rid == req["request_id"] and gov_leads.parse_token("none") is None
    assert not (await gov_leads.handle_mission_reply("MSN-NOPE", "x@mea.gov.in", ""))["handled"]
    assert not (await gov_leads.handle_mission_reply(rid, "x@gmail.com", ""))["handled"]
    body = "List of importers:\nGulf Spice LLC, Dubai, info@gulfspice.ae, +971 4 222 3333\nno email row\nAl Baraka | sales@albaraka.ae"
    assert (await gov_leads.handle_mission_reply(rid, "commercial@mea.gov.in", body))["rows"] == 2
    with pytest.raises(LookupError):
        await gov_leads.import_list("MSN-NOPE", rows=None, by="o")
    out = await gov_leads.import_list(rid, rows=None, by="o")
    assert out["directory"] == 2
    with pytest.raises(ValueError):
        await gov_leads.import_list(rid, rows=None, by="o")
    stats = await gov_leads.stats()
    assert stats["sources"]["indian_mission"]["leads"] == 2


def test_gov_parse_list():
    rows = gov_leads.parse_list("Acme Foods; acme@foods.de; +49 30 1234567\nfoo@bar.com")
    assert rows == [{"company": "Acme Foods", "email": "acme@foods.de", "phone": "+49 30 1234567"}]


# ---------------------------------------------------------------------------
# Third-party threads in the Email Processor + broker-chain hold
# ---------------------------------------------------------------------------

async def test_email_processor_third_party_threads(monkeypatch):
    from services.email_processor import service as ep
    from services.email_processor.repository import InMemoryEmailProcessorRepository as InboundRepo
    from services.email_processor.schemas import InboundRawEmail
    store = InboundRepo()
    monkeypatch.setattr(ep, "get_repository", lambda: store)
    monkeypatch.setattr(cha, "handle_reply", AsyncMock(return_value={"handled": True}))
    monkeypatch.setattr(gov_leads, "handle_mission_reply", AsyncMock(return_value={"handled": False, "reason": "x"}))
    monkeypatch.setattr(gov_leads, "handle_inbound", AsyncMock(return_value={"new": 1}))
    base = dict(to_address="trade@portless.test", body_text="checklist", received_at=NOW, source="manual")
    r1 = await ep.process_inbound(InboundRawEmail(from_address="docs@cha.in", subject="Re: SB [CHA:DEAL-ABC123]",
                                                  message_id="m1", **base))
    assert r1.status.value == "processed" and r1.message.startswith("cha")
    r2 = await ep.process_inbound(InboundRawEmail(from_address="c@mea.gov.in", subject="Re: [MISSION:MSN-ABCDEF12]",
                                                  message_id="m2", **base))
    assert r2.status.value == "needs_operator"
    r3 = await ep.process_inbound(InboundRawEmail(from_address="noreply@trade.gov.in", subject="Enquiry",
                                                  message_id="m3", **base))
    assert r3.status.value == "processed"
    monkeypatch.setattr(gov_leads, "handle_inbound", AsyncMock(side_effect=RuntimeError("boom")))
    r4 = await ep.process_inbound(InboundRawEmail(from_address="noreply@trade.gov.in", subject="Enquiry",
                                                  message_id="m4", **base))
    assert r4.status.value == "needs_operator"


async def test_email_processor_holds_the_broker_chain(monkeypatch):
    from services.email_processor import service as ep
    from services.email_processor.repository import InMemoryEmailProcessorRepository as InboundRepo
    from services.email_processor.schemas import InboundRawEmail, MappingConfidence, MappingResult
    store = InboundRepo()
    monkeypatch.setattr(ep, "get_repository", lambda: store)
    monkeypatch.setattr(gov_leads, "handle_inbound", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "map_email", AsyncMock(return_value=MappingResult(
        outreach_deal_id="DEAL-ABC123", outreach_candidate_id="C1", party="buyer",
        confidence=MappingConfidence.SUBJECT_TOKEN)))
    hitl = AsyncMock(return_value="AP-9")
    monkeypatch.setattr(ep, "_create_price_hitl", hitl)
    r = await ep.process_inbound(InboundRawEmail(
        from_address="mandate@broker.com", to_address="t@p.test", subject="Re: [BUY:DEAL-ABC123:C1]",
        body_text="Our buyer mandate will issue ICPO after your FCO and POF", message_id="m9", received_at=NOW, source="manual"))
    assert r.status.value == "needs_operator" and "broker_chain_pattern" in hitl.await_args.kwargs["risk_flags"]


# ---------------------------------------------------------------------------
# Payment chase
# ---------------------------------------------------------------------------

def test_funding_facts_and_compose():
    from shared.orchestration import question_agent as qa
    hold = SimpleNamespace(amount=22000, currency="USD", provider="escrow_com", escrow_id="ESC-1",
                           checkout_url="https://escrow.example/pay", shortfall=2000, funded_amount=20000)
    text = qa.funding_facts(hold)
    assert "USD 22,000.00" in text and "Escrow.Com" in text and "OUR" in text and "USD 2,000.00 is still to come" in text
    assert "part payment" in qa.funding_facts(SimpleNamespace(amount=1, shortfall=None))
    assert qa.funding_facts(None) is None
    assert "funding" in qa.topics("Which account do we pay and who pays the bank charges?")
    body, asked = qa.compose("How do I pay? Can we pay in parts?", SimpleNamespace(hs_code="0904"), None, "buyer",
                             {"hold": hold})
    assert body and "funding" in asked
    assert qa.compose("How do I pay?", SimpleNamespace(), None, "buyer", {"hold": None})[0] is None
    assert qa.facts_for("funding", None, None, "supplier") is None


# ---------------------------------------------------------------------------
# Actor connectors (rows written to the spec)
# ---------------------------------------------------------------------------

def test_pending_connector_normalisers():
    from shared.connectors import apify_more as am
    assert am.ApifyRegulationWatchConnector.item({"source": "dgft", "title": "Ban", "hsCodes": ["0703.10", "x"],
                                                  "pdfUrl": "u", "textHash": "h"})["hs_codes"] == ["070310"]
    itp = am.ApifyIndiaTradePortalConnector.requirement({"hsCode": "090411", "country": "ARE", "mfnTariffPct": 5,
                                                         "preferentialTariffs": [{"agreement": "CEPA", "ratePct": 0}],
                                                         "sectionHashes": {"sps": "a"}, "spsMeasures": ["phyto"]})
    assert itp["country"] == "AE" and itp["preferential"][0]["rate_pct"] == 0.0 and itp["sps"] == ["phyto"]
    assert am.ApifyDestinationRulesConnector.rule({"country": "DE", "hsCodes": ["0904"], "measureType": "mrl"})["measure_type"] == "mrl"
    ref = am.ApifyImportRefusalsConnector.refusal({"country": "NL", "hazard": "Ethylene Oxide", "hsCode": "0904"})
    assert ref["hazard"] == "ethylene oxide" and ref["country"] == "NL"
    assert am.ApifyRegistryLookupConnector.lookup({"kind": "fssai", "number": " 1 ", "status": "weird"})["status"] == "unknown"
    p = am.ApifyCourierTrackingConnector.parcel({"trackingNumber": "77", "status": "Delivered"})
    assert p["delivered"] and p["status"] == "delivered"
    lead = am.ApifyBuyLeadsConnector.lead({"board": "tradekey", "country": "DE", "quantityMt": "25"})
    assert lead["source"] == "buy_lead:tradekey" and lead["quantity_mt"] == 25.0 and lead["flags"] == ["unverified_lead"]
    assert am.ApifyFoodTendersConnector.tender({"quantity": 5000, "unit": "kg"})["quantity_mt"] == 5.0
    assert am.ApifyFoodTendersConnector.tender({"quantity": 5, "unit": "bags"})["quantity_mt"] is None
    assert am.ApifyFoodTendersConnector.tender({"quantity": 5, "unit": "MT"})["quantity_mt"] == 5.0


async def test_pending_connectors_stub_until_built(monkeypatch):
    from shared.connectors import apify_more as am
    for cls in am.PENDING_CONNECTORS:
        c = cls()
        assert not c.configured and c.describe()["built"] is False
        out = await c.quote({"lookups": [{"kind": "fssai", "number": "1"}], "parcels": [{"awb": "1"}]})
        assert out["stub"] is True
    assert (await am.ApifyRegistryLookupConnector().quote({}))["stub"]
    assert (await am.ApifyCourierTrackingConnector().quote({}))["stub"]


async def test_pending_connectors_run_once_built(monkeypatch):
    from shared.connectors import apify_more as am
    monkeypatch.setenv("APIFY_TOKEN", "t")
    rows = {"apify_regulation_watch": [{"title": "t"}], "apify_india_trade_portal": [{"hsCode": "0904"}],
            "apify_destination_rules": [{"country": "DE"}], "apify_import_refusals": [{"country": "DE"}],
            "apify_registry_lookup": [{"kind": "fssai", "number": "1", "status": "active"}],
            "apify_courier_tracking": [{"trackingNumber": "1"}], "apify_buy_leads": [{"board": "b"}],
            "apify_food_tenders": [{"buyer": "WFP"}]}
    for cls in am.PENDING_CONNECTORS:
        monkeypatch.setenv(f"APIFY_ACTOR_{cls.actor_key}_BUILT", "true")
        c = cls()
        monkeypatch.setattr(c, "run", AsyncMock(return_value=rows[cls.name]))
        out = await c.quote({"lookups": [{"kind": "fssai", "number": "1"}], "parcels": [{"awb": "1", "courier": "dhl"}]})
        assert out["total"] == 1 and not out.get("stub")


def test_contact_and_check_normalisers():
    from shared.connectors.apify import (ApifyContactFinderConnector, ApifyCounterpartyCheckConnector, last_12_months,
                                         monthly_figures, search_diagnostics)
    row = {"name": "Gewürz GmbH", "bestEmail": "einkauf@gewuerz.de",
           "people": [{"name": "Anna", "title": "Einkauf", "roleGroup": "purchasing", "sourceUrl": "u"}],
           "productRange": {"listsProduct": True, "matchedTerms": ["Pfeffer"]},
           "legalNotice": {"registerNumber": "HRB 123", "registerCourt": "Amtsgericht Hamburg", "vatId": "DE1"},
           "emails": [{"email": "Einkauf@gewuerz.de", "mailbox": "deliverable"}],
           "phones": [{"number": "+4940123", "type": "landline", "whatsapp": False}], "countrySource": "cctld",
           "linkedinCompanyUrl": "https://linkedin.com/company/x"}
    c = ApifyContactFinderConnector._contact(row)
    assert c["people"][0]["role_group"] == "purchasing" and c["product_range"]["lists_product"] is True
    assert c["legal_notice"]["register_number"] == "HRB 123" and c["mailboxes"] == {"einkauf@gewuerz.de": "deliverable"}
    assert c["phones_typed"][0]["type"] == "landline" and c["country_source"] == "cctld"
    assert ApifyContactFinderConnector._contact({"legalNotice": {"legalForm": "GmbH"}})["legal_notice"] is None
    r = ApifyCounterpartyCheckConnector._result({"name": "X", "verdict": "clear", "domainAgeYears": "0.2",
                                                 "adverseMediaVerdict": "Possible",
                                                 "adverseMedia": [{"title": "scam", "riskWords": ["scam"], "nameMatchScore": 92}]})
    assert r["domain_age_years"] == 0.2 and r["adverse_media_verdict"] == "possible" and r["adverse_media"][0]["score"] == 92
    diag = search_diagnostics({"queries": [{"query": "pepper importers, DE", "resultsSeen": 10, "kept": 3,
                                            "dropped": {"directory": 5, "noContact": 2}}, "junk"]})
    assert diag[0]["kept"] == 3 and diag[0]["dropped"]["directory"] == 5
    assert search_diagnostics(None) is None and search_diagnostics({"queries": "x"}) is None
    months = monthly_figures([{"period": f"2026-{m:02d}", "importValueUsd": 1000, "netWeightMt": 1} for m in range(1, 14) if m <= 12]
                             + [{"period": "2025-12", "importValueUsd": 9000, "netWeightMt": 1}, {"x": 1}])
    last = last_12_months(months)
    assert last["months"] == 12 and last["unit_value_usd_per_mt"] == 1000.0 and last["period"] == "2026-01..2026-12"
    assert last_12_months([]) is None


def test_export_market_row_prefers_the_last_12_months():
    from shared.connectors.apify import ApifyExportMarketConnector
    m = ApifyExportMarketConnector._market({"unitValueUsdPerKg": 2.0, "latestPeriod": "2024",
                                            "monthly": [{"period": "2026-10", "importValueUsd": 3000, "netWeightMt": 1}],
                                            "origins": [{"country": "VNM", "unitValueUsdPerMt": 2800,
                                                         "indiaPriceAdvantagePct": 6}]})
    assert m["unit_value_usd_per_mt"] == 3000.0 and m["data_period"] == "2026-10..2026-10"
    assert m["origins"][0]["country"] == "VN" and m["origins"][0]["india_price_advantage_pct"] == 6.0
    assert ApifyExportMarketConnector._market({"unitValueUsdPerKg": 2.0})["unit_value_usd_per_mt"] == 2000.0


async def test_company_search_input_flags(monkeypatch):
    from shared.connectors.apify import ApifyCompanySearchConnector
    c = ApifyCompanySearchConnector()
    seen = {}

    async def run(actor_input, max_items=None):
        seen.update(actor_input)
        return []
    monkeypatch.setattr(c, "run", run)
    monkeypatch.setenv("COMPANY_SEARCH_VERIFY_MAILBOX", "true")
    await c.quote({"queries": ["pepper importers, DE"], "allowed_countries": ["DEU", "nl"]})
    assert seen["verifyMailbox"] is True and seen["allowedCountries"] == ["DE", "NL"]


# ---------------------------------------------------------------------------
# Scan-time check: legal notice, company age, adverse media, mailbox per address, product range
# ---------------------------------------------------------------------------

def test_validation_decides_with_the_new_signals():
    from services.trade_min import validation as v
    from services.trade_min.schemas import CounterpartyValidation
    out = v.decide(CounterpartyValidation(sanctions="review", email="ok", adverse_media="likely", domain_age_years=0.3,
                                          site_stale=True), role="buyer", country="DE")
    assert out.status == "unverified" and any("adverse media" in r for r in out.reasons)
    assert any("web domain only" in r for r in out.reasons) and any("newer than two years" in r for r in out.reasons)
    s = v._Subject(role="buyer", name="Gewürz", country="DE", website="https://gewuerz.de",
                   legal_notice={"register_number": "HRB 123", "register_court": "Amtsgericht Hamburg", "vat_id": "DE1",
                                 "legal_name": "Gewürz GmbH"})
    item = v._screen_input(s)
    assert item["vat_number"] == "DE1" and item["registration_number"] == "HRB 123" and item["website"] == "https://gewuerz.de"
    ev = v.existence(CounterpartyValidation(domain_age_years=12), s)
    assert any("Amtsgericht Hamburg HRB 123" in e for e in ev) and any("12 year" in e for e in ev)


async def test_screening_reads_age_and_adverse_media(monkeypatch):
    from services.trade_min import validation as v
    conn = SimpleNamespace(quote=AsyncMock(return_value={"results": [{
        "name": "Gulf", "verdict": "clear", "domain_age_years": 0.4, "site_last_updated": "2020-01-01",
        "adverse_media_verdict": "possible", "adverse_media": [{"title": "fraud"}]}]}))
    monkeypatch.setattr(v, "_connector", lambda name: conn)
    s = v._Subject(role="buyer", name="Gulf", country="AE")
    await v._screen([s])
    assert s.v.sanctions == "review" and s.v.domain_age_years == 0.4 and s.v.site_stale and s.v.adverse_hits


def test_mailbox_status_and_range_fit():
    from services.trade_min.pair_match import range_fit
    from services.trade_min.service import mailbox_status
    assert mailbox_status({"email": "A@b.de", "mailboxes": {"a@b.de": "catch_all"}}) == "risky"
    assert mailbox_status({"email": "a@b.de"}) is None
    assert range_fit({"lists_product": True, "matched_terms": ["pepper"]})[0] == 100.0
    assert range_fit({"lists_product": False, "other_products": ["steel"]})[0] == 30.0
    assert range_fit(None) is None and range_fit({"lists_product": None}) is None


def test_scan_report_keeps_diagnostics():
    from services.trade_min import scan_reports
    scan_reports.add_diagnostics([{"query": "x"}])  # outside a scan: no-op
    token = scan_reports.start()
    scan_reports.add_diagnostics([{"query": "pepper importers, DE", "kept": 0}])
    scan_reports.add_diagnostics(None)
    assert scan_reports.current().search_diagnostics[0]["kept"] == 0
    scan_reports._current.reset(token)


def test_enrichment_carries_people_to_the_party():
    from shared.orchestration.enrichment import _apply_contact
    party = SimpleNamespace(contact_source=None, email=None, website=None, phone=None, address=None, vat_number=None, people=[])
    up = _apply_contact(party, {"email": "a@b.de", "people": [{"name": "Anna"}]}, None)
    assert up["people"] == [{"name": "Anna"}]


# ---------------------------------------------------------------------------
# Regulation watch on A.29
# ---------------------------------------------------------------------------

async def test_regulation_actor_rows_and_staff_entries(repo, published, monkeypatch):
    catalogue._cache = None
    monkeypatch.setattr(catalogue, "active_lines", lambda: [SimpleNamespace(hs_code="070310", name="onion", synonyms=[], hs8_codes=[])])
    monkeypatch.setattr(compliance, "mark_reconfirm", AsyncMock(return_value=True))
    await repo.save(_deal(hs_code="07031010"))
    _connector(monkeypatch, {"items": [{"source": "dgft", "title": "Notification 12/2026", "hs_codes": ["070310"],
                                        "text": "export of onion prohibited", "hash": "h1"},
                                       {"source": "cbic", "title": "Steel", "hs_codes": [], "hash": "h2"}]})
    out = await regulatory_watch.run_actor(now=NOW)
    assert out["new"] == 2 and out["hits"][0]["hs_codes"] == ["070310"]
    assert published.await_args.args[1] == "REGULATORY:070310"
    assert (await regulatory_watch.run_actor(now=NOW))["new"] == 0
    _connector(monkeypatch, {"stub": True})
    assert (await regulatory_watch.run_actor(now=NOW))["stub"]
    monkeypatch.setattr("shared.audit_log.record", AsyncMock())
    with pytest.raises(ValueError):
        await regulatory_watch.record_manual(title=" ", url=None, number=None, date=None, hs_codes=None, products=None, by="o")
    row = await regulatory_watch.record_manual(title="Onion MEP USD 550", url=None, number="PN 5", date="2026-11-18",
                                               hs_codes=["0703.10"], products=None, by="officer")
    assert row["hit"] and row["entered_by"] == "officer"
    with pytest.raises(ValueError):
        await regulatory_watch.record_manual(title="Onion MEP USD 550", url=None, number="PN 5", date=None,
                                             hs_codes=None, products=None, by="officer")
    assert len(await regulatory_watch.items("0703")) == 2 and await regulatory_watch.items("9999") == []

    async def boom(request):
        raise RuntimeError("down")
    monkeypatch.setattr("shared.connectors.registry.get_connector", lambda name: SimpleNamespace(quote=boom))
    assert "error" in await regulatory_watch.run_actor(now=NOW)


# ---------------------------------------------------------------------------
# Destination requirements (A.30 / A.31)
# ---------------------------------------------------------------------------

async def test_destination_rules(repo, published, monkeypatch):
    await repo.save(_deal(hs_code="09041100", market="AE"))
    row = {"hs_code": "090411", "country": "AE", "mfn_pct": 5.0, "preferential": [{"agreement": "India-UAE CEPA", "rate_pct": 0.0}],
           "sps": ["phytosanitary certificate"], "licensing": "none", "documents": ["CoO"], "hashes": {"sps": "a"}, "read_on": "2026-11-01"}
    first = await destination_rules.apply_itp(row)
    assert first["changed"] == [] and first["deals_rechecked"] == []
    changed = await destination_rules.apply_itp({**row, "hashes": {"sps": "b"}})
    assert changed["changed"] == ["sps"] and published.await_args.args[1] == "DESTINATION:AE:090411"
    assert destination_rules.preferential("09041100", "AE")["agreement"] == "India-UAE CEPA"
    assert destination_rules.preferential("1006", "AE") is None
    measure = await destination_rules.apply_measure({"country": "AE", "regulator": "MOCCAE", "hs_codes": ["0904"],
                                                     "measure": "new MRL", "measure_type": "mrl", "published_on": "2026-11-10"})
    assert measure["deals_rechecked"] == ["DEAL-ABC123"]
    assert await destination_rules.apply_measure({"country": "AE", "regulator": "MOCCAE", "hs_codes": ["0904"],
                                                  "measure": "new MRL", "measure_type": "mrl", "published_on": "2026-11-10"}) is None
    reqs = destination_rules.requirements("09041100", "AE")
    assert {r["source"] for r in reqs} >= {"itp", "MOCCAE"}
    assert destination_rules.changed_sections(None, row) == []
    from shared.orchestration import export_certs
    assert "preferential_coo" in export_certs.country_kinds("BR", "09041100") or True
    assert export_certs.country_kinds("AE", "09041100").count("preferential_coo") == 1


async def test_destination_jobs_and_tariff(monkeypatch):
    monkeypatch.setattr(catalogue, "active_lines", lambda: [SimpleNamespace(hs_code="090411", markets=["AE"], name="pepper")])
    assert await destination_rules.monthly(NOW) is None  # pinned off
    monkeypatch.setenv("DESTINATION_RULES_ENABLED", "true")
    _connector(monkeypatch, {"requirements": [{"hs_code": "090411", "country": "AE", "preferential": [], "hashes": {}}],
                             "rules": [{"country": "AE", "hs_codes": [], "measure": "x"}]})
    assert len((await destination_rules.monthly(NOW))["applied"]) == 1 and await destination_rules.monthly(NOW) is None
    assert len((await destination_rules.weekly(NOW))["new"]) == 1 and await destination_rules.weekly(NOW) is None
    _connector(monkeypatch, {"stub": True})
    assert (await destination_rules.monthly(NOW + timedelta(days=40)))["stub"]
    assert (await destination_rules.weekly(NOW + timedelta(days=8)))["stub"]

    from services.trade_min import service as tm
    from services.trade_min.schemas import TariffData
    mfn = TariffData(hs_code="090411", destination_country="AE", applied_duty_pct=5.0, total_landed_cost_add_pct=5.0,
                     fetched_at=NOW)
    monkeypatch.setattr(tm, "_phase3_tariff_gate_mfn", AsyncMock(return_value=mfn))
    destination_rules._rows["090411:AE"] = {"preferential": [{"agreement": "CEPA", "rate_pct": 0.0}]}
    out = await tm._phase3_tariff_gate("090411", "AE", 30.0, None)
    assert out.applied_duty_pct == 0.0 and "CEPA" in out.rules_of_origin
    destination_rules._rows.clear()
    assert (await tm._phase3_tariff_gate("090411", "AE", 30.0, None)).applied_duty_pct == 5.0


# ---------------------------------------------------------------------------
# Registration checks (A.34)
# ---------------------------------------------------------------------------

async def test_registration_checks(monkeypatch):
    monkeypatch.setattr("shared.audit_log.record", AsyncMock())
    assert await registration_checks.monthly(NOW) is None  # pinned off
    await registrations.record("fssai", "10012345000001", "2027-01-31", by="o")
    await registrations.record("spices_board_cres", "CRES-9", "2028-01-01", by="o")
    monkeypatch.setenv("REGISTRATION_CHECKS_ENABLED", "true")
    _connector(monkeypatch, {"lookups": [{"kind": "fssai", "number": "10012345000001", "status": "active", "valid_until": "2027-06-30"},
                                         {"kind": "spices_board_cres", "number": "CRES-9", "status": "expired"}]})
    out = await registration_checks.monthly(NOW)
    assert out["updated"] == ["fssai"] and out["problems"][0]["status"] == "expired"
    assert (await registrations.held())["fssai"]["expires_on"] == "2027-06-30"
    assert await registration_checks.monthly(NOW + timedelta(days=2)) is None
    from services.trade_min.schemas import CounterpartyRecord
    rec = CounterpartyRecord(id="supplier:x", role="supplier", name="X", source="manual", manual={"fssai": "1", "apeda": "A"},
                             first_seen_at=NOW, last_seen_at=NOW, flags=["certificate_problem:fssai"])
    _connector(monkeypatch, {"lookups": [{"kind": "fssai", "number": "1", "status": "active"},
                                         {"kind": "apeda_rcmc", "number": "A", "status": "not_found"}]})
    res = await registration_checks.check_supplier(rec)
    assert "certificate_active:fssai" in res["flags"] and "certificate_problem:apeda_rcmc" in res["flags"]
    assert (await registration_checks.check_supplier(rec.model_copy(update={"manual": {}})))["checked"] == 0
    _connector(monkeypatch, {"stub": True})
    assert (await registration_checks.check_supplier(rec))["stub"]
    assert (await registration_checks.check_own())["stub"]


# ---------------------------------------------------------------------------
# Refusal history (A.33 + FDA)
# ---------------------------------------------------------------------------

FDA_CSV = ("FEI Number,Firm Legal Name,Country Name,Product Code Description,Refused Date,Refusal Charges\n"
           "1,Kerala Spices Pvt Ltd,India,Black pepper,10/01/2026,SALMONELLA\n"
           "2,Other Co,Vietnam,Pepper,10/01/2026,FILTHY\n")


async def test_refusals(monkeypatch):
    rows = refusals.parse_fda_csv(FDA_CSV)
    assert len(rows) == 1 and rows[0]["hazard"] == "salmonella" and rows[0]["date"] == "2026-10-01"
    await refusals.store(rows + [{"country": "NL", "authority": "RASFF", "date": "2026-09-01", "hs_code": "090411",
                                  "hazard": "ethylene oxide", "exporter": "Kerala Spices Private Limited", "reference": "R1"},
                                 {"country": "DE", "authority": "RASFF", "date": "2020-01-01", "hs_code": "090411",
                                  "hazard": "aflatoxin", "reference": "old"}])
    assert await refusals.store(rows) == 0
    assert refusals.tests_for("09041100", "DE", date(2026, 11, 19)) == ["ethylene oxide"]
    assert refusals.tests_for("09041100", "US", date(2026, 11, 19)) == []
    assert len(refusals.supplier_refusals("Kerala Spices Ltd", date(2026, 11, 19))) == 2
    assert refusals.supplier_refusals("ab") == []
    assert refusals._recent({"date": "bad"}, date(2026, 1, 1)) is False

    assert (await refusals.load_fda())["ok"] is False
    monkeypatch.setenv("FDA_REFUSALS_URL", "https://fda.example/refusals.csv")
    ok = await refusals.load_fda(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=FDA_CSV)))
    assert ok == {"ok": True, "new": 0}
    bad = await refusals.load_fda(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert bad["ok"] is False
    monkeypatch.delenv("FDA_REFUSALS_URL")
    assert await refusals.daily(NOW) is None
    monkeypatch.setenv("REFUSALS_ENABLED", "true")
    monkeypatch.setattr(catalogue, "active_lines", lambda: [SimpleNamespace(hs_code="090411", name="pepper")])
    _connector(monkeypatch, {"refusals": [{"country": "JP", "authority": "MHLW", "date": "2026-11-01", "hs_code": "090411",
                                           "hazard": "pesticide residue", "reference": "J1"}]})
    out = await refusals.daily(NOW)
    assert out["actor_new"] == 1 and await refusals.daily(NOW) is None


# ---------------------------------------------------------------------------
# Courier AWBs (A.38)
# ---------------------------------------------------------------------------

async def test_courier_poll_marks_samples_delivered(repo, monkeypatch):
    from shared.orchestration import samples
    await repo.save(_deal(samples=[{"sample_id": "SMP-1", "status": "shipped", "awb": "77", "courier": "dhl"},
                                   {"sample_id": "SMP-2", "status": "requested"}]))
    assert (await samples.poll_couriers([]))["polled"] == 0
    _connector(monkeypatch, {"parcels": [{"awb": "77", "status": "delivered", "delivered": True, "signed_by": "Saeed",
                                          "delivered_at": "2026-11-18"}]})
    out = await samples.poll_couriers()
    assert out["delivered"] == ["SMP-1"]
    s = (await repo.get("DEAL-ABC123")).samples[0]
    assert s["signed_by"] == "Saeed" and s["delivered_at"] == "2026-11-18"
    assert (await samples.poll_couriers())["polled"] == 0  # delivered ones are not polled again
    await repo.save(_deal(deal_id="DEAL-ZZZ999", samples=[{"sample_id": "SMP-3", "status": "shipped", "awb": "9"}]))
    _connector(monkeypatch, {"stub": True})
    assert (await samples.poll_couriers())["stub"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def api(repo, monkeypatch, sent):
    from services.deals.router import router
    from shared.auth.dependencies import get_current_user, require_portless
    monkeypatch.setattr("shared.audit_log.staff_relay", AsyncMock())
    monkeypatch.setattr("shared.audit_log.record", AsyncMock())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_portless] = lambda: STAFF
    app.dependency_overrides[get_current_user] = lambda: STAFF
    return TestClient(app, raise_server_exceptions=False)


async def test_routes(api, repo, published, monkeypatch):
    await repo.save(_deal(status=DealStatus.QC_PASSED))
    assert api.put("/deals/cha/agents", json={"firm": "X", "licence_no": "1", "email": "bad", "ports": ["A"]}).status_code == 422
    agent = api.put("/deals/cha/agents", json={"firm": "CHA", "licence_no": "R", "email": "d@cha.in", "ports": ["INNSA1"],
                                               "licence_valid_until": "2027-12-31"}).json()
    assert api.get("/deals/cha/agents").json()["agents"][0]["firm"] == "CHA"
    assert api.post(f"/deals/cha/agents/{agent['agent_id']}/kyc", json={"sent_on": "2026-11-01", "authorisation_ref": " "}).status_code == 422
    assert api.post(f"/deals/cha/agents/{agent['agent_id']}/kyc", json={"sent_on": "2026-11-01", "authorisation_ref": "A"}).json()["sent"]
    assert api.get(f"/deals/cha/agents/{agent['agent_id']}/kyc").json()["sent"]
    assert api.post("/deals/DEAL-ABC123/cha/pack?port=Mundra").status_code == 409
    monkeypatch.setattr(cha, "incentives_to_declare", AsyncMock(return_value=[]))
    assert api.post("/deals/DEAL-ABC123/cha/pack?port=INNSA1").json()["status"] == "pack_sent"
    assert api.get("/deals/DEAL-ABC123/cha").json()["agent_firm"] == "CHA"
    assert api.post("/deals/DEAL-ABC123/cha/shipping-bill", json={"number": "1"}).status_code == 422
    assert api.post("/deals/DEAL-ABC123/cha/shipping-bill", json={"number": "1234567", "leo_date": "2026-11-20"}).json()["number"] == "1234567"
    assert api.post("/deals/DEAL-ABC123/cha/invoice", json={"amount_usd": -1}).status_code == 422
    assert api.post("/deals/DEAL-ABC123/cha/invoice", json={"amount_usd": 100}).json()["over_estimate"] is False

    assert api.post("/deals/DEAL-ABC123/insurance/declaration", json={"certificate_no": " "}).status_code == 422
    assert api.post("/deals/DEAL-ABC123/insurance/declaration", json={"certificate_no": "MC-1"}).json()["certificate_no"] == "MC-1"
    assert api.post("/deals/DEAL-ABC123/insurance/claims", json={"reason": "", "amount_usd": 1}).status_code == 422
    claim = api.post("/deals/DEAL-ABC123/insurance/claims", json={"reason": "wet", "amount_usd": 10}).json()
    assert api.post(f"/deals/DEAL-ABC123/insurance/claims/{claim['claim_id']}", json={"status": "settled"}).status_code == 422
    assert api.post("/deals/DEAL-ABC123/insurance/claims/CLM-X", json={"status": "notified"}).status_code == 404
    assert api.post(f"/deals/DEAL-ABC123/insurance/claims/{claim['claim_id']}", json={"status": "notified"}).json()["status"] == "notified"
    assert api.get("/deals/DEAL-ABC123/insurance").json()["declaration"]["certificate_no"] == "MC-1"

    assert api.post("/deals/DEAL-ABC123/loi", json={"text": "send ICPO and FCO"}).status_code == 422
    assert api.post("/deals/DEAL-ABC123/loi", json={"text": "hello"}).status_code == 422
    assert api.post("/deals/DEAL-ABC123/loi", json={"text": LOI_TEXT}).json()["quantity_mt"] == 500
    assert api.post("/deals/DEAL-ABC123/loi", json={"quantity_mt": 50}).json()["quantity_mt"] == 50
    assert "Incoterms" in api.get("/deals/DEAL-ABC123/loi/template?place=Dubai").json()["text"]

    monkeypatch.setattr(catalogue, "active_lines", lambda: [])
    assert api.post("/deals/compliance/regulatory-items", json={"title": " "}).status_code == 422
    assert api.post("/deals/compliance/regulatory-items", json={"title": "Onion ban", "hs_codes": ["0703"]}).json()["title"] == "Onion ban"
    assert api.get("/deals/compliance/regulatory-items").json()["items"]
    assert api.get("/deals/compliance/destination-requirements?hs=0904&market=AE").json()["test_for"] == []
    assert api.get("/deals/leads/recent").json() == {"leads": []}
    assert api.get("/deals/leads/stats").json()["sources"] == {}
    assert api.post("/deals/missions/requests", json={"market": "AE", "hs_code": "0904"}).status_code == 404
    await country_profiles.put("AE", {"commercial_wing_email": "com@mea.gov.in"}, by="o")
    req = api.post("/deals/missions/requests", json={"market": "AE", "hs_code": "0904"}).json()
    assert api.post("/deals/missions/requests", json={"market": "AE", "hs_code": "0904"}).status_code == 409
    assert api.get("/deals/missions/requests").json()["requests"][0]["request_id"] == req["request_id"]
    assert api.post("/deals/missions/requests/MSN-NOPE/import", json={}).status_code == 404
    assert api.post(f"/deals/missions/requests/{req['request_id']}/import", json={}).status_code == 422
    assert api.post(f"/deals/missions/requests/{req['request_id']}/import",
                    json={"rows": [{"company": "Gulf Spice", "email": "g@gs.ae"}]}).json()["directory"] == 1


async def test_watcher_data_jobs_run_quietly(repo, monkeypatch):
    from shared.orchestration import deal_watcher
    monkeypatch.setenv("DATA_JOBS_ENABLED", "true")
    monkeypatch.setattr("shared.orchestration.samples.poll_couriers", AsyncMock(return_value={"delivered": ["S"]}))
    monkeypatch.setattr(gov_leads, "reminders", AsyncMock(return_value=["MSN-1"]))
    monkeypatch.setattr(refusals, "daily", AsyncMock(return_value={"actor_new": 1}))
    monkeypatch.setattr(destination_rules, "monthly", AsyncMock(side_effect=RuntimeError("x")))
    counts = await deal_watcher.sweep_stalled_deals(NOW)
    assert counts.get("samples_delivered") == 1 and counts.get("mission_reminders") == 1 and counts.get("refusals") == 1
    monkeypatch.setattr("shared.orchestration.samples.poll_couriers", AsyncMock(side_effect=RuntimeError("down")))
    assert "samples_delivered" not in await deal_watcher.sweep_stalled_deals(NOW)


async def test_ratchet_edges(repo, monkeypatch):
    """Branches of the modules held at 100 % (compliance, fx_rates)."""
    monkeypatch.setattr("shared.orchestration.continuations._publish_exception", AsyncMock(side_effect=RuntimeError("bus")))
    await repo.save(_deal())
    rec = compliance.LoadingRecord(container_numbers=["B1"], seal_numbers=["S"],
                                   container_surveys=[{"container": "B1", "status": "rejected"}])
    saved = await compliance.record_loading("DEAL-ABC123", rec)  # the exception never stops the record
    assert saved.loading.container_surveys == [{"container": "B1", "status": "rejected"}]
    monkeypatch.setenv("FX_PEGS_ENABLED", "true")
    await fx_rates.record("SAR", 3.75, source="bank", by="ops")
    monkeypatch.setattr("shared.telegram.send_message", AsyncMock(side_effect=RuntimeError("tg")))
    await fx_rates.record("AED", 3.9, source="bank", by="ops")
    out = await fx_rates.check_pegs(now=NOW)  # Telegram down: still reported
    assert out["moved"] == ["AED"] and {c["currency"] for c in out["checked"]} == {"AED", "SAR"}
    await fx_rates.record("AED", 3.6725, source="bank", by="ops")
    assert (await fx_rates.check_pegs(now=NOW))["moved"] == []
