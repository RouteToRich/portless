"""Maersk freight, inspection phases, trade notes, the consolidated order e-mails.

  • Maersk connector: OAuth client-credentials + Consumer-Key, DCSA booking body,
    401 refresh, Track & Trace, eBL, VGM — the documented request shapes
  • the shipment lifecycle on the carrier simulator: booking → confirmed →
    VGM gate → submitted → B/L draft gate → approved → issued → sailed →
    arrived (deal CLEARED) → delivered (acceptance window)
  • the deal's freight status line at each step; the shipment view; the webhook
  • inspection phases DUPRO / PSI / PEO: the buyer's words, dates, QIMA service
    types, only the PSI moves the deal, PEO holds customs
  • every outside party gets the trade brief, without the other side's details
  • the order-confirmed e-mail: one per party, each with only its own side
"""
from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.carrier_sandbox import maersk as maersk_sim
from services.carrier_sandbox import main as carrier_sim
from services.shield_link import freight
from services.shield_link import service as sl
from services.shield_link.repository import InMemoryShieldLinkRepository, get_repository
from services.shield_link.schemas import FreightBookingRequest, QcBookingRequest
from shared.connectors import maersk as maersk_mod
from shared.connectors.maersk import MaerskConnector, normalize_events
from shared.connectors.payments import ConnectorError
from shared.domain.enums import FreightStatus
from shared.domain.events import EventTopic
from shared.orchestration import freight_status, inspection_plan, trade_brief
from shared.orchestration.qc_schedule import today as qc_today


def _deal(status="QC_PASSED", **extra):
    return SimpleNamespace(deal_id="DEAL-F1", status=SimpleNamespace(value=status), hs_code="1006.30",
                           quantity_mt=50.0, parties=[], **extra)


@pytest.fixture(autouse=True)
def _repo():
    import services.shield_link.repository as repo_mod
    repo_mod._repository = InMemoryShieldLinkRepository()
    repo_mod._motor_client = None
    maersk_mod._TOKENS.clear()
    carrier_sim.reset()
    yield
    repo_mod._repository = None
    carrier_sim.reset()


@pytest.fixture
def world(monkeypatch):
    """Carrier simulator on, the deal at QC_PASSED, events / gates / transitions captured."""
    monkeypatch.setenv("CARRIER_SANDBOX_PROVIDERS", "maersk")
    monkeypatch.setenv("MAERSK_CARRIER_SERVICE_CODE", "FL1")
    state = SimpleNamespace(deal=_deal(), events=[], gates=[], transitions=[])

    async def _emit(topic, deal_id, payload):
        state.events.append((topic, payload))

    async def _gate(deal_id, action, summary, financial_impact_usd, callback_payload, idempotency_key, **kw):
        if idempotency_key not in [g["key"] for g in state.gates]:
            state.gates.append({"action": action, "summary": summary, "payload": callback_payload,
                                "key": idempotency_key, "flags": kw.get("risk_flags")})

    async def _transition(deal_id, target, reason):
        state.transitions.append(target)
        state.deal = _deal(target)

    loading = SimpleNamespace(container_numbers=["MSKU1111111", "MSKU2222222"], seal_numbers=["S-1", "S-2"],
                              loaded_quantity_mt=50.0)
    with (
        patch.object(sl, "_fetch_deal", new=AsyncMock(side_effect=lambda _id: state.deal)),
        patch.object(sl, "_transition_deal", new=AsyncMock(side_effect=_transition)),
        patch.object(sl, "_emit", new=_emit),
        patch("shared.orchestration.continuations._create_gate", new=_gate),
        patch.object(freight, "_loading", new=AsyncMock(return_value=loading)),
    ):
        yield state


def _topics(world):
    return [t for t, _ in world.events]


async def _book(world, **kw):
    return await sl.book_freight(FreightBookingRequest(
        deal_id="DEAL-F1", origin_port="Chennai, IN", destination_port="Jebel Ali, AE", quantity_mt=50.0,
        container_numbers=["MSKU1111111", "MSKU2222222"], **kw))


