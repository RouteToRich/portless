"""Freight, customs and regulatory connectors; the IMAP poller; LLM call tracking; reorder prediction."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from services.email_gateway import imap_poller as imap
from services.email_gateway.imap_poller import ImapPoller
from shared.connectors import regulatory as reg
from shared.connectors import shields
from shared.connectors.reorder import ReorderWindowEngine, _parse_date
from shared.llm_gateway import mongo_connection as mc
from shared.llm_gateway.db_tracker import MongoTracker, _doc_to_record

_REAL_CLIENT = httpx.AsyncClient


class _Http:
    """Answers every outgoing request and remembers what was sent."""

    def __init__(self, status=200, body=None, by_url=None):
        self.status, self.body, self.by_url = status, {"ok": True} if body is None else body, by_url or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        for fragment, reply in self.by_url.items():
            if fragment in str(request.url):
                return httpx.Response(200, json=reply)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def http():
    handler = _Http()
    with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)):
        yield handler


# ---------------------------------------------------------------------------
# Freight + customs
# ---------------------------------------------------------------------------

FREIGHT = [
    (shields.CogoportConnector, "COGOPORT", "x-cogoport-signature", "Token K"),
    (shields.FreightosConnector, "FREIGHTOS", "x-freightos-signature", "Bearer K"),
]


@pytest.mark.parametrize(("cls", "prefix", "header", "auth"), FREIGHT)
class TestFreightConnectors:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, prefix):
        for suffix in ("API_KEY", "WEBHOOK_SECRET", "BASE_URL"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)

    async def test_stub_quote_uses_the_requested_lane(self, cls, prefix, header, auth):
        quote = await cls().quote({"origin_port": "Mundra", "destination_port": "Hamburg"})
        assert quote["stub"] is True and (quote["origin"], quote["destination"]) == ("Mundra", "Hamburg") and quote["rate_usd"] > 0

    async def test_stub_booking_is_confirmed_with_future_dates(self, cls, prefix, header, auth):
        booking = await cls().book({})
        assert booking["status"] == "CONFIRMED" and booking["stub"] is True and booking["ext_ref"]
        assert date.fromisoformat(booking["eta"]) > date.fromisoformat(booking["etd"]) > date.today()  # noqa: DTZ011

    async def test_stub_status(self, cls, prefix, header, auth):
        assert await cls().status("R1") == {"ext_ref": "R1", "status": "IN_TRANSIT", "stub": True}

    @pytest.mark.parametrize("operation", ["quote", "book", "status"])
    async def test_live_call_is_authenticated(self, cls, prefix, header, auth, operation, http, monkeypatch):
        monkeypatch.setenv(f"{prefix}_API_KEY", "K")
        result = await getattr(cls(), operation)("R1" if operation == "status" else {"a": 1})
        assert result == {"ok": True} and http.requests[0].headers["authorization"] == auth

    @pytest.mark.parametrize("operation", ["quote", "book", "status"])
    async def test_provider_error_raises(self, cls, prefix, header, auth, operation, http, monkeypatch):
        monkeypatch.setenv(f"{prefix}_API_KEY", "K")
        http.status = 503
        with pytest.raises(shields.ConnectorError, match="503"):
            await getattr(cls(), operation)("R1" if operation == "status" else {})

    async def test_webhook_without_secret_is_accepted(self, cls, prefix, header, auth):
        assert await cls().webhook_verify({}, b"x") is True

    async def test_webhook_signature(self, cls, prefix, header, auth, monkeypatch):
        monkeypatch.setenv(f"{prefix}_WEBHOOK_SECRET", "s")
        good = hmac.new(b"s", b"body", hashlib.sha256).hexdigest()
        assert await cls().webhook_verify({header: good}, b"body") is True
        assert await cls().webhook_verify({header: good}, b"other") is False
        assert await cls().webhook_verify({}, b"body") is False


class TestIcegate:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.delenv("ICEGATE_API_KEY", raising=False)
        monkeypatch.delenv("ICEGATE_BASE_URL", raising=False)

    async def test_quote_does_not_apply(self):
        assert await shields.IcegateConnector().quote({}) == {}

    async def test_stubs(self):
        filed = await shields.IcegateConnector().book({})
        assert filed["status"] == "FILED" and filed["shipping_bill_number"].startswith("SB") and filed["let_export_order"].startswith("LEO-")
        assert (await shields.IcegateConnector().status("SB1"))["stub"] is True

    @pytest.mark.parametrize("operation", ["book", "status"])
    async def test_live(self, operation, http, monkeypatch):
        monkeypatch.setenv("ICEGATE_API_KEY", "K")
        assert await getattr(shields.IcegateConnector(), operation)("S" if operation == "status" else {}) == {"ok": True}
        assert http.requests[0].headers["x-api-key"] == "K"

    @pytest.mark.parametrize("operation", ["book", "status"])
    async def test_live_failure(self, operation, http, monkeypatch):
        monkeypatch.setenv("ICEGATE_API_KEY", "K")
        http.status = 500
        with pytest.raises(shields.ConnectorError):
            await getattr(shields.IcegateConnector(), operation)("S" if operation == "status" else {})

    async def test_webhook_relies_on_ip_allowlisting(self):
        assert await shields.IcegateConnector().webhook_verify({}, b"") is True


@pytest.mark.parametrize(("provider", "cls"), [
    ("cogoport", shields.CogoportConnector), ("FREIGHTOS", shields.FreightosConnector), ("unknown", shields.CogoportConnector),
])
def test_freight_registry(provider, cls):
    assert isinstance(shields.get_freight_connector(provider), cls)


# ---------------------------------------------------------------------------
# Regulatory
# ---------------------------------------------------------------------------

@pytest.fixture
def _no_cache():
    with patch.object(reg, "cache_get", new=AsyncMock(return_value=None)), patch.object(reg, "cache_set", new=AsyncMock()) as stored:
        yield stored


@pytest.fixture
def setu(monkeypatch):
    monkeypatch.setenv("API_SETU_KEY", "K")
    monkeypatch.setenv("API_SETU_CLIENT_ID", "C")
    monkeypatch.delenv("GSTN_USE_SANDBOX", raising=False)
    monkeypatch.delenv("DGFT_USE_SANDBOX", raising=False)


class TestHelpers:
    @pytest.mark.parametrize(("value", "expected"), [("", 0.0), ("garbage", 0.0)])
    def test_age_of_bad_dates(self, value, expected):
        assert reg._compute_age_years(value) == expected

    def test_age(self):
        two_years = (date.today() - timedelta(days=731)).isoformat()  # noqa: DTZ011
        assert reg._compute_age_years(two_years) == 2.0

    def test_headers(self):
        assert reg._apisetu_headers("K", "C")["X-APISETU-CLIENTID"] == "C"
        assert "X-APISETU-CLIENTID" not in reg._apisetu_headers("K", "")

    async def test_cache_hit_skips_the_fetch(self):
        fetch = AsyncMock()
        with patch.object(reg, "cache_get", new=AsyncMock(return_value={"cached": 1})):
            assert await reg._cached_lookup("n", ("a",), fetch) == {"cached": 1}
        fetch.assert_not_awaited()

    async def test_empty_result_is_not_cached(self, _no_cache):
        assert await reg._cached_lookup("n", ("a",), AsyncMock(return_value={})) == {}
        _no_cache.assert_not_awaited()

    async def test_result_is_cached(self, _no_cache):
        await reg._cached_lookup("n", ("a",), AsyncMock(return_value={"x": 1}))
        _no_cache.assert_awaited_once()


class TestIndianRegistries:
    async def test_gstn(self, setu, http, _no_cache):
        http.body = {"gstnStatus": "active", "legalNameOfBusiness": "Acme", "registrationDate": "2020-01-01", "taxpayerType": "Regular"}
        result = await reg.GstnConnector().quote({"gstin": " 33abc "})
        assert result == {"gstin": "33ABC", "status": "ACTIVE", "legal_name": "Acme", "registration_date": "2020-01-01", "taxpayer_type": "Regular"}
        assert http.requests[0].headers["x-apisetu-apikey"] == "K"

    async def test_gstn_unknown_fields(self, setu, http, _no_cache):
        http.body = {"tradeName": "T"}
        result = await reg.GstnConnector().status("33A")
        assert result["status"] == "UNKNOWN" and result["legal_name"] == "T"

    async def test_gstn_sandbox(self, setu, http, _no_cache, monkeypatch):
        monkeypatch.setenv("GSTN_USE_SANDBOX", "1")
        await reg.GstnConnector().quote({"gstin": "1"})
        assert "sandbox" in str(http.requests[0].url)

    async def test_dgft(self, setu, http, _no_cache):
        http.body = {"iec": "I1", "iecStatus": "active", "entityName": "E", "natureOfConcern": "N", "pan": "P", "iecIssueDate": "D"}
        assert await reg.DgftConnector().status("i1") == {
            "iec": "I1", "status": "ACTIVE", "entity_name": "E", "nature_of_concern": "N", "pan": "P", "iec_issue_date": "D",
        }

    async def test_dgft_defaults(self, setu, http, _no_cache, monkeypatch):
        monkeypatch.setenv("DGFT_USE_SANDBOX", "1")
        http.body = {"status": "x"}
        result = await reg.DgftConnector().quote({"iec": "i2"})
        assert (result["iec"], result["status"]) == ("I2", "X") and "sandbox" in str(http.requests[0].url)

    async def test_dgft_denied_list_is_still_a_stub(self, setu):
        result = await reg.DgftConnector().book({"entity_name": "Anyone"})
        assert result["on_denied_list"] is False and "Stub" in result["note"]

    async def test_mca(self, setu, http, _no_cache):
        incorporated = (date.today() - timedelta(days=1461)).isoformat()  # noqa: DTZ011
        http.body = {"companyName": "C", "companyStatus": "active", "dateOfIncorporation": incorporated, "companyCategory": "K", "classOfCompany": "Private"}
        result = await reg.McaConnector().status("u1")
        assert result["cin"] == "U1" and result["company_status"] == "ACTIVE" and result["age_years"] == 4.0

    async def test_mca_empty(self, setu, http, _no_cache):
        http.body = {"x": 1}
        result = await reg.McaConnector().quote({"cin": "u2"})
        assert result["company_status"] == "UNKNOWN" and result["age_years"] == 0.0

    @pytest.mark.parametrize(("active", "registered"), [(True, True), (False, False)])
    async def test_udyam(self, setu, http, _no_cache, active, registered):
        http.body = {"isActive": active, "enterpriseName": "E", "msmeType": "SMALL", "majorActivity": "M"}
        result = await reg.UdyamConnector().status("udyam-1")
        assert result == {"udyam_number": "UDYAM-1", "registered": registered, "enterprise_name": "E", "msme_type": "small", "activity": "M"}

    @pytest.mark.parametrize("cls", [reg.GstnConnector, reg.McaConnector, reg.UdyamConnector])
    async def test_no_booking(self, setu, cls):
        with pytest.raises(NotImplementedError):
            await cls().book({})

    @pytest.mark.parametrize("cls", [reg.GstnConnector, reg.DgftConnector, reg.McaConnector, reg.UdyamConnector, reg.ViesConnector])
    async def test_no_webhooks(self, setu, cls):
        with pytest.raises(NotImplementedError):
            await cls().webhook_verify({}, b"")

    async def test_http_failure_propagates(self, setu, http, _no_cache):
        http.status = 500
        with pytest.raises(httpx.HTTPStatusError):
            await reg.GstnConnector().quote({"gstin": "1"})

    def test_missing_key_is_an_error(self, monkeypatch):
        monkeypatch.delenv("API_SETU_KEY", raising=False)
        with pytest.raises(KeyError):
            reg.GstnConnector()


class TestVies:
    @pytest.mark.parametrize(("valid", "expected"), [(True, True), (False, False)])
    async def test_quote(self, http, _no_cache, valid, expected):
        http.body = {"isValid": valid, "traderName": "T", "traderAddress": "A", "requestDate": "R"}
        result = await reg.ViesConnector().quote({"country_code": " de ", "vat_number": "x1"})
        assert result == {"vat_number": "X1", "country_code": "DE", "valid": expected, "company_name": "T", "company_address": "A", "request_date": "R"}

    @pytest.mark.parametrize("operation", ["book", "status"])
    async def test_read_only(self, operation):
        with pytest.raises(NotImplementedError):
            await getattr(reg.ViesConnector(), operation)({})


class TestAllianz:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("ALLIANZ_TRADE_CLIENT_ID", "I")
        monkeypatch.setenv("ALLIANZ_TRADE_CLIENT_SECRET", "S")
        monkeypatch.delenv("ALLIANZ_TRADE_WEBHOOK_SECRET", raising=False)

    @pytest.fixture
    def api(self, http):
        http.by_url = {"oauth2/token": {"access_token": "T"}}
        return http

    async def test_quote(self, api):
        assert await reg.AllianzTradeConnector().quote({"company_name": "C", "country": "ES"}) == {"ok": True}
        call = api.requests[1]
        assert call.headers["authorization"] == "Bearer T"
        assert json.loads(call.content) == {"companyName": "C", "country": "ES", "vatNumber": ""}

    async def test_book(self, api):
        await reg.AllianzTradeConnector().book({"buyer_name": "B", "buyer_country": "ES", "cover_amount": 5, "deal_id": "D"})
        assert json.loads(api.requests[1].content)["externalReference"] == "D"

    async def test_status(self, api):
        assert await reg.AllianzTradeConnector().status("P1") == {"ok": True}
        assert api.requests[1].url.path.endswith("/policies/P1")

    async def test_webhook_needs_a_secret(self):
        with pytest.raises(ValueError, match="WEBHOOK_SECRET"):
            await reg.AllianzTradeConnector().webhook_verify({}, b"")

    async def test_webhook(self, monkeypatch):
        monkeypatch.setenv("ALLIANZ_TRADE_WEBHOOK_SECRET", "s")
        digest = hmac.new(b"s", b"b", hashlib.sha256).hexdigest()
        connector = reg.AllianzTradeConnector()
        assert await connector.webhook_verify({"x-signature": f"sha256={digest}"}, b"b") is True
        assert await connector.webhook_verify({"x-signature": "sha256=00"}, b"b") is False
        assert await connector.webhook_verify({"x-signature": digest}, b"b") is False


# ---------------------------------------------------------------------------
# Reorder prediction
# ---------------------------------------------------------------------------

def _profile(*days_ago):
    today = date.today()  # noqa: DTZ011
    return {"buyer_details": {"buyer_name": "B", "volza_buyer_id": 7},
            "shipments": [{"shipment_date": (today - timedelta(days=d)).isoformat()} for d in days_ago]}


class TestReorder:
    @pytest.mark.parametrize("value", ["2026-01-02T03:04:05.123", "2026-01-02T03:04:05Z", "2026-01-02T03:04:05", "2026-01-02"])
    def test_date_formats(self, value):
        assert _parse_date(value) == date(2026, 1, 2)

    def test_bad_date(self):
        assert _parse_date("soon") is None

    def test_no_data(self):
        result = ReorderWindowEngine().compute({})
        assert (result["urgency_label"], result["confidence"], result["predicted_next_order"]) == ("UNKNOWN", "INSUFFICIENT_DATA", None)

    def test_single_shipment_uses_the_default_interval(self):
        result = ReorderWindowEngine().compute(_profile(10))
        assert (result["avg_reorder_days"], result["confidence"], result["days_until_next_order"]) == (90, "INSUFFICIENT_DATA", 80)

    def test_same_day_duplicates_are_ignored(self):
        assert ReorderWindowEngine().compute(_profile(10, 10))["confidence"] == "INSUFFICIENT_DATA"

    @pytest.mark.parametrize(("days_ago", "confidence", "average"), [
        ((100, 40), "LOW", 60), ((160, 100, 40), "MEDIUM", 60), ((220, 160, 100, 40), "HIGH", 60),
    ])
    def test_confidence_grows_with_history(self, days_ago, confidence, average):
        result = ReorderWindowEngine().compute(_profile(*days_ago))
        assert (result["confidence"], result["avg_reorder_days"]) == (confidence, average)

    @pytest.mark.parametrize(("last_ago", "gap", "label", "score"), [
        (70, 60, "OVERDUE", 10), (60, 60, "IMMINENT", 0), (46, 60, "IMMINENT", 0), (45, 60, "UPCOMING", 0),
        (30, 60, "UPCOMING", 0), (29, 60, "WATCHING", 0), (10, 100, "WATCHING", 0), (5, 200, "DORMANT", 0),
    ])
    def test_urgency_bands(self, last_ago, gap, label, score):
        result = ReorderWindowEngine().compute(_profile(last_ago + gap, last_ago))
        assert (result["urgency_label"], result["urgency_score"]) == (label, score)

    def test_identity_is_reported(self):
        result = ReorderWindowEngine().compute(_profile(5))
        assert (result["buyer_name"], result["volza_buyer_id"], result["shipments_analysed"]) == ("B", "7", 1)

    def test_batch_puts_most_overdue_first(self):
        results = ReorderWindowEngine().batch_compute([{}, _profile(5, 205), _profile(170, 100), _profile(130, 100)])
        assert [r["urgency_label"] for r in results] == ["OVERDUE", "OVERDUE", "DORMANT", "UNKNOWN"]
        assert [r["urgency_score"] for r in results[:2]] == [70, 30]


# ---------------------------------------------------------------------------
# LLM call tracking
# ---------------------------------------------------------------------------

class _Conn:
    def __init__(self):
        self.db = AsyncMongoMockClient()["gw"]
        self.ensure_indexes = AsyncMock()
        self.close = AsyncMock()
        self._db_name = "gw"

    def get_collection(self, name):
        return self.db[name]


@pytest.fixture
def conn():
    return _Conn()


@pytest.fixture
def tracker(conn):
    return MongoTracker(connection=conn, session_id="S1")


class TestTracker:
    async def test_start_writes_pending(self, tracker, conn):
        record = await tracker.start_request(caller="neg", model="m", deal_id="D1")
        doc = await conn.db["llm_requests"].find_one({"request_id": record.request_id})
        assert (doc["status"], doc["session_id"], doc["metadata"]) == ("pending", "S1", {"deal_id": "D1"})

    async def test_indexes_are_created_once(self, tracker, conn):
        await tracker.start_request()
        await tracker.start_request()
        conn.ensure_indexes.assert_awaited_once()

    async def test_complete_updates_outcome_and_record(self, tracker, conn):
        record = await tracker.start_request(caller="c", model="m", estimated_cost_usd=0.5)
        await tracker.complete_request(record, prompt_tokens=10, completion_tokens=5, latency_ms=12.345, retry_count=1)
        doc = await conn.db["llm_requests"].find_one({"request_id": record.request_id})
        assert (doc["status"], doc["total_tokens"], doc["latency_ms"], doc["estimated_cost_usd"]) == ("success", 15, 12.35, 0.5)
        assert record.total_tokens == 15 and "completed_at" in doc

    async def test_complete_creates_the_document_when_start_was_lost(self, tracker, conn):
        from shared.llm_gateway.tracker import RequestRecord
        record = RequestRecord(caller="c", model="m")
        await tracker.complete_request(record, status="error", error_message="boom")
        doc = await conn.db["llm_requests"].find_one({"request_id": record.request_id})
        assert (doc["status"], doc["caller"], doc["error_message"]) == ("error", "c", "boom")

    async def test_storage_failure_never_breaks_the_llm_call(self, conn):
        col = MagicMock(update_one=AsyncMock(side_effect=RuntimeError("down")))
        conn.get_collection = lambda _n: col
        tracker = MongoTracker(connection=conn)
        record = await tracker.start_request(caller="c")
        await tracker.complete_request(record)
        assert record.status == "success"

    async def _finish(self, tracker, status="success", caller="c", model="m", tokens=10):
        record = await tracker.start_request(caller=caller, model=model, estimated_cost_usd=0.25)
        await tracker.complete_request(record, prompt_tokens=tokens, latency_ms=100.0, status=status)
        return record

    async def test_records_filters(self, tracker):
        await self._finish(tracker)
        await self._finish(tracker, status="error", caller="d", model="n")
        assert len(await tracker.get_records()) == 2
        assert [r.caller for r in await tracker.get_records(caller="d")] == ["d"]
        assert [r.status for r in await tracker.get_records(status="error")] == ["error"]
        assert [r.model for r in await tracker.get_records(model="n")] == ["n"]
        assert len(await tracker.get_records(session_only=True)) == 2

    async def test_records_failure_is_empty(self, conn):
        col = MagicMock(find=MagicMock(side_effect=RuntimeError("down")))
        conn.get_collection = lambda _n: col
        assert await MongoTracker(connection=conn).get_records() == []

    async def test_stats(self, tracker):
        await self._finish(tracker)
        await self._finish(tracker, status="fallback")
        await self._finish(tracker, status="error")
        await tracker.start_request()
        stats = await tracker.get_stats()
        assert (stats["total_requests"], stats["successes"], stats["fallbacks"], stats["errors"]) == (3, 1, 1, 1)
        assert stats["success_rate"] == 0.333 and stats["total_tokens"] == 30 and stats["avg_latency_ms"] == 100.0
        assert stats["total_cost_usd"] == 0.75

    async def test_stats_filters(self, tracker):
        await self._finish(tracker)
        assert (await tracker.get_stats(caller="nobody"))["total_requests"] == 0
        assert (await tracker.get_stats(session_only=True))["total_requests"] == 1

    async def test_stats_with_no_rows(self, conn):
        col = MagicMock(aggregate=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[]))))
        conn.get_collection = lambda _n: col
        assert await MongoTracker(connection=conn).get_stats() == {"total_requests": 0}

    @pytest.mark.parametrize(("method", "empty"), [("get_stats", {"total_requests": 0, "error": "down"}), ("get_model_breakdown", []), ("get_caller_breakdown", [])])
    async def test_aggregation_failures(self, conn, method, empty):
        col = MagicMock(aggregate=MagicMock(side_effect=RuntimeError("down")))
        conn.get_collection = lambda _n: col
        assert await getattr(MongoTracker(connection=conn), method)() == empty

    async def test_model_breakdown_sorted_by_requests(self, tracker):
        await self._finish(tracker, model="a")
        await self._finish(tracker, model="b")
        await self._finish(tracker, model="b", status="error")
        rows = await tracker.get_model_breakdown()
        assert [r["model"] for r in rows] == ["b", "a"] and rows[0]["error_rate"] == 0.5

    async def test_caller_breakdown_sorted_by_cost(self, tracker):
        await self._finish(tracker, caller="a")
        await self._finish(tracker, caller="b")
        await self._finish(tracker, caller="b")
        rows = await tracker.get_caller_breakdown()
        assert [r["caller"] for r in rows] == ["b", "a"] and rows[0]["total_cost_usd"] == 0.5

    async def test_close(self, tracker, conn):
        await tracker.close()
        conn.close.assert_awaited_once()

    def test_repr(self, tracker):
        assert "S1" in repr(tracker) and "gw" in repr(tracker)

    def test_doc_defaults(self):
        record = _doc_to_record({})
        assert (record.status, record.total_tokens, record.metadata) == ("unknown", 0, {})

    async def test_create_warms_indexes(self, conn):
        with patch("shared.llm_gateway.mongo_connection.get_connection", new=AsyncMock(return_value=conn)):
            await MongoTracker.create(uri="u", db_name="d")
        conn.ensure_indexes.assert_awaited_once()

    def test_from_settings(self, conn):
        settings = MagicMock(mongo_uri="u", mongo_db_name="d", mongo_collection="c")
        with patch("shared.llm_gateway.mongo_connection.get_connection_sync", return_value=conn) as factory:
            tracker = MongoTracker.from_settings(settings)
        factory.assert_called_once_with(uri="u", db_name="d")
        assert tracker._collection_name == "c"


class TestMongoConnection:
    @pytest.fixture(autouse=True)
    def _pool(self):
        saved = dict(mc._pool)
        mc._pool.clear()
        mc._pool_lock = asyncio.Lock()
        yield
        mc._pool.clear()
        mc._pool.update(saved)

    @pytest.fixture
    def client(self):
        fake = MagicMock()
        fake.admin.command = AsyncMock()
        fake.__getitem__.return_value = AsyncMongoMockClient()["gw"]
        with patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=fake):
            yield fake

    async def test_connection_is_shared_per_uri_and_db(self, client):
        first = await mc.get_connection("u", "d")
        assert await mc.get_connection("u", "d") is first
        assert await mc.get_connection("u", "other") is not first
        assert client.admin.command.await_count == 2

    def test_sync_factory_shares_the_pool(self, client):
        first = mc.get_connection_sync("u", "d")
        assert mc.get_connection_sync("u", "d") is first

    async def test_defaults_come_from_settings(self, client, monkeypatch):
        monkeypatch.setattr(mc, "get_connection_sync", mc.get_connection_sync)
        from shared.llm_gateway.config import llm_gateway_settings as s
        connection = await mc.get_connection()
        assert connection._db_name == s.mongo_db_name

    async def test_close_all(self, client):
        await mc.get_connection("u", "d")
        await mc.close_all()
        client.close.assert_called_once()
        assert mc._pool == {}

    async def test_collection_and_database(self, client):
        connection = mc.MongoConnection("u", "d")
        assert connection.get_database() is client["d"]
        assert connection.get_collection("c").name == "c"

    async def test_ping(self, client):
        connection = mc.MongoConnection("u", "d")
        assert await connection.ping() is True
        client.admin.command = AsyncMock(side_effect=RuntimeError("down"))
        assert await connection.ping() is False

    async def test_indexes_are_built_once(self, client):
        connection = mc.MongoConnection("u", "d")
        indexes = [{"keys": [("a", 1)], "options": {"name": "a"}}]
        await connection.ensure_indexes("c", indexes)
        with patch.object(connection, "get_collection") as collection:
            await connection.ensure_indexes("c", indexes)
        collection.assert_not_called()

    async def test_index_failure_is_tolerated(self, client):
        connection = mc.MongoConnection("u", "d")
        col = MagicMock(create_index=AsyncMock(side_effect=RuntimeError("no")))
        with patch.object(connection, "get_collection", return_value=col):
            await connection.ensure_indexes("c", [{"keys": [("a", 1)]}])
        assert "c" in connection._indexes_done

    async def test_concurrent_index_build_runs_once(self, client):
        connection = mc.MongoConnection("u", "d")
        col = MagicMock(create_index=AsyncMock())
        with patch.object(connection, "get_collection", return_value=col):
            await asyncio.gather(*(connection.ensure_indexes("c", [{"keys": [("a", 1)]}]) for _ in range(3)))
        col.create_index.assert_awaited_once()

    def test_repr(self, client):
        assert "d" in repr(mc.MongoConnection("u", "d"))

    def test_missing_motor_is_reported(self):
        with patch.dict("sys.modules", {"motor.motor_asyncio": None}), pytest.raises(RuntimeError, match="motor"):
            mc.MongoConnection("u", "d")


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def _mime(subject="Quote", sender="ann@acme.com", body="Price 900", message_id="<m1>", **headers):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "t@p.io", subject
    if message_id:
        msg["Message-ID"] = message_id
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body + " " + "x" * 120)
    return msg


class TestMimeParsing:
    def test_rfc2047_subject(self):
        assert imap._decode_header_value("=?UTF-8?B?4K6k?= plain") .startswith("\u0ba4")

    def test_plain_header(self):
        assert imap._decode_header_value("hello") == "hello"

    def test_single_part_text(self):
        assert imap._extract_bodies(_mime(body="Hi"))[0].startswith("Hi")

    def test_single_part_html(self):
        msg = EmailMessage()
        msg.set_content("<b>Hi</b>", subtype="html")
        plain, html = imap._extract_bodies(msg)
        assert plain == "" and "<b>Hi</b>" in html

    def test_multipart_keeps_the_first_of_each(self):
        msg = _mime(body="plain")
        msg.add_alternative("<i>first</i>", subtype="html")
        msg.add_alternative("<i>second</i>", subtype="html")
        plain, html = imap._extract_bodies(msg)
        assert plain.startswith("plain") and "first" in html and "second" not in html

    def test_empty_payload(self):
        msg = EmailMessage()
        assert imap._extract_bodies(msg) == ("", "")

    def test_references(self):
        assert imap._parse_references("<a> junk  <b>") == ["<a>", "<b>"]

    def test_attachments(self):
        msg = _mime()
        msg.add_attachment(b"PDF", maintype="application", subtype="pdf", filename="po.pdf")
        attachment = imap._extract_attachments(msg)[0]
        assert (attachment.filename, attachment.content_type, attachment.size) == ("po.pdf", "application/pdf", 3)

    def test_attachment_without_a_name(self):
        msg = _mime()
        msg.add_attachment(b"X", maintype="application", subtype="octet-stream", disposition="attachment")
        assert imap._extract_attachments(msg)[0].filename == "attachment"

    def test_no_attachments_on_a_simple_mail(self):
        assert imap._extract_attachments(_mime()) == []

    def test_full_conversion(self):
        raw = imap._mime_to_raw_email(_mime(In_Reply_To="<p1>", References="<a> <b>", sender="Ann <ann@acme.com>"))
        assert (raw.from_address, raw.to_address, raw.message_id, raw.in_reply_to, raw.references) == (
            "ann@acme.com", "t@p.io", "<m1>", "<p1>", ["<a>", "<b>"],
        )
        assert raw.source == imap.InboundEmailSource.IMAP


class _FakeImap:
    def __init__(self, mails=None, search=("OK", None), head_status="OK", body_status="OK", logout_error=None):
        self.mails = mails or {}
        self.search_result = search
        self.head_status, self.body_status = head_status, body_status
        self.logout_error = logout_error
        self.stored, self.selected, self.bodies = [], None, []
        self.logged_out = False

    async def wait_hello_from_server(self):
        return None

    async def login(self, *_):
        return None

    async def select(self, folder):
        self.selected = folder

    async def search(self, *_):
        status, data = self.search_result
        return status, data if data is not None else [" ".join(self.mails).encode()]

    async def fetch(self, uid, what):
        head, body = self.mails[uid]
        if "RFC822" in what:
            self.bodies.append(uid)
            if isinstance(body, Exception):
                raise body
            return (self.body_status, body if isinstance(body, list) else [b"1 FETCH", body, b")"])
        return (self.head_status, [head.encode()])

    async def store(self, uid, *_):
        self.stored.append(uid)

    async def logout(self):
        self.logged_out = True
        if self.logout_error:
            raise self.logout_error


def _entry(message_id="<m1>", sender="ann@acme.com", flags="", body=None):
    head = f"FLAGS ({flags})\r\n" + (f"Message-ID: {message_id}\r\n" if message_id else "") + f"From: {sender}\r\n"
    return head, (body if body is not None else _mime(message_id=message_id or None, sender=sender).as_bytes())


@pytest.fixture
def imap_env(monkeypatch):
    for name, value in (("imap_user", "me@p.io"), ("imap_password", "pw"), ("gmail_sender_address", "out@p.io"), ("imap_lookback_days", 3), ("imap_poll_interval_seconds", 0)):
        monkeypatch.setattr(imap.settings, name, value)


async def _fetched(poller, fake):
    with patch("aioimaplib.IMAP4_SSL", return_value=fake):
        return [m async for m in poller._fetch_new()]


class TestFetch:
    @pytest.fixture
    def poller(self, imap_env):
        return ImapPoller(AsyncMock(), folder="Replies")

    async def test_new_mail_is_forwarded_and_marked_seen(self, poller):
        fake = _FakeImap({"1": _entry()})
        mails = await _fetched(poller, fake)
        assert [m.message_id for m in mails] == ["<m1>"] and fake.stored == ["1"]
        assert fake.selected == "Replies" and fake.logged_out

    async def test_already_read_mail_with_an_id_is_still_forwarded(self, poller):
        assert len(await _fetched(poller, _FakeImap({"1": _entry(flags="\\Seen")}))) == 1

    @pytest.mark.parametrize("own", ["me@p.io", "out@p.io", "ME@P.IO"])
    async def test_own_outbound_copies_are_skipped_without_download(self, poller, own):
        fake = _FakeImap({"1": _entry(sender=own)})
        assert await _fetched(poller, fake) == [] and fake.bodies == []

    async def test_second_cycle_does_not_download_again(self, poller):
        fake = _FakeImap({"1": _entry()})
        await _fetched(poller, fake)
        assert await _fetched(poller, fake) == [] and fake.bodies == ["1"]

    async def test_read_mail_without_an_id_is_skipped(self, poller):
        assert await _fetched(poller, _FakeImap({"1": _entry(message_id="", flags="\\Seen")})) == []

    async def test_unread_mail_without_an_id_is_forwarded_every_time_it_is_unread(self, poller):
        assert len(await _fetched(poller, _FakeImap({"1": _entry(message_id="")}))) == 1
        assert poller._forwarded == {}

    @pytest.mark.parametrize("search", [("NO", [b""]), ("OK", [b"   "])])
    async def test_nothing_to_do(self, poller, search):
        fake = _FakeImap(search=search)
        assert await _fetched(poller, fake) == [] and fake.logged_out

    async def test_header_failure_skips_the_message(self, poller):
        assert await _fetched(poller, _FakeImap({"1": _entry()}, head_status="NO")) == []

    async def test_body_failure_skips_the_message(self, poller):
        fake = _FakeImap({"1": _entry()}, body_status="NO")
        assert await _fetched(poller, fake) == [] and fake.stored == []

    async def test_short_fetch_response_is_skipped(self, poller):
        assert await _fetched(poller, _FakeImap({"1": _entry(body=[b"only"])})) == []

    async def test_no_body_part_is_skipped(self, poller):
        assert await _fetched(poller, _FakeImap({"1": _entry(body=[b"a", b"b"])})) == []

    async def test_one_bad_message_does_not_stop_the_rest(self, poller):
        fake = _FakeImap({"1": _entry(body=RuntimeError("bad")), "2": _entry(message_id="<m2>")})
        assert [m.message_id for m in await _fetched(poller, fake)] == ["<m2>"]

    async def test_byte_uids(self, poller):
        fake = _FakeImap({"1": _entry()}, search=("OK", [b"1"]))
        assert len(await _fetched(poller, fake)) == 1

    async def test_logout_failure_is_ignored(self, poller):
        assert len(await _fetched(poller, _FakeImap({"1": _entry()}, logout_error=RuntimeError("x")))) == 1

    async def test_missing_library_yields_nothing(self, poller):
        with patch.dict("sys.modules", {"aioimaplib": None}):
            assert [m async for m in poller._fetch_new()] == []


class TestPollerHelpers:
    def test_since_date_is_locale_independent(self):
        with patch.object(imap, "datetime") as clock:
            clock.now.return_value = datetime(2026, 3, 9, tzinfo=UTC)
            assert ImapPoller._imap_since_date(3) == "06-Mar-2026"

    def test_envelope(self):
        blob = 'FLAGS (\\Seen \\Answered)\r\nMessage-ID: <m1>\r\nFrom: "Ann" <Ann@Acme.com>'
        assert ImapPoller._parse_envelope(blob) == ("<m1>", "ann@acme.com", True)

    def test_empty_envelope(self):
        assert ImapPoller._parse_envelope("") == ("", "", False)

    def test_cache_is_pruned(self):
        poller = ImapPoller(AsyncMock())
        for i in range(ImapPoller._FORWARDED_CACHE_MAX + 1):
            poller._remember_forwarded(f"<{i}>")
        assert len(poller._forwarded) == ImapPoller._FORWARDED_CACHE_MAX // 2 + 1
        assert f"<{ImapPoller._FORWARDED_CACHE_MAX}>" in poller._forwarded and "<0>" not in poller._forwarded

    def test_factories_tag_the_party(self, monkeypatch):
        monkeypatch.setattr(imap.settings, "imap_buyer_folder", "B")
        monkeypatch.setattr(imap.settings, "imap_supplier_folder", "S")
        assert (imap.create_buyer_poller(AsyncMock())._party_hint, imap.create_buyer_poller(AsyncMock())._folder) == ("buyer", "B")
        assert (imap.create_supplier_poller(AsyncMock())._party_hint, imap.create_supplier_poller(AsyncMock())._folder) == ("supplier", "S")


class TestPollLoop:
    @staticmethod
    def _stream(*batches):
        calls = iter(batches)

        async def fetch():
            batch = next(calls, None)
            if batch is None:
                raise asyncio.CancelledError
            if isinstance(batch, Exception):
                raise batch
            for item in batch:
                yield item
        return fetch

    async def _run(self, poller, *batches):
        poller._running = True
        poller._fetch_new = self._stream(*batches)
        with patch.object(imap.asyncio, "sleep", new=AsyncMock()) as sleep, pytest.raises(asyncio.CancelledError):
            await poller._poll_loop()
        return sleep

    async def test_party_hint_is_passed(self, imap_env):
        callback = AsyncMock()
        await self._run(ImapPoller(callback, party_hint="buyer"), ["mail"])
        callback.assert_awaited_once_with("mail", party_override="buyer")

    async def test_no_hint(self, imap_env):
        callback = AsyncMock()
        await self._run(ImapPoller(callback), ["mail"])
        callback.assert_awaited_once_with("mail")

    async def test_callback_failure_does_not_stop_the_batch(self, imap_env):
        callback = AsyncMock(side_effect=[RuntimeError("x"), None])
        await self._run(ImapPoller(callback), ["a", "b"])
        assert callback.await_count == 2

    async def test_backoff_grows_and_is_capped(self, imap_env):
        errors = [RuntimeError("x")] * 11
        sleep = await self._run(ImapPoller(AsyncMock()), *errors)
        delays = [c.args[0] for c in sleep.await_args_list]
        assert delays[:2] == [60, 120] and delays[-1] == 600

    async def test_success_resets_the_backoff(self, imap_env):
        sleep = await self._run(ImapPoller(AsyncMock()), RuntimeError("x"), [], RuntimeError("y"))
        assert [c.args[0] for c in sleep.await_args_list] == [60, 0, 60]

    async def test_loop_stops_when_not_running(self):
        poller = ImapPoller(AsyncMock())
        await poller._poll_loop()


class TestPollerLifecycle:
    async def test_no_user_disables_polling(self, monkeypatch):
        monkeypatch.setattr(imap.settings, "imap_user", "")
        poller = ImapPoller(AsyncMock())
        await poller.start()
        assert poller._task is None

    async def test_no_password_disables_polling(self, monkeypatch):
        monkeypatch.setattr(imap.settings, "imap_user", "u")
        monkeypatch.setattr(imap.settings, "imap_password", "")
        poller = ImapPoller(AsyncMock())
        await poller.start()
        assert poller._task is None

    async def test_start_then_stop(self, imap_env):
        poller = ImapPoller(AsyncMock())
        with patch.object(poller, "_poll_loop", new=lambda: asyncio.sleep(60)):
            await poller.start()
            assert poller._running and poller._task is not None
            await poller.stop()
        assert poller._running is False and poller._task.cancelled()

    async def test_stop_before_start(self):
        await ImapPoller(AsyncMock()).stop()
