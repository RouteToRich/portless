"""API gateway delegation, QC provider HTTP, market intel, approvals helpers, sign-in, e-mail history on Mongo."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from agents.negotiation_agent import email_history as eh
from agents.negotiation_agent import market_intel as mi
from agents.negotiation_agent.email_history import EmailHistoryRecord
from services.api_gateway import service as gw
from services.hitl_approval import repository as hitl_repo
from services.hitl_approval import service as hitl
from services.hitl_approval.schemas import CreateApprovalRequest, ResolveApprovalRequest
from shared.auth import identity as idn
from shared.connectors import qc_providers as qc
from shared.domain.events import EventTopic
from tests.factories import make

_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
def http():
    state = SimpleNamespace(status=200, body={"ok": True}, text=None, requests=[])

    def handler(request):
        state.requests.append(request)
        if state.text is not None:
            return httpx.Response(state.status, text=state.text)
        return httpx.Response(state.status, json=state.body)

    with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)):
        yield state


class TestGateway:
    async def test_downstream_health_reports_each_service(self):
        clients = {"up": MagicMock(health=AsyncMock()), "down": MagicMock(health=AsyncMock(side_effect=RuntimeError))}
        with patch.object(gw.clients, "ALL_CLIENTS", clients):
            assert await gw.downstream_health() == {"up": "ok", "down": "unreachable"}

    async def test_create_deal(self):
        response = make(gw.DealResponse)
        fake = MagicMock(create_deal=AsyncMock(return_value=response.model_dump(mode="json")))
        with patch.object(gw.clients, "deals", fake):
            assert (await gw.create_deal(make(gw.CreateDealRequest))).deal_id == response.deal_id

    @pytest.mark.parametrize(("call", "args", "client", "method"), [
        (gw.get_deal, ("D",), "deals", "get_deal"),
        (gw.update_deal_status, ("D", {"a": 1}), "deals", "update_status"),
        (gw.list_opportunities, (), "trade_min", "list_opportunities"),
        (gw.list_approvals, ("PENDING",), "hitl_approval", "list_approvals"),
        (gw.resolve_approval, ("A", {"a": 1}), "hitl_approval", "resolve_approval"),
        (gw.get_exception, ("E",), "exception_resolver", "get_exception"),
    ])
    async def test_delegates(self, call, args, client, method):
        fake = MagicMock(**{method: AsyncMock(return_value="done")})
        with patch.object(gw.clients, client, fake):
            assert await call(*args) == "done"
        getattr(fake, method).assert_awaited_once_with(*args)

    async def test_list_deals_filters_by_status(self, http):
        from services.deals.client import DealsClient
        with patch.object(gw.clients, "deals", DealsClient()), patch("shared.clients.base.httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(lambda r: http.requests.append(r) or httpx.Response(200, json=[])), **kw)):
            await gw.list_deals("PAID")
        assert http.requests[0].url.params["status"] == "PAID" and http.requests[0].url.params["limit"] == "100"

    async def test_scan_sends_the_payload_fields(self, http):
        from services.trade_min.client import TradeMinClient
        with patch.object(gw.clients, "trade_min", TradeMinClient()), patch("shared.clients.base.httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(lambda r: http.requests.append(r) or httpx.Response(200, json=[])), **kw)):
            await gw.scan_opportunities({"hs_codes": ["1520"], "target_markets": ["ES"]})
        import json
        assert json.loads(http.requests[0].content) == {"hs_codes": ["1520"], "target_markets": ["ES"]}

    async def test_webhook_ingest_is_not_built(self):
        with pytest.raises(NotImplementedError):
            await gw.ingest_webhook("stripe", MagicMock())


class TestQcProviderHttp:
    """The generic JSON adapter (Intertek / TÜV SÜD). SGS and BV are booked
    through their portals and QIMA has its own contract (tests/test_qc_inspection.py)."""

    @pytest.fixture
    def connector(self, monkeypatch):
        monkeypatch.setenv("INTERTEK_API_KEY", "K")
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        return qc.IntertekConnector()

    @pytest.mark.parametrize(("operation", "arg"), [("quote", {"quantity_mt": 20}), ("book", {}), ("status", "R")])
    async def test_live(self, connector, http, operation, arg):
        assert await getattr(connector, operation)(arg) == {"ok": True}

    @pytest.mark.parametrize(("operation", "arg"), [("quote", {}), ("book", {}), ("status", "R")])
    async def test_failure(self, connector, http, operation, arg):
        http.status = 500
        with pytest.raises(qc.ConnectorError):
            await getattr(connector, operation)(arg)

    async def test_stub_status(self, monkeypatch):
        monkeypatch.delenv("INTERTEK_API_KEY", raising=False)
        result = await qc.IntertekConnector().status("R")
        assert result["stub"] is True and result["status"] == "BOOKED"


class TestMarketIntel:
    def test_supplier_query(self):
        assert "India export pricing" in mi._build_query("1520", "ES", "supplier")

    async def test_search(self, http, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "K")
        http.body = {"results": [{"content": "useful", "title": "t", "url": "u"}]}
        assert len(await mi._search_web("q")) == 1

    async def test_search_http_failure(self, http, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "K")
        http.status = 500
        assert await mi._search_web("q") == []

    async def test_search_bad_shape(self, http, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "K")
        http.body = {"results": "nope"}
        assert await mi._search_web("q") == []

    def test_long_context_is_cut(self):
        chunks = [{"content": "x" * (mi._MAX_CONTEXT_CHARS + 500), "title": "t", "url": "u"}]
        assert mi._format_results(chunks).endswith("…")

    async def test_fetch_failure_is_empty(self, monkeypatch):
        monkeypatch.setattr(mi.settings, "market_search_enabled", True)
        with patch.object(mi, "_search_web", new=AsyncMock(side_effect=RuntimeError)):
            assert await mi.fetch_market_intel("1520", "ES", "buyer") == ""

    @pytest.mark.parametrize(("text", "expected"), [("1 USD = 84.5 INR today", 84.5), ("USD to INR 5000", None), ("forecast: USD INR to hit 90 next year", None)])
    def test_parse_rate(self, text, expected):
        assert mi._parse_usd_inr_rate([{"content": text}]) == expected

    async def test_rate_lookup(self, monkeypatch):
        monkeypatch.setattr(mi.settings, "market_search_enabled", True)
        with patch.object(mi, "_search_web", new=AsyncMock(return_value=[{"content": "1 USD = 84.5 INR"}])):
            assert await mi.fetch_usd_inr_rate() == 84.5

    async def test_rate_lookup_failure(self, monkeypatch):
        monkeypatch.setattr(mi.settings, "market_search_enabled", True)
        with patch.object(mi, "_search_web", new=AsyncMock(side_effect=RuntimeError)):
            assert await mi.fetch_usd_inr_rate() is None


class TestHitlHelpers:
    @pytest.fixture(autouse=True)
    def _repo(self, monkeypatch):
        hitl_repo._repository = hitl_repo.InMemoryHitlApprovalRepository()
        monkeypatch.setattr(hitl, "notify", AsyncMock())
        monkeypatch.setattr(hitl, "execute_callback", AsyncMock())
        monkeypatch.setattr(hitl, "execute_reject_callback", AsyncMock())
        yield
        hitl_repo._repository = None

    def test_bad_threshold(self, monkeypatch):
        monkeypatch.setenv("FOUR_EYES_THRESHOLD_USD", "lots")
        assert hitl.four_eyes_threshold_usd() is None

    async def test_emit_failure_is_contained(self):
        with patch.object(hitl, "get_publisher", side_effect=RuntimeError):
            await hitl._emit(EventTopic.DEAL_EXCEPTION, "D", {})

    async def test_emit(self):
        publisher = MagicMock(publish=AsyncMock())
        with patch.object(hitl, "get_publisher", return_value=publisher):
            await hitl._emit(EventTopic.DEAL_EXCEPTION, "D", {"a": 1})
        publisher.publish.assert_awaited_once()

    @pytest.mark.parametrize(("expires", "has_tz"), [("2030-01-01T00:00:00", True), ("garbage", None)])
    async def test_expiry_parsing(self, monkeypatch, expires, has_tz):
        monkeypatch.setattr(hitl, "_emit", AsyncMock())
        created = await hitl.create_approval(CreateApprovalRequest(deal_id="D", action="ISSUE_PO", autonomy_level="L1", expires_at=expires))
        if has_tz:
            assert created.expires_at.tzinfo is not None

    async def test_edited_body_reaches_the_callback(self, monkeypatch):
        monkeypatch.setattr(hitl, "_emit", AsyncMock())
        created = await hitl.create_approval(CreateApprovalRequest(deal_id="D", action="SEND_NEGOTIATION_EMAIL", autonomy_level="L0", callback_payload={}))
        await hitl.resolve_approval(created.approval_id, ResolveApprovalRequest(decision="APPROVE", resolved_by="ops", modified_body="edited"))
        await asyncio.sleep(0)
        assert hitl.execute_callback.await_args.args[2]["modified_body"] == "edited"

    async def test_soft_reject_runs_the_reject_callback(self, monkeypatch):
        emit = AsyncMock()
        monkeypatch.setattr(hitl, "_emit", emit)
        created = await hitl.create_approval(CreateApprovalRequest(deal_id="D", action="SEND_NEGOTIATION_EMAIL", autonomy_level="L0"))
        await hitl.resolve_approval(created.approval_id, ResolveApprovalRequest(decision="REJECT", resolved_by="ops"))
        await asyncio.sleep(0)
        hitl.execute_reject_callback.assert_awaited_once()
        assert EventTopic.DEAL_EXCEPTION not in [c.args[0] for c in emit.await_args_list]

    async def test_get_unknown(self):
        with pytest.raises(hitl.ApprovalNotFoundError):
            await hitl.get_approval("x")

    async def test_watcher_resolves_expired_and_survives_errors(self, monkeypatch):
        expired = SimpleNamespace(default_on_timeout="REJECT")
        repo = MagicMock(list_pending_expired=AsyncMock(side_effect=[[expired], RuntimeError("x"), asyncio.CancelledError]))
        monkeypatch.setattr(hitl, "get_repository", lambda: repo)
        monkeypatch.setattr(hitl, "_auto_resolve", AsyncMock())
        with patch.object(hitl.asyncio, "sleep", new=AsyncMock()):
            await hitl.run_timeout_watcher()
        hitl._auto_resolve.assert_awaited_once()


class TestIdentity:
    @pytest.fixture(autouse=True)
    def _clean(self):
        idn.clear_cache()
        yield
        idn.clear_cache()

    @staticmethod
    def _settings(**kw):
        base = {"gateway_url": "https://gw.test", "introspection_cache_seconds": 60, "introspection_timeout_seconds": 5.0, "account_id": "", "trust_gateway_headers": False, "timeout_seconds": 5.0}
        return SimpleNamespace(**{**base, **kw})

    @pytest.mark.parametrize(("status", "text"), [(500, None), (200, "not json")])
    async def test_bad_gateway_answers(self, http, status, text):
        http.status, http.text = status, text if text else "x"
        with patch.object(idn, "_settings", return_value=self._settings()), pytest.raises(idn.IdentityError) as err:
            await idn._ask_gateway("t")
        assert err.value.status == 503

    def test_no_user_header(self):
        assert idn.identity_from_headers({}) is None

    def test_no_expected_account_warns_once(self, monkeypatch):
        monkeypatch.setattr(idn, "_warned_no_account", False)
        with patch.object(idn, "_settings", return_value=self._settings()):
            idn.check_account(MagicMock(account_id="X"))
            idn.check_account(MagicMock(account_id="Y"))
        assert idn._warned_no_account is True

    async def test_identity_without_email_is_refused(self):
        with patch.object(idn, "identity_for_token", new=AsyncMock(return_value=MagicMock(email=""))), patch.object(idn, "_settings", return_value=self._settings()), pytest.raises(idn.IdentityError) as err:
            await idn.resolve({}, "t")
        assert err.value.status == 401

    async def test_full_cache_is_cleared(self, monkeypatch):
        monkeypatch.setattr(idn, "_CACHE_MAX_ENTRIES", 1)
        idn._CACHE["old"] = (0.0, None)
        body = {"user_id": "U", "email": "a@b.co"}
        with patch.object(idn, "ask_gateway", new=AsyncMock(return_value=body)), patch.object(idn, "_settings", return_value=self._settings()):
            await idn.identity_for_token("t")
        assert "old" not in idn._CACHE


class TestEmailHistoryMongo:
    @pytest.fixture
    def repo(self):
        with patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=lambda *_a, **_k: AsyncMongoMockClient()):
            yield eh.MongoEmailHistoryRepository("mongodb://x", "d", "c")

    @staticmethod
    def _email(**kw):
        return make(EmailHistoryRecord, **{"email_id": "E1", "negotiation_id": "N1", "deal_id": "D1", "status": "sent", **kw})

    async def test_round_trip(self, repo):
        await repo._ensure_indexes()
        await repo.save(self._email())
        assert (await repo.get("E1")).negotiation_id == "N1"
        assert await repo.get("x") is None

    async def test_lists(self, repo):
        await repo.save(self._email())
        await repo.save(self._email(email_id="E2", negotiation_id="N2", deal_id="D2"))
        assert [e.email_id for e in await repo.list_by_negotiation("N1")] == ["E1"]
        assert [e.email_id for e in await repo.list_by_deal("D2")] == ["E2"]

    async def test_init(self, monkeypatch):
        saved = eh._email_repo
        monkeypatch.setattr(eh.settings, "negotiation_mongo_uri", "mongodb://x")
        with patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=lambda *_a, **_k: AsyncMongoMockClient()):
            await eh.init_email_repository()
        assert isinstance(eh.get_email_repository(), eh.MongoEmailHistoryRepository)
        eh._email_repo = saved

    async def test_init_failure_falls_back(self, monkeypatch):
        saved = eh._email_repo
        monkeypatch.setattr(eh.settings, "negotiation_mongo_uri", "mongodb://x")
        with patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=RuntimeError):
            await eh.init_email_repository()
        assert isinstance(eh.get_email_repository(), eh.InMemoryEmailHistoryRepository)
        eh._email_repo = saved