# ---------------------------------------------------------------------------
# Connector contract
# ---------------------------------------------------------------------------

class TestMaerskConnector:
    @pytest.fixture
    def live(self, monkeypatch):
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("MAERSK_CONSUMER_KEY", "ck-1")
        monkeypatch.setenv("MAERSK_CLIENT_SECRET", "cs-1")
        monkeypatch.setenv("MAERSK_CARRIER_SERVICE_CODE", "FL1")
        return MaerskConnector()

    @staticmethod
    def _transport(monkeypatch, handler):
        seen: list[httpx.Request] = []
        real = httpx.AsyncClient

        def _record(request):
            seen.append(request)
            if request.url.path.endswith("/access_token"):
                return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
            return handler(request)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_record), **kw))
        return seen

    async def test_booking_request_is_dcsa_with_our_references(self, live, monkeypatch):
        seen = self._transport(monkeypatch, lambda r: httpx.Response(201, json={
            "carrierBookingReference": "254000001", "bookingStatus": "RECEIVED"}))
        out = await live.book({"deal_id": "DEAL-F1", "po_number": "PO-9", "hs_code": "1006.30", "quantity_mt": 50,
                               "container_type": "42GP", "origin_port": "Chennai, IN", "origin_unlocode": "INMAA",
                               "destination_port": "Jebel Ali, AE", "destination_unlocode": "AEJEA",
                               "commodity": "Parboiled rice", "note": "PortLess deal DEAL-F1 · PO PO-9",
                               "container_numbers": ["MSKU1111111"],
                               "contacts": [{"name": "PortLess", "email": "ops@portless.example"}]})
        assert out["ext_ref"] == "254000001" and out["booking_status"] == "RECEIVED"
        token, booking = seen
        assert str(token.url) == "https://api.maersk.com/customer-identity/oauth/v2/access_token"
        form = dict(x.split("=") for x in token.content.decode().split("&"))
        assert form == {"grant_type": "client_credentials", "client_id": "ck-1", "client_secret": "cs-1"}
        assert token.headers["consumer-key"] == "ck-1"
        assert str(booking.url) == "https://api.maersk.com/dcsa/bkg/v2/bookings"
        assert booking.headers["consumer-key"] == "ck-1" and booking.headers["authorization"] == "Bearer tok-1"
        body = json.loads(booking.content)
        assert body["carrierServiceCode"] == "FL1"
        assert (body["receiptTypeAtOrigin"], body["deliveryTypeAtDestination"]) == ("CY", "CY")
        assert (body["cargoMovementTypeAtOrigin"], body["cargoMovementTypeAtDestination"]) == ("FCL", "FCL")
        assert body["requestedEquipments"] == [{"ISOEquipmentCode": "42GP", "units": 2, "isShipperOwned": False,
                                                "equipmentReferences": ["MSKU1111111"]}]
        assert body["commodities"][0] == {"commodityType": "Parboiled rice", "HSCodes": ["1006.30"],
                                          "cargoGrossWeight": 50000.0, "cargoGrossWeightUnit": "KGM"}
        assert [loc["location"]["UNLocationCode"] for loc in body["shipmentLocations"]] == ["INMAA", "AEJEA"]
        assert {"type": "PO", "value": "PO-9"} in body["references"] and body["remarks"].startswith("PortLess deal")

    async def test_a_401_refreshes_the_token_once(self, live, monkeypatch):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401 if calls["n"] == 1 else 200, json={"bookingStatus": "CONFIRMED"})

        seen = self._transport(monkeypatch, handler)
        assert (await live.status("254"))["bookingStatus"] == "CONFIRMED"
        assert sum(1 for r in seen if r.url.path.endswith("/access_token")) == 2

    async def test_service_code_is_required(self, live, monkeypatch):
        monkeypatch.setenv("MAERSK_CARRIER_SERVICE_CODE", "")
        self._transport(monkeypatch, lambda r: httpx.Response(201, json={}))
        with pytest.raises(ConnectorError, match="carrierServiceCode"):
            await MaerskConnector().book({"quantity_mt": 10})

    async def test_tracking_ebl_and_vgm_paths(self, live, monkeypatch):
        def handler(request):
            if request.url.path.endswith("/events"):
                return httpx.Response(200, json=[{"eventType": "TRANSPORT", "transportEventTypeCode": "DEPA"}])
            return httpx.Response(202 if request.method == "POST" else 200, json={"transportDocumentStatus": "DRAFT"})

        seen = self._transport(monkeypatch, handler)
        events = await live.events(booking_ref="254")
        assert events[0]["transportEventTypeCode"] == "DEPA"
        assert seen[-1].url.params["carrierBookingReference"] == "254"
        assert (await live.transport_document("254"))["transportDocumentStatus"] == "DRAFT"
        await live.approve_transport_document("254")
        assert seen[-1].method == "PATCH" and seen[-1].url.path == "/dcsa/ebl/v3/transport-documents/254/approve"
        out = await live.submit_vgm("254", [{"equipment_reference": "MSKU1", "verified_gross_mass_kg": 28750}],
                                    {"party_name": "PortLess", "signatory": "Ops", "contact_email": "o@p.example"})
        assert out["status"] == "ACCEPTED"
        assert seen[-1].url.path == "/export-shipments/vgm/v1/shipments/254/vgm"
        vgm = json.loads(seen[-1].content)
        box = vgm["containers"][0]
        assert vgm["transportDocumentReference"] == "254"
        assert box["equipmentReference"] == "MSKU1" and box["verifiedGrossMass"] == 28750.0
        assert box["massUnit"] == "KGM" and box["weighingMethod"] == "METHOD2" and box["weighingDate"]
        assert box["responsibleParty"] == {"partyName": "PortLess", "signatory": "Ops", "contactEmail": "o@p.example"}

    async def test_provider_errors_surface(self, live, monkeypatch):
        self._transport(monkeypatch, lambda r: httpx.Response(400, json={"errors": [{"message": "bad"}]}))
        with pytest.raises(ConnectorError, match="400"):
            await live.book({"quantity_mt": 10})

    async def test_webhook_token(self, monkeypatch):
        assert await MaerskConnector().webhook_verify({}, b"{}") is True
        monkeypatch.setenv("MAERSK_WEBHOOK_SECRET", "w")
        assert await MaerskConnector().webhook_verify({"x-portless-webhook-token": "w"}, b"{}") is True
        assert await MaerskConnector().webhook_verify({"x-portless-webhook-token": "x"}, b"{}") is False

    def test_dcsa_events_become_milestones(self):
        mk = lambda code, field, loc, cls="ACT": {"eventType": "X", field: code, "eventClassifierCode": cls,
                                                  "eventDateTime": f"2026-12-0{len(code)}T00:00:00Z",
                                                  "eventLocation": {"UNLocationCode": loc}, "equipmentReference": "MSKU1"}
        events = normalize_events([
            mk("GTIN", "equipmentEventTypeCode", "INMAA"), mk("LOAD", "equipmentEventTypeCode", "INMAA"),
            mk("DEPA", "transportEventTypeCode", "INMAA"), mk("ARRI", "transportEventTypeCode", "LKCMB"),
            mk("ARRI", "transportEventTypeCode", "AEJEA"), mk("DISC", "equipmentEventTypeCode", "AEJEA"),
            mk("GTOT", "equipmentEventTypeCode", "AEJEA"), mk("ARRI", "transportEventTypeCode", "AEJEA", "EST"),
        ], origin="INMAA", destination="AEJEA")
        by = [(e["code"], e["location"]["unlocode"], e["milestone"]) for e in events]
        assert ("ARRI", "LKCMB", None) in by  # transshipment is not the arrival
        assert ("ARRI", "AEJEA", "ARRIVED") in by and ("GTOT", "AEJEA", "DELIVERED") in by
        assert ("DEPA", "INMAA", "DEPARTED") in by and ("LOAD", "INMAA", "LOADED") in by
        assert sum(1 for e in events if e["classifier"] == "EST" and e["milestone"]) == 0  # estimates never move a deal


