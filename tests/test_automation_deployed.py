"""Automation mode on a deployed backend: no .env file, settings come from the platform,
a gateway in front with a short timeout (services/automation/safety.py, service.py)."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.automation import safety, service
from services.automation.service import AutomationError


@pytest.fixture(autouse=True)
def _no_leftover_processes():
    """These tests put fake child processes on the module; never leave one behind."""
    saved = service._view_process, service._process
    yield
    service._view_process, service._process = saved
    service._logs["view"].clear()
    service._logs["worker"].clear()


@pytest.fixture
def deployed(monkeypatch, tmp_path):
    """No .env file; real settings only in the process environment, as on a hosting platform."""
    monkeypatch.setattr(safety, "REPO_ROOT", tmp_path)
    for key, value in {
        "MONGO_URI": "mongodb+srv://deployed.invalid/portless", "LLM_GATEWAY_URL": "https://gateway.invalid",
        "LLM_GATEWAY_EMAIL": "svc@portless.invalid", "LLM_GATEWAY_PASSWORD": "pw", "LLM_GATEWAY_ACCOUNT_ID": "acc",
        "AUTH_PORTLESS_EMAILS": "ops@portless.invalid", "GMAIL_APP_PASSWORD": "real-smtp", "TAVILY_API_KEY": "real-key",
        "DEAL_STALL_SLA_HOURS": "48", "DEALS_MONGO_URI": "mongodb://other.invalid", "APIFY_TOKEN": "apify-real",
        "PATH": "/usr/bin", "HOME": "/home/app", "PYTHONUNBUFFERED": "1",
    }.items():
        monkeypatch.setenv(key, value)


def test_a_run_gets_none_of_the_platforms_settings(deployed):
    env = safety.worker_env()
    assert env["MONGO_URI"] == "mongodb+srv://deployed.invalid/portless"   # kept: the automation database's cluster
    assert env["LLM_GATEWAY_URL"] == "https://gateway.invalid" and env["LLM_GATEWAY_PASSWORD"] == "pw"
    assert env["MONGO_DB_NAME"] == safety.AUTOMATION_DB
    assert "TAVILY_API_KEY" not in env and "DEAL_STALL_SLA_HOURS" not in env and "DEALS_MONGO_URI" not in env
    assert env["GMAIL_APP_PASSWORD"] == "" and env["APIFY_TOKEN"] == ""
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/app" and env["PYTHONUNBUFFERED"] == "1"


def test_the_view_server_gets_its_configured_port(deployed):
    assert safety.worker_env(view=True)["AUTOMATION_VIEW_PORT"] == str(safety.VIEW_PORT)


def test_the_view_server_accepts_the_deployed_staff_list(deployed):
    """Without a .env file the staff list lives in the platform's environment; the view server must use it."""
    emails = safety.worker_env(view=True)["AUTH_PORTLESS_EMAILS"].split(",")
    assert "ops@portless.invalid" in emails and set(safety.STAFF_EMAILS) <= set(emails)


def test_a_live_scan_uses_the_deployed_apify_token(deployed):
    assert safety.worker_env(live_apify=True)["APIFY_TOKEN"] == "apify-real"


