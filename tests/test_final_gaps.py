"""Apify runs, the LLM gateway client, knowledge, catalogue, price book, e-mail mapping and the autonomy policy."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from services.email_processor import mapper as mp
from services.email_processor.schemas import MappingConfidence
from shared import catalogue as cat
from shared import knowledge as kn
from shared import pricebook as pb
from shared.catalogue import ProductLine
from shared.connectors import apify as ap
from shared.llm_gateway import remote as rm
from shared.llm_gateway import remote_sdk as sdk
from shared.orchestration import autonomy_policy as au
from tests.factories import NOW, make

_REAL_CLIENT = httpx.AsyncClient
NEG = "12345678-1234-1234-1234-123456789012"


def _col():
    return AsyncMongoMockClient()["t"]["c"]


class _Broken:
    def __getattr__(self, _name):
        raise RuntimeError("store down")


# ---------------------------------------------------------------------------
# Apify
# ---------------------------------------------------------------------------

class _Actor:
    """A fake Apify API: start → poll → dataset."""

    def __init__(self, polls=("SUCCEEDED",), rows=None):
        self.polls, self.rows = list(polls), [{"a": 1}] if rows is None else rows
        self.requests, self.start_status, self.poll_status, self.items_status = [], 201, 200, 200
        self.dataset = "DS"
        self.first_status = "RUNNING"

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/abort"):
            return httpx.Response(200, json={})
        if "/datasets/" in path:
            return httpx.Response(self.items_status, json=self.rows)
        if "/actor-runs/" in path:
            status = self.polls.pop(0) if self.polls else "SUCCEEDED"
            return httpx.Response(self.poll_status, json={"data": {"id": "R", "status": status, "defaultDatasetId": self.dataset}})
        return httpx.Response(self.start_status, json={"data": {"id": "R", "status": self.first_status, "defaultDatasetId": self.dataset}})


@pytest.fixture
def actor(monkeypatch):
    fake = _Actor()
    monkeypatch.setenv("APIFY_TOKEN", "T")
    monkeypatch.setenv("APIFY_MAX_RUNS_PER_DAY", "0")
    monkeypatch.setattr(ap.asyncio, "sleep", AsyncMock())
    ap.reset_run_budget_for_tests()
    with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(fake), **kw)):
        yield fake
    ap.reset_run_budget_for_tests()


def _connector():
    return ap.ApifyContactFinderConnector()


class TestApifyRun:
    async def test_rows_come_back(self, actor):
        assert await _connector().run({"x": 1}) == [{"a": 1}]
        assert ap.runs_today() == 1

    async def test_polls_until_terminal(self, actor):
        actor.polls = ["RUNNING", "SUCCEEDED"]
        await _connector().run({})
        assert sum("/actor-runs/" in r.url.path for r in actor.requests) == 2

    async def test_memory_setting_is_sent(self, actor):
        connector = _connector()
        connector._memory_mbytes = 512
        await connector.run({})
        assert actor.requests[0].url.params["memory"] == "512"

    async def test_daily_budget_stops_paid_runs(self, actor, monkeypatch):
        monkeypatch.setenv("APIFY_MAX_RUNS_PER_DAY", "1")
        await _connector().run({})
        count = len(actor.requests)
        assert await _connector().run({}) == [] and await _connector().run({}) == []
        assert len(actor.requests) == count

    def test_old_days_are_forgotten(self):
        ap._runs_by_day["2000-01-01"] = 5
        ap._count_run()
        assert "2000-01-01" not in ap._runs_by_day
        ap.reset_run_budget_for_tests()

    async def test_start_failure(self, actor):
        actor.start_status = 402
        with pytest.raises(ap.ApifyError):
            await _connector().run({})

    async def test_poll_failure(self, actor):
        actor.poll_status = 500
        with pytest.raises(ap.ApifyError):
            await _connector().run({})

    async def test_dataset_failure(self, actor):
        actor.items_status = 500
        with pytest.raises(ap.ApifyError):
            await _connector().run({})

    async def test_no_dataset(self, actor):
        actor.dataset = ""
        assert await _connector().run({}) == []

    async def test_dataset_not_a_list(self, actor):
        actor.rows = {"oops": 1}
        assert await _connector().run({}) == []

    async def test_time_budget_aborts_and_keeps_partial_rows(self, actor):
        connector = _connector()
        connector._timeout = -1
        actor.polls = []
        assert await connector.run({}) == [{"a": 1}]
        assert any(r.url.path.endswith("/abort") for r in actor.requests)

    async def test_abort_failure_is_tolerated(self, actor):
        connector = _connector()
        connector._timeout = -1
        real = actor.__call__

        def failing(request):
            if request.url.path.endswith("/abort"):
                raise httpx.ConnectError("x")
            return real(request)

        with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(failing), **kw)):
            assert await connector.run({}) == [{"a": 1}]

    async def test_book_and_status(self, actor):
        connector = _connector()
        assert (await connector.book({"x": 1}))["run_id"] == "R"
        assert (await connector.status("R"))["dataset_id"] == "DS"

    @pytest.mark.parametrize(("operation", "arg"), [("book", {}), ("status", "R")])
    async def test_book_and_status_failures(self, actor, operation, arg):
        actor.start_status = actor.poll_status = 500
        with pytest.raises(ap.ApifyError):
            await getattr(_connector(), operation)(arg)

    @pytest.mark.parametrize(("operation", "arg"), [("book", {}), ("status", "R")])
    async def test_stubs_without_a_token(self, monkeypatch, operation, arg):
        monkeypatch.setenv("APIFY_TOKEN", "")
        with patch.object(ap, "_autodiscover_token", return_value=""):
            assert (await getattr(_connector(), operation)(arg))["stub"] is True

    async def test_webhook_without_a_secret(self, monkeypatch):
        monkeypatch.delenv("APIFY_WEBHOOK_SECRET", raising=False)
        assert await _connector().webhook_verify({}, b"") is True


class TestApifyHelpers:
    def test_autodiscovery_off(self, monkeypatch):
        monkeypatch.setenv("APIFY_TOKEN_AUTODISCOVER", "false")
        assert ap._autodiscover_token() == ""

    def test_autodiscovery_reads_the_cli_login(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APIFY_TOKEN_AUTODISCOVER", "true")
        (tmp_path / ".apify").mkdir()
        (tmp_path / ".apify" / "auth.json").write_text(json.dumps({"token": "abc"}))
        monkeypatch.setattr(ap.Path, "home", lambda: tmp_path)
        assert ap._autodiscover_token() == "abc"

    @pytest.mark.parametrize("content", ["{broken", json.dumps({"token": ""})])
    def test_autodiscovery_bad_file(self, monkeypatch, tmp_path, content):
        monkeypatch.setenv("APIFY_TOKEN_AUTODISCOVER", "true")
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / ".apify").mkdir()
        (tmp_path / ".apify" / "auth.json").write_text(content)
        monkeypatch.setattr(ap.Path, "home", lambda: tmp_path)
        assert ap._autodiscover_token() == ""

    def test_autodiscovery_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APIFY_TOKEN_AUTODISCOVER", "true")
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(ap.Path, "home", lambda: tmp_path)
        assert ap._autodiscover_token() == ""

    @pytest.mark.parametrize(("raw", "default", "expected"), [(None, True, True), ("  ", False, False), ("off", True, False), ("yes", False, True)])
    def test_flag(self, monkeypatch, raw, default, expected):
        monkeypatch.delenv("X_FLAG", raising=False)
        if raw is not None:
            monkeypatch.setenv("X_FLAG", raw)
        assert ap._flag("X_FLAG", default) is expected

    def test_numbers_with_bad_env(self, monkeypatch):
        monkeypatch.setenv("X_NUM", "abc")
        assert ap._int("X_NUM", 3) == 3 and ap._float("X_NUM", 1.5) == 1.5

    @pytest.mark.parametrize("value", ["x", None, float("nan"), float("inf")])
    def test_safe_float(self, value):
        assert ap._safe_float(value) is None

    def test_iso3(self):
        assert ap.iso3("") is None and ap.iso3("ESP") == "ESP"

    @pytest.mark.parametrize(("status", "milestone"), [("delivered", "DELIVERED"), ("nonsense", None)])
    def test_milestone(self, status, milestone):
        assert ap.carrier_milestone(status) == milestone


class TestApifyInputs:
    async def test_alibaba_needs_products(self):
        with pytest.raises(ValueError, match="products or query"):
            await ap.ApifyAlibabaRfqConnector().quote({})

    async def test_export_markets_needs_hs(self):
        with pytest.raises(ValueError, match="hs_code"):
            await ap.ApifyExportMarketConnector().quote({})

    async def test_india_suppliers_needs_input(self):
        with pytest.raises(ValueError, match="hs_code or keyword"):
            await ap.ApifyIndiaSupplierConnector().quote({})

    async def test_counterparty_check_needs_parties(self):
        with pytest.raises(ValueError, match="counterparties"):
            await ap.ApifyCounterpartyCheckConnector().quote({})

    async def test_email_qualifier_needs_emails(self):
        with pytest.raises(ValueError, match="emails"):
            await ap.ApifyEmailQualifierConnector().quote({})

    async def test_alibaba_query_joins_products(self):
        connector = ap.ApifyAlibabaRfqConnector()
        with patch.object(connector, "run", new=AsyncMock(return_value=[])) as run:
            await connector.quote({"products": ["a"], "query": "b"})
        assert run.await_args.args[0]["products"] == ["a", "b"]

    async def test_comtrade_key_is_passed(self, monkeypatch):
        monkeypatch.setenv("COMTRADE_SUBSCRIPTION_KEY", "K")
        connector = ap.ApifyExportMarketConnector()
        with patch.object(connector, "run", new=AsyncMock(return_value=[])) as run:
            await connector.quote({"hs_code": "1", "markets": ["ES"]})
        assert run.await_args.args[0]["comtradeKey"] == "K"

    async def test_supplier_state(self):
        connector = ap.ApifyIndiaSupplierConnector()
        with patch.object(connector, "run", new=AsyncMock(return_value=[])) as run:
            await connector.quote({"hs_code": "1", "state": "TN"})
        assert run.await_args.args[0]["states"] == ["TN"]

    async def test_counterparty_identifiers_are_passed(self):
        connector = ap.ApifyCounterpartyCheckConnector()
        with patch.object(connector, "run", new=AsyncMock(return_value=[])) as run:
            await connector.quote({"counterparties": [{"name": "A", "lei": "L", "registration_number": "R"}]})
        item = json.dumps(run.await_args.args[0])
        assert '"L"' in item and '"R"' in item


# ---------------------------------------------------------------------------
# LLM gateway client
# ---------------------------------------------------------------------------

def _settings(**kw):
    base = {"gateway_url": "https://gw.test", "gateway_llm_path": "/api/llm", "gateway_token": "", "gateway_email": "svc@p.io", "gateway_password": "pw", "gateway_account_id": "A",
            "gateway_token_ttl_seconds": 3000, "gateway_timeout_seconds": 5.0, "gateway_configured": True, "gateway_auth_path": "/auth/login"}
    return SimpleNamespace(**{**base, **kw})


class TestServiceAuth:
    def _auth(self, handler, **kw):
        return rm._ServiceAccountAuth(_settings(**kw), transport=httpx.MockTransport(handler))

    def test_static_token(self):
        assert rm._ServiceAccountAuth(_settings(gateway_token="T"))._bearer() == "T"

    def test_sync_sign_in_is_cached(self):
        calls = []

        def login(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"access_token": "J"})

        auth = self._auth(login)
        assert auth._bearer() == "J" and auth._bearer() == "J"
        assert len(calls) == 1 and calls[0]["account_id"] == "A"

    def test_sign_in_without_an_account(self):
        calls = []

        def login(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"access_token": "J"})

        self._auth(login, gateway_account_id="")._bearer()
        assert "account_id" not in calls[0]

    @pytest.mark.parametrize(("status", "body"), [(401, {}), (200, {})])
    def test_sign_in_failure(self, status, body):
        auth = self._auth(lambda _r: httpx.Response(status, json=body))
        with pytest.raises(sdk.GatewayError):
            auth._bearer()

    def test_sync_flow_retries_once_on_401(self):
        tokens = iter(["J1", "J2"])
        auth = self._auth(lambda _r: httpx.Response(200, json={"access_token": next(tokens)}))
        flow = auth.sync_auth_flow(httpx.Request("GET", "https://x.test"))
        first = next(flow)
        assert first.headers["authorization"] == "Bearer J1"
        second = flow.send(httpx.Response(401))
        assert second.headers["authorization"] == "Bearer J2"
        with pytest.raises(StopIteration):
            flow.send(httpx.Response(200))

    def test_sync_flow_no_retry_on_success(self):
        auth = self._auth(lambda _r: httpx.Response(200, json={"access_token": "J"}))
        flow = auth.sync_auth_flow(httpx.Request("GET", "https://x.test"))
        next(flow)
        with pytest.raises(StopIteration):
            flow.send(httpx.Response(200))


class TestRemoteSingletons:
    @pytest.fixture(autouse=True)
    def _reset(self):
        rm.reset_remote_client()
        yield
        rm.reset_remote_client()

    async def test_close_with_a_client(self):
        client = rm.get_remote_client(_settings(gateway_token="T"), transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
        assert client is rm.get_remote_client()
        await rm.aclose_remote_client()
        assert rm._client is None and rm._auth is None

    async def test_close_without_a_client(self):
        await rm.aclose_remote_client()

    def test_real_transport_path(self):
        assert rm.get_remote_client(_settings(gateway_token="T")) is not None

    def test_no_url(self):
        with pytest.raises(rm.GatewayNotConfiguredError):
            rm._service_auth(_settings(gateway_url=" "))

    def test_no_credential(self):
        with pytest.raises(rm.GatewayNotConfiguredError):
            rm._service_auth(_settings(gateway_configured=False))


class TestRemoteSdk:
    @staticmethod
    def _client(handler):
        http = _REAL_CLIENT(base_url="https://gw.test", transport=httpx.MockTransport(handler))
        return sdk.RemoteLLMClient(base_url="https://gw.test", client=http)

    def test_error_with_a_non_json_body(self):
        with pytest.raises(sdk.GatewayError) as err:
            sdk._raise_for_error(httpx.Response(502, text="bad gateway", request=httpx.Request("GET", "https://x")))
        assert err.value.status_code == 502 and err.value.code == "http_error"

    def test_jwt_header(self):
        client = sdk.RemoteLLMClient(base_url="https://gw.test", jwt="J")
        assert client._client.headers["authorization"] == "Bearer J"

    async def test_stream(self):
        body = 'ignored\ndata: {not json}\ndata: {"content": "Hi", "model": "m"}\ndata: {"usage": {"prompt_tokens": 1, "completion_tokens": 2}}\ndata: [DONE]\ndata: {"content": "after"}'
        client = self._client(lambda _r: httpx.Response(200, text=body))
        events = [e async for e in client.stream([{"role": "user", "content": "x"}], caller="c")]
        assert [e.content for e in events] == ["Hi", ""]
        assert (events[0].is_final, events[1].is_final, events[1].completion_tokens) == (False, True, 2)

    async def test_stream_rejected_before_it_starts(self):
        client = self._client(lambda _r: httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no", "request_id": "R"}}))
        with pytest.raises(sdk.GatewayError) as err:
            [e async for e in client.stream([], caller="c")]
        assert err.value.code == "unauthorized" and "id=R" in str(err.value)

    async def test_stats_and_health(self):
        seen = []

        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"ok": True})

        client = self._client(handler)
        assert await client.stats("c") == {"ok": True} and await client.stats() == {"ok": True} and await client.health() == {"ok": True}
        assert seen[0] == {"caller": "c"} and seen[1] == {}

    async def test_owned_client_is_closed(self):
        async with sdk.RemoteLLMClient(base_url="https://gw.test") as client:
            assert client._owns_client

    async def test_borrowed_client_is_left_open(self):
        http = _REAL_CLIENT(base_url="https://gw.test")
        await sdk.RemoteLLMClient(base_url="https://gw.test", client=http).aclose()
        assert not http.is_closed
        await http.aclose()


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------

class TestKnowledge:
    @pytest.fixture(autouse=True)
    def _reset(self):
        kn.reset_for_tests()
        yield
        kn.reset_for_tests()

    def test_timeout_with_a_bad_value(self, monkeypatch):
        monkeypatch.setenv("KNOWLEDGE_TIMEOUT_SECONDS", "soon")
        assert kn._timeout() == 60.0

    def test_switched_off(self, monkeypatch):
        monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
        assert kn.enabled() is False

    def test_settings_failure(self, monkeypatch):
        monkeypatch.setenv("KNOWLEDGE_ENABLED", "true")
        with patch("shared.llm_gateway.config.llm_gateway_settings", new=SimpleNamespace()):
            assert kn.enabled() is False

    async def test_client_is_built_once_and_closed(self):
        with patch("shared.llm_gateway.config.llm_gateway_settings", new=_settings()), patch("shared.llm_gateway.remote._service_auth", return_value=None):
            client = kn.get_client()
            assert kn.get_client() is client
        await kn.aclose()
        assert client.is_closed and kn._client is None

    async def test_close_cancels_background_writes(self):
        task = MagicMock()
        kn._pending.add(task)
        await kn.aclose()
        task.cancel.assert_called_once()
        kn._pending.clear()

    async def test_explicit_acl(self, monkeypatch):
        monkeypatch.setenv("KNOWLEDGE_ACL", "a, b")
        assert await kn._acl() == ["a", "b"]

    async def test_acl_failure(self, monkeypatch):
        monkeypatch.delenv("KNOWLEDGE_ACL", raising=False)
        with patch("shared.llm_gateway.remote._service_auth", side_effect=RuntimeError):
            assert await kn._acl() == []

    async def test_acl_opaque_token(self, monkeypatch):
        monkeypatch.delenv("KNOWLEDGE_ACL", raising=False)
        with patch("shared.llm_gateway.remote._service_auth", return_value=MagicMock(_abearer=AsyncMock(return_value="opaque"))):
            assert await kn._acl() == []

    @pytest.mark.parametrize(("enabled", "text"), [(False, "x"), (True, ""), (True, "   ")])
    def test_nothing_to_remember(self, enabled, text):
        with patch.object(kn, "enabled", return_value=enabled):
            assert kn.remember(text, path="p", metadata={}) is None

    def test_no_event_loop(self):
        with patch.object(kn, "enabled", return_value=True):
            assert kn.remember("x", path="p", metadata={}) is None

    async def test_remember_runs_in_the_background(self):
        with patch.object(kn, "enabled", return_value=True), patch.object(kn, "ingest_text", new=AsyncMock()) as ingest:
            task = kn.remember(" hello ", path="p", metadata={"a": 1, "b": None, "c": ""})
            await task
        assert ingest.await_args.args[0] == "hello" and ingest.await_args.kwargs["metadata"] == {"a": "1"}

    async def test_background_failure_is_contained(self):
        with patch.object(kn, "ingest_text", new=AsyncMock(side_effect=RuntimeError)):
            await kn._ingest_safely("t", "p", {})

    @pytest.mark.parametrize(("enabled", "query"), [(False, "q"), (True, "  ")])
    async def test_nothing_to_recall(self, enabled, query):
        with patch.object(kn, "enabled", return_value=enabled):
            assert await kn.recall(query) == []

    async def test_recall_is_cached(self):
        with patch.object(kn, "enabled", return_value=True), patch.object(kn, "retrieve", new=AsyncMock(return_value=[])) as retrieve, patch.object(kn, "_ours", return_value=True):
            await kn.recall("q")
            kn._recall_cache[("q", 5)] = (kn.time.monotonic(), [{"cached": 1}])
            assert await kn.recall("q") == [{"cached": 1}]
        retrieve.assert_awaited_once()

    def test_account_id_fallback(self, monkeypatch):
        monkeypatch.setenv("PORTLESS_ACCOUNT_ID", " A1 ")
        with patch("shared.llm_gateway.config.llm_gateway_settings", new=SimpleNamespace()):
            assert kn.account_id() == "A1"

    def test_context_skips_empty_and_respects_the_limit(self):
        results = [{"text": ""}, {"text": "short"}, {"text": "x" * 500}]
        out = kn.as_context(results, max_chars=30)
        assert "short" in out and "xxx" not in out


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def _line(**kw):
    return {"product_id": "P1", "hs_code": "2905.43", "name": "Glycerine", "markets": ["es "], "products": [" glycerol ", ""], **kw}


class TestCatalogue:
    @pytest.fixture(autouse=True)
    def _reset(self):
        cat.reset_for_tests()
        yield
        cat.reset_for_tests()

    def test_mongo_unavailable(self):
        with patch("shared.config.settings", new=SimpleNamespace(mongo_uri="u", mongo_db_name="d")), patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=RuntimeError):
            assert cat._mongo_collection() is None

    def test_mongo_collection(self):
        with patch("shared.config.settings", new=SimpleNamespace(mongo_uri="u", mongo_db_name="d")), patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=lambda *_a: AsyncMongoMockClient()):
            assert cat._mongo_collection() is not None

    async def test_persist_and_reload(self):
        col = _col()
        with patch.object(cat, "_mongo_collection", return_value=col):
            await cat.set_catalogue([_line()], updated_by="ops")
            cat.reset_for_tests()
            loaded = await cat.load_catalogue()
        assert loaded.products[0].hs_code == "290543" and loaded.products[0].markets == ["ES"] and loaded.products[0].products == ["glycerol"]

    async def test_empty_store(self):
        with patch.object(cat, "_mongo_collection", return_value=_col()):
            assert (await cat.load_catalogue()).products == []

    async def test_store_failures_keep_memory(self):
        with patch.object(cat, "_mongo_collection", return_value=_Broken()):
            await cat.set_catalogue([_line()])
            await cat.load_catalogue()
        assert cat.get_catalogue().products[0].name == "Glycerine"

    @pytest.mark.parametrize(("line", "message"), [(_line(hs_code="abcd"), "4 digits"), (_line(name=" "), "needs a name")])
    async def test_invalid_lines(self, line, message):
        with pytest.raises(ValueError, match=message):
            await cat.set_catalogue([line])

    async def test_lookups_without_a_catalogue(self):
        assert cat.products_for("1520") == [] and cat.seller_profile_for("1520") is None and cat.scan_defaults() is None

    async def test_most_specific_line_wins(self):
        await cat.set_catalogue([_line(product_id="A", hs_code="290543"), _line(product_id="B", hs_code="2905", seller_profile="general")])
        assert cat.line_for("290543").product_id == "A"
        assert cat.seller_profile_for("290543") is None
        assert cat.products_for("290543") == ["glycerol"]


# ---------------------------------------------------------------------------
# Price book
# ---------------------------------------------------------------------------

def _obs(price=900.0, **kw):
    return pb.PriceObservation(**{"hs_code": "1520", "side": "buyer", "kind": "buyer_target", "price_usd_per_mt": price, **kw})


class TestPriceBook:
    @pytest.fixture(autouse=True)
    def _reset(self):
        pb.reset_for_tests()
        yield
        pb.reset_for_tests()

    def test_mongo_unavailable(self):
        with patch("shared.config.settings", new=SimpleNamespace(mongo_uri="u", mongo_db_name="d")), patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=RuntimeError):
            assert pb._mongo_collection() is None

    def test_mongo_collection(self):
        with patch("shared.config.settings", new=SimpleNamespace(mongo_uri="u", mongo_db_name="d")), patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=lambda *_a: AsyncMongoMockClient()):
            assert pb._mongo_collection() is not None

    @pytest.mark.parametrize("price", [0.0, -5.0, float("inf")])
    async def test_bad_price_is_dropped(self, price):
        assert await pb.record(_obs(price)) is None

    async def test_mongo_round_trip(self):
        col = _col()
        with patch.object(pb, "_mongo_collection", return_value=col):
            saved = await pb.record(_obs())
            rows = await pb.observations("1520")
        assert [r.obs_id for r in rows] == [saved.obs_id] and pb._memory == []

    async def test_write_failure_keeps_it_in_memory(self):
        with patch.object(pb, "_mongo_collection", return_value=MagicMock(insert_one=AsyncMock(side_effect=RuntimeError))):
            await pb.record(_obs())
        assert len(pb._memory) == 1

    async def test_read_failure_uses_memory(self):
        pb._memory.append(_obs())
        with patch.object(pb, "_mongo_collection", return_value=_Broken()):
            assert len(await pb.observations("1520")) == 1

    async def test_duplicates_are_merged(self):
        item = _obs()
        pb._memory.extend([item, item])
        assert len(await pb.observations("1520")) == 1

    def test_quantile_fallback(self):
        assert pb._weighted_quantile([(1.0, 0.1), (2.0, 0.1)], 5.0) == 2.0

    def test_other_market_and_grade_count_less(self):
        near = _obs(900.0, market="ES", grade="USP")
        far = _obs(1500.0, market="DE", grade="technical")
        assert pb.estimate_from([near, far], hs_code="1520", side="buyer", market="ES", grade="USP").median < 1200.0

    async def test_priors_from_the_latest_scan(self):
        old = SimpleNamespace(scanned_at=NOW - timedelta(days=9), price_benchmark_usd_per_mt=1.0)
        new = SimpleNamespace(scanned_at=NOW, price_benchmark_usd_per_mt=1200.0, price_gap_pct=50.0, price_benchmark_source="s", tariff_data=SimpleNamespace(applied_duty_pct=5.0))
        repo = MagicMock(list_by_hs_market=AsyncMock(return_value=[old, new]))
        with patch("services.trade_min.repository.get_repository", new=AsyncMock(return_value=repo)):
            priors = await pb.public_priors("1520", "ES")
        assert (priors["buyer"], priors["supplier"], priors["source"], priors["tariff_pct"]) == (1200.0, 800.0, "s", 5.0)

    async def test_priors_extreme_gap_is_ignored(self):
        scan = SimpleNamespace(scanned_at=NOW, price_benchmark_usd_per_mt=1200.0, price_gap_pct=-95.0, tariff_data=None)
        with patch("services.trade_min.repository.get_repository", new=AsyncMock(return_value=MagicMock(list_by_hs_market=AsyncMock(return_value=[scan])))):
            assert (await pb.public_priors("1520", "ES"))["supplier"] is None

    async def test_priors_failure(self):
        with patch("services.trade_min.repository.get_repository", new=AsyncMock(side_effect=RuntimeError)):
            assert (await pb.public_priors("1520", "ES"))["buyer"] is None


# ---------------------------------------------------------------------------
# Inbound e-mail mapping
# ---------------------------------------------------------------------------

def _email(party="buyer", direction="outbound", negotiation_id=NEG, status="sent", to="b@x.co"):
    return SimpleNamespace(party=party, direction=SimpleNamespace(value=direction), negotiation_id=negotiation_id, status=SimpleNamespace(value=status), to_address=to)


class TestPartyOfEmail:
    async def test_no_id(self):
        assert await mp._party_of_email(None, NEG) is None

    async def test_lookup_failure(self):
        with patch("agents.negotiation_agent.email_history.get_email_repository", side_effect=RuntimeError):
            assert await mp._party_of_email("E", NEG) is None

    @pytest.mark.parametrize("record", [None, _email(negotiation_id="other"), _email(direction="inbound")])
    async def test_not_ours(self, record):
        repo = MagicMock(get=AsyncMock(return_value=record))
        with patch("agents.negotiation_agent.email_history.get_email_repository", return_value=repo):
            assert await mp._party_of_email("E", NEG) is None


class TestSenderCheck:
    @staticmethod
    def _repo(session):
        return patch("agents.negotiation_agent.repository.get_repository", return_value=MagicMock(get_session=AsyncMock(return_value=session)))

    async def test_no_session(self):
        with self._repo(None):
            assert await mp._sender_check(NEG, "buyer", "a@b.co") == "unknown"

    async def test_lookup_failure(self):
        with patch("agents.negotiation_agent.repository.get_repository", side_effect=RuntimeError):
            assert await mp._sender_check(NEG, "buyer", "a@b.co") == "unknown"

    async def test_addresses_we_wrote_to_count_and_failures_are_tolerated(self):
        session = SimpleNamespace(deal_id="D")
        addresses = {"buyer": set(), "supplier": set()}
        emails = MagicMock(list_by_negotiation=AsyncMock(return_value=[_email(to=" New@X.co "), _email(direction="inbound"), _email(status="draft")]))
        with self._repo(session), patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError)), \
             patch("agents.negotiation_agent.email_safety.party_addresses", return_value=addresses), \
             patch("agents.negotiation_agent.email_history.get_email_repository", return_value=emails), \
             patch("agents.negotiation_agent.email_safety.classify_sender", return_value="verified") as classify:
            assert await mp._sender_check(NEG, "buyer", "new@x.co") == "verified"
        assert classify.call_args.args[2]["buyer"] == {"new@x.co"}

    async def test_history_failure_still_classifies(self):
        with self._repo(SimpleNamespace(deal_id="D")), patch("services.deals.service.get_deal", new=AsyncMock(return_value=None)), \
             patch("agents.negotiation_agent.email_safety.party_addresses", return_value={"buyer": set()}), \
             patch("agents.negotiation_agent.email_history.get_email_repository", side_effect=RuntimeError), \
             patch("agents.negotiation_agent.email_safety.classify_sender", return_value="unknown"):
            assert await mp._sender_check(NEG, "buyer", "a@b.co") == "unknown"


class TestMapEmail:
    async def test_session_status_failure(self):
        with patch("agents.negotiation_agent.repository.get_repository", side_effect=RuntimeError):
            assert await mp._session_status(NEG) == "UNKNOWN"

    async def test_get_party(self):
        repo = MagicMock(list_by_negotiation=AsyncMock(return_value=[_email(direction="inbound"), _email(party="supplier", to="A@B.co")]))
        with patch("agents.negotiation_agent.email_history.get_email_repository", return_value=repo):
            assert await mp._get_party(NEG, "a@b.co") == "supplier"
            assert await mp._get_party(NEG, "x@y.z") is None

    async def test_get_party_failure(self):
        with patch("agents.negotiation_agent.email_history.get_email_repository", side_effect=RuntimeError):
            assert await mp._get_party(NEG, "a@b.co") is None

    async def test_override_on_a_closed_negotiation(self):
        with patch.object(mp, "_resolve_mapping", new=AsyncMock(return_value=(False, "AGREED"))):
            result = await mp.map_email("a@b.co", "s", "", [], negotiation_id_override=NEG)
        assert result.confidence == MappingConfidence.MANUAL_OVERRIDE

    async def test_references_header(self):
        with patch.object(mp, "_parse_neg_id_from_header", return_value=NEG), patch.object(mp, "_resolve_mapping", new=AsyncMock(return_value=(True, "ROUND"))), \
             patch.object(mp, "_resolve_party", new=AsyncMock(return_value=("buyer", "verified"))):
            result = await mp.map_email("a@b.co", "s", "", ["<x>"])
        assert result.negotiation_id == NEG and result.party == "buyer"

    async def test_subject_token(self):
        with patch.object(mp, "_resolve_mapping", new=AsyncMock(return_value=(True, "ROUND"))), patch.object(mp, "_resolve_party", new=AsyncMock(return_value=("supplier", "verified"))):
            result = await mp.map_email("a@b.co", f"Re: [NEG:{NEG}]", "", [])
        assert result.confidence == MappingConfidence.SUBJECT_TOKEN and result.party == "supplier"

    async def test_outreach_parse_failure_is_tolerated(self):
        with patch("shared.orchestration.outreach.parse_token", side_effect=RuntimeError), patch.object(mp, "_resolve_mapping", new=AsyncMock(return_value=(False, None))):
            result = await mp.map_email("a@b.co", "hello", "", [])
        assert result.negotiation_id is None


# ---------------------------------------------------------------------------
# Autonomy policy
# ---------------------------------------------------------------------------

class TestAutonomyPolicy:
    @pytest.fixture(autouse=True)
    def _reset(self):
        au.reset_for_tests()
        yield
        au.reset_for_tests()

    @pytest.mark.parametrize("value", ["x", None])
    def test_bad_numbers(self, value):
        assert au._norm_seconds(value) is None and au._norm_usd(value) is None

    def test_action_classes(self):
        assert au.action_classes()

    def test_mongo_unavailable(self):
        with patch("shared.config.settings", new=SimpleNamespace(mongo_uri="u", mongo_db_name="d")), patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=RuntimeError):
            assert au._mongo_collection() is None

    def test_mongo_collection(self):
        with patch("shared.config.settings", new=SimpleNamespace(mongo_uri="u", mongo_db_name="d")), patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=lambda *_a: AsyncMongoMockClient()):
            assert au._mongo_collection() is not None

    def test_clean_gate(self):
        assert au._clean_gate({"level": "l1", "default_on_timeout": "approve", "timeout_seconds": 60, "auto_approve_below_usd": 5}) == {
            "level": "L1", "default_on_timeout": "APPROVE", "timeout_seconds": 60, "auto_approve_below_usd": 5.0,
        }

    async def test_persist_and_reload_including_v1_documents(self):
        col = _col()
        await col.insert_one({"_id": au._DOC_ID, "levels": {"ISSUE_PO": "L1", "UNKNOWN": "L2"}, "gates": {"BOOK_FREIGHT": {"level": "L2"}, "ISSUE_PO": {"timeout_seconds": 60}, "NOPE": {}, "FILE_CUSTOMS": "bad", "RESOLVE_EXCEPTION": {"level": "zz"}}, "money_auto_approve_below_usd": 500})
        with patch.object(au, "_mongo_collection", return_value=col):
            await au.load_policy()
        overrides = au.current_overrides()
        assert overrides["ISSUE_PO"] == {"level": "L1", "timeout_seconds": 60} and overrides["BOOK_FREIGHT"] == {"level": "L2"}
        assert "UNKNOWN" not in overrides and au.money_auto_approve_below_usd() == 500.0

    async def test_load_failure_keeps_defaults(self):
        with patch.object(au, "_mongo_collection", return_value=_Broken()):
            await au.load_policy()
        assert au.current_overrides() == {}

    async def test_saved_to_the_store(self):
        col = _col()
        with patch.object(au, "_mongo_collection", return_value=col):
            await au.set_policy({"ISSUE_PO": "L1"}, updated_by="ops")
        assert (await col.find_one({"_id": au._DOC_ID})) is not None

    async def test_save_failure_is_kept_in_memory(self):
        with patch.object(au, "_mongo_collection", return_value=_Broken()):
            await au.set_policy({"ISSUE_PO": "L1"}, updated_by="ops")
        assert au.resolve_level("ISSUE_PO") == "L1"

    async def test_unknown_action(self):
        with pytest.raises(ValueError, match="Unknown action"):
            await au.set_policy({}, updated_by="o", gates={"NOPE": {"level": "L1"}})

    async def test_unknown_action_in_levels(self):
        with pytest.raises(ValueError, match="Unknown action"):
            await au.set_policy({"NOPE": "L1"}, updated_by="o")

    async def test_blank_value_clears_that_setting(self):
        await au.set_policy({}, updated_by="o", gates={"ISSUE_PO": {"timeout_seconds": 60, "level": "L1"}})
        await au.set_policy({}, updated_by="o", gates={"ISSUE_PO": {"timeout_seconds": None}})
        assert au.current_overrides()["ISSUE_PO"] == {"level": "L1"}

    async def test_bad_money_threshold(self):
        with pytest.raises(ValueError, match="money threshold"):
            await au.set_policy({}, updated_by="o", money_auto_approve_below_usd="abc")


def test_factory_builds_a_line():
    assert make(ProductLine, hs_code="1520").hs_code == "1520"
