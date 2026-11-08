"""Booting and stopping: one failing part never stops the rest; standalone apps start cleanly."""
from __future__ import annotations

import asyncio
from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as monolith
from services.email_gateway import main as gateway_main
from services.trade_min import main as trade_main
from shared.bus import factory

STARTUP = {
    "trade_min": "services.trade_min.main.on_startup",
    "deals": "services.deals.main.on_startup",
    "sourcing": "services.sourcing.main.on_startup",
    "negotiation": "agents.negotiation_agent.main.on_startup",
    "dealmaker": "services.dealmaker.repository.init_repository",
    "docugen": "services.docugen.repository.init_repository",
    "shield_link": "services.shield_link.repository.init_repository",
    "escrow": "services.escrow_guard.repository.init_repository",
    "hitl": "services.hitl_approval.repository.init_repository",
    "exception_resolver": "services.exception_resolver.repository.init_repository",
    "auth": "services.auth.repository.init_repository",
    "email_processor": "services.email_processor.main.on_startup",
    "email_gateway": "services.email_gateway.main.on_startup",
    "autonomy_policy": "shared.orchestration.autonomy_policy.load_policy",
    "event_bus": "shared.bus.init_event_bus",
    "resume_negotiations": "shared.orchestration.continuations.resume_parked_negotiations",
    "resume_auto_flow": "shared.orchestration.auto_flow.resume_in_flight",
}
SHUTDOWN = {
    "event_bus": "shared.bus.close_event_bus",
    "email_gateway": "services.email_gateway.main.on_shutdown",
    "email_processor": "services.email_processor.main.on_shutdown",
    "auth": "services.auth.repository.close_repository",
    "exception_resolver": "services.exception_resolver.repository.close_repository",
    "llm_client": "shared.llm_gateway.remote.aclose_remote_client",
    "hitl": "services.hitl_approval.repository.close_repository",
    "escrow": "services.escrow_guard.repository.close_repository",
    "shield_link": "services.shield_link.repository.close_repository",
    "docugen": "services.docugen.repository.close_repository",
    "dealmaker": "services.dealmaker.repository.close_repository",
    "negotiation": "agents.negotiation_agent.main.on_shutdown",
    "sourcing": "services.sourcing.main.on_shutdown",
    "deals": "services.deals.main.on_shutdown",
    "trade_min": "services.trade_min.main.on_shutdown",
}
WATCHERS = (
    "agents.negotiation_agent.service.run_stale_watcher",
    "services.hitl_approval.service.run_timeout_watcher",
    "shared.orchestration.deal_watcher.run_deal_watcher",
    "shared.orchestration.digest.run_digest_scheduler",
)


async def _forever():
    await asyncio.Event().wait()


class _Hooks:
    def __init__(self):
        self.started = {name: AsyncMock() for name in STARTUP}
        self.stopped = {name: AsyncMock() for name in SHUTDOWN}

    def __enter__(self):
        self._patches = [patch(STARTUP[n], new=m) for n, m in self.started.items()]
        self._patches += [patch(SHUTDOWN[n], new=m) for n, m in self.stopped.items()]
        self._patches += [patch(w, new=_forever) for w in WATCHERS]
        self._patches += [
            patch("shared.readiness.assert_ready_or_log"),
            patch("shared.knowledge.aclose", new=AsyncMock()),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *_):
        for item in self._patches:
            item.stop()


@pytest.fixture
def hooks(monkeypatch):
    monkeypatch.setenv("AUTO_FLOW_ENABLED", "true")
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")
    with _Hooks() as active:
        yield active


async def _run_lifespan():
    async with monolith.lifespan(FastAPI()):
        pass


class TestMonolithBoot:
    async def test_every_part_starts_and_stops(self, hooks):
        await _run_lifespan()
        for name, hook in {**hooks.started}.items():
            if name not in ("resume_negotiations", "resume_auto_flow"):
                hook.assert_awaited_once()
        for hook in hooks.stopped.values():
            hook.assert_awaited_once()

    @pytest.mark.parametrize("name", sorted(STARTUP))
    async def test_startup_failure_does_not_stop_the_boot(self, hooks, name):
        hooks.started[name].side_effect = RuntimeError("down")
        await _run_lifespan()
        hooks.stopped["trade_min"].assert_awaited_once()
        assert hooks.started["email_gateway"].await_count == 1

    @pytest.mark.parametrize("name", sorted(SHUTDOWN))
    async def test_shutdown_failure_does_not_stop_the_rest(self, hooks, name):
        hooks.stopped[name].side_effect = RuntimeError("down")
        await _run_lifespan()
        for other, hook in hooks.stopped.items():
            if other != name:
                hook.assert_awaited_once()

    @pytest.mark.parametrize("watcher", ["shared.orchestration.deal_watcher.run_deal_watcher", "shared.orchestration.digest.run_digest_scheduler"])
    async def test_watcher_that_cannot_start(self, hooks, watcher):
        with patch(watcher, side_effect=RuntimeError("no")):
            await _run_lifespan()
        hooks.stopped["trade_min"].assert_awaited_once()

    async def test_watcher_that_already_finished(self, hooks):
        with patch("shared.orchestration.deal_watcher.run_deal_watcher", new=AsyncMock()), \
             patch("services.hitl_approval.service.run_timeout_watcher", new=AsyncMock()), \
             patch("agents.negotiation_agent.service.run_stale_watcher", new=AsyncMock()):
            async with monolith.lifespan(FastAPI()):
                await asyncio.sleep(0)


