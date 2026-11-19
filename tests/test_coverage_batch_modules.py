"""Full line + branch coverage of the modules added on 2026-11-19 (part 1): Indian ids, site facts,
outreach A/B, relay evidence, volume, supplier onboarding, company-led, claims, product spec,
markets, personal data. Each test names the behaviour it pins, not just the line."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation, CompanyNote
from shared import catalogue

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
ONION = {"product_id": "onion", "hs_code": "070310", "name": "Onion", "markets": ["AE"],
         "attributes": [{"key": "variety", "values": ["red", "pink"]},
                        {"key": "size", "values": ["55 mm+"], "hard": False}]}


def _rec(**kw) -> CounterpartyRecord:
    base = dict(id="buyer:gulf.ae", role="buyer", name="Gulf Foods", country="AE", email="buy@gulf.ae",
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


# ── shared/indian_ids.py ───────────────────────────────────────────────────

def test_cin_helpers_reject_non_cins():
    from shared import indian_ids
    assert indian_ids.incorporation_year("not a cin") is None
    assert indian_ids.company_age_years(None) is None
    assert indian_ids.is_listed("U01100MH2005PTC123456") is False
    assert indian_ids.find_udyam("no number here") is None


# ── services/trade_min/site_facts.py ────────────────────────────────────────

def test_site_facts_survive_a_broken_catalogue():
    from services.trade_min import site_facts
    with patch("shared.catalogue.line_for", side_effect=RuntimeError("boom")):
        assert site_facts.facts_from_text("070310", "HACCP certified") == {"certificates": ["HACCP"]}
    assert site_facts.facts_from_text("070310", None) == {}


# ── shared/orchestration/outreach_ab.py ────────────────────────────────────

async def test_outreach_variant_keeps_an_assigned_one_and_survives_no_repository(monkeypatch):
    from shared.orchestration import outreach_ab as ab
    monkeypatch.setenv("OUTREACH_AB_MIN_SENDS", "x")
    assert ab.min_sends() == 20
    assert await ab.variant_for(SimpleNamespace(), SimpleNamespace(outreach_variant="b")) == "b"
    with patch("services.deals.repository.get_repository", side_effect=RuntimeError("no store")):
        v = await ab.variant_for(SimpleNamespace(hs_code="070310", market="AE"),
                                 SimpleNamespace(candidate_id="C1", outreach_variant=None))
    assert v in ab.VARIANTS


def test_outreach_stats_skip_unsent_and_unknown_variants():
    from shared.orchestration import outreach_ab as ab
    deal = SimpleNamespace(hs_code="070310", market="AE", buyer_candidates=[
        SimpleNamespace(outreach_variant="z", contacted_at=NOW),
        SimpleNamespace(outreach_variant="a", contacted_at=None)])
    assert ab.stats([deal]) == {}


# ── shared/orchestration/relay_evidence.py ─────────────────────────────────

async def test_supplier_steps_without_commitment_store_and_with_verified_bank():
    from shared import audit_log
    from shared.orchestration.relay_evidence import supplier_steps
    audit_log.set_collection_for_tests(None)
    deal = SimpleNamespace(deal_id="D-STEPS-1", supplier_offers=[], parties=[])
    with patch("shared.orchestration.supplier_commitment.get", side_effect=RuntimeError("down")), \
            patch("services.trade_min.supplier_onboarding.bank_verified_for_deal", new=AsyncMock(return_value=True)):
        out = await supplier_steps(deal)
    bank = next(s for s in out["steps"] if s["step"] == "bank_details")
    assert bank["done"] is True
    with patch("shared.orchestration.supplier_commitment.get", new=AsyncMock(return_value=None)), \
            patch("services.trade_min.supplier_onboarding.bank_verified_for_deal", side_effect=RuntimeError("x")):
        out = await supplier_steps(deal)
    assert next(s for s in out["steps"] if s["step"] == "bank_details")["done"] is False


# ── services/trade_min/volume.py ───────────────────────────────────────────

def test_volume_line_parts_and_bands():
    from services.trade_min import volume
    assert volume.volume_line(None, None) is None
    assert volume.volume_line(None, 52) == "ships every ~1 week"
    assert volume.volume_line(120, None) == "Buys ~120 MT/yr"
    assert [volume.order_size_band(annual_volume_mt=v) for v in (100, 500, 5000)] == ["small", "medium", "large"]
    assert volume.order_size_band(market_import_value_usd=100_000_000, importers_found=2, product_lines=6,
                                  text="nationwide chain") == "large"
    assert volume.order_size_band(market_import_value_usd=500_000, importers_found=5) == "small"
    assert volume.order_size_band(market_import_value_usd=5_000_000, importers_found=1) == "medium"
    assert volume.stated_order_mt([SimpleNamespace(quantity=25000, quantity_unit="KG")]) == (
        25.0, "order size stated / recorded by 1 buyer(s)")
    assert volume.stated_order_mt([SimpleNamespace(quantity=None, annual_volume_mt=None, shipments_12m=None)]) == (None, None)


# ── services/trade_min/supplier_onboarding.py ──────────────────────────────

async def test_onboarding_refusals_and_drive_storage(monkeypatch, tmp_path):
    from services.trade_min import supplier_onboarding as so
    store = await directory.get_directory()
    await store.upsert(_rec(id="supplier:acme.in", role="supplier", name="Acme", country="IN", email="s@acme.in",
                            website="https://acme.in"))
    with pytest.raises(LookupError):
        await so.add_document("nobody", kind="iec", filename="x", by="ops", url="https://x")
    with pytest.raises(ValueError, match="give the file"):
        await so.add_document("acme.in", kind="iec", filename="x", by="ops")
    with pytest.raises(ValueError, match="base64"):
        await so.add_document("acme.in", kind="iec", filename="x", by="ops", content_b64="@@not base64@@")
    drive = MagicMock(is_configured=MagicMock(return_value=True),
                      upload_document=MagicMock(return_value={"file_id": "F1", "web_view_link": "https://drive/F1"}))
    with patch("shared.connectors.google_drive.get_connector", return_value=drive), \
            patch("shared.audit_log.record", new=AsyncMock()):
        doc = await so.add_document("acme.in", kind="fssai", filename="f.pdf", by="ops",
                                    content_b64=base64.b64encode(b"x").decode())
    assert doc["storage"] == "drive" and doc["file_id"] == "F1"
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    broken = MagicMock(is_configured=MagicMock(return_value=True), upload_document=MagicMock(side_effect=RuntimeError("quota")))
    with patch("shared.connectors.google_drive.get_connector", return_value=broken), \
            patch("shared.audit_log.record", new=AsyncMock()):
        doc = await so.add_document("acme.in", kind="iso", filename="iso.pdf", by="ops",
                                    content_b64=base64.b64encode(b"y").decode())
    assert doc["storage"] == "local"
    unknown = SimpleNamespace(parties=[SimpleNamespace(role="supplier", legal_name="Nobody", website=None, email=None)])
    assert await so.bank_verified_for_deal(unknown) is None
    assert so.verification_required() is False  # pinned off for the suite


# ── services/trade_min/company_led.py ──────────────────────────────────────

async def test_company_led_groups_filter_buyers(monkeypatch):
    from services.trade_min import company_led
    monkeypatch.setenv("COMPANY_LED_MIN_BUYERS", "oops")
    assert company_led._int("COMPANY_LED_MIN_BUYERS", 3) == 3
    monkeypatch.setenv("COMPANY_LED_MIN_BUYERS", "1")
    store = await directory.get_directory()
    assert await company_led.groups() == []  # no suppliers
    for i in range(2):
        await store.upsert(_rec(id=f"supplier:s{i}", role="supplier", name=f"S{i}", country="IN", email=f"a@s{i}.in",
                                website=f"https://s{i}.in"))
    await store.upsert(_rec(id="buyer:unverified", name="U", email="u@u.ae", website="https://u.ae"))
    await store.upsert(_rec(id="buyer:indian", name="I", country="IN", email="i@i.in", website="https://i.in",
                            validation=CounterpartyValidation(status="verified")))
    await store.upsert(_rec(id="buyer:other", name="O", email="o@o.ae", website="https://o.ae", hs_codes=["100630"],
                            validation=CounterpartyValidation(status="verified")))
    assert await company_led.groups() == []
    await store.upsert(_rec(validation=CounterpartyValidation(status="verified")))
    monkeypatch.setenv("COMPANY_LED_MIN_BUYERS", "2")
    await store.upsert(_rec(id="buyer:b2", name="B2", email="b@b2.ae", website="https://b2.ae",
                            validation=CounterpartyValidation(status="verified")))
    await store.upsert(_rec(id="buyer:qa", name="QA", country="QA", email="q@q.qa", website="https://q.qa",
                            validation=CounterpartyValidation(status="verified")))
    assert [g["market"] for g in await company_led.groups()] == ["AE"]  # QA has one buyer only


async def test_company_led_creates_even_when_checks_fail():
    from services.trade_min import company_led
    from services.trade_min.repository import repository
    repository.clear()
    store = await directory.get_directory()
    for i in range(3):
        await store.upsert(_rec(id=f"buyer:b{i}", name=f"B{i}", email=f"b@b{i}.ae", website=f"https://b{i}.ae",
                                validation=CounterpartyValidation(status="verified")))
    for i in range(2):
        await store.upsert(_rec(id=f"supplier:s{i}", role="supplier", name=f"S{i}", country="IN", email=f"a@s{i}.in",
                                website=f"https://s{i}.in"))
    with patch("services.trade_min.start_check.annotate", side_effect=RuntimeError("news down")):
        out = await company_led.create(now=NOW)
    assert len(out["created"]) == 1
    repository.clear()


# ── shared/orchestration/claims.py ─────────────────────────────────────────

async def test_claim_options_from_the_deal_facts(monkeypatch):
    from shared.orchestration import claims
    monkeypatch.setenv("QC_REINSPECTION_MAX", "bad")
    assert claims._f("QC_REINSPECTION_MAX", 1) == 1
    deal = SimpleNamespace(deal_id="D", quantity_mt=50, margin_floor_pct=None,
                           inspections=[{"result": "FAIL"}, {"result": "FAIL"}])
    facts = {"shipped_quantity_mt": 50.0, "unit_price": 400.0, "po": SimpleNamespace(agreed_price=320.0)}
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
            patch("shared.documents.facts.deal_facts", new=AsyncMock(return_value=facts)):
        out = await claims.claim_options("D", affected_share=0.5)
    assert out["sale_value_usd"] == 20000.0 and "reinspection" not in [o["option"] for o in out["options"]]
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
            patch("shared.documents.facts.deal_facts", new=AsyncMock(return_value={"po": None})):
        out = await claims.claim_options("D")
    assert out["options"] == [] and "no PI / PO price" in out["reason"]


def test_claim_options_when_portless_is_at_fault():
    from shared.orchestration.claims import options
    rows = {r["option"]: r for r in options(sale_value_usd=10000, purchase_value_usd=8000, margin_floor_pct=8,
                                             inspection_fee_usd=300, reinspections_left=1, supplier_at_fault=False)}
    assert rows["reinspection"]["portless_cost_usd"] == 300 and rows["discount"]["portless_cost_usd"] == 1000.0
    assert rows["cancel_and_refund"]["portless_cost_usd"] == 2000.0


# ── shared/product_spec.py ─────────────────────────────────────────────────

def test_product_spec_merge_edges_and_summaries():
    from shared import product_spec as ps
    cur, _ = ps.merge(None, "070310", {"variety": "red"}, source="email")
    cur, ch = ps.merge(cur, "070310", {"variety": "red", "size": "", "x": None}, source="email", product="Onion")
    assert ch == [] and cur.product == "Onion"
    line = catalogue.line_for("070310")
    conflicts, matches = ps.conflicts(line, {"size": "55 mm+"}, {"size": "40-60 mm"})
    assert conflicts == [] and matches == 0  # size is not a hard attribute
    assert ps.summary_line(None) == ""
    offer, _ = ps.merge(None, "070310", {"capacity_mt_per_month": 300, "price_usd_per_mt": 410}, source="email")
    assert ps.summary_line(offer) == "300 MT/month · USD 410/MT"
    assert ps.from_buyer_requirement(None) == {} and ps.from_supplier_offer(None) == {}
    offer_rec = SimpleNamespace(grade="A", capacity_mt_per_month=300, moq_mt=20, fob_price_usd_per_mt=410,
                                incoterm="FOB", port_of_loading="Nhava Sheva", certifications=[], lead_time_days=14,
                                payment_terms=None, spec={"variety": "red"})
    assert ps.from_supplier_offer(offer_rec)["variety"] == "red" and ps.from_supplier_offer(offer_rec)["certificates"] is None


def test_agreed_spec_edges():
    from shared.product_spec import agreed_spec
    cand = SimpleNamespace(candidate_id="C", status="selected", requirement=SimpleNamespace(grade=None, spec={"variety": "red"}))
    offer = SimpleNamespace(offer_id="O", spec={"variety": "pink", "size": "55 mm+"})
    deal = SimpleNamespace(selected_pair={"offer_id": "O"}, buyer_candidates=[cand], supplier_offers=[offer])
    assert agreed_spec(deal) == {"variety": "red"}  # the supplier's other value and extra keys do not change it


# ── services/trade_min/markets.py ──────────────────────────────────────────

async def test_markets_edges(monkeypatch):
    from services.trade_min import markets
    monkeypatch.setenv("WORLDWIDE_MARKET_MIN_BUYERS", "x")
    assert markets.min_buyers() == 3
    assert markets.contactable(_rec(validation=CounterpartyValidation(status="unverified", email="bounced"))) is False
    with pytest.raises(ValueError):
        await markets.accept_market("onion", "S", by="ops")
    already = await markets.accept_market("onion", "AE", by="ops")
    assert already["markets"] == ["AE"]
    with pytest.raises(LookupError):
        await markets.markets_for_supplier("nobody")
    catalogue.reset_for_tests()
    assert await markets.proposed_markets() == []


async def test_proposed_markets_skip_other_products_and_blocked_buyers():
    from services.trade_min import markets
    store = await directory.get_directory()
    await store.upsert(_rec(id="buyer:x1", country="SA", hs_codes=["100630"], email="a@x1.sa", website="https://x1.sa"))
    await store.upsert(_rec(id="buyer:x2", country="SA", do_not_contact=True, email="a@x2.sa", website="https://x2.sa"))
    for i in range(4):
        await store.upsert(_rec(id=f"buyer:y{i}", name=f"Y{i}", country="SA", email=f"a@y{i}.sa", website=f"https://y{i}.sa"))
    await store.upsert(_rec(id="buyer:qa", name="Q", country="QA", email="q@q.qa", website="https://q.qa"))  # too few
    rows = await markets.proposed_markets()
    assert rows[0]["buyers"] == 4 and len(rows[0]["examples"]) == 3
    await store.upsert(_rec(id="supplier:z", role="supplier", name="Z", country="IN", email="z@z.in", website="https://z.in"))
    await store.upsert(_rec(id="buyer:home", name="Home", country="IN", email="h@h.in", website="https://h.in"))
    out = await markets.markets_for_supplier("supplier:z")
    assert {m["market"] for m in out["markets"]} == {"SA", "QA"}  # every market with a contactable buyer


# ── services/trade_min/personal_data.py ────────────────────────────────────

async def test_personal_data_settings_and_errors(monkeypatch):
    from services.trade_min import personal_data as pd
    monkeypatch.setenv("RECORD_RETENTION_YEARS", "x")
    monkeypatch.setenv("PERSONAL_DATA_RETENTION_DAYS", "x")
    assert pd.retention_years() == 8 and pd.personal_retention_days() == 730
    assert pd._aware("2026-01-01") is None
    with patch("services.deals.repository.get_repository", side_effect=RuntimeError("down")):
        assert await pd._deals() == []
    with pytest.raises(LookupError):
        await pd.erase("buyer", "nobody", by="ops", reason="request")


async def test_erase_without_addresses_and_old_deals_released():
    from services.trade_min import personal_data as pd
    store = await directory.get_directory()
    await store.upsert(_rec(email=None, notes=[CompanyNote(note_id="N1", text="called Ali", by="ops", at=NOW)]))
    old_deal = SimpleNamespace(deal_id="D-OLD", status="PAID", updated_at=NOW - timedelta(days=365 * 9),
                               parties=[SimpleNamespace(legal_name="Gulf Foods", email=None, website="https://gulf.ae")])
    stranger = SimpleNamespace(deal_id="D-Y", status="PAID", updated_at=NOW,
                               parties=[SimpleNamespace(legal_name="Unrelated Co", email="u@unrelated.com", website=None)])
    other = SimpleNamespace(deal_id="D-X", status="MATCHING", parties=[SimpleNamespace(legal_name="Someone", email="z@z.com", website=None)],
                            buyer_candidates=[])
    with patch.object(pd, "_deals", new=AsyncMock(return_value=[old_deal, stranger, other])), \
            patch("shared.audit_log.record", new=AsyncMock()):
        out = await pd.erase("buyer", "buyer:gulf.ae", by="ops", reason="request", now=NOW)
    assert out["held_by_deals"] == [] and out["note"] == ""
    assert (await store.get("buyer:gulf.ae")).notes[0].text == "[erased]"


async def test_erase_cleans_only_matching_prospect_candidates():
    from services.trade_min import personal_data as pd
    store = await directory.get_directory()
    await store.upsert(_rec())
    prospect = SimpleNamespace(deal_id="D-P", status="MATCHING", parties=[], buyer_candidates=[
        SimpleNamespace(candidate_id="C1", email="someone@else.com"), SimpleNamespace(candidate_id="C2", email=None)])
    signed = SimpleNamespace(deal_id="D-S", status="PI_SENT", parties=[], buyer_candidates=[])
    upd = AsyncMock()
    with patch.object(pd, "_deals", new=AsyncMock(return_value=[prospect, signed])), \
            patch("services.deals.service.update_buyer_candidate", new=upd), \
            patch("shared.audit_log.record", new=AsyncMock()):
        out = await pd.erase("buyer", "gulf.ae", by="ops", reason="request", now=NOW)
    assert out["prospect_candidates_cleaned"] == 0 and upd.await_count == 0


async def test_retention_skips_contacted_manual_and_contactless(monkeypatch):
    from services.trade_min import personal_data as pd
    monkeypatch.setenv("PERSONAL_DATA_RETENTION_DAYS", "30")
    store = await directory.get_directory()
    old = datetime.now(timezone.utc) - timedelta(days=400)
    await store.upsert(_rec(id="buyer:nocontact", email=None, phone=None, website="https://nc.ae", first_seen_at=old, last_seen_at=old))
    await store.upsert(_rec(id="supplier:manual", role="supplier", email="m@m.in", website="https://m.in", manual={"x": 1},
                            first_seen_at=old, last_seen_at=old))
    contacted = _rec(id="buyer:contacted", email="c@c.ae", website="https://c.ae", first_seen_at=old, last_seen_at=old)
    await store.upsert(contacted)

    async def annotate(rows):
        return [r.model_copy(update={"relationship": {"stage": "contacted" if r.id == "buyer:contacted" else "new"}})
                for r in rows]
    with patch("services.trade_min.relationships.annotate", new=annotate):
        assert await pd.retention_candidates() == []
        assert (await pd.run_retention(by="ops"))["scrubbed"] == 0


def test_stamp_of_no_evidence():
    from shared.orchestration.relay_evidence import stamp
    assert stamp(None) is None
