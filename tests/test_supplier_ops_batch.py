"""2026-11-19 batch 3: supplier relays with evidence, onboarding documents + bank check, container
plan, freight quotes per box type, agreed spec in documents, export policy per exact product,
site facts + certificates, Census anchor, CN8 codes, company-led opportunities, price discovery,
reorder agent, document checking, claim options, outreach A/B, personal data erase / retention."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation
from shared import catalogue

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
_REAL_CLIENT = httpx.AsyncClient


def _rec(**kw) -> CounterpartyRecord:
    base = dict(id="supplier:nashik.in", role="supplier", name="Nashik Agro Exports", country="IN",
                email="sales@nashik.in", website="https://nashik.in", source="apify_company_search",
                sources=["apify_company_search"], hs_codes=["070310"], first_seen_at=NOW, last_seen_at=NOW)
    base.update(kw)
    return CounterpartyRecord(**base)


@pytest.fixture(autouse=True)
async def _fresh():
    directory.reset_directory()
    await catalogue.set_catalogue([{
        "product_id": "onion", "hs_code": "070310", "name": "Onion", "markets": ["AE"], "hs8_codes": ["07031010"],
        "form": "fresh", "container_mt": 26.0,
        "attributes": [{"key": "variety", "values": ["red", "pink"]}, {"key": "size", "values": ["55 mm+", "40-60 mm"]}],
    }])
    yield
    directory.reset_directory()
    catalogue.reset_for_tests()


# ---------------------------------------------------------------------------
# Relays with evidence, onboarding documents, bank check
# ---------------------------------------------------------------------------

def test_evidence_required_switch(monkeypatch):
    from shared.orchestration.relay_evidence import Evidence, check, stamp
    check(None)  # optional by default
    monkeypatch.setenv("RELAY_EVIDENCE_REQUIRED", "true")
    with pytest.raises(ValueError):
        check(None)
    with pytest.raises(ValueError):
        check(Evidence(note="short"))
    check(Evidence(kind="photo", ref="drive://signed-po.jpg"))
    assert stamp(Evidence(kind="whatsapp", note="supplier confirmed 2 x 20ft"))["at"]


async def test_supplier_steps_show_relays_and_evidence():
    from shared import audit_log
    from shared.orchestration.relay_evidence import supplier_steps
    audit_log.set_collection_for_tests(None)
    await audit_log.record("supplier.po_ack_relayed", "ops@x", "D-RELAY",
                           {"reason": "phone", "evidence": {"kind": "photo", "ref": "drive://po.jpg"}})
    await audit_log.record("supplier.ready_date_relayed", "ops@x", "D-RELAY", {"request": {}})
    deal = SimpleNamespace(deal_id="D-RELAY", supplier_offers=[SimpleNamespace(source="quote")], parties=[])
    with patch("shared.orchestration.supplier_commitment.get", new=AsyncMock(return_value=None)):
        out = await supplier_steps(deal)
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["quote"]["how"] == "e-mail"
    assert steps["po_acknowledgement"]["how"] == "staff relay" and steps["po_acknowledgement"]["evidence"]["ref"] == "drive://po.jpg"
    assert steps["goods_ready_date"]["evidence_missing"] is True
    assert steps["bank_details"]["done"] is False


async def test_onboarding_documents_and_bank_verification(monkeypatch, tmp_path):
    from services.trade_min import supplier_onboarding as so
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    store = await directory.get_directory()
    await store.upsert(_rec())
    with patch("shared.audit_log.record", new=AsyncMock()):
        doc = await so.add_document("nashik.in", kind="cancelled_cheque", filename="cheque.pdf", by="ops@x",
                                    content_b64=base64.b64encode(b"%PDF cheque").decode())
        assert doc["storage"] == "local" and len(doc["sha256"]) == 64
        with pytest.raises(ValueError):
            await so.add_document("nashik.in", kind="passport", filename="x", by="ops@x", url="https://x")
        gst = await so.add_document("nashik.in", kind="gst_certificate", filename="gst.pdf", by="ops@x", url="https://d/x")
        with pytest.raises(ValueError):
            await so.verify_bank("nashik.in", doc_id=gst["doc_id"], by="ops@x")
        out = await so.verify_bank("nashik.in", doc_id=doc["doc_id"], by="ops@x")
    assert out["bank_verified_by"] == "ops@x"
    deal = SimpleNamespace(parties=[SimpleNamespace(role="supplier", legal_name="Nashik Agro Exports",
                                                    website="https://nashik.in", email="sales@nashik.in")])
    assert await so.bank_verified_for_deal(deal) is True
    assert await so.bank_verified_for_deal(SimpleNamespace(parties=[])) is None


# ---------------------------------------------------------------------------
# Container plan, freight quotes per box type
# ---------------------------------------------------------------------------

def test_box_plan_from_the_catalogue():
    from services.shield_link.service import _box_plan
    req = SimpleNamespace(container_numbers=[], container_type=None)
    plan = _box_plan(req, "070310", 52.0)  # 26 MT per 20' → two 20'
    assert plan["containers"] == 2 and plan["container_type"] == "22GP"
    assert _box_plan(SimpleNamespace(container_numbers=["MSCU1234567"], container_type=None), "070310", 52.0) == {"containers": 1}


def test_cheapest_plan_per_tonne():
    from services.shield_link.freight_compare import cheapest_plan
    quotes = [{"provider": "maersk", "equipment": "20DRY", "rate_usd": 1500.0},
              {"provider": "maersk", "equipment": "40HC", "rate_usd": 1400.0},
              {"provider": "cogoport", "equipment": "20DRY", "rate_usd": 2000.0},
              {"provider": "cogoport", "equipment": "LCL", "rate_usd": 900.0, "stub": True}]
    best = cheapest_plan(quotes, 50.0, None)
    assert best["provider"] == "maersk" and best["boxes"] == {"40HC": 2} and best["usd"] == 2800.0
    small = cheapest_plan([{"provider": "cogoport", "equipment": "20DRY", "rate_usd": 1400.0},
                           {"provider": "cogoport", "equipment": "LCL", "rate_usd": 600.0}], 8.0, None)
    assert small["boxes"] == {"LCL": 1}


async def test_compare_asks_each_box_type_and_lcl(monkeypatch):
    from services.shield_link import freight_compare as fc
    monkeypatch.setenv("COGOPORT_API_KEY", "real")
    monkeypatch.delenv("MAERSK_CONSUMER_KEY", raising=False)
    monkeypatch.delenv("FREIGHTOS_API_KEY", raising=False)
    asked = []

    async def one(provider, request):
        asked.append(request["container_type"])
        rate = {"20DRY": 1400.0, "40DRY": 1800.0, "40HC": 1850.0, "LCL": 500.0}[request["container_type"]]
        return {"provider": provider, "rate_usd": rate, "transit_days": 20}
    monkeypatch.setattr(fc, "_one", one)
    out = await fc.compare(origin="Nhava Sheva", destination="Jebel Ali", market=None, hs_code="070310", quantity_mt=10)
    assert set(asked) == {"20DRY", "40DRY", "40HC", "LCL"} and out["plan"]["boxes"] == {"LCL": 1}


# ---------------------------------------------------------------------------
# Agreed spec in documents, export policy per exact product
# ---------------------------------------------------------------------------

def _deal_with_spec():
    req = SimpleNamespace(grade="A grade", spec={"variety": "red", "size": ["55 mm+", "40-60 mm"]})
    cand = SimpleNamespace(candidate_id="C1", requirement=req, status="selected")
    offer = SimpleNamespace(offer_id="O1", spec={"size": "55 mm+"})
    return SimpleNamespace(hs_code="070310", selected_pair={"candidate_id": "C1", "offer_id": "O1"},
                           buyer_candidates=[cand], supplier_offers=[offer])


def test_agreed_spec_reaches_the_document_description():
    from services.dealmaker.service import _hs_description
    from shared.product_spec import agreed_spec, spec_text
    deal = _deal_with_spec()
    assert agreed_spec(deal) == {"grade": "A grade", "variety": "red", "size": "55 mm+"}
    assert spec_text(deal) == "A grade · red · 55 mm+"
    assert _hs_description("070310", deal).endswith("— A grade · red · 55 mm+")
    assert "—" not in _hs_description("070310", SimpleNamespace(selected_pair=None, buyer_candidates=[], supplier_offers=[]))


def test_inspector_and_carrier_notes_carry_the_spec():
    from shared.orchestration.trade_brief import note
    b = {"deal_id": "D", "desk": "x", "spec": "red · 55 mm+"}
    assert "specification: red · 55 mm+" in note(b, "inspector") and "specification" in note(b, "carrier")
    assert "specification" not in note(b, "escrow")


async def test_export_policy_reads_the_exact_code_and_form():
    from shared.orchestration import compliance
    assert compliance.exact_product("070310") == ("07031010", "fresh")
    f = await compliance.check_hs("070310")
    assert f.data["hs_code"] == "07031010" and f.data["form"] == "fresh" and "fresh onions" in f.message
    assert compliance.exact_product("100630") == ("100630", None)


# ---------------------------------------------------------------------------
# Site facts and certificates
# ---------------------------------------------------------------------------

def test_site_facts_and_certificates():
    from services.trade_min.site_facts import certificates, facts_from_text, product_facts_json
    text = "Exporters of red onion 55 mm+ — FSSAI licensed, APEDA RCMC holder, ISO 22000 and HACCP certified"
    assert certificates(text) == ["FSSAI", "APEDA RCMC", "ISO 22000", "HACCP"]
    assert facts_from_text("070310", text)["variety"] == "red"
    pf = product_facts_json("070310", text, ref="https://x.in")
    assert pf["070310"]["facts"]["certificates"]["source"] == "website"
    assert product_facts_json("070310", "nothing useful") == {}


def test_certificates_raise_supplier_trust():
    from services.trade_min.relationships import score_of
    from services.trade_min.site_facts import product_facts_json
    plain = _rec()
    certified = _rec(product_facts=product_facts_json("070310", "FSSAI and HACCP certified"))
    s0, _ = score_of(plain, {}, None)
    s1, basis = score_of(certified, {}, None)
    assert s1 - s0 == 4 and "2 certificate(s) (+4)" in basis


async def test_scan_record_keeps_site_facts():
    from services.trade_min.schemas import SellerCandidate
    s = SellerCandidate(candidate_id="x", company_name="Acme Agro", location_city="", location_state="",
                        hs_code="070310", source="apify_company_search", website="https://acme.in",
                        fit_reason="We pack pink onion, Halal certified")
    rec = directory._supplier_record(s, "070310", "AE", "", None, NOW)
    facts = rec.product_facts["070310"]["facts"]
    assert facts["variety"]["value"] == "pink" and facts["certificates"]["value"] == ["Halal"]


# ---------------------------------------------------------------------------
# Census anchor, CN8 codes
# ---------------------------------------------------------------------------

async def test_census_monthly_unit_values(monkeypatch):
    from shared.connectors import trade_intel
    monkeypatch.setenv("CENSUS_API_KEY", "k")
    rows = [["CTY_CODE", "CTY_NAME", "GEN_VAL_MO", "GEN_CIF_MO", "GEN_QY1_MO", "UNIT_QY1", "I_COMMODITY"],
            ["-", "TOTAL FOR ALL COUNTRIES", "300000", "320000", "200000", "KG", "070310"],
            ["5330", "INDIA", "80000", "85000", "100000", "KG", "070310"]]
    with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows)))):
        out = await trade_intel.CensusTradeConnector().quote({"hs_code": "070310", "market": "US", "time": "2026-08"})
    assert out["unit_value_usd_per_mt"] == 1500.0 and out["india_unit_value_usd_per_mt"] == 800.0
    assert out["data_period"] == "2026-08" and out["source"] == "census"


async def test_hmrc_queries_the_exact_cn8_codes(monkeypatch):
    from shared.connectors import national_trade as nt
    monkeypatch.setenv("NATIONAL_TRADE_SOURCES_ENABLED", "true")
    seen = {}

    def handler(request):
        seen["filter"] = request.url.params.get("$filter")
        return httpx.Response(200, json={"value": []})
    out = await nt.UkHmrcTradeConnector(transport=httpx.MockTransport(handler)).quote(
        {"hs_code": "070310", "hs8_codes": ["07031010", "08031010"]})
    assert "(CommodityId eq 7031010)" in seen["filter"] and out["cn8_codes"] == ["07031010"]


def test_found_by_census_is_national():
    from services.trade_min import service as svc
    assert svc.found_by_for("census:india") == "national_statistics"
    assert svc._hs8_for("070310") == ["07031010"]


# ---------------------------------------------------------------------------
# Company-led opportunities
# ---------------------------------------------------------------------------

async def test_company_led_opportunity_from_the_directory():
    from services.trade_min import company_led
    from services.trade_min.repository import get_repository, repository
    repository.clear()
    store = await directory.get_directory()
    for i in range(3):
        await store.upsert(_rec(id=f"buyer:b{i}", role="buyer", name=f"Gulf Buyer {i}", country="AE",
                                email=f"buy@b{i}.ae", website=f"https://b{i}.ae",
                                validation=CounterpartyValidation(status="verified")))
    await store.upsert(_rec())
    await store.upsert(_rec(id="supplier:two.in", name="Second Agro", email="s@two.in", website="https://two.in"))
    groups = await company_led.preview()
    assert groups and groups[0]["market"] == "AE" and len(groups[0]["buyers"]) == 3
    out = await company_led.create(now=NOW)
    assert out["created"][0]["buyers"] == 3 and out["created"][0]["suppliers"] == 2
    opp = await (await get_repository()).get(out["created"][0]["opportunity_id"])
    assert opp.found_by == "directory" and opp.status == "MARGINAL"
    again = await company_led.create(now=NOW)
    assert again["created"] == [] and again["skipped"]
    repository.clear()


def test_price_discovery_switch(monkeypatch):
    from shared.orchestration import matchmaking
    assert matchmaking.price_discovery_all_buyers() is True
    monkeypatch.setenv("PRICE_DISCOVERY_ALL_BUYERS", "false")
    assert matchmaking.price_discovery_all_buyers() is False


# ---------------------------------------------------------------------------
# Reorder agent, document check, claims
# ---------------------------------------------------------------------------

def test_repeat_price():
    from shared.orchestration.reorder_agent import repeat_price
    assert repeat_price(500.0, 300.0, 320.0, 400.0) == 520.0
    assert repeat_price(500.0, 300.0, 200.0, 450.0) == 450.0  # never below the minimum offer
    assert repeat_price(None, None, None, None) is None


async def test_offer_repeat_opens_a_priced_gate():
    from shared.orchestration import reorder_agent
    old = SimpleNamespace(deal_id="D-OLD", hs_code="070310", market="AE", quantity_mt=26.0, margin_floor_pct=8.0,
                          buyer_target_price_usd=None,
                          parties=[SimpleNamespace(role="buyer", legal_name="Gulf Foods", email="buy@gulf.ae", country="AE")])
    new = SimpleNamespace(deal_id="D-NEW", buyer_candidates=[])
    with_cand = SimpleNamespace(deal_id="D-NEW", buyer_candidates=[SimpleNamespace(candidate_id="REORDER-D-OLD",
                                                                                     legal_name="Gulf Foods", contact_name=None)])
    gate = AsyncMock(return_value="APR-9")
    repo = SimpleNamespace(set_fields=AsyncMock(return_value=True))
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=old)), \
            patch("shared.orchestration.reorder_agent._prices", new=AsyncMock(
                return_value={"buyer": 500.0, "supplier_then": 300.0, "supplier_now": 310.0})), \
            patch("shared.orchestration.reorders.reorder", new=AsyncMock(return_value=new)), \
            patch("services.deals.service.add_buyer_candidate", new=AsyncMock(return_value=with_cand)), \
            patch("shared.orchestration.matchmaking._create_gate", new=gate), \
            patch("services.deals.repository.get_repository", return_value=repo):
        out = await reorder_agent.offer_repeat("D-OLD")
    assert out["offered"] and out["price_low_usd_per_mt"] >= 510.0 and out["approval_id"] == "APR-9"
    payload = gate.await_args.kwargs["payload"]
    assert payload["to_address"] == "buy@gulf.ae" and "your next order" in payload["subject"]


def test_document_check_text():
    from shared.orchestration.document_check import check_text
    facts = {"hs_code": "070310", "product": "Onion", "loaded_quantity_mt": 52.0, "container_numbers": ["MSCU1234567"],
             "seal_numbers": ["SL998"], "buyer": SimpleNamespace(legal_name="Gulf Foods Trading LLC"),
             "supplier": SimpleNamespace(legal_name="Nashik Agro Exports"), "port_of_loading": "Nhava Sheva",
             "port_of_destination": "Jebel Ali", "unit_price": 400.0}
    good = ("COMMERCIAL INVOICE  Consignee: Gulf Foods Trading LLC  HS 0703.10  Fresh onion 52.0 MT  "
            "Container MSCU 123456 7 seal SL998  Nhava Sheva to Jebel Ali  Total USD 20,800.00")
    res = {f["field"]: f["status"] for f in check_text("commercial_invoice", good, facts)}
    assert set(res.values()) == {"ok"}, res
    bad = good.replace("52.0 MT", "48.0 MT").replace("MSCU 123456 7", "TGHU 765432 1") + " Shipper: Nashik Agro Exports"
    res = {f["field"]: f["status"] for f in check_text("commercial_invoice", bad, facts)}
    assert res["quantity_mt"] == "mismatch" and res["containers"] == "mismatch" and res["supplier_named"] == "mismatch"


def test_claim_options_ranked():
    from shared.orchestration.claims import options
    rows = options(sale_value_usd=20000, purchase_value_usd=16000, margin_floor_pct=8, inspection_fee_usd=300,
                   reinspections_left=1, affected_share=0.5)
    names = [r["option"] for r in rows]
    assert names[0] in ("reinspection", "discount", "replacement_lot") and "partial_refund" in names
    disc = next(r for r in rows if r["option"] == "discount")
    assert disc["discount_usd"] == 1000.0 and disc["supplier_cost_usd"] == 1000.0
    none_left = options(sale_value_usd=20000, purchase_value_usd=19000, margin_floor_pct=8, inspection_fee_usd=300,
                        reinspections_left=0)
    assert "reinspection" not in [r["option"] for r in none_left] and "discount" not in [r["option"] for r in none_left]


# ---------------------------------------------------------------------------
# Outreach A/B
# ---------------------------------------------------------------------------

def test_outreach_variants(monkeypatch):
    from shared.orchestration import outreach_ab as ab
    monkeypatch.setenv("OUTREACH_AB_MIN_SENDS", "2")
    deal = SimpleNamespace(hs_code="070310", market="AE", buyer_candidates=[
        SimpleNamespace(outreach_variant="a", contacted_at=NOW, replied_at=None, status="contacted"),
        SimpleNamespace(outreach_variant="a", contacted_at=NOW, replied_at=None, status="contacted"),
        SimpleNamespace(outreach_variant="b", contacted_at=NOW, replied_at=NOW, status="interested"),
        SimpleNamespace(outreach_variant="b", contacted_at=NOW, replied_at=None, status="contacted"),
    ])
    st = ab.stats([deal])["070310:AE"]
    assert st["a"]["reply_rate"] == 0.0 and st["b"]["reply_rate"] == 0.5
    assert ab.choose("anyone", st) == "b"
    assert ab.choose("c1", {}) in ("a", "b")


def test_variant_b_opener_in_the_introduction():
    from shared.orchestration import outreach
    deal = SimpleNamespace(deal_id="D1", hs_code="070310", quantity_mt=None, market="AE")
    cand = SimpleNamespace(candidate_id="C1", legal_name="Gulf", pitch_angle=None, quantity_mt=None, requirement=None,
                           contact_name=None, email="b@g.ae", outreach_variant=None)
    _, body_b = outreach.draft(deal, cand, variant="b")
    _, body_a = outreach.draft(deal, cand, variant="a")
    assert "neutral escrow account, and the goods are inspected" in body_b and "your company imports" in body_a


# ---------------------------------------------------------------------------
# Personal data
# ---------------------------------------------------------------------------

async def test_erase_on_request_keeps_statutory_records():
    from services.trade_min import personal_data
    store = await directory.get_directory()
    await store.upsert(_rec(id="buyer:gulf.ae", role="buyer", name="Gulf Foods", country="AE", email="ali@gulf.ae",
                            website="https://gulf.ae", people=[{"name": "Ali", "email": "ali@gulf.ae"}]))
    signed = SimpleNamespace(deal_id="D-SIGNED", status="PAID", updated_at=NOW - timedelta(days=100),
                             parties=[SimpleNamespace(legal_name="Gulf Foods", email="ali@gulf.ae", website="https://gulf.ae")],
                             buyer_candidates=[])
    prospect = SimpleNamespace(deal_id="D-PROS", status="MATCHING", updated_at=NOW, parties=[],
                               buyer_candidates=[SimpleNamespace(candidate_id="C1", email="ali@gulf.ae")])
    upd = AsyncMock()
    with patch.object(personal_data, "_deals", new=AsyncMock(return_value=[signed, prospect])), \
            patch("services.deals.service.update_buyer_candidate", new=upd), \
            patch("shared.audit_log.record", new=AsyncMock()):
        out = await personal_data.erase("buyer", "gulf.ae", by="ops@x", reason="DPDP request", now=NOW)
    rec = await store.get("buyer:gulf.ae")
    assert rec.email is None and rec.people == [] and rec.do_not_contact
    assert out["held_by_deals"][0]["deal_id"] == "D-SIGNED" and out["prospect_candidates_cleaned"] == 1
    upd.assert_awaited_once()


async def test_retention_sweep_only_untouched_old_contacts(monkeypatch):
    from services.trade_min import personal_data
    monkeypatch.setenv("PERSONAL_DATA_RETENTION_DAYS", "30")
    store = await directory.get_directory()
    old = datetime.now(timezone.utc) - timedelta(days=400)
    await store.upsert(_rec(id="buyer:old.ae", role="buyer", name="Old", email="x@old.ae", website="https://old.ae",
                            first_seen_at=old, last_seen_at=old))
    await store.upsert(_rec(id="buyer:new.ae", role="buyer", name="New", email="y@new.ae", website="https://new.ae"))
    with patch("services.trade_min.relationships._deals", new=AsyncMock(return_value=[])), \
            patch("shared.audit_log.record", new=AsyncMock()):
        out = await personal_data.run_retention(by="ops@x")
    assert out["ids"] == ["buyer:old.ae"]
    assert (await store.get("buyer:old.ae")).email is None and (await store.get("buyer:new.ae")).email == "y@new.ae"


def test_new_routes_registered():
    from app import app
    paths = {r.path for r in app.routes}
    for p in ("/deals/{deal_id}/supplier-steps", "/deals/{deal_id}/claim-options", "/deals/{deal_id}/documents/check",
              "/deals/reorders/{deal_id}/offer", "/deals/outreach/variants", "/trade-min/opportunities/company-led",
              "/trade-min/suppliers/{record_id:path}/documents", "/trade-min/suppliers/{record_id:path}/bank-verified",
              "/trade-min/{role}/{record_id:path}/erase", "/trade-min/personal-data/retention"):
        assert p in paths, p