class TestMonolithRoutes:
    @pytest.fixture
    def api(self):
        with patch("shared.auth.middleware.AuthMiddleware.dispatch", new=lambda _s, request, call_next: call_next(request)):
            yield TestClient(monolith.create_app())

    def test_readiness(self, api):
        with patch("shared.readiness.evaluate", return_value={"ready": False}):
            assert api.get("/health/readiness").json() == {"ready": False}

    def test_health(self, api):
        assert api.get("/health").json()["status"] == "ok"

    def test_downstream_view(self, api):
        body = api.get("/health/downstream").json()
        assert body and set(body.values()) == {"ok"}


STANDALONE = [
    "services.dealmaker.main", "services.deals.main", "services.docugen.main",
    "services.escrow_guard.main", "services.exception_resolver.main", "services.hitl_approval.main", "services.match_engine.main",
    "services.shield_link.main", "services.sourcing.main", "agents.negotiation_agent.main",
]


@pytest.mark.parametrize("path", STANDALONE)
async def test_standalone_app_boots_and_closes_the_bus(path):
    module = import_module(path)
    repo_path = path.replace(".main", ".repository")
    try:
        repo_module = import_module(repo_path)
    except ModuleNotFoundError:
        repo_module = None
    saved = getattr(repo_module, "_repository", None)
    app = module.create_app()
    with patch("shared.bus.init_event_bus", new=AsyncMock()) as started, patch("shared.bus.close_event_bus", new=AsyncMock()) as closed:
        try:
            async with app.router.lifespan_context(app):
                pass
        finally:
            if repo_module is not None and hasattr(repo_module, "_repository"):
                repo_module._repository = saved
    assert started.await_args.kwargs == {"consume": False}
    closed.assert_awaited_once()


class TestTradeMinStartup:
    @pytest.mark.parametrize(("call", "kwargs"), [(trade_main._run_nightly_scan, {}), (trade_main._run_reorder_window, {"min_arbitrage_rank": 10.0})])
    async def test_scheduled_scans(self, call, kwargs):
        with patch("services.trade_min.service.scan_opportunities", new=AsyncMock(return_value=[])) as scan:
            await call()
        for name, value in kwargs.items():
            assert getattr(scan.await_args.args[0], name) == value

    @pytest.mark.parametrize("call", [trade_main._run_nightly_scan, trade_main._run_reorder_window])
    async def test_scheduled_scan_failure_is_contained(self, call):
        with patch("services.trade_min.service.scan_opportunities", new=AsyncMock(side_effect=RuntimeError)):
            await call()

    def test_cron(self):
        assert trade_main._cron_to_kwargs("0 2 * * 1") == {"minute": "0", "hour": "2", "day": "*", "month": "*", "day_of_week": "1"}

    @pytest.mark.parametrize("expression", ["", "0 2 *", "0 2 * * * *"])
    def test_bad_cron(self, expression):
        with pytest.raises(ValueError, match="5-field"):
            trade_main._cron_to_kwargs(expression)

    async def test_catalogue_failure_does_not_stop_the_scheduler(self, monkeypatch):
        scheduler = MagicMock(running=True)
        monkeypatch.setattr(trade_main, "AsyncIOScheduler", lambda **_k: scheduler)
        monkeypatch.setattr(trade_main, "init_repository", AsyncMock())
        monkeypatch.setattr(trade_main, "init_cache", AsyncMock())
        monkeypatch.setattr(trade_main, "close_repository", AsyncMock(), raising=False)
        monkeypatch.setattr(trade_main, "close_cache", AsyncMock(), raising=False)
        with patch("shared.catalogue.load_catalogue", new=AsyncMock(side_effect=RuntimeError)):
            await trade_main.on_startup()
        scheduler.start.assert_called_once()
        await trade_main.on_shutdown()
        scheduler.shutdown.assert_called_once_with(wait=False)

    async def test_shutdown_without_scheduler(self, monkeypatch):
        monkeypatch.setattr(trade_main, "_scheduler", None)
        monkeypatch.setattr(trade_main, "close_repository", AsyncMock(), raising=False)
        monkeypatch.setattr(trade_main, "close_cache", AsyncMock(), raising=False)
        await trade_main.on_shutdown()