# ---------------------------------------------------------------------------
# The shipment, end to end on the carrier simulator
# ---------------------------------------------------------------------------

class TestShipmentLifecycle:
    async def test_book_confirm_vgm_bl_voyage_delivery(self, world):
        maersk_sim.STATE.auto = False
        tpo = await _book(world)
        assert tpo.provider == "maersk" and tpo.booking_status == "RECEIVED" and world.transitions == ["IN_TRANSIT"]
        sent = maersk_sim.STATE.requests[0]["body"]
        assert sent["remarks"].startswith("PortLess deal DEAL-F1")
        assert [loc["location"]["UNLocationCode"] for loc in sent["shipmentLocations"]] == ["INMAA", "AEJEA"]
        record = await get_repository().get(tpo.tpo_id)
        assert freight_status.compute("IN_TRANSIT", [record])[0] == FreightStatus.BOOKING_REQUESTED

        # Maersk confirms — the containers stuffed under supervision are on the booking.
        await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/confirm")
        out = await freight.sync_deal("DEAL-F1")
        record = await get_repository().get(tpo.tpo_id)
        assert out["booking_status"] == "CONFIRMED" and record.vessel == "MAERSK SANDBOX" and record.eta
        assert [c["equipment_reference"] for c in record.containers] == ["MSKU1111111", "MSKU2222222"]
        assert {c.get("seal") for c in record.containers} == {"S-1", "S-2"}
        assert EventTopic.FREIGHT_BOOKING_UPDATED in _topics(world)
        vgm_gate = next(g for g in world.gates if g["action"] == "SUBMIT_VGM")
        boxes = vgm_gate["payload"]["containers"]
        assert boxes[0]["cargo_kg"] == 25000.0 and boxes[0]["verified_gross_mass_kg"] == 25000.0 + boxes[0]["tare_kg"]
        assert "solas_declaration" in vgm_gate["flags"]
        bl_gate = next(g for g in world.gates if g["action"] == "APPROVE_TRANSPORT_DOCUMENT")
        assert bl_gate["payload"]["transport_document_reference"] == tpo.ext_ref
        assert freight_status.compute("IN_TRANSIT", [record])[0] == FreightStatus.DOCS_PENDING

        # The operator approves both gates (the callbacks run).
        await freight.submit_vgm(tpo.tpo_id, boxes)
        assert tpo.ext_ref in maersk_sim.STATE.vgm
        await freight.approve_transport_document(tpo.tpo_id)
        assert maersk_sim.STATE.documents[tpo.ext_ref]["transportDocumentStatus"] == "APPROVED"
        await carrier_sim.control(f"/maersk/_sandbox/transport-documents/{tpo.ext_ref}/issue")
        await freight.sync_deal("DEAL-F1")
        issued = next(p for t, p in world.events if t == EventTopic.TRANSPORT_DOCUMENT_ISSUED)
        assert issued["bl_number"] == tpo.ext_ref and issued["seal_numbers"] == ["S-1", "S-2"]
        record = await get_repository().get(tpo.tpo_id)
        assert record.vgm_status == "SUBMITTED" and record.ebl_status == "ISSUED"
        assert freight_status.compute("IN_TRANSIT", [record])[0] == FreightStatus.BL_ISSUED

        # The voyage.
        await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/advance", {"to": "DEPA"})
        await freight.sync_deal("DEAL-F1")
        record = await get_repository().get(tpo.tpo_id)
        assert record.last_milestone == "DEPARTED"
        assert freight_status.compute("IN_TRANSIT", [record])[0] == FreightStatus.DEPARTED
        await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/advance", {"to": "ARRI"})
        await freight.sync_deal("DEAL-F1")
        assert world.transitions[-1] == "CLEARED"  # arrival clears the deal, as before
        await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/advance", {"to": "GTOT"})
        await freight.sync_deal("DEAL-F1")
        record = await get_repository().get(tpo.tpo_id)
        assert record.last_milestone == "DELIVERED" and record.delivered_at is not None
        delivered = [p for t, p in world.events if t == EventTopic.FREIGHT_MILESTONE and p["milestone"] == "DELIVERED"]
        assert delivered  # escrow opens the acceptance window on this
        assert freight_status.compute("CLEARED", [record])[0] == FreightStatus.DELIVERED

        view = await freight.shipment_view("DEAL-F1")
        assert view["booking"]["booking_reference"] == tpo.ext_ref and view["booking"]["ebl_status"] == "ISSUED"
        assert len(view["containers"]) == 2 and [e["code"] for e in view["events"]][:3] == ["GTIN", "GTIN", "LOAD"]

    async def test_a_rejected_booking_is_an_exception(self, world):
        maersk_sim.STATE.auto = False
        tpo = await _book(world)
        await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/reject")
        await freight.sync_deal("DEAL-F1")
        record = await get_repository().get(tpo.tpo_id)
        assert record.status == "FAILED" and record.booking_status == "REJECTED"
        assert any(t == EventTopic.DEAL_EXCEPTION and p["action_required"] == "rebook_freight" for t, p in world.events)
        assert freight_status.compute("IN_TRANSIT", [record])[0] == FreightStatus.BOOKING_REJECTED

    async def test_vgm_waits_for_the_loaded_quantity(self, world):
        with patch.object(freight, "_loading", new=AsyncMock(return_value=None)):
            await _book(world)
        assert not [g for g in world.gates if g["action"] == "SUBMIT_VGM"]

    async def test_the_simulator_refuses_a_vgm_for_a_foreign_container(self, world):
        maersk_sim.STATE.auto = False
        tpo = await _book(world)
        await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/confirm")
        await freight.sync_deal("DEAL-F1")
        with pytest.raises(sl.ConnectorCallError, match="not on this booking"):
            await freight.submit_vgm(tpo.tpo_id, [{"equipment_reference": "XXXU0000000", "verified_gross_mass_kg": 1}])

    async def test_maersk_webhook_rereads_the_booking(self, world, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.shield_link.router import router
        monkeypatch.setenv("MAERSK_WEBHOOK_SECRET", "w-1")
        maersk_sim.STATE.auto = False
        tpo = await _book(world)
        await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/confirm")
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        body = {"carrierBookingReference": tpo.ext_ref, "bookingStatus": "CONFIRMED"}
        assert client.post("/shield-link/carrier/maersk/webhook?token=nope", json=body).status_code == 401
        ok = client.post("/shield-link/carrier/maersk/webhook?token=w-1", json=body)
        assert ok.status_code == 200 and ok.json()["booking_status"] == "CONFIRMED"
        assert client.get("/shield-link/deals/DEAL-F1/shipment").json()["booking"]["booking_status"] == "CONFIRMED"

    async def test_forwarder_bookings_keep_the_old_path(self, world):
        world.deal = _deal()
        tpo = await sl.book_freight(FreightBookingRequest(deal_id="DEAL-F1", provider="cogoport"))
        assert tpo.provider == "cogoport" and (await freight.sync_deal("DEAL-F1"))["status"] == "no_booking"


# ---------------------------------------------------------------------------
# Inspection phases
# ---------------------------------------------------------------------------

class TestInspectionPhases:
    def test_the_buyers_words(self):
        assert inspection_plan.detect_types("We also need a DUPRO and a pre-shipment inspection.") == ["DUPRO", "PSI"]
        assert inspection_plan.detect_types("Please add a pre-customs clearance inspection") == ["PSI", "PEO"]
        assert inspection_plan.detect_types("No DUPRO needed, PSI is enough") == ["PSI"]
        assert inspection_plan.detect_types("Price is fine") is None
        assert inspection_plan.detect_types("ok\n> PortLess can arrange DUPRO and PEO") is None
        assert inspection_plan.normalise(["peo", "dupro"]) == ["DUPRO", "PSI", "PEO"]

    def test_dupro_date_sits_inside_production(self):
        today = date(2026, 11, 11)
        ready = today + timedelta(days=30)
        d = inspection_plan.dupro_date(ready, on=today)
        assert d is not None and today < d < ready and (ready - d).days >= 3
        assert inspection_plan.dupro_date(today + timedelta(days=3), on=today) is None  # too late for a DUPRO

    def test_qima_service_type_per_phase(self, monkeypatch):
        from shared.connectors.qc_providers import ConnectorError as QcConnectorError
        from shared.connectors.qc_providers import QimaConnector
        q = QimaConnector()
        assert (q.service_type("PSI"), q.service_type("DUPRO")) == ("1", "2")
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        with pytest.raises(QcConnectorError, match="QIMA_SERVICE_TYPE_PEO"):
            QimaConnector().service_type("PEO")
        monkeypatch.setenv("QIMA_SERVICE_TYPE_PEO", "17")
        assert QimaConnector().service_type("PEO") == "17"

    def test_qima_order_for_a_dupro(self, monkeypatch):
        from shared.connectors.qc_providers import QimaConnector
        monkeypatch.setenv("QC_SANDBOX_PROVIDERS", "qima")
        order = QimaConnector().build_inspection_order({
            "deal_id": "D", "tpo_id": "T", "inspection_type": "DUPRO", "expected_finished_pct": 30,
            "quantity_mt": 50, "hs_code": "1006.30", "note": "PortLess deal D · PO P", "services": ["DURING_PRODUCTION_INSPECTION"],
        }, "u-1")
        product = order["products"][0]
        assert order["serviceType"] == "2" and product["expectedFinishedPct"] == 30 and product["expectedPackedPct"] == 0
        assert "PortLess deal D" in product["sampleReferenceClientComments"] and order["customField3"].startswith("DUPRO")

    async def test_a_dupro_never_moves_the_deal(self, monkeypatch):
        deal = _deal("PO_ISSUED")
        transition = AsyncMock()
        with (patch.object(sl, "_fetch_deal", new=AsyncMock(return_value=deal)),
              patch.object(sl, "_transition_deal", new=transition), patch.object(sl, "_emit", new=AsyncMock())):
            tpo = await sl.book_qc(QcBookingRequest(deal_id="DEAL-F1", inspection_type="DUPRO", quantity_mt=50,
                                                    goods_ready_date=(qc_today() + timedelta(days=30)).isoformat()))
        record = await get_repository().get(tpo.tpo_id)
        assert record.inspection_type == "DUPRO" and transition.await_count == 0
        assert record.note.startswith("PortLess deal DEAL-F1")
        assert tpo.inspection_type == "DUPRO"

    async def test_peo_needs_a_passed_psi(self):
        with (patch.object(sl, "_fetch_deal", new=AsyncMock(return_value=_deal("PO_ISSUED"))),
              pytest.raises(sl.InvalidDealState, match="QC_PASSED")):
            await sl.book_qc(QcBookingRequest(deal_id="DEAL-F1", inspection_type="PEO"))

    async def test_phase_result_is_its_own_event(self):
        from datetime import UTC, datetime

        from services.shield_link import qc
        from services.shield_link.schemas import ThirdPartyOrderRecord
        now = datetime.now(UTC)
        tpo = ThirdPartyOrderRecord(tpo_id="TPO-D", deal_id="DEAL-F1", order_type="QC", provider="qima",
                                    status="BOOKED", inspection_type="DUPRO", created_at=now, updated_at=now)
        await get_repository().save(tpo)
        events, transition = [], AsyncMock()

        async def _emit(topic, deal_id, payload):
            events.append((topic, payload))

        with patch.object(sl, "_emit", new=_emit), patch.object(sl, "_transition_deal", new=transition):
            out = await qc.apply_update(tpo, source="webhook", result="FAIL", notes="moisture 15%")
        assert out["inspection_type"] == "DUPRO" and transition.await_count == 0
        assert events[0][0] == EventTopic.QC_PHASE_RESULT and events[0][1]["result"] == "FAIL"

    def test_phase_lines(self):
        from datetime import UTC, datetime

        from services.shield_link.schemas import ThirdPartyOrderRecord
        from shared.orchestration.qc_status import phase_lines
        now = datetime.now(UTC)
        orders = [ThirdPartyOrderRecord(tpo_id="A", deal_id="D", order_type="QC", provider="qima", status="COMPLETED",
                                        inspection_type="DUPRO", qc_result="PASS", created_at=now, updated_at=now),
                  ThirdPartyOrderRecord(tpo_id="B", deal_id="D", order_type="QC", provider="qima", status="BOOKED",
                                        inspection_type="PSI", provider_status="CONFIRMED", created_at=now, updated_at=now)]
        lines = {x["type"]: x["status"] for x in phase_lines(["DUPRO", "PSI", "PEO"], orders)}
        assert lines == {"DUPRO": "PASSED", "PSI": "SCHEDULED", "PEO": "PLANNED"}

    async def test_continuation_books_dupro_before_psi_and_peo_holds_customs(self, monkeypatch):
        from shared.orchestration import continuations as c
        booked = []

        async def _book_qc(req, **kw):
            booked.append(req.inspection_type)
            return SimpleNamespace(tpo_id=f"T-{req.inspection_type}", provider="qima")

        ready = (qc_today() + timedelta(days=30)).isoformat()
        with (patch("services.shield_link.service.book_qc", new=_book_qc),
              patch.object(inspection_plan, "plan_for", new=AsyncMock(return_value=["DUPRO", "PSI", "PEO"])),
              patch("shared.orchestration.supplier_commitment.get",
                    new=AsyncMock(return_value=SimpleNamespace(qc_ready_date=ready))),
              patch.object(c, "_negotiation_numbers", new=AsyncMock(return_value=(None, 50.0)))):
            await c.book_qc_for_deal("DEAL-F1", _deal("PO_ISSUED"))
            assert booked == ["DUPRO", "PSI"]
            assert await c._peo_cleared("DEAL-F1", _deal("IN_TRANSIT")) is False
            assert booked[-1] == "PEO"


# ---------------------------------------------------------------------------
# Trade brief + order-confirmed e-mails
# ---------------------------------------------------------------------------

class TestBriefAndNotices:
    def test_notes_by_audience(self):
        b = {"deal_id": "D1", "pi_id": "PI-1", "po_id": "PO-1", "product": "Parboiled rice", "hs_code": "1006.30",
             "quantity_mt": 50, "incoterm": "CIF", "port_of_loading": "Chennai, IN", "port_of_destination": "Jebel Ali, AE",
             "inspection_types": ["DUPRO", "PSI"], "inspection_agency": "qima", "carrier": "maersk",
             "carrier_booking": "254", "escrow_provider": "tazapay", "desk": "ops@portless.example"}
        carrier = trade_brief.note(b, "carrier")
        assert "PO PO-1" in carrier and "PI-1" not in carrier and "Chennai, IN → Jebel Ali, AE" in carrier
        assert "supervised" in carrier and "contact ops@portless.example" in carrier
        inspector = trade_brief.note(b, "inspector")
        assert "inspection DUPRO/PSI by QIMA" in inspector and "Jebel Ali" not in inspector
        escrow = trade_brief.note(b, "escrow")
        assert "PI PI-1" in escrow and "PO-1" not in escrow and "Maersk booking 254" in escrow
        payout = trade_brief.note(b, "payout")
        assert "PO PO-1" in payout and "PI-1" not in payout and "CIF" not in payout
        assert trade_brief.unlocode("Jebel Ali, AE") == "AEJEA" and trade_brief.unlocode("Nhava Sheva, IN") == "INNSA"
        assert trade_brief.unlocode("Nowhere") is None and trade_brief.unlocode("INMAA") == "INMAA"

    def test_order_confirmed_buyer_and_supplier_each_see_only_their_side(self):
        from shared.orchestration.notifications import (
            _tpl_order_confirmed_buyer,
            _tpl_order_confirmed_supplier,
        )
        pi = SimpleNamespace(pi_id="PI-1", buyer_name="Gulf Foods LLC", quantity_mt=50, agreed_price=640.0,
                             currency="USD", incoterm="CIF", total_value=32000.0, port_of_destination="Jebel Ali, AE",
                             escrow_provider="escrow_com")
        esc = SimpleNamespace(provider="escrow_com", escrow_id="ESC-1", checkout_url=None,
                              tranches=[SimpleNamespace(pct=0.3, trigger="PO_ISSUED")])
        common = {"deal_id": "D1", "product": "Parboiled rice", "hs_code": "1006.30",
                  "inspection_types": ["DUPRO", "PSI", "PEO"], "agency": "QIMA", "carrier": "Maersk",
                  "port_of_loading": "Chennai, IN"}
        _s, buyer = _tpl_order_confirmed_buyer({**common, "pi": pi, "escrow": esc})
        for phrase in ("PI-1", "640.00", "Escrow.com", "During Production Inspection (DUPRO) by QIMA",
                       "Pre-Customs Clearance Inspection (PEO)", "Maersk", "marine insurance", "Fund the escrow"):
            assert phrase in buyer, phrase
        assert "Kaveri" not in buyer and "590" not in buyer
        _s, supplier = _tpl_order_confirmed_supplier({**common, "quantity_mt": 50, "supplier_price": 590.0,
                                                      "supplier_name": "Kaveri Mills"})
        for phrase in ("Kaveri Mills", "590.00", "FOB Chennai, IN", "Pre-Shipment Inspection (PSI) by QIMA",
                       "Maersk", "goods-ready date"):
            assert phrase in supplier, phrase
        assert "Gulf" not in supplier and "640" not in supplier and "Jebel Ali" not in supplier

    async def test_order_confirmed_goes_to_both_parties(self):
        from shared.orchestration import notifications
        sent = []

        async def _party(deal_id, party, kind, ctx, **kw):
            sent.append((party, kind))
            return f"E-{party}"

        deal = SimpleNamespace(deal_id="D1", hs_code="1006.30", parties=[])
        with (patch.object(notifications, "notify_party", new=_party),
              patch("shared.orchestration.trade_brief.brief", new=AsyncMock(return_value={"deal_id": "D1"})),
              patch("shared.orchestration.continuations._supplier_port", new=AsyncMock(return_value="Chennai, IN"))):
            out = await notifications.notify_order_confirmed(deal, SimpleNamespace(quantity_mt=50), None)
        assert out == ["E-buyer", "E-supplier"]
        assert sent == [("buyer", "order_confirmed_buyer"), ("supplier", "order_confirmed_supplier")]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class TestWiring:
    def test_automation_runs_maersk_on_the_simulator(self, monkeypatch):
        from services.automation import safety
        env = safety.worker_env()
        assert env["CARRIER_SANDBOX_PROVIDERS"] == "maersk" and env["MAERSK_CONSUMER_KEY"] == ""
        monkeypatch.setenv("MONGO_DB_NAME", safety.AUTOMATION_DB)
        monkeypatch.setenv("AUTOMATION_WORKER", "1")
        for key in safety._MUST_BE_BLANK:
            monkeypatch.setenv(key, "")
        monkeypatch.setenv("MAERSK_CLIENT_SECRET", "real")
        with pytest.raises(SystemExit, match="MAERSK_CLIENT_SECRET"):
            safety.assert_isolated()

    def test_live_mode_never_uses_the_simulator(self, monkeypatch):
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("CARRIER_SANDBOX_PROVIDERS", "maersk")
        assert not maersk_mod.in_simulator() and MaerskConnector().stubbed is False

    def test_freight_gates_resolve_and_call_back(self):
        from services.hitl_approval.callbacks import _CALLBACK_MAP
        from shared.orchestration.autonomy_policy import resolve_gate
        assert {"SUBMIT_VGM", "APPROVE_TRANSPORT_DOCUMENT"} <= set(_CALLBACK_MAP)
        assert resolve_gate("SUBMIT_VGM").level == resolve_gate("APPROVE_TRANSPORT_DOCUMENT").level == "L0"

    def test_auth_lets_the_carrier_webhook_through_to_its_token_check(self):
        from shared.auth.middleware import _PROVIDER_WEBHOOK_PATH_SUFFIXES
        assert "/shield-link/carrier/maersk/webhook" in _PROVIDER_WEBHOOK_PATH_SUFFIXES

    def test_basic_token_form_is_what_the_simulator_expects(self):
        assert base64
