"""services/automation/safety.py at 100 %: the guard that keeps an automation run off the live
database, mailboxes, payment keys and the network. It protects production data, so it is part of
the coverage target even though the rest of services/automation (the test runner) is not."""
from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services.automation import safety


def test_worker_env_without_mongo_with_live_apify_and_view(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "apify-live")
    monkeypatch.setenv("AUTH_PORTLESS_EMAILS", "boss@portless.example")
    with patch("dotenv.dotenv_values", return_value={}):
        env = safety.worker_env(use_mongo=False, live_apify=True, view=True)
    assert env["MONGO_URI"] == "" and env["APIFY_TOKEN"] == "apify-live" and env["AUTOMATION_APIFY"] == "1"
    assert env["MONGO_DB_NAME"] == safety.AUTOMATION_DB and env["INTEGRATION_MODE"] == "sandbox"
    assert "boss@portless.example" in env["AUTH_PORTLESS_EMAILS"]


_REPO_MODULES = ["agents.negotiation_agent.repository", "services.dealmaker.repository", "services.deals.repository",
                 "services.docugen.repository", "services.escrow_guard.repository",
                 "services.exception_resolver.repository", "services.hitl_approval.repository",
                 "services.shield_link.repository"]


def _with_repos(cls):
    patches = [patch(f"{m}.get_repository", return_value=cls()) for m in _REPO_MODULES]
    for p in patches:
        p.start()
    return patches


def test_worker_env_blanks_dotenv_keys_and_keeps_mongo():
    with patch("dotenv.dotenv_values", return_value={"SECRET_KEY": "x", "MONGO_URI": "mongodb://real/db"}):
        env = safety.worker_env()
    assert env["SECRET_KEY"] == "" and env["MONGO_URI"] == "mongodb://real/db"
    assert "AUTOMATION_APIFY" not in env and env["APIFY_MAX_RUNS_PER_DAY"] == "0"


def test_on_the_automation_database_or_refused():
    class InMemoryFallbackRepository:
        pass

    class MongoRepo:
        pass
    patches = _with_repos(InMemoryFallbackRepository)
    try:
        with pytest.raises(SystemExit, match="not on the database"):
            safety.assert_on_automation_database()
    finally:
        for p in patches:
            p.stop()
    patches = _with_repos(MongoRepo)
    try:
        safety.assert_on_automation_database()
    finally:
        for p in patches:
            p.stop()


async def test_startup_resume_is_switched_off(monkeypatch):
    from shared.orchestration import continuations
    monkeypatch.setattr(continuations, "resume_parked_negotiations", continuations.resume_parked_negotiations)
    safety.without_startup_resume()
    assert await continuations.resume_parked_negotiations() is None


def test_assert_isolated(monkeypatch):
    monkeypatch.setenv("MONGO_DB_NAME", safety.AUTOMATION_DB)
    monkeypatch.setenv("AUTOMATION_WORKER", "1")
    monkeypatch.setenv("INTEGRATION_MODE", "sandbox")
    for key in safety._MUST_BE_BLANK:  # noqa: SLF001
        monkeypatch.delenv(key, raising=False)
    safety.assert_isolated()
    monkeypatch.setenv("AUTOMATION_APIFY", "1")
    monkeypatch.setenv("APIFY_TOKEN", "allowed for a live scan")
    safety.assert_isolated()
    monkeypatch.setenv("AUTOMATION_WORKER", "0")
    monkeypatch.setenv("MONGO_DB_NAME", "portless")
    monkeypatch.setenv("INTEGRATION_MODE", "live")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "real")
    with pytest.raises(SystemExit) as exc:
        safety.assert_isolated()
    msg = str(exc.value)
    assert "MONGO_DB_NAME" in msg and "automation worker" in msg and "INTEGRATION_MODE" in msg and "GMAIL_APP_PASSWORD" in msg


def test_network_guard(monkeypatch):
    calls: list = []

    def fake_connect(sock, address):
        calls.append(("connect", address))

    def fake_connect_ex(sock, address):
        calls.append(("connect_ex", address))
        return 0

    def fake_lookup(host, *args, **kwargs):
        calls.append(("dns", host))
        return [(None, None, None, None, ("10.1.2.3", 0))]

    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", fake_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", fake_lookup)
    monkeypatch.setattr(safety.asyncio, "set_event_loop_policy", MagicMock())
    monkeypatch.setattr(safety.sys, "platform", "win32")
    monkeypatch.setenv("LLM_GATEWAY_URL", "https://gateway.example/api")
    monkeypatch.setenv("AUTOMATION_APIFY", "1")
    monkeypatch.setenv("MONGO_URI", "mongodb+srv://u:p@cluster0.abc.mongodb.net/db?retryWrites=true")
    guard = safety.NetworkGuard()
    guard.install()
    safety.asyncio.set_event_loop_policy.assert_called_once()
    sock = SimpleNamespace()

    socket.socket.connect(sock, ("127.0.0.1", 27017))          # loopback: allowed
    socket.socket.connect(sock, "/tmp/unix.sock")               # not an address tuple: allowed
    with pytest.raises(ConnectionRefusedError):
        socket.socket.connect(sock, ("8.8.4.4", 443))
    assert socket.socket.connect_ex(sock, ("9.9.9.9", 53)) == 0  # DNS port: allowed
    assert socket.socket.connect_ex(sock, ("8.8.4.4", 443)) == 111

    assert socket.getaddrinfo("127.0.0.1", 80)[0][4][0] == "10.1.2.3"  # an allowed IP passes straight
    assert socket.getaddrinfo(None, 80)
    socket.getaddrinfo(b"gateway.example", 443)                        # bytes host, allowed
    socket.getaddrinfo("shard-00.abc.mongodb.net", 27017)              # the Mongo cluster suffix
    socket.getaddrinfo("api.apify.com", 443)                           # live-scan market data
    socket.socket.connect(sock, ("10.1.2.3", 443))                     # resolved IPs become allowed
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("smtp.gmail.com", 587)
    assert guard.blocked == ["connect ('8.8.4.4', 443)", "connect_ex ('8.8.4.4', 443)", "dns smtp.gmail.com"]


def test_network_guard_without_gateway_or_mongo(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    monkeypatch.setattr(safety.sys, "platform", "linux")
    monkeypatch.delenv("LLM_GATEWAY_URL", raising=False)
    monkeypatch.delenv("AUTOMATION_APIFY", raising=False)
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017/x")
    guard = safety.NetworkGuard()
    guard.install()
    assert guard._suffixes == () and "localhost" in guard._hosts  # noqa: SLF001
    monkeypatch.delenv("MONGO_URI")
    bare = safety.NetworkGuard()
    bare.install()
    assert bare._hosts == {"localhost"}  # noqa: SLF001
