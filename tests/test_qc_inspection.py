"""QC inspection with QIMA (API), SGS SMART and Bureau Veritas (portal).

Coverage:
  • services/qc_sandbox — the QIMA Public API contract (auth, booking 200/202/
    400/409, async booking, order search / detail, cancel, report search,
    pdf-info, file download) and the SGS / BV portal simulation.
  • shared/connectors/qc_providers — QimaConnector request mapping (documented
    headers, MD5 password, dd-MMM-yyyy dates, InspectionOrder fields), status
    mapping, report result, documents, token renewal, duplicate booking;
    portal connectors for SGS and BV.
  • services/shield_link — booking on the supplier's goods-ready date, polling,
    webhook and operator status updates, report storage, PASS / FAIL / PENDING,
    idempotency, reschedule, the deal QC view and routes.
  • shared/orchestration — qc_schedule (dates, ready-date reading),
    supplier_commitment (goods-ready date from the PO acknowledgement / a later
    reply / an operator), continuations (PO → readiness request), notices,
    deal watcher poll + reminder timing.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from services.qc_sandbox import main as sandbox_main
from services.qc_sandbox import qima as sbx
from shared.connectors import qc_providers as qc
from shared.orchestration import qc_schedule

TODAY = date(2026, 11, 11)  # a Wednesday


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    sandbox_main.reset()
    qc.reset_qima_sessions()
    for var in ("QIMA_API_ACCESS_TOKEN", "QIMA_API_KEY", "QIMA_ACCOUNT", "QIMA_PASSWORD", "QIMA_PASSWORD_MD5",
                "QIMA_PRODUCT_CATEGORY_ID", "QIMA_PRODUCT_FAMILY_ID", "QIMA_PRODUCT_TYPE_ID", "QC_SANDBOX_URL",
                "SGS_BOOKING_EMAIL", "BUREAU_VERITAS_BOOKING_EMAIL", "SGS_WEBHOOK_SECRET", "BUREAU_VERITAS_WEBHOOK_SECRET"):
        monkeypatch.setenv(var, "")  # blank, not deleted: a later load_dotenv must not refill it from .env
    monkeypatch.setenv("SHIELD_LINK_STORAGE_DIR", str(tmp_path / "qc-docs"))
    import services.shield_link.repository as repo_mod
    from services.shield_link.repository import InMemoryShieldLinkRepository
    repo_mod._repository = InMemoryShieldLinkRepository()
    yield
    repo_mod._repository = None


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setenv("QC_SANDBOX_PROVIDERS", "qima,sgs,bv")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox_main.app), base_url="http://qc-sandbox.local")


def _session_headers(token: str, user_id: str = sbx.SANDBOX_USER_ID) -> dict[str, str]:
    return {"Ai-Api-Access-Token": sbx.SANDBOX_ACCESS_TOKEN, "Ai-User-Id": user_id,
            "Authorization": f"Bearer {token}", "Referer": sbx.REFERER}


async def _login(client: httpx.AsyncClient) -> str:
    resp = await client.post("/auth/v2/token", headers={"Ai-Api-Access-Token": sbx.SANDBOX_ACCESS_TOKEN, "Referer": sbx.REFERER},
                             json={"account": sbx.SANDBOX_ACCOUNT, "password": sbx.md5_hex(sbx.SANDBOX_PASSWORD), "userType": "client"})
    assert resp.status_code == 200
    return resp.json()["content"]["token"]["token"]


def _booking(**over) -> dict:
    body = {
        "userId": sbx.SANDBOX_USER_ID, "serviceDate": "18-DEC-2030", "shipDate": "20-DEC-2030", "serviceType": "1",
        "bookingType": "General", "referenceNumber": "PO-1", "supplierName": "Supplier A", "supplierContactName": "Manager A",
        "supplierCountryID": "India", "supplierAddress": "Plot 1, Kakinada, Andhra Pradesh",
        "products": [{"name": "Rice", "categoryId": "bigCat3", "familyId": "bigCat3_s1", "typeId": "bigCat3_s1_t200",
                      "refs": [{"poNumber": "PO-1", "skuCode": "10063020", "qty": 20}]}],
    }
    body.update(over)
    return body


def _deal(status: str = "PO_ISSUED", deal_id: str = "DEAL-QC-1"):
    supplier = SimpleNamespace(role="supplier", legal_name="Godavari Rice Mills", email="sales@godavari.invalid",
                               phone="+91 90000 00000", address="Plot 4, Industrial Estate, Kakinada, 533003",
                               country="IN")
    return SimpleNamespace(deal_id=deal_id, status=SimpleNamespace(value=status), hs_code="10063020", parties=[supplier])


@pytest.fixture
def wired_service(monkeypatch):
    """Shield-Link with a fake deal: transitions and events recorded."""
    from services.shield_link import service as sl
    state = SimpleNamespace(deal=_deal(), transitions=[], events=[])

    async def fetch(_deal_id):
        return state.deal

    async def transition(deal_id, target, reason):
        state.transitions.append(target)
        state.deal.status = SimpleNamespace(value=target)

    async def emit(topic, deal_id, payload):
        state.events.append((getattr(topic, "value", topic), payload))

    monkeypatch.setattr(sl, "_fetch_deal", fetch)
    monkeypatch.setattr(sl, "_transition_deal", transition)
    monkeypatch.setattr(sl, "_emit", emit)
    monkeypatch.setattr(sl, "_po_number", AsyncMock(return_value="PO-DEAL-QC-1"))
    return state


def _topics(state) -> list[str]:
    return [t for t, _ in state.events]


# ---------------------------------------------------------------------------
# QIMA sandbox — the published contract
# ---------------------------------------------------------------------------

class TestQimaSandboxContract:
    async def test_token_needs_the_access_token_and_the_md5_password(self, sandbox):
        bad_app = await sandbox.post("/auth/v2/token", headers={"Ai-Api-Access-Token": "nope", "Referer": sbx.REFERER},
                                     json={"account": sbx.SANDBOX_ACCOUNT, "password": sbx.md5_hex(sbx.SANDBOX_PASSWORD), "userType": "client"})
        assert bad_app.status_code == 403
        plain = await sandbox.post("/auth/v2/token", headers={"Ai-Api-Access-Token": sbx.SANDBOX_ACCESS_TOKEN, "Referer": sbx.REFERER},
                                   json={"account": sbx.SANDBOX_ACCOUNT, "password": sbx.SANDBOX_PASSWORD, "userType": "client"})
        assert plain.status_code == 401, "a plain-text password is refused"
        ok = await sandbox.post("/auth/v2/token", headers={"Ai-Api-Access-Token": sbx.SANDBOX_ACCESS_TOKEN, "Referer": sbx.REFERER},
                                json={"account": sbx.SANDBOX_ACCOUNT, "password": sbx.md5_hex(sbx.SANDBOX_PASSWORD), "userType": "client"})
        token = ok.json()["content"]["token"]
        assert set(token) >= {"id", "userId", "userType", "token", "validBefore", "refreshKey", "loginType"}
        assert ok.json()["message"] is None and int(token["validBefore"]) > 0

    async def test_refresh_and_revoke(self, sandbox):
        login = await sandbox.post("/auth/v2/token", headers={"Ai-Api-Access-Token": sbx.SANDBOX_ACCESS_TOKEN, "Referer": sbx.REFERER},
                                   json={"account": sbx.SANDBOX_ACCOUNT, "password": sbx.md5_hex(sbx.SANDBOX_PASSWORD), "userType": "client"})
        tok = login.json()["content"]["token"]
        headers = {"Ai-Api-Access-Token": sbx.SANDBOX_ACCESS_TOKEN, "Authorization": f"Bearer {tok['token']}",
                   "Ai-Api-Refresh-Key": tok["refreshKey"], "Referer": sbx.REFERER}
        assert (await sandbox.put("/auth/v2/token", headers={**headers, "Ai-Api-Refresh-Key": "wrong"})).status_code == 401
        renewed = (await sandbox.put("/auth/v2/token", headers=headers)).json()["content"]["token"]
        assert renewed["token"] != tok["token"]
        revoke = {**headers, "Authorization": f"Bearer {renewed['token']}", "Ai-Api-Refresh-Key": renewed["refreshKey"]}
        assert (await sandbox.delete("/auth/v2/token", headers=revoke)).json()["content"] is True

    async def test_every_call_needs_the_four_headers(self, sandbox):
        token = await _login(sandbox)
        h = _session_headers(token)
        assert (await sandbox.post("/v1.0/inspection", json=_booking(), headers={**h, "Ai-User-Id": "someone"})).status_code == 401
        assert (await sandbox.post("/v1.0/inspection", json=_booking(), headers={**h, "Authorization": "Bearer x"})).status_code == 401
        assert (await sandbox.post("/v1.0/inspection", json=_booking(), headers={**h, "Referer": "x"})).status_code == 403

    async def test_booking_without_a_factory_is_202_with_the_documented_message(self, sandbox):
        token = await _login(sandbox)
        resp = await sandbox.post("/v1.0/inspection", json=_booking(), headers=_session_headers(token))
        assert resp.status_code == 202
        body = resp.json()
        assert body["message"] == "Factory not provided ,factory confirm skipped"
        gi = body["content"]["orderGeneralInfo"]
        assert gi["status"] == "20" and gi["statusText"] == "WAITING_FOR_VALIDATION"
        assert gi["serviceTypeText"] == "PSI" and gi["orderNumber"].startswith("Q26") and gi["orderNumber"].endswith("-PP")
        assert gi["expectedInspectionDate"] == "18-Dec-2030" and body["content"]["orderFactory"] is None
        assert body["content"]["products"][0]["productBean"]["prodQuantity"] == 20

    async def test_booking_with_a_factory_is_200(self, sandbox):
        token = await _login(sandbox)
        resp = await sandbox.post("/v1.0/inspection", json=_booking(factoryName="Mill 1", referenceNumber="PO-2"),
                                  headers=_session_headers(token))
        assert resp.status_code == 200 and resp.json()["content"]["orderFactory"]["factoryName"] == "Mill 1"

    @pytest.mark.parametrize(("over", "fragment"), [
        ({"serviceDate": "2030-12-18"}, "dd-MMM-yyyy"),
        ({"products": []}, "At least one product"),
        ({"serviceType": "99"}, "serviceType"),
        ({"supplierName": None}, "supplier info"),
        ({"userId": "other"}, "userId"),
    ])
    async def test_booking_validation_is_400(self, sandbox, over, fragment):
        token = await _login(sandbox)
        resp = await sandbox.post("/v1.0/inspection", json=_booking(**over), headers=_session_headers(token))
        assert resp.status_code == 400 and fragment in resp.json()["message"]

    async def test_duplicate_booking_is_409(self, sandbox):
        token = await _login(sandbox)
        h = _session_headers(token)
        assert (await sandbox.post("/v1.0/inspection", json=_booking(), headers=h)).status_code == 202
        dup = await sandbox.post("/v1.0/inspection", json=_booking(), headers=h)
        assert dup.status_code == 409 and dup.json()["message"] == "Duplicate booking request found"

    async def test_async_booking_is_polled_until_succeed(self, sandbox):
        token = await _login(sandbox)
        h = _session_headers(token)
        accepted = await sandbox.post("/v1.0/async-inspection", json=_booking(referenceNumber="PO-A"), headers=h)
        request_id = accepted.json()["content"]
        assert "booking-result" in accepted.json()["message"]
        first = (await sandbox.get(f"/booking-result/{request_id}", headers=h)).json()["content"]
        assert first["status"] == "PENDING" and first["orderId"] is None
        second = (await sandbox.get(f"/booking-result/{request_id}", headers=h)).json()["content"]
        assert second["status"] == "SUCCEED" and second["orderId"] in sbx.STATE.orders

    async def test_async_booking_failure_names_the_reason(self, sandbox):
        token = await _login(sandbox)
        h = _session_headers(token)
        rid = (await sandbox.post("/v1.0/async-inspection", json=_booking(products=[]), headers=h)).json()["content"]
        await sandbox.get(f"/booking-result/{rid}", headers=h)
        res = (await sandbox.get(f"/booking-result/{rid}", headers=h)).json()["content"]
        assert res["status"] == "FAILED" and "product" in res["comments"]

    async def test_search_detail_cancel(self, sandbox):
        token = await _login(sandbox)
        h = _session_headers(token)
        order_id = (await sandbox.post("/v1.0/inspection", json=_booking(), headers=h)).json()["content"]["orderGeneralInfo"]["orderId"]
        uid = sbx.SANDBOX_USER_ID
        found = (await sandbox.get(f"/user/{uid}/inspection-orders", params={"keyword": "PO-1", "service-type": "psi"}, headers=h)).json()
        assert found["content"]["totalSize"] == 1 and found["content"]["pageItems"][0]["orderId"] == order_id
        assert (await sandbox.get(f"/user/{uid}/inspection-orders", params={"status": "60"}, headers=h)).json()["content"]["totalSize"] == 0
        detail = (await sandbox.get(f"/user/{uid}/inspection-order/{order_id}", headers=h)).json()["content"]
        assert detail["orderPrice"]["inspectionChargeType"] == "MAN_DAY" and detail["orderExtra"]["cancelable"] is True
        missing = await sandbox.get(f"/user/{uid}/inspection-order/NOPE", headers=h)
        assert missing.status_code == 500 and missing.json()["message"] == "PSI_0001:The order is not existing!"
        assert (await sandbox.delete(f"/user/{uid}/inspection/{order_id}", headers=h)).status_code == 400, "reason is required"
        assert (await sandbox.delete(f"/user/{uid}/inspection/{order_id}", params={"reason": "request by client"}, headers=h)).json()["content"] is True
        assert (await sandbox.delete(f"/user/{uid}/inspection/{order_id}", params={"reason": "again"}, headers=h)).status_code == 500

    async def test_inspection_reports_and_pdf(self, sandbox):
        token = await _login(sandbox)
        h = _session_headers(token)
        uid = sbx.SANDBOX_USER_ID
        order = (await sandbox.post("/v1.0/inspection", json=_booking(), headers=h)).json()["content"]["orderGeneralInfo"]
        assert (await sandbox.get(f"/user/{uid}/reports", params={"keyword": order["orderNumber"]}, headers=h)).json()["content"]["totalSize"] == 0
        await sandbox.post(f"/_sandbox/qima/orders/{order['orderId']}/inspect", json={"result": "FAIL", "major_found": 9})
        reports = (await sandbox.get(f"/user/{uid}/reports", params={"keyword": order["orderNumber"]}, headers=h)).json()["content"]["pageItems"]
        assert reports[0]["overrallResult"] == "FAILED" and reports[0]["status"] == 30 and reports[0]["majorFound"] == 9
        assert isinstance(reports[0]["inspectionDate"], int), "report dates are Unix milliseconds"
        pid = reports[0]["productId"]
        info = (await sandbox.get(f"/user/{uid}/report/{pid}/pdf-info", headers=h)).json()["content"]
        assert info[0]["fileType"] == "PROD_FINAL_REPORT" and info[0]["srcId"] == pid
        pdf = await sandbox.get(f"/user/{uid}/file/{info[0]['id']}", headers=h)
        assert pdf.content.startswith(b"%PDF-") and pdf.content.rstrip().endswith(b"%%EOF")
        inter = (await sandbox.get(f"/user/{uid}/interactive-report/{pid}", params={"orderId": order["orderId"]}, headers=h)).json()
        assert inter["message"] == "OK" and inter["content"]["overallResult"]["overallResult"] == "Fail"


# ---------------------------------------------------------------------------
# QimaConnector
# ---------------------------------------------------------------------------

def _request(**over) -> dict:
    req = {
        "deal_id": "DEAL-QC-1", "tpo_id": "TPO-QC-1", "po_number": "PO-DEAL-QC-1", "inspection_date": "2026-11-20",
        "ship_date": "2026-11-27", "hs_code": "10063020", "product_name": "Milled rice", "quantity_mt": 20.0,
        "supplier": {"name": "Godavari Rice Mills", "email": "sales@godavari.invalid", "phone": "+91 9",
                     "address": "Plot 4, Kakinada, 533003", "city": "Kakinada", "country_code": "IN"},
        "services": ["PRE_SHIPMENT_INSPECTION", "LOADING_SUPERVISION"], "min_packed_pct": 80,
    }
    req.update(over)
    return req


class TestQimaConnector:
    def test_status_mapping(self):
        m = qc.map_qima_order_status
        assert m("20", "WAITING_FOR_VALIDATION", False) == qc.REQUESTED
        assert m("20", "WAITING_FOR_VALIDATION", True) == qc.REQUESTED  # the code wins: QIMA has not validated
        assert m("30", "VALIDATED", True) == qc.CONFIRMED
        assert m("45", "Waiting for inspection", True) == qc.CONFIRMED
        assert m("60", "", False) == qc.INSPECTED
        assert m("70", "Report Sent", False) == qc.INSPECTED
        assert m("90", "CANCELLED", False) == qc.CANCELLED
        assert m("", "", False) == qc.REQUESTED

    def test_result_from_reports(self):
        assert qc.qima_result([]) is None
        assert qc.qima_result([{"status": 20, "overrallResult": "PASSED"}]) is None, "not diffused yet"
        assert qc.qima_result([{"status": 30, "overrallResult": "PASSED"}, {"status": 40, "overrallResult": "PASSED"}]) == "PASS"
        assert qc.qima_result([{"status": 30, "overrallResult": "PASSED"}, {"status": 30, "overrallResult": "FAILED"}]) == "FAIL"
        assert qc.qima_result([{"status": 30, "overrallResult": "PENDING"}]) == "PENDING"

    async def test_unconfigured_books_a_stub(self):
        result = await qc.QimaConnector().book(_request())
        assert result["stub"] is True and result["channel"] == "stub" and result["inspection_date"] == "2026-11-20"
        assert qc.QimaConnector().pollable is False

    async def test_booking_maps_onto_the_documented_inspection_order(self, sandbox):
        result = await qc.QimaConnector().book(_request())
        body = result["request"]
        assert body["serviceDate"] == "20-Nov-2026" and body["shipDate"] == "27-Nov-2026" and body["serviceType"] == "1"
        assert body["supplierCountryID"] == "India" and body["supplierContinentID"] == "Asia"
        assert body["supplierName"] == "Godavari Rice Mills" and body["supplierContactEmail"] == "sales@godavari.invalid"
        product = body["products"][0]
        assert product["refs"] == [{"poNumber": "PO-DEAL-QC-1", "skuCode": "10063020", "qty": 20}]
        assert product["expectedFinishedPct"] == 100 and product["expectedPackedPct"] == 80
        assert product["checkList"] == {"sampleLevel": "II", "aqlCritical": "0", "aqlMajor": "2.5", "aqlMinor": "4.0"}
        assert body["customField1"] == "DEAL-QC-1" and body["customField2"] == "TPO-QC-1"
        assert "LOADING_SUPERVISION" in body["customField3"]
        assert result["ext_ref"] in sbx.STATE.orders and result["provider_order_number"].startswith("Q26")
        assert result["provider_status"] == qc.REQUESTED and result["http_status"] == 202
        assert result["inspection_date"] == "2026-11-20" and result["channel"] == "sandbox"

    async def test_live_calls_send_the_documented_headers_and_md5_password(self, monkeypatch):
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("QIMA_API_ACCESS_TOKEN", "app-token")
        monkeypatch.setenv("QIMA_ACCOUNT", "portless")
        monkeypatch.setenv("QIMA_PASSWORD", "secret")
        monkeypatch.setenv("QIMA_PRODUCT_CATEGORY_ID", "bigCat9")
        monkeypatch.setenv("QIMA_PRODUCT_FAMILY_ID", "bigCat9_s1")
        monkeypatch.setenv("QIMA_PRODUCT_TYPE_ID", "bigCat9_s1_t1")
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            if req.url.path == "/auth/v2/token":
                return httpx.Response(200, json={"message": None, "content": {"token": {
                    "token": "jwt", "userId": "U1", "validBefore": str(int(datetime.now().timestamp()) + 3600), "refreshKey": "r"}}})
            return httpx.Response(202, json={"message": "Factory not provided ,factory confirm skipped", "content": {
                "orderGeneralInfo": {"orderId": "O1", "orderNumber": "Q2600000001-PP", "status": "20",
                                     "statusText": "WAITING_FOR_VALIDATION", "actualInspectionDate": "20-Nov-2026"},
                "orderExtra": {"cancelable": False}, "products": []}})

        connector = qc.QimaConnector()
        monkeypatch.setattr(connector, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://ppapi.qima.com"))
        result = await connector.book(_request())
        login = json.loads(seen[0].content)
        assert login == {"account": "portless", "password": hashlib.md5(b"secret").hexdigest(), "userType": "client"}  # noqa: S324
        assert seen[0].headers["Ai-Api-Access-Token"] == "app-token" and seen[0].headers["Referer"] == "https://preprodmy.qima.com/"
        book = seen[-1]
        assert book.url.path == "/v1.0/inspection" and book.headers["Ai-User-Id"] == "U1"
        assert book.headers["Authorization"] == "Bearer jwt" and book.headers["Ai-Api-Access-Token"] == "app-token"
        assert json.loads(book.content)["products"][0]["categoryId"] == "bigCat9"
        assert result["ext_ref"] == "O1" and result["inspection_date"] == "2026-11-20" and result["channel"] == "api"

    async def test_live_booking_without_a_product_category_is_refused(self, monkeypatch):
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("QIMA_API_ACCESS_TOKEN", "t")
        monkeypatch.setenv("QIMA_ACCOUNT", "a")
        monkeypatch.setenv("QIMA_PASSWORD_MD5", "0" * 32)
        connector = qc.QimaConnector()

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": {"token": {"token": "j", "userId": "U", "validBefore": "9999999999"}}})

        monkeypatch.setattr(connector, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x"))
        with pytest.raises(qc.ConnectorError, match="product-tree"):
            await connector.book(_request())

    async def test_sign_in_failure_is_a_connector_error(self, monkeypatch):
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("QIMA_API_ACCESS_TOKEN", "t")
        monkeypatch.setenv("QIMA_ACCOUNT", "a")
        monkeypatch.setenv("QIMA_PASSWORD", "wrong")
        connector = qc.QimaConnector()
        monkeypatch.setattr(connector, "_client", lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "no"})), base_url="https://x"))
        with pytest.raises(qc.ConnectorError, match="sign-in failed: HTTP 401"):
            await connector.status("O1")

    async def test_a_duplicate_booking_returns_the_existing_order(self, sandbox):
        first = await qc.QimaConnector().book(_request())
        again = await qc.QimaConnector().book(_request())
        assert again["duplicate"] is True and again["ext_ref"] == first["ext_ref"]

    async def test_status_follows_the_order_to_the_report(self, sandbox):
        connector = qc.QimaConnector()
        order = await connector.book(_request())
        oid = order["ext_ref"]
        assert (await connector.status(oid))["provider_status"] == qc.REQUESTED
        await sandbox.post(f"/_sandbox/qima/orders/{oid}/validate", json={"inspection_date": "2026-11-23"})
        confirmed = await connector.status(oid)
        assert confirmed["provider_status"] == qc.CONFIRMED and confirmed["inspection_date"] == "2026-11-23"
        assert confirmed["result"] is None
        await sandbox.post(f"/_sandbox/qima/orders/{oid}/inspect", json={"result": "PASS"})
        done = await connector.status(oid)
        assert done["provider_status"] == qc.REPORT_ISSUED and done["result"] == "PASS"
        assert done["reports"][0]["defects"]["majorFound"] == 0 and done["product_ids"]
        files = await connector.documents(oid, done["product_ids"])
        data, mime = await connector.download(files[0]["file_id"])
        assert data.startswith(b"%PDF-") and mime == "application/pdf" and files[0]["file_type"] == "PROD_FINAL_REPORT"

    async def test_an_expired_token_signs_in_again(self, sandbox):
        connector = qc.QimaConnector()
        order = await connector.book(_request())
        await sandbox.post("/_sandbox/qima/expire-tokens")
        assert (await connector.status(order["ext_ref"]))["ext_ref"] == order["ext_ref"]

    async def test_cancel(self, sandbox):
        connector = qc.QimaConnector()
        order = await connector.book(_request())
        assert (await connector.cancel(order["ext_ref"], "supplier late"))["cancelled"] is True
        assert sbx.STATE.orders[order["ext_ref"]]["cancel_reason"] == "supplier late"
        assert (await connector.status(order["ext_ref"]))["provider_status"] == qc.CANCELLED

    async def test_quote_is_the_catalogue_estimate(self, sandbox):
        quote = await qc.QimaConnector().quote({"quantity_mt": 20})
        assert quote["price_source"] == "catalogue_estimate" and quote["fee_usd"] == 289.0 + 1.5 * 20


# ---------------------------------------------------------------------------
# Portal connectors (SGS SMART, BV OneSource Connect)
# ---------------------------------------------------------------------------

class TestPortalConnectors:
    @pytest.mark.parametrize(("cls", "portal", "prefix"), [
        (qc.SgsConnector, "SGS SMART", "SGS-REQ-"), (qc.BureauVeritasConnector, "BV OneSource Connect", "BV-REQ-")])
    async def test_booking_request_holds_what_the_booking_form_needs(self, cls, portal, prefix, monkeypatch):
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv(f"{cls.env_prefix}_BOOKING_EMAIL", "bookings@desk.invalid")
        result = await cls().book(_request())
        assert result["channel"] == "portal" and result["portal"] == portal and result["ext_ref"].startswith(prefix)
        assert result["provider_status"] == qc.REQUESTED and result["stub"] is False
        req = result["booking_request"]
        assert req["inspection_date"] == "2026-11-20" and req["services"] == ["PRE_SHIPMENT_INSPECTION", "LOADING_SUPERVISION"]
        assert req["inspection_site"]["company"] == "Godavari Rice Mills" and req["product"]["hs_code"] == "10063020"
        assert req["sampling"]["standard"] == "ISO 2859-1" and req["readiness"] == {"produced_pct": 100, "packed_pct_min": 80}

    async def test_portal_status_is_pushed_never_polled(self, monkeypatch):
        connector = qc.SgsConnector()
        assert connector.pollable is False and connector.is_configured() is False
        monkeypatch.setenv("SGS_BOOKING_EMAIL", "bookings@sgs.invalid")
        assert qc.SgsConnector().is_configured() is True
        assert "no status API" in (await connector.status("SGS-REQ-1"))["detail"]
        assert (await connector.cancel("SGS-REQ-1", "x"))["cancelled"] is False

    async def test_sandbox_desk_receives_the_booking(self, sandbox):
        result = await qc.SgsConnector().book(_request())
        desk = (await sandbox.get("/_sandbox/sgs/bookings")).json()["bookings"]
        assert result["ext_ref"] in desk and result["channel"] == "sandbox"

    def test_catalogue_says_how_each_is_reached(self):
        assert qc.QC_PROVIDER_CATALOG["qima"]["channel"] == "api"
        assert qc.QC_PROVIDER_CATALOG["sgs"]["channel"] == qc.QC_PROVIDER_CATALOG["bv"]["channel"] == "portal"
        assert "no public API" in qc.QC_PROVIDER_CATALOG["sgs"]["api_integration"]

    def test_sandbox_switch(self, monkeypatch):
        monkeypatch.setenv("QC_SANDBOX_PROVIDERS", "all")
        assert qc.sandbox_providers() == {"qima", "sgs", "bv"}
        monkeypatch.setenv("QC_SANDBOX_PROVIDERS", "qima, intertek")
        assert qc.sandbox_providers() == {"qima"}


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

class TestSchedule:
    def test_business_days_and_lead_time(self, monkeypatch):
        assert qc_schedule.add_business_days(date(2026, 11, 13), 1) == date(2026, 11, 16)  # Fri → Mon
        assert qc_schedule.earliest_inspection_date(TODAY) == date(2026, 11, 13)
        monkeypatch.setenv("QC_BOOKING_LEAD_BUSINESS_DAYS", "3")
        assert qc_schedule.earliest_inspection_date(TODAY) == date(2026, 11, 16)

    def test_inspection_date_is_the_ready_date_or_the_first_day_the_agency_can_attend(self):
        assert qc_schedule.inspection_date_for("2026-11-25", TODAY) == date(2026, 11, 25)
        assert qc_schedule.inspection_date_for("2026-11-12", TODAY) == date(2026, 11, 13), "lead time wins"
        assert qc_schedule.inspection_date_for("2026-11-21", TODAY) == date(2026, 11, 23), "Saturday → Monday"
        assert qc_schedule.inspection_date_for(None, TODAY) == date(2026, 11, 13)

    @pytest.mark.parametrize(("text", "expected"), [
        ("Goods ready for inspection on 2026-12-15.", date(2026, 12, 15)),
        ("Ready for inspection on 15 December 2026", date(2026, 12, 15)),
        ("we will be ready by the 4th of Dec", date(2026, 12, 4)),
        ("ready on December 15, 2026", date(2026, 12, 15)),
        ("ready on Dec 2", date(2026, 12, 2)),
        ("ready on 15/12/2026", date(2026, 12, 15)),
        ("ready in 10 days", date(2026, 11, 21)),
        ("ready within 2 weeks", date(2026, 11, 25)),
        ("ready in 5 working days", date(2026, 11, 18)),
        ("ready on 5 Jan", date(2027, 1, 5)),
        ("production starts 20 Nov, packing done by 30 November 2026", date(2026, 11, 30)),
    ])
    def test_ready_date_reading(self, text, expected):
        assert qc_schedule.parse_ready_date(text, TODAY) == expected

    @pytest.mark.parametrize("text", ["We acknowledge the PO.", "ready on 1 January 2020", "ready on 2028-01-01", "", None,
                                      "we may start soon", "invoice 31/02/2027"])
    def test_no_date(self, text):
        assert qc_schedule.parse_ready_date(text, TODAY) is None


# ---------------------------------------------------------------------------
# Shield-Link — booking and lifecycle
# ---------------------------------------------------------------------------

class TestBookingOnTheReadyDate:
    async def test_qima_booking_on_the_suppliers_ready_date(self, sandbox, wired_service):
        from services.shield_link.repository import get_repository
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import book_qc
        ready = qc_schedule.add_business_days(qc_schedule.today(), 8)
        res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="qima", quantity_mt=20, goods_ready_date=ready.isoformat()))
        tpo = await get_repository().get(res.tpo_id)
        assert tpo.inspection_date == qc_schedule.inspection_date_for(ready).isoformat()
        assert tpo.channel == "sandbox" and tpo.provider_order_number.startswith("Q26") and tpo.provider_status == "REQUESTED"
        assert tpo.goods_ready_date == ready.isoformat() and tpo.status_history[0]["source"] == "booking"
        booking = sbx.STATE.orders[tpo.ext_ref]["booking"]
        assert booking["referenceNumber"] == "PO-DEAL-QC-1" and booking["supplierName"] == "Godavari Rice Mills"
        assert wired_service.transitions == ["SUPPLIER_DISPATCH_READY"]
        booked = dict(wired_service.events)["qc.booked"]
        assert booked["provider_order_number"] == tpo.provider_order_number and booked["inspection_date"] == tpo.inspection_date

    async def test_an_explicit_date_inside_the_lead_time_is_moved(self, sandbox, wired_service):
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import book_qc
        res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs", inspection_date=qc_schedule.today().isoformat()))
        assert res.inspection_date == qc_schedule.earliest_inspection_date().isoformat()

    async def test_portal_booking_is_e_mailed_to_the_booking_desk(self, monkeypatch, wired_service):
        from services.email_gateway import sender
        from services.email_gateway.schemas import SendEmailResult, SendOutcome
        from services.shield_link.repository import get_repository
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import book_qc
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("BUREAU_VERITAS_BOOKING_EMAIL", "bookings@bv.invalid")
        sent = AsyncMock(return_value=SendEmailResult(outcome=SendOutcome.SENT, message_id="<m1>"))
        monkeypatch.setattr(sender, "send_email", sent)
        res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="bv", quantity_mt=20))
        req = sent.await_args.args[0]
        assert req.to_address == "bookings@bv.invalid" and res.ext_ref in req.subject
        assert "ISO 2859-1" in req.body and "Godavari Rice Mills" in req.body and res.inspection_date in req.body
        tpo = await get_repository().get(res.tpo_id)
        assert tpo.provider_payload["booking_email_outcome"]["outcome"] == "sent" and tpo.channel == "portal"

    @pytest.mark.parametrize("address", ["", "your_sgs_booking_desk_email_here"])
    async def test_live_portal_booking_without_a_real_desk_address_is_refused(self, wired_service, monkeypatch, address):
        from services.shield_link.repository import get_repository
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import ConnectorCallError, book_qc
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("SGS_BOOKING_EMAIL", address)
        with pytest.raises(ConnectorCallError, match="SGS_BOOKING_EMAIL"):
            await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs"))
        orders = await get_repository().list_by_deal("DEAL-QC-1")
        assert [o.status for o in orders] == ["FAILED"] and wired_service.transitions == []

    async def test_live_portal_booking_that_never_reached_the_desk_fails(self, wired_service, monkeypatch):
        from services.email_gateway import sender
        from services.email_gateway.schemas import SendEmailResult, SendOutcome
        from services.shield_link.repository import get_repository
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import ConnectorCallError, book_qc
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("SGS_BOOKING_EMAIL", "bookings@sgs.invalid")
        monkeypatch.setattr(sender, "send_email", AsyncMock(return_value=SendEmailResult(outcome=SendOutcome.LOGGED)))
        with pytest.raises(ConnectorCallError, match="did not reach the SGS booking desk"):
            await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs"))
        orders = await get_repository().list_by_deal("DEAL-QC-1")
        assert orders[0].status == "FAILED" and wired_service.transitions == []

    async def test_sandbox_portal_booking_is_a_stub_with_no_e_mail(self, wired_service, monkeypatch):
        from services.email_gateway import sender
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import book_qc
        sent = AsyncMock()
        monkeypatch.setattr(sender, "send_email", sent)
        res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs"))
        assert res.channel == "stub" and wired_service.transitions == ["SUPPLIER_DISPATCH_READY"]
        sent.assert_not_awaited()


async def _qima_booking(wired_service):
    from services.shield_link.repository import get_repository
    from services.shield_link.schemas import QcBookingRequest
    from services.shield_link.service import book_qc
    res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="qima", quantity_mt=20))
    return await get_repository().get(res.tpo_id)


class TestQimaPolling:
    async def test_poll_follows_the_order_and_applies_the_report_once(self, sandbox, wired_service):
        from services.shield_link import qc as lifecycle
        from services.shield_link.repository import get_repository
        tpo = await _qima_booking(wired_service)
        first = await lifecycle.poll_qc("DEAL-QC-1", force=True)
        assert first["orders"][0]["provider_status"] == "REQUESTED"
        assert (await lifecycle.poll_qc("DEAL-QC-1"))["orders"][0]["status"] == "not_due", "throttled by QC_POLL_MINUTES"
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/validate", json={"inspection_date": "2026-12-01"})
        await lifecycle.poll_qc("DEAL-QC-1", force=True)
        mid = await get_repository().get(tpo.tpo_id)
        assert mid.provider_status == "CONFIRMED" and mid.status == "IN_PROGRESS" and mid.inspection_date == "2026-12-01"
        assert "qc.status_updated" in _topics(wired_service)
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/inspect", json={"result": "PASS", "minor_found": 2})
        await lifecycle.poll_qc("DEAL-QC-1", force=True)
        done = await get_repository().get(tpo.tpo_id)
        assert done.qc_result == "PASS" and done.status == "COMPLETED" and done.provider_status == "REPORT_ISSUED"
        assert len(done.qc_documents) == 1 and done.qc_documents[0]["file_type"] == "PROD_FINAL_REPORT"
        data, mime = await lifecycle.load_report(done.qc_documents[0]["storage_uri"])
        assert data.startswith(b"%PDF-") and done.crf_hash == hashlib.sha256(data).hexdigest()
        assert done.qc_defects["minorFound"] == 2
        assert wired_service.transitions == ["SUPPLIER_DISPATCH_READY", "QC_PASSED"]
        passed = dict(wired_service.events)["qc.passed"]
        assert passed["crf_hash"] == done.crf_hash and passed["provider_order_number"] == done.provider_order_number
        again = await lifecycle.poll_qc("DEAL-QC-1", force=True)
        assert again["orders"][0]["status"] == "closed" and wired_service.transitions.count("QC_PASSED") == 1

    async def test_a_failed_report_moves_the_deal_to_qc_failed(self, sandbox, wired_service):
        from services.shield_link import qc as lifecycle
        tpo = await _qima_booking(wired_service)
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/inspect", json={"result": "FAIL", "remark": "moisture 16%"})
        await lifecycle.poll_qc("DEAL-QC-1", force=True)
        assert wired_service.transitions[-1] == "QC_FAILED"
        failed = dict(wired_service.events)["qc.failed"]
        assert failed["defects"]["majorFound"] == 6 and "moisture" in failed["notes"]

    async def test_a_pending_report_goes_to_an_operator(self, sandbox, wired_service):
        from services.shield_link import qc as lifecycle
        from services.shield_link.repository import get_repository
        tpo = await _qima_booking(wired_service)
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/inspect", json={"result": "PENDING"})
        await lifecycle.poll_qc("DEAL-QC-1", force=True)
        rec = await get_repository().get(tpo.tpo_id)
        assert rec.qc_result == "PENDING" and wired_service.transitions == ["SUPPLIER_DISPATCH_READY"]
        exc = dict(wired_service.events)["deal.exception"]
        assert exc["stage"] == "QC_RESULT_PENDING" and "record PASS or FAIL" in exc["error"]

    async def test_a_provider_error_is_logged_not_raised(self, sandbox, wired_service, monkeypatch):
        from services.shield_link import qc as lifecycle
        await _qima_booking(wired_service)
        monkeypatch.setattr(qc.QimaConnector, "status", AsyncMock(side_effect=qc.ConnectorError("HTTP 500")))
        assert (await lifecycle.poll_qc("DEAL-QC-1", force=True))["orders"][0]["status"] == "error"

    async def test_portal_and_stub_orders_are_not_polled(self, wired_service):
        from services.shield_link import qc as lifecycle
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import book_qc
        await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs"))
        assert (await lifecycle.poll_qc("DEAL-QC-1", force=True))["orders"][0]["status"] == "not_pollable"
        assert (await lifecycle.poll_qc("NO-DEAL"))["status"] == "no_booking"


class TestPortalWebhooksAndOperator:
    async def _post(self, webhook):
        from services.shield_link.schemas import QcWebhookPayload
        from services.shield_link.service import handle_qc_result
        raw = webhook["body"].encode()
        payload = QcWebhookPayload(**json.loads(raw))
        return await handle_qc_result(payload.ext_ref, payload, webhook["headers"], raw, require_signature=True)

    async def test_sgs_confirmation_then_report_by_signed_webhook(self, sandbox, wired_service, monkeypatch):
        from services.shield_link.repository import get_repository
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import book_qc
        monkeypatch.setenv("SGS_WEBHOOK_SECRET", "s3cret")
        res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs", quantity_mt=20))
        confirm = (await sandbox.post(f"/_sandbox/sgs/bookings/{res.ext_ref}/confirm", json={"inspection_date": "2026-12-02"})).json()
        out = await self._post(confirm["webhook"])
        assert out["provider_status"] == "CONFIRMED"
        tpo = await get_repository().get(res.tpo_id)
        assert tpo.inspection_date == "2026-12-02" and tpo.provider_order_number.startswith("SGS-IN-")
        report = (await sandbox.post(f"/_sandbox/sgs/bookings/{res.ext_ref}/report", json={
            "result": "PASS", "container_numbers": ["SBXU0000001"], "seal_numbers": ["SEAL-1"],
            "loaded_quantity_mt": 20, "loaded_hs_code": "10063020"})).json()
        assert (await self._post(report["webhook"]))["result"] == "PASS"
        done = await get_repository().get(res.tpo_id)
        assert done.qc_documents and done.qc_documents[0]["source"] == "webhook"
        assert done.crf_hash == json.loads(report["webhook"]["body"])["crf_hash"]
        passed = dict(wired_service.events)["qc.passed"]
        assert passed["seal_numbers"] == ["SEAL-1"] and passed["container_numbers"] == ["SBXU0000001"]
        assert (await self._post(report["webhook"]))["status"] == "ignored", "a replayed report changes nothing"

    async def test_a_badly_signed_webhook_is_refused(self, sandbox, wired_service, monkeypatch):
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import WebhookVerifyError, book_qc
        monkeypatch.setenv("SGS_WEBHOOK_SECRET", "s3cret")
        res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs"))
        confirm = (await sandbox.post(f"/_sandbox/sgs/bookings/{res.ext_ref}/confirm", json={})).json()["webhook"]
        confirm["headers"]["x-sgs-signature"] = "0" * 64
        with pytest.raises(WebhookVerifyError):
            await self._post(confirm)

    async def test_a_webhook_with_neither_status_nor_result_is_refused(self, wired_service):
        from services.shield_link.schemas import QcBookingRequest, QcWebhookPayload
        from services.shield_link.service import InvalidDealState, book_qc, handle_qc_result
        res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs"))
        with pytest.raises(InvalidDealState):
            await handle_qc_result(res.ext_ref, QcWebhookPayload(ext_ref=res.ext_ref), {}, b"{}")

    async def test_operator_relays_a_bv_portal_result(self, wired_service):
        from services.shield_link import qc as lifecycle
        from services.shield_link.schemas import QcBookingRequest, QcStatusUpdateRequest
        from services.shield_link.service import InvalidDealState, book_qc
        res = await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="bv"))
        await lifecycle.record_qc_status(res.ext_ref, QcStatusUpdateRequest(
            status="CONFIRMED", inspection_date="2026-12-03", provider_order_number="BV-123", source="operator:qa"))
        with pytest.raises(InvalidDealState):
            await lifecycle.record_qc_status(res.tpo_id, QcStatusUpdateRequest(status="REPORT_ISSUED"))
        out = await lifecycle.record_qc_status(res.tpo_id, QcStatusUpdateRequest(
            status="REPORT_ISSUED", result="FAIL", notes="broken grains 9%", defects={"majorFound": 4}))
        assert out["result"] == "FAIL" and wired_service.transitions[-1] == "QC_FAILED"
        view = await lifecycle.get_deal_qc("DEAL-QC-1")
        order = view.orders[0]
        assert order.provider_order_number == "BV-123" and order.inspection_date == "2026-12-03" and order.qc_result == "FAIL"
        assert [h["status"] for h in order.status_history] == ["REQUESTED", "CONFIRMED", "REPORT_ISSUED"]
        assert order.status_history[1]["source"] == "operator:qa"

    async def test_an_unknown_reference_is_404(self):
        from services.shield_link import qc as lifecycle
        from services.shield_link.schemas import QcStatusUpdateRequest
        from services.shield_link.service import TpoNotFoundError
        with pytest.raises(TpoNotFoundError):
            await lifecycle.record_qc_status("NOPE", QcStatusUpdateRequest(status="CONFIRMED"))


class TestViewDocumentsReschedule:
    async def test_deal_view_and_document_download(self, sandbox, wired_service):
        from services.shield_link import qc as lifecycle
        tpo = await _qima_booking(wired_service)
        view = await lifecycle.get_deal_qc("DEAL-QC-1")
        assert "polled" in view.next_step and view.orders[0].channel == "sandbox"
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/inspect", json={"result": "PASS"})
        await lifecycle.poll_qc("DEAL-QC-1", force=True)
        data, mime, name = await lifecycle.get_qc_document(tpo.tpo_id, 0)
        assert data.startswith(b"%PDF-") and mime == "application/pdf" and name.endswith(".pdf")
        with pytest.raises(FileNotFoundError):
            await lifecycle.get_qc_document(tpo.tpo_id, 5)
        assert (await lifecycle.get_deal_qc("DEAL-QC-1")).next_step == "Report issued: PASS."

    async def test_reschedule_cancels_at_qima_and_books_the_new_date(self, sandbox, wired_service):
        from services.shield_link import qc as lifecycle
        from services.shield_link.repository import get_repository
        from services.shield_link.schemas import QcRescheduleRequest
        tpo = await _qima_booking(wired_service)
        new_day = qc_schedule.add_business_days(qc_schedule.today(), 10).isoformat()
        new = await lifecycle.reschedule(tpo.tpo_id, QcRescheduleRequest(inspection_date=new_day, reason="supplier delayed packing"))
        old = await get_repository().get(tpo.tpo_id)
        assert old.status == "CANCELLED" and sbx.STATE.orders[tpo.ext_ref]["cancel_reason"] == "supplier delayed packing"
        assert new.inspection_date == new_day and new.tpo_id != tpo.tpo_id
        assert wired_service.transitions == ["SUPPLIER_DISPATCH_READY"], "the deal does not move"
        booked = [p for t, p in wired_service.events if t == "qc.booked"][-1]
        assert booked["rescheduled_from"] == tpo.tpo_id
        assert sbx.STATE.orders[new.ext_ref]["booking"]["previousOrderNumber"] == tpo.provider_order_number

    async def test_a_finished_inspection_cannot_be_rescheduled(self, sandbox, wired_service):
        from services.shield_link import qc as lifecycle
        from services.shield_link.schemas import QcRescheduleRequest
        from services.shield_link.service import InvalidDealState
        tpo = await _qima_booking(wired_service)
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/inspect", json={"result": "PASS"})
        await lifecycle.poll_qc("DEAL-QC-1", force=True)
        with pytest.raises(InvalidDealState):
            await lifecycle.reschedule(tpo.tpo_id, QcRescheduleRequest(inspection_date="2026-12-20", reason="too late"))


# ---------------------------------------------------------------------------
# Supplier readiness (supplier_commitment + continuations)
# ---------------------------------------------------------------------------

@pytest.fixture
def readiness(monkeypatch):
    from shared.orchestration import continuations, notifications
    from shared.orchestration import supplier_commitment as sc
    monkeypatch.setenv("QC_READINESS_DATE_REQUIRED", "true")
    monkeypatch.setenv("PO_ACK_REQUIRED", "true")
    sc.reset_for_tests()
    monkeypatch.setattr(sc, "_mongo", lambda: None)
    state = SimpleNamespace(deal=_deal(), notices=[], booked=[])

    async def get_deal(_deal_id):
        return state.deal

    async def notify(deal_id, party, kind, context, **kw):
        state.notices.append((party, kind, context))
        return f"EMAIL-{kind}"

    async def book(deal_id, deal=None):
        rec = await sc.get(deal_id)
        state.booked.append(rec.qc_ready_date if rec else None)
        return SimpleNamespace(tpo_id="TPO-1", provider="qima",
                               inspection_date=qc_schedule.inspection_date_for(rec.qc_ready_date if rec else None).isoformat())

    monkeypatch.setattr("services.deals.service.get_deal", get_deal)
    monkeypatch.setattr(notifications, "notify_party", notify)
    monkeypatch.setattr(continuations, "book_qc_for_deal", book)
    return SimpleNamespace(sc=sc, state=state)


class TestSupplierReadiness:
    async def test_an_acknowledgement_naming_the_date_books_for_it(self, readiness):
        sc, st = readiness.sc, readiness.state
        await sc.await_po_ack("DEAL-QC-1", 20.0)
        when = qc_schedule.add_business_days(qc_schedule.today(), 9)
        msg = await sc.handle_supplier_reply("DEAL-QC-1", "PO_ISSUED",
                                             f"We acknowledge the PO. Goods ready for inspection on {when.isoformat()}.", "IN-1")
        assert st.booked == [when.isoformat()] and "inspection booked for" in msg
        rec = await sc.get("DEAL-QC-1")
        assert rec.po_ack == "acknowledged" and rec.qc_ready_status == "received" and rec.qc_ready_by == "supplier e-mail IN-1"

    async def test_an_acknowledgement_without_a_date_asks_for_it_once(self, readiness):
        sc, st = readiness.sc, readiness.state
        await sc.await_po_ack("DEAL-QC-1", 20.0)
        msg = await sc.handle_supplier_reply("DEAL-QC-1", "PO_ISSUED", "We acknowledge the purchase order.", "IN-1")
        assert "asked for the date" in msg and st.booked == []
        kinds = [k for _, k, _ in st.notices]
        assert kinds == ["qc_readiness_request"]
        ctx = st.notices[0][2]
        assert ctx["packed_pct"] == 80 and ctx["lead_days"] == 2
        assert await sc.request_qc_readiness("DEAL-QC-1") is False, "never asked twice"
        assert await sc.handle_supplier_reply("DEAL-QC-1", "PO_ISSUED", "Thanks, noted.", "IN-2") is None
        when = qc_schedule.add_business_days(qc_schedule.today(), 6)
        msg = await sc.handle_supplier_reply("DEAL-QC-1", "PO_ISSUED", f"The lot will be packed and ready on {when:%d %B %Y}.", "IN-3")
        assert st.booked == [when.isoformat()] and when.isoformat() in msg

    async def test_a_date_with_a_refusal_is_left_for_a_person(self, readiness):
        sc = readiness.sc
        await sc.await_po_ack("DEAL-QC-1", 20.0)
        await sc.handle_supplier_reply("DEAL-QC-1", "PO_ISSUED", "We acknowledge the purchase order.", "IN-1")
        when = qc_schedule.add_business_days(qc_schedule.today(), 6)
        assert await sc.handle_supplier_reply("DEAL-QC-1", "PO_ISSUED", f"We cannot be ready on {when.isoformat()}.", "IN-2") is None

    async def test_operator_records_the_date_heard_by_phone(self, readiness):
        sc, st = readiness.sc, readiness.state
        await sc.await_po_ack("DEAL-QC-1", 20.0)
        when = qc_schedule.add_business_days(qc_schedule.today(), 7)
        out = await sc.record_qc_readiness("DEAL-QC-1", when, by="operator:qa", packed_pct=85)
        assert out["status"] == "booked" and st.booked == [when.isoformat()]
        rec = await sc.get("DEAL-QC-1")
        assert rec.po_ack == "acknowledged" and "goods-ready date given" in rec.po_ack_by and rec.qc_ready_packed_pct == 85

    async def test_a_date_after_the_po_stage_is_only_recorded(self, readiness):
        sc, st = readiness.sc, readiness.state
        st.deal.status = SimpleNamespace(value="SUPPLIER_DISPATCH_READY")
        out = await sc.record_qc_readiness("DEAL-QC-1", qc_schedule.today() + timedelta(days=9), by="operator:qa")
        assert out["status"] == "recorded" and st.booked == []

    async def test_without_the_switch_the_inspection_is_booked_at_once(self, readiness, monkeypatch):
        sc, st = readiness.sc, readiness.state
        monkeypatch.setenv("QC_READINESS_DATE_REQUIRED", "false")
        await sc.await_po_ack("DEAL-QC-1", 20.0)
        await sc.handle_supplier_reply("DEAL-QC-1", "PO_ISSUED", "We acknowledge the PO.", "IN-1")
        assert st.booked == [None] and not st.notices

    async def test_po_issued_without_the_ack_step_asks_for_the_date(self, readiness, monkeypatch):
        from shared.domain.events import DomainEvent, EventTopic
        from shared.orchestration import continuations
        monkeypatch.setenv("PO_ACK_REQUIRED", "false")
        monkeypatch.setattr(continuations, "_get_deal_in_status", AsyncMock(return_value=readiness.state.deal))
        monkeypatch.setattr(continuations, "_negotiation_numbers", AsyncMock(return_value=(400.0, 20.0)))
        monkeypatch.setattr(continuations, "_tranche_gate", AsyncMock())
        await continuations.on_po_issued(DomainEvent(topic=EventTopic.PO_ISSUED, deal_id="DEAL-QC-1", payload={}))
        assert readiness.state.booked == []
        assert ("supplier", "qc_readiness_request") in [(p, k) for p, k, _ in readiness.state.notices]

    async def test_book_qc_for_deal_passes_the_ready_date(self, monkeypatch):
        from shared.orchestration import continuations
        from shared.orchestration import supplier_commitment as sc
        sc.reset_for_tests()
        monkeypatch.setattr(sc, "_mongo", lambda: None)
        rec = sc.Commitment(deal_id="DEAL-QC-1", agreed_price=0, quantity_mt=20, status="confirmed", qc_ready_date="2026-12-09")
        await sc._save(rec)
        monkeypatch.setattr(continuations, "_negotiation_numbers", AsyncMock(return_value=(400.0, 20.0)))
        book = AsyncMock(return_value=SimpleNamespace(tpo_id="T", provider="qima"))
        monkeypatch.setattr("services.shield_link.service.book_qc", book)
        await continuations.book_qc_for_deal("DEAL-QC-1", _deal())
        assert book.await_args.args[0].goods_ready_date == "2026-12-09"


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------

class TestNotices:
    def test_po_asks_for_the_goods_ready_date(self):
        from shared.orchestration import notifications
        po = SimpleNamespace(po_id="PO-1", supplier_name="Godavari", quantity_mt=20, agreed_price=400, currency="USD",
                             incoterm="FOB", total_value=8000, port_of_loading="Kakinada", delivery_deadline="2026-12-31",
                             payment_terms="escrow", quality_spec=None, storage_uri=None)
        _, body = notifications._TEMPLATES["po_issued"]({"deal_id": "D1", "po": po, "hs_code": "10063020"})
        assert "acknowledge this Purchase Order" in body and "100% produced and at least 80% packed" in body
        assert "2 working days" in body

    def test_readiness_request(self):
        from shared.orchestration import notifications
        subject, body = notifications._TEMPLATES["qc_readiness_request"](
            {"deal_id": "D1", "supplier_name": "Godavari", "packed_pct": 80, "lead_days": 2, "earliest": "2026-11-13"})
        assert "ready for inspection" in subject.lower() and "2026-11-13" in body and "seals the container" in body

    def test_booked_notice_tells_the_supplier_what_to_prepare(self):
        from shared.orchestration import notifications
        subject, body = notifications._TEMPLATES["qc_booked"]({
            "deal_id": "D1", "provider": "qima", "ext_ref": "O1", "provider_order_number": "Q2600000001-PP",
            "inspection_date": "2026-11-20", "loading_supervision": True, "packed_pct": 80, "po_id": "PO-1"})
        assert subject.startswith("Pre-shipment inspection booked")
        assert "Q2600000001-PP" in body and "2026-11-20" in body and "packing list" in body
        assert "supervises the container stuffing" in body and "move the booking" in body


# ---------------------------------------------------------------------------
# Deal watcher
# ---------------------------------------------------------------------------

class TestWatcher:
    async def test_poll_and_no_reminder_before_the_inspection(self, monkeypatch, sandbox, wired_service):
        from shared.orchestration import deal_watcher, notifications
        tpo = await _qima_booking(wired_service)
        reminder = AsyncMock(return_value="E")
        monkeypatch.setattr(notifications, "notify_qc_readiness_reminder", reminder)
        deal = SimpleNamespace(deal_id="DEAL-QC-1", updated_at=datetime.now(timezone.utc) - timedelta(days=10))
        counts = {"reminders": 0, "exceptions": 0}
        await deal_watcher._sweep_dispatch_ready(deal, datetime.now(timezone.utc), counts)
        reminder.assert_not_awaited()
        from services.shield_link.repository import get_repository
        assert (await get_repository().get(tpo.tpo_id)).last_polled_at is not None
        late = datetime.combine(qc_schedule.to_date(tpo.inspection_date), datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=4)
        deal.updated_at = late - timedelta(hours=80)  # one reminder period (72 h) since the booking moved the deal
        await deal_watcher._sweep_dispatch_ready(deal, late, counts)
        assert counts["reminders"] == 1

    async def test_a_report_found_by_the_watcher_moves_the_deal(self, sandbox, wired_service):
        from shared.orchestration import deal_watcher
        tpo = await _qima_booking(wired_service)
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/inspect", json={"result": "PASS"})
        counts = {"reminders": 0, "exceptions": 0}
        await deal_watcher._sweep_dispatch_ready(SimpleNamespace(deal_id="DEAL-QC-1", updated_at=None), datetime.now(timezone.utc), counts)
        assert counts["qc_results"] == 1 and wired_service.transitions[-1] == "QC_PASSED"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _api(staff: bool = True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.shield_link import router as shield_router
    from shared.auth.dependencies import AuthUser, require_portless
    app = FastAPI()
    app.include_router(shield_router.router)
    if staff:
        app.dependency_overrides[require_portless] = lambda: AuthUser(user_id="u1", email="ops@portless.invalid", is_portless=True)
    return TestClient(app, raise_server_exceptions=False)


class TestRoutes:
    def test_status_relay(self, monkeypatch):
        from services.shield_link import router as r
        from services.shield_link.service import InvalidDealState, TpoNotFoundError
        mock = AsyncMock(return_value={"status": "ok"})
        monkeypatch.setattr(r, "record_qc_status", mock)
        api = _api()
        assert api.post("/shield-link/qc/T1/status", json={"status": "CONFIRMED"}).status_code == 200
        assert mock.await_args.args[1].source == "operator:ops@portless.invalid"
        monkeypatch.setattr(r, "record_qc_status", AsyncMock(side_effect=TpoNotFoundError("T1")))
        assert api.post("/shield-link/qc/T1/status", json={"status": "CONFIRMED"}).status_code == 404
        monkeypatch.setattr(r, "record_qc_status", AsyncMock(side_effect=InvalidDealState("D", "x", "y")))
        assert api.post("/shield-link/qc/T1/status", json={"status": "REPORT_ISSUED"}).status_code == 422
        assert api.post("/shield-link/qc/T1/status", json={"status": "NOPE"}).status_code == 422

    def test_staff_only(self):
        api = _api(staff=False)
        assert api.post("/shield-link/qc/T1/status", json={"status": "CONFIRMED"}).status_code in (401, 403)
        assert api.post("/shield-link/deals/D1/qc-readiness", json={"ready_date": "2026-12-01"}).status_code in (401, 403)

    def test_view_poll_document(self, monkeypatch):
        from services.shield_link import router as r
        from services.shield_link.schemas import DealQcResponse
        monkeypatch.setattr(r, "get_deal_qc", AsyncMock(return_value=DealQcResponse(deal_id="D1", orders=[])))
        monkeypatch.setattr(r, "poll_qc", AsyncMock(return_value={"status": "ok"}))
        monkeypatch.setattr(r, "get_qc_document", AsyncMock(return_value=(b"%PDF-1.4", "application/pdf", "r.pdf")))
        api = _api()
        assert api.get("/shield-link/deals/D1/qc").json()["deal_id"] == "D1"
        assert api.post("/shield-link/deals/D1/qc/poll").json()["status"] == "ok"
        doc = api.get("/shield-link/qc/T1/documents/0")
        assert doc.status_code == 200 and doc.headers["content-type"] == "application/pdf"
        monkeypatch.setattr(r, "get_qc_document", AsyncMock(side_effect=FileNotFoundError("none")))
        assert api.get("/shield-link/qc/T1/documents/3").status_code == 404

    def test_readiness_and_reschedule(self, monkeypatch):
        from services.shield_link import router as r
        from services.shield_link.service import ConnectorCallError
        from shared.orchestration import supplier_commitment as sc
        record = AsyncMock(return_value={"status": "booked"})
        monkeypatch.setattr(sc, "record_qc_readiness", record)
        api = _api()
        assert api.post("/shield-link/deals/D1/qc-readiness", json={"ready_date": "2026-12-01", "packed_pct": 90}).json()["status"] == "booked"
        assert record.await_args.kwargs["by"] == "operator:ops@portless.invalid"
        assert api.post("/shield-link/deals/D1/qc-readiness", json={"ready_date": "soon"}).status_code == 422
        assert api.post("/shield-link/deals/D1/qc-readiness", json={"ready_date": "2026-12-01", "packed_pct": 120}).status_code == 422
        monkeypatch.setattr(r, "reschedule_qc", AsyncMock(side_effect=ConnectorCallError("down")))
        assert api.post("/shield-link/qc/T1/reschedule", json={"inspection_date": "2026-12-01", "reason": "late"}).status_code == 502
        assert api.post("/shield-link/qc/T1/reschedule", json={"inspection_date": "2026-12-01", "reason": ""}).status_code == 422

    def test_webhook_without_status_or_result_is_422(self, monkeypatch):
        from services.shield_link import router as r
        from services.shield_link.service import InvalidDealState
        monkeypatch.setattr(r, "handle_qc_result", AsyncMock(side_effect=InvalidDealState("D", "x", "y")))
        assert _api().post("/shield-link/qc/webhook", json={"ext_ref": "E1"}).status_code == 422


# ---------------------------------------------------------------------------
# Automation environment
# ---------------------------------------------------------------------------

class TestAutomationEnvironment:
    def test_the_worker_runs_qc_on_the_sandbox_without_real_credentials(self, monkeypatch):
        from services.automation import safety
        env = safety.worker_env()
        assert env["QC_SANDBOX_PROVIDERS"] == "qima,sgs,bv"
        assert env["QIMA_API_ACCESS_TOKEN"] == "" and env["QC_SANDBOX_URL"] == ""
        monkeypatch.setenv("MONGO_DB_NAME", safety.AUTOMATION_DB)
        monkeypatch.setenv("AUTOMATION_WORKER", "1")
        for key in safety._MUST_BE_BLANK:
            monkeypatch.setenv(key, "")
        monkeypatch.setenv("QIMA_PASSWORD", "real")
        with pytest.raises(SystemExit, match="QIMA_PASSWORD"):
            safety.assert_isolated()


# ---------------------------------------------------------------------------
# One implementation: live calls the provider, sandbox (automation) stubs
# ---------------------------------------------------------------------------

class TestIntegrationMode:
    def test_mode_and_placeholders(self, monkeypatch):
        from shared import integration_mode as im
        assert im.sandbox(), "the suite runs in sandbox (tests/conftest.py)"
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        assert im.live() and not im.sandbox()
        monkeypatch.delenv("INTEGRATION_MODE")
        assert im.live(), "live is the default"
        assert im.is_placeholder("your_qima_api_access_token_here") and not im.is_placeholder("tok_123")
        monkeypatch.setenv("COGOPORT_API_KEY", "your_cogoport_api_key_here")
        assert not im.configured("COGOPORT_API_KEY")

    def test_the_simulator_never_runs_in_live_mode(self, monkeypatch):
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("QC_SANDBOX_PROVIDERS", "all")
        assert not qc.in_sandbox("qima") and qc.QimaConnector().stubbed is False

    async def test_live_qima_sends_a_placeholder_key_to_qima(self, monkeypatch):
        """No silent stub: the placeholder goes to QIMA, QIMA refuses it, the caller sees why."""
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        monkeypatch.setenv("QIMA_API_ACCESS_TOKEN", "your_qima_api_access_token_here")
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(403, json={"message": "Ai-Api-Access-Token is invalid"})

        connector = qc.QimaConnector()
        monkeypatch.setattr(connector, "_client", lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://ppapi.qima.com"))
        with pytest.raises(qc.ConnectorError, match="403"):
            await connector.book(_request())
        assert seen and seen[0].headers["Ai-Api-Access-Token"] == "your_qima_api_access_token_here"

    @pytest.mark.parametrize(("module", "cls", "arg"), [
        ("shared.connectors.shields", "CogoportConnector", {}),
        ("shared.connectors.shields", "FreightosConnector", {}),
        ("shared.connectors.shields", "IcegateConnector", {}),
        ("shared.connectors.escrow", "TazapayEscrowConnector", {"amount": 10}),
        ("shared.connectors.escrow", "EscrowComConnector", {"amount": 10, "buyer": {"email": "buyer@buyer.invalid"}}),
        ("shared.connectors.payments", "StripeTreasuryConnector", {"amount": 10}),
        ("shared.connectors.payments", "PayoneerConnector", {"amount": 10}),
        ("shared.connectors.qc_providers", "IntertekConnector", {}),
    ])
    async def test_live_freight_escrow_and_payouts_always_call_the_provider(self, monkeypatch, module, cls, arg):
        import importlib
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        calls: list[str] = []
        real = httpx.AsyncClient

        def client(**kw):
            def handler(req: httpx.Request) -> httpx.Response:
                calls.append(str(req.url))
                return httpx.Response(401, json={"error": "invalid credentials"})
            kw.pop("transport", None)
            return real(transport=httpx.MockTransport(handler), **kw)

        monkeypatch.setattr(httpx, "AsyncClient", client)
        mod = importlib.import_module(module)
        with pytest.raises(Exception) as err:
            await getattr(mod, cls)().book(arg)
        assert calls, f"{cls} answered without calling the provider"
        assert "ConnectorError" in type(err.value).__name__

    @pytest.mark.parametrize(("module", "cls", "arg"), [
        ("shared.connectors.shields", "CogoportConnector", {}),
        ("shared.connectors.escrow", "TazapayEscrowConnector", {"amount": 10}),
        ("shared.connectors.payments", "PayoneerConnector", {"amount": 10}),
        ("shared.connectors.qc_providers", "QimaConnector", _request()),
    ])
    async def test_sandbox_answers_from_the_stub_even_with_a_key(self, monkeypatch, module, cls, arg):
        import importlib
        for key in ("COGOPORT_API_KEY", "TAZAPAY_API_KEY", "PAYONEER_CLIENT_ID", "QIMA_API_ACCESS_TOKEN"):
            monkeypatch.setenv(key, "real-looking-key")
        mod = importlib.import_module(module)
        assert (await getattr(mod, cls)().book(arg))["stub"] is True

    def test_automation_refuses_to_run_live(self, monkeypatch):
        from services.automation import safety
        monkeypatch.setenv("MONGO_DB_NAME", safety.AUTOMATION_DB)
        monkeypatch.setenv("AUTOMATION_WORKER", "1")
        for key in safety._MUST_BE_BLANK:
            monkeypatch.setenv(key, "")
        safety.assert_isolated()
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        with pytest.raises(SystemExit, match="INTEGRATION_MODE"):
            safety.assert_isolated()
        assert safety.worker_env()["INTEGRATION_MODE"] == "sandbox"


# ---------------------------------------------------------------------------
# Only QIMA is booked (QC_PROVIDERS_ENABLED default "qima")
# ---------------------------------------------------------------------------

class TestQimaOnly:
    @pytest.fixture(autouse=True)
    def qima_only(self, monkeypatch):
        monkeypatch.delenv("QC_PROVIDERS_ENABLED", raising=False)

    def test_only_qima_is_enabled_and_offered(self):
        from agents.negotiation_agent.graph import _qc_provider_options_summary
        from services.shield_link.service import list_qc_providers
        assert qc.enabled_qc_providers() == ["qima"] and qc.cheapest_qc_provider(["sgs", "bv"]) == "qima"
        catalog = list_qc_providers()
        assert catalog.default_provider == "qima" and [p.provider for p in catalog.providers] == ["qima"]
        summary = _qc_provider_options_summary()
        assert "QIMA" in summary and "SGS" not in summary and "Bureau Veritas" not in summary

    def test_the_switch_can_widen_the_list(self, monkeypatch):
        monkeypatch.setenv("QC_PROVIDERS_ENABLED", "qima, Bureau Veritas")
        assert qc.enabled_qc_providers() == ["qima", "bv"]
        monkeypatch.setenv("QC_PROVIDERS_ENABLED", "all")
        assert len(qc.enabled_qc_providers()) == 5

    async def test_an_explicit_request_for_another_provider_is_refused(self, wired_service):
        from services.shield_link.schemas import QcBookingRequest, QcQuoteRequest
        from services.shield_link.service import UnknownQcProviderError, book_qc, quote_qc
        with pytest.raises(UnknownQcProviderError, match="supported: qima"):
            await book_qc(QcBookingRequest(deal_id="DEAL-QC-1", provider="sgs"))
        with pytest.raises(UnknownQcProviderError):
            await quote_qc(QcQuoteRequest(provider="bv", quantity_mt=20))

    async def test_a_buyer_who_chose_sgs_is_inspected_by_qima(self, wired_service, monkeypatch):
        from services.shield_link import service as sl
        from services.shield_link.schemas import QcBookingRequest
        monkeypatch.setattr(sl, "_buyer_opted_qc_provider", AsyncMock(return_value="sgs"))
        res = await sl.book_qc(QcBookingRequest(deal_id="DEAL-QC-1"))
        assert res.provider == "qima"

    async def test_the_negotiation_prices_and_records_qima_for_a_disabled_choice(self, monkeypatch):
        from agents.negotiation_agent import service as neg
        provider, per_unit = await neg._quote_qc_cost_per_unit("SGS", 20.0, "D1", "10063020")
        assert provider == "qima" and per_unit > 0


# ---------------------------------------------------------------------------
# The deal's QC status (shared/orchestration/qc_status.py)
# ---------------------------------------------------------------------------

def _order(**kw):
    from services.shield_link.schemas import ThirdPartyOrderRecord
    base = {"tpo_id": "T1", "deal_id": "D1", "order_type": "QC", "provider": "qima", "status": "BOOKED",
            "provider_status": "REQUESTED", "inspection_date": (qc_schedule.today() + timedelta(days=5)).isoformat(),
            "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}
    base.update(kw)
    return ThirdPartyOrderRecord(**base)


class TestQcStatus:
    def _c(self, **kw):
        return SimpleNamespace(**{"po_ack": None, "qc_ready_status": None, "qc_ready_asked_at": None, **kw})

    def test_every_stage(self):
        from shared.domain.enums import QcStatus as S
        from shared.orchestration.qc_status import compute
        past = (qc_schedule.today() - timedelta(days=5)).isoformat()
        earlier = datetime.now(timezone.utc) - timedelta(days=9)
        cases = [
            ("ESCROW_FUNDED", None, [], None),
            ("PO_ISSUED", self._c(po_ack="awaiting"), [], S.AWAITING_PO_ACK),
            ("PO_ISSUED", self._c(po_ack="acknowledged", qc_ready_status="awaiting"), [], S.AWAITING_READY_DATE),
            ("PO_ISSUED", None, [], S.NOT_BOOKED),
            ("PO_ISSUED", None, [_order(status="FAILED", provider_status=None)], S.BOOKING_FAILED),
            ("SUPPLIER_DISPATCH_READY", None, [_order()], S.REQUESTED),
            ("SUPPLIER_DISPATCH_READY", None, [_order(provider_status="CONFIRMED")], S.SCHEDULED),
            ("SUPPLIER_DISPATCH_READY", None, [_order(provider_status="INSPECTED")], S.INSPECTED),
            ("SUPPLIER_DISPATCH_READY", None, [_order(inspection_date=past)], S.OVERDUE),
            ("SUPPLIER_DISPATCH_READY", None, [_order(qc_result="PENDING", provider_status="REPORT_ISSUED")], S.RESULT_PENDING),
            ("SUPPLIER_DISPATCH_READY", None, [_order(tpo_id="T0", status="FAILED", qc_result="FAIL", created_at=earlier),
                                               _order(provider_status="CONFIRMED")], S.REINSPECTION),
            ("QC_FAILED", None, [_order(status="FAILED", qc_result="FAIL")], S.FAILED),
            ("DISPUTE", None, [_order(status="FAILED", qc_result="FAIL")], S.FAILED),
            ("REFUNDED", None, [_order(status="FAILED", qc_result="FAIL")], S.FAILED),
            ("QC_PASSED", None, [_order(status="COMPLETED", qc_result="PASS", provider_order_number="Q26-1")], S.PASSED),
            ("IN_TRANSIT", None, [_order(status="COMPLETED", qc_result="PASS")], S.PASSED),
            ("CANCELLED", None, [], None),
        ]
        for status, commitment, orders, want in cases:
            got, detail = compute(status, commitment, orders)
            assert got == want, (status, got, detail)
            assert (detail is None) == (want is None)

    def test_a_cancelled_booking_does_not_count(self):
        from shared.domain.enums import QcStatus as S
        from shared.orchestration.qc_status import compute
        got, detail = compute("SUPPLIER_DISPATCH_READY", None, [_order(status="CANCELLED"), _order(tpo_id="T2", provider_status="CONFIRMED", provider_order_number="Q26-9")])
        assert got == S.SCHEDULED and "Q26-9" in detail

    async def test_the_workflow_keeps_it_on_the_deal(self, sandbox, monkeypatch):
        """Real deal, real transitions: the QC status follows booking → poll → report."""
        from services.deals.repository import get_repository as deals_repo
        from services.deals.schemas import DealRecord
        from services.shield_link import qc as lifecycle
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import book_qc
        from shared.domain.enums import DealStatus, QcStatus as S
        import services.deals.repository as drepo
        drepo._repository = drepo.InMemoryFallbackRepository()
        now = datetime.now(timezone.utc)
        from shared.domain.enums import PartyRole
        from shared.domain.models import Party
        supplier = Party(role=PartyRole.SUPPLIER, legal_name="Godavari Rice Mills", country="IN",
                         email="sales@godavari.invalid", address="Plot 4, Industrial Estate, Kakinada, 533003")
        await deals_repo().save(DealRecord(deal_id="DEAL-QS-1", operator_id="op", org_id="o", hs_code="10063020",
                                           market="AE", status=DealStatus.PO_ISSUED, parties=[supplier],
                                           created_at=now, updated_at=now))
        await book_qc(QcBookingRequest(deal_id="DEAL-QS-1", provider="qima", quantity_mt=20))
        deal = await deals_repo().get("DEAL-QS-1")
        assert deal.status == DealStatus.SUPPLIER_DISPATCH_READY and deal.qc_status == S.REQUESTED
        assert "QIMA inspection requested for" in deal.qc_status_detail
        tpo = (await lifecycle.get_deal_qc("DEAL-QS-1")).orders[0]
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/validate", json={})
        await lifecycle.poll_qc("DEAL-QS-1", force=True)
        assert (await deals_repo().get("DEAL-QS-1")).qc_status == S.SCHEDULED
        await sandbox.post(f"/_sandbox/qima/orders/{tpo.ext_ref}/inspect", json={"result": "FAIL"})
        await lifecycle.poll_qc("DEAL-QS-1", force=True)
        deal = await deals_repo().get("DEAL-QS-1")
        assert deal.status == DealStatus.QC_FAILED and deal.qc_status == S.FAILED
        view = await lifecycle.get_deal_qc("DEAL-QS-1")
        assert view.qc_status == "FAILED" and view.qc_status_label == "Failed"
        drepo._repository = None

    async def test_sync_never_raises(self, monkeypatch):
        from shared.orchestration import qc_status
        import services.deals.repository as drepo
        monkeypatch.setattr(drepo, "get_repository", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        assert await qc_status.sync("D1") is None
        assert await qc_status.sync(None) is None