def test_the_run_environment_passes_the_isolation_check(deployed, monkeypatch):
    env = safety.worker_env()
    for key in list(sys.modules["os"].environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    safety.assert_isolated()


class _Booting:
    """A child process that is still starting (never ready within the wait)."""
    pid = 4242

    def poll(self):
        return None


class _Died:
    pid = 4243

    def poll(self):
        return 1


async def test_a_booting_view_server_answers_starting_quickly(monkeypatch):
    monkeypatch.setenv("AUTOMATION_VIEW_WAIT_SECONDS", "0.6")
    monkeypatch.setattr(service, "_view_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_db", lambda: object())
    monkeypatch.setattr(service, "_start_view_process", lambda: setattr(service, "_view_process", _Booting()))
    service._started["view"] = service.now()
    loop = asyncio.get_running_loop()
    began = loop.time()
    with pytest.raises(AutomationError) as err:
        await service._ensure_view_server(SimpleNamespace())
    assert err.value.status == 503 and "is starting" in err.value.detail
    assert loop.time() - began < 3     # never holds the gateway for 90 s


async def test_a_view_server_that_died_says_why(monkeypatch):
    monkeypatch.setenv("AUTOMATION_VIEW_WAIT_SECONDS", "0.6")
    monkeypatch.setattr(service, "_view_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_db", lambda: object())
    monkeypatch.setattr(service, "_start_view_process", lambda: setattr(service, "_view_process", _Died()))
    service._logs["view"].extend(["Automation refused: MONGO_DB_NAME is 'portless'"])
    with pytest.raises(AutomationError) as err:
        await service._ensure_view_server(SimpleNamespace())
    assert err.value.status == 503 and "stopped while starting" in err.value.detail and "Automation refused" in err.value.detail


async def test_production_never_starts_the_view_server(monkeypatch):
    monkeypatch.setenv("ENV_PROFILE", "production")
    monkeypatch.setattr(service, "_view_ready", AsyncMock(return_value=False))
    with pytest.raises(AutomationError) as err:
        await service._ensure_view_server(SimpleNamespace())
    assert err.value.status == 403


@pytest.mark.parametrize("env,mongo,starts", [
    ({}, "mongodb://x", True),
    ({"ENV_PROFILE": "production"}, "mongodb://x", False),
    ({"AUTOMATION_VIEW_AUTOSTART": "false"}, "mongodb://x", False),
    ({"AUTOMATION_WORKER": "1"}, "mongodb://x", False),
    ({}, "", False),
])
async def test_warm_up_starts_the_view_server_only_where_it_belongs(monkeypatch, env, mongo, starts):
    for key in ("ENV_PROFILE", "AUTOMATION_VIEW_AUTOSTART", "AUTOMATION_WORKER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from shared.config import settings
    monkeypatch.setattr(settings, "mongo_uri", mongo)
    monkeypatch.setattr(service, "_view_ready", AsyncMock(return_value=False))
    started = []
    monkeypatch.setattr(service, "_start_view_process", lambda: started.append(True))
    await service.warm_up()
    assert bool(started) is starts


async def test_diagnostics_reports_the_view_server_and_its_log(monkeypatch):
    from shared.config import settings
    monkeypatch.setattr(settings, "mongo_uri", "mongodb://x")
    monkeypatch.setattr(service, "_view_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_view_process", _Died())
    service._logs["view"].clear()
    service._logs["view"].append("Traceback: boom")
    report = await service.diagnostics()
    assert report["mongo_configured"] is True and report["database"] == safety.AUTOMATION_DB
    assert report["view"]["ready"] is False and report["view"]["exit_code"] == 1
    assert report["view"]["log_tail"] == ["Traceback: boom"]


@pytest.mark.parametrize("var", ["VERCEL", "AWS_LAMBDA_FUNCTION_NAME"])
async def test_serverless_says_why_instead_of_spawning(monkeypatch, var):
    """A serverless function cannot keep the view server or a run alive: refuse in plain words."""
    monkeypatch.delenv("ENV_PROFILE", raising=False)
    monkeypatch.setenv(var, "1")
    monkeypatch.setattr(service, "_view_ready", AsyncMock(return_value=False))
    spawned = []
    monkeypatch.setattr(service, "_start_view_process", lambda: spawned.append(True))
    with pytest.raises(AutomationError) as err:
        await service._ensure_view_server(SimpleNamespace())
    assert err.value.status == 501 and "long-running server" in err.value.detail
    with pytest.raises(AutomationError):
        await service.start_run("staff")
    await service.warm_up()
    assert not spawned
    assert (await service.diagnostics())["runs_allowed"] is False
