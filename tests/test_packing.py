"""The buyer's packing requirements: asked, passed to supplier / PI / PO / inspector, priced.

Covers shared/orchestration/packing.py and where it is wired: the agreement
checklist and briefing (negotiation agent), capture from the buyer's reply, the
cost sheet, the supplier's firm order request and confirmation, the QIMA and
portal booking payloads, and the staff endpoint.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.orchestration import packing


@pytest.fixture(autouse=True)
def packing_required(monkeypatch):
    monkeypatch.setenv("NEGOTIATION_PACKING_REQUIRED", "true")
    monkeypatch.delenv("QC_PROVIDERS_ENABLED", raising=False)  # QIMA only


# ---------------------------------------------------------------------------
# Reading, merging, describing, pricing
# ---------------------------------------------------------------------------

class TestParse:
    @pytest.mark.parametrize(("text", "check"), [
        ("Please pack in 25 kg PP woven bags with inner liner, our brand printed on each bag in Arabic, fumigated.",
         lambda s: s.material == "pp" and s.unit_weight_kg == 25 and s.inner_liner and s.private_label and s.fumigation
         and "Arabic" in (s.marking or "")),
        ("Your standard export packing is fine.", lambda s: s.standard_ok and not s.material),
        ("50kg jute bags, palletised please", lambda s: s.material == "jute" and s.unit_weight_kg == 50 and s.palletised),
        ("1 kg retail packs in cartons with our private label", lambda s: s.material == "carton" and s.private_label),
        ("1 MT jumbo bags", lambda s: s.material == "fibc"),
        ("bulk in container with flexi bag", lambda s: s.material == "bulk"),
        ("container must be lined with kraft paper and silica gel", lambda s: s.container_lining),
    ])
    def test_reads_what_the_buyer_wrote(self, text, check):
        spec = packing.parse_packing(text)
        assert spec is not None and check(spec), spec

    @pytest.mark.parametrize("text", ["We accept your offer and price.", "", None, "Delivery to Jebel Ali please."])
    def test_nothing_about_packing(self, text):
        assert packing.parse_packing(text) is None

    def test_a_later_message_adds_to_the_first(self):
        first = packing.parse_packing("25 kg PP bags")
        both = packing.merge(first, packing.parse_packing("also palletised and fumigated"))
        assert both.unit_weight_kg == 25 and both.palletised and both.fumigation and both.material == "pp"
        assert packing.merge(None, None) is None

    def test_special_packing_overrides_standard(self):
        merged = packing.merge(packing.parse_packing("standard packing is fine"), packing.parse_packing("50 kg jute bags"))
        assert not merged.standard_ok and merged.material == "jute"


class TestPriceAndChecks:
    def test_standard_packing_costs_nothing_extra(self):
        spec = packing.parse_packing("your usual export packing is ok")
        assert not packing.special(spec) and packing.surcharge(spec, 20) == (0.0, [])
        assert packing.qc_checklist(spec) == ["Packing: supplier's standard export packing — check it is intact, clean and sea-worthy"]

    def test_special_packing_is_priced_from_settings(self, monkeypatch):
        spec = packing.parse_packing("50 kg jute bags with our marking printed on each bag, palletised")
        extra, parts = packing.surcharge(spec, 25)
        assert extra == pytest.approx(18 + 6 + 220 / 25)
        assert any("jute" in p for p in parts) and any("pallets" in p for p in parts)
        monkeypatch.setenv("TRADE_COST_PACKING_JUTE_USD_PER_MT", "30")
        assert packing.surcharge(spec, 25)[0] == pytest.approx(30 + 6 + 220 / 25)

    def test_inspector_checks_weight_marking_pallets(self):
        checks = packing.qc_checklist(packing.parse_packing("25 kg PP bags, private label, palletised, fumigated"))
        text = " | ".join(checks)
        assert "net weight 25 kg" in text and "artwork" in text and "Pallets" in text and "Fumigation" in text


# ---------------------------------------------------------------------------
# Asked before the agreement
# ---------------------------------------------------------------------------

def _session(**kw):
    from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    base = dict(negotiation_id="N1", deal_id="D1", trade_match_id="TM", buyer_id="B", seller_id="S",
                product_code="10063020", buyer_country_code="AE", buyer_target_price=500.0, supplier_quote_price=400.0,
                policy=NegotiationPolicy(), quantity_mt=20.0, status="ROUND", round_no=1, created_at=now, updated_at=now)
    base.update(kw)
    return NegotiationSession(**base)


class TestAsked:
    def test_the_agreement_waits_for_packing(self, monkeypatch):
        from agents.negotiation_agent.guards import missing_agreement_info
        missing = missing_agreement_info(_session())
        assert any("packing" in m for m in missing)
        assert not any("inspection agency" in m for m in missing), "one QC provider: nothing to choose"
        assert missing_agreement_info(_session(packing={"standard_ok": True})) == []
        monkeypatch.setenv("NEGOTIATION_PACKING_REQUIRED", "false")
        assert missing_agreement_info(_session()) == []

    def test_with_several_providers_the_choice_is_still_asked(self, monkeypatch):
        from agents.negotiation_agent.guards import missing_agreement_info
        monkeypatch.setenv("QC_PROVIDERS_ENABLED", "qima,sgs")
        asked = [m for m in missing_agreement_info(_session(packing={"standard_ok": True})) if "inspection agency" in m]
        assert asked and "QIMA" in asked[0] and "SGS" in asked[0] and "Intertek" not in asked[0]

    def test_briefing_asks_packing_not_the_agency(self):
        from agents.negotiation_agent.briefing import PACKING_QUESTION, buyer_missing
        missing = buyer_missing({"destination_port": "Jebel Ali", "grade": "1121"}, qc_selected=False,
                                quantity_known=True, packing_known=False)
        assert PACKING_QUESTION in missing and not any("inspection agency" in m for m in missing)
        assert PACKING_QUESTION not in buyer_missing({"packaging": "25 kg bags"}, qc_selected=True, quantity_known=True,
                                                      packing_known=False)

    def test_packing_is_read_from_the_new_reply_not_the_quote(self):
        from agents.negotiation_agent.service import _capture_buyer_packing
        quoted_only = ("Thanks, we agree.\n\nOn Mon, PortLess wrote:\n> Please tell us your packing: bag type, "
                       "or that our standard export packing suits you.")
        assert _capture_buyer_packing(_session(), quoted_only).packing is None
        got = _capture_buyer_packing(_session(), "We agree. Pack in 50 kg jute bags please.\n\n> earlier text")
        assert got.packing["material"] == "jute" and got.packing["unit_weight_kg"] == 50

    async def test_seeded_from_the_buyers_outreach_reply(self, monkeypatch):
        from agents.negotiation_agent import briefing, service
        cand = SimpleNamespace(requirement=SimpleNamespace(packaging="25kg PP bags, palletised"))
        monkeypatch.setattr(briefing, "_load_deal", AsyncMock(return_value=SimpleNamespace(buyer_candidates=[cand])))
        monkeypatch.setattr(briefing, "_selected_candidate", lambda deal, email: cand)
        got = await service._seed_packing_from_outreach(_session())
        assert got.packing["unit_weight_kg"] == 25 and got.packing["palletised"]
        assert got.packing["source"] == "buyer's reply to our introduction"


# ---------------------------------------------------------------------------
# Priced on the cost sheet
# ---------------------------------------------------------------------------

def _sheet(**kw):
    from shared.orchestration import trade_costs
    return trade_costs.build(quantity_mt=25, market="AE", freight_usd_per_mt=40, freight_is_quote=True, qc_usd_per_mt=12,
                             qc_is_quote=True, supplier_address="Kakinada, Andhra Pradesh 533003", **kw)


def _line(sheet, key="packing"):
    return next(line for line in sheet.lines if line.key == key)


class TestCostSheet:
    def test_unknown_packing_is_flagged_not_priced(self):
        sheet = _sheet()
        assert _line(sheet).usd_per_mt == 0 and any("packing" in w for w in sheet.warnings)

    def test_special_packing_raises_portless_costs_until_the_supplier_confirms(self):
        spec = packing.parse_packing("50 kg jute bags, palletised").model_dump()
        base = _sheet(packing={"standard_ok": True})
        special = _sheet(packing=spec)
        assert _line(special).usd_per_mt == pytest.approx(18 + 220 / 25)
        assert special.fixed_usd_per_mt == pytest.approx(base.fixed_usd_per_mt + 18 + 220 / 25, abs=0.01)
        confirmed = _sheet(packing=spec, packing_in_supplier_price=True)
        assert _line(confirmed).usd_per_mt == 0 and _line(confirmed).paid_by.startswith("supplier")
        assert confirmed.fixed_usd_per_mt == pytest.approx(base.fixed_usd_per_mt, abs=0.01)


# ---------------------------------------------------------------------------
# Passed to the supplier and the inspector
# ---------------------------------------------------------------------------

class TestPassedOn:
    def test_order_request_states_the_packing_and_asks_for_a_price_including_it(self):
        from shared.orchestration import notifications
        _, body = notifications._TEMPLATES["supplier_order_request"]({
            "deal_id": "D1", "supplier_name": "Godavari", "price": 400, "quantity_mt": 20, "hs_code": "10063020",
            "hours": 24, "packing": "50 kg jute bags; palletised", "packing_special": True})
        assert "Packing          : 50 kg jute bags; palletised" in body
        assert "Your price must include this packing" in body
        _, std = notifications._TEMPLATES["supplier_order_request"]({
            "deal_id": "D1", "supplier_name": "G", "price": 400, "quantity_mt": 20, "hs_code": "10063020", "hours": 24,
            "packing": "Supplier's standard export packing (buyer accepts it)", "packing_special": False})
        assert "standard export packing, which the buyer has accepted" in std

    def test_po_notice_carries_the_packing(self):
        from shared.orchestration import notifications
        po = SimpleNamespace(po_id="PO-1", supplier_name="G", quantity_mt=20, agreed_price=400, currency="USD",
                             incoterm="FOB", total_value=8000, port_of_loading="Kakinada", delivery_deadline="2026-12-31",
                             payment_terms="escrow", quality_spec=None, packing_spec="25 kg PP bags", storage_uri=None)
        _, body = notifications._TEMPLATES["po_issued"]({"deal_id": "D1", "po": po, "hs_code": "10063020"})
        assert "Packing          : 25 kg PP bags (checked by the inspector)" in body

    def test_qima_booking_carries_packing_and_the_checks(self, monkeypatch):
        from shared.connectors import qc_providers as qc
        monkeypatch.setenv("QC_SANDBOX_PROVIDERS", "qima")
        spec = packing.parse_packing("25 kg PP bags with inner liner, palletised").model_dump()
        body = qc.QimaConnector().build_inspection_order({
            "deal_id": "D1", "inspection_date": "2026-12-01", "quantity_mt": 20, "hs_code": "10063020",
            "supplier": {"name": "G", "address": "Kakinada", "country_code": "IN"}, "packing": spec}, "U1")
        product = body["products"][0]
        assert product["unitPacking"].startswith("25 kg PP woven bags") and product["outerPacking"] == "palletised"
        assert "net weight 25 kg" in product["sampleReferenceClientComments"]

    def test_portal_booking_request_carries_packing(self):
        from shared.connectors import qc_providers as qc
        req = qc.SgsConnector().booking_request({"packing": {"material": "jute", "unit_weight_kg": 50}}, "SGS-REQ-1")
        assert req["packing"]["required"] == "50 kg jute bags" and req["packing"]["checks"]
        assert qc.SgsConnector().booking_request({}, "R")["packing"]["required"] == "supplier's standard export packing"

    async def test_supplier_confirmation_puts_the_packing_inside_its_price(self, monkeypatch):
        from agents.negotiation_agent import service as neg
        from shared.orchestration import continuations
        from shared.orchestration import supplier_commitment as sc
        sc.reset_for_tests()
        monkeypatch.setattr(sc, "_mongo", lambda: None)
        session = _session(packing=packing.parse_packing("50 kg jute bags").model_dump())
        saved: list = []

        class Repo:
            async def save_session(self, s):
                saved.append(s)

        monkeypatch.setattr(sc, "_session", AsyncMock(return_value=session))
        monkeypatch.setattr("agents.negotiation_agent.repository.get_repository", lambda: Repo())
        monkeypatch.setattr(neg, "refresh_cost_sheet", AsyncMock(side_effect=lambda s: s))
        monkeypatch.setattr(continuations, "pi_after_compliance", AsyncMock())
        await sc._save(sc.Commitment(deal_id="D1", agreed_price=500, quantity_mt=20, status="requested",
                                     attempts=[sc.Attempt(supplier_id="S1", price_usd_per_mt=400,
                                                          requested_at=sc._now())]))
        await sc.record_confirmed("D1", supplier_id="S1", price=400)
        assert saved[-1].packing_in_supplier_price is True and saved[-1].supplier_last_quote == 400


# ---------------------------------------------------------------------------
# Staff endpoint
# ---------------------------------------------------------------------------

class TestEndpoint:
    def _api(self, monkeypatch, session):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.deals import router as deals_router
        from shared.auth.dependencies import AuthUser, get_current_user, require_portless
        staff = AuthUser(user_id="u1", email="ops@portless.invalid", is_portless=True)
        app = FastAPI()
        app.include_router(deals_router.router)
        app.dependency_overrides[require_portless] = lambda: staff
        app.dependency_overrides[get_current_user] = lambda: staff
        monkeypatch.setattr(deals_router, "_org_deal", AsyncMock(return_value=SimpleNamespace(deal_id="D1")))
        saved: list = []

        class Repo:
            async def get_session_by_deal(self, deal_id):
                return saved[-1] if saved else session

            async def save_session(self, s):
                saved.append(s)

        monkeypatch.setattr("agents.negotiation_agent.repository.get_repository", lambda: Repo())
        monkeypatch.setattr("agents.negotiation_agent.service.refresh_cost_sheet", AsyncMock(side_effect=lambda s: s))
        return TestClient(app, raise_server_exceptions=False), saved

    def test_staff_record_packing_heard_by_phone(self, monkeypatch):
        api, saved = self._api(monkeypatch, _session(packing_in_supplier_price=True))
        resp = api.put("/deals/D1/packing", json={"text": "50 kg jute bags, palletised"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["special"] and body["surcharge_usd_per_mt"] > 0 and body["qc_checks"]
        assert saved[-1].packing["material"] == "jute" and saved[-1].packing_in_supplier_price is False
        assert saved[-1].packing["source"] == "operator:ops@portless.invalid"
        resp = api.put("/deals/D1/packing", json={"standard_ok": True})
        assert resp.json()["special"] is False
        assert api.put("/deals/D1/packing", json={}).status_code == 422
        assert api.get("/deals/D1/packing").json()["description"].startswith("Supplier's standard")