class TestEmailGatewayStartup:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(gateway_main, "_buyer_poller", None)
        monkeypatch.setattr(gateway_main, "_supplier_poller", None)

    def test_in_process_by_default(self, monkeypatch):
        from services.email_processor import service as processor
        monkeypatch.setattr(gateway_main.settings, "forward_via_http", False)
        assert gateway_main._forward_callback() is processor.process_inbound

    def test_http_forwarding(self, monkeypatch):
        monkeypatch.setattr(gateway_main.settings, "forward_via_http", True)
        assert gateway_main._forward_callback().__name__ == "submit_inbound"

    async def test_one_shared_folder_runs_one_poller(self, monkeypatch):
        monkeypatch.setattr(gateway_main.settings, "imap_buyer_folder", "INBOX")
        monkeypatch.setattr(gateway_main.settings, "imap_supplier_folder", "INBOX")
        poller = MagicMock(start=AsyncMock(), stop=AsyncMock())
        with patch.object(gateway_main, "ImapPoller", return_value=poller) as build:
            await gateway_main.on_startup()
        assert build.call_args.kwargs["party_hint"] is None and gateway_main._supplier_poller is None
        await gateway_main.on_shutdown()
        poller.stop.assert_awaited_once()

    async def test_two_folders_run_two_tagged_pollers(self, monkeypatch):
        monkeypatch.setattr(gateway_main.settings, "imap_buyer_folder", "B")
        monkeypatch.setattr(gateway_main.settings, "imap_supplier_folder", "S")
        buyer, supplier = (MagicMock(start=AsyncMock(), stop=AsyncMock()) for _ in range(2))
        with patch.object(gateway_main, "create_buyer_poller", return_value=buyer), patch.object(gateway_main, "create_supplier_poller", return_value=supplier):
            await gateway_main.on_startup()
        buyer.start.assert_awaited_once()
        supplier.start.assert_awaited_once()
        await gateway_main.on_shutdown()
        supplier.stop.assert_awaited_once()

    async def test_shutdown_without_pollers(self):
        await gateway_main.on_shutdown()


class TestEventBusFactory:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        saved = factory._active_publisher, factory._active_nats_bus
        factory._active_publisher, factory._active_nats_bus = None, None
        factory._lock = asyncio.Lock()
        monkeypatch.setattr(factory, "set_publisher", MagicMock())
        monkeypatch.setattr(factory, "_register_continuations", MagicMock())
        yield
        factory._active_publisher, factory._active_nats_bus = saved

    def test_relative_creds_path(self, monkeypatch, tmp_path):
        (tmp_path / "x.creds").write_text("c")
        monkeypatch.setattr(factory, "_REPO_ROOT", tmp_path)
        monkeypatch.setenv("NATS_CREDS_FILE", "x.creds")
        assert factory._resolve_creds_file() == str(tmp_path / "x.creds")

    def test_missing_creds_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NATS_CREDS_FILE", str(tmp_path / "nope.creds"))
        assert factory._resolve_creds_file() is None

    def test_unknown_backend_is_auto(self, monkeypatch):
        monkeypatch.setenv("EVENT_BUS_BACKEND", "weird")
        monkeypatch.setattr(factory, "_resolve_creds_file", lambda: None)
        assert factory._resolve_backend() == ("inprocess", None)

    async def test_resolution_failure_uses_in_process(self):
        with patch.object(factory, "_resolve_backend", side_effect=RuntimeError):
            assert isinstance(await factory.init_event_bus(), factory.InProcessEventBus)

    async def test_nats_failure_falls_back_even_if_close_fails(self):
        bus = MagicMock(connect=AsyncMock(side_effect=RuntimeError("no")), close=AsyncMock(side_effect=RuntimeError("x")))
        with patch("shared.bus.nats_bus.NatsJetStreamBus", return_value=bus):
            assert isinstance(await factory._init_nats(None, False), factory.InProcessEventBus)

    async def test_nats_close_error_is_contained(self):
        factory._active_publisher = object()
        factory._active_nats_bus = MagicMock(close=AsyncMock(side_effect=RuntimeError))
        await factory.close_event_bus()
        assert factory._active_nats_bus is None and factory._active_publisher is None
