"""LLM failure rate and refused webhooks page someone (todo: monitoring and alerting)."""
from __future__ import annotations

import pytest

from shared import failure_monitor as fm


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for v in ("FAILURE_WINDOW_MINUTES", "LLM_FAILURE_MIN_CALLS", "LLM_FAILURE_RATE_PCT", "WEBHOOK_REFUSED_ALERT"):
        monkeypatch.delenv(v, raising=False)
    fm.reset_for_tests()
    yield
    fm.reset_for_tests()


def test_llm_alert_needs_enough_calls_and_a_high_rate():
    for i in range(4):
        fm.record("llm", "buyer_agent", False, now=100.0 + i)
    assert fm.status(now=110.0)["alerts"] == []          # 4 calls: too few
    fm.record("llm", "buyer_agent", True, now=105.0)
    fm.record("llm", "buyer_agent", False, now=106.0)
    (alert,) = fm.status(now=110.0)["alerts"]
    assert "buyer_agent" in alert and "5 of 6" in alert
    assert fm.status(now=110.0 + 16 * 60)["alerts"] == []  # out of the window


def test_refused_webhooks_alert():
    for i in range(5):
        fm.record("webhook", "Tazapay", False, now=100.0 + i)
    fm.record("webhook", "Payoneer", True, now=101.0)
    (alert,) = fm.status(now=110.0)["alerts"]
    assert alert.startswith("WEBHOOKS REFUSED: Tazapay")


async def test_each_alert_once_per_hour():
    sent = []

    async def send(m):
        sent.append(m)
    for i in range(6):
        fm.record("webhook", "Stripe", False, now=100.0 + i)
    assert len(await fm.check_and_alert(now=110.0, send=send, business=[])) == 1
    assert await fm.check_and_alert(now=200.0, send=send, business=[]) == []
    for i in range(6):
        fm.record("webhook", "Stripe", False, now=110.0 + 3600 + i)
    assert len(await fm.check_and_alert(now=120.0 + 3600, send=send, business=[])) == 1 and len(sent) == 2


async def test_signature_check_and_llm_client_feed_it(monkeypatch):
    from shared.webhooks import signature_valid

    class TazapayConnector:
        _webhook_secret = "s"

        async def webhook_verify(self, h, b):
            return False
    assert await signature_valid(TazapayConnector(), {}, b"{}") is False
    assert fm.status()["rows"][0] == {"kind": "webhook", "key": "Tazapay", "total": 1, "failed": 1, "failure_pct": 100.0}


async def test_money_gate_waiting_and_exceptions_piling_up(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    now = datetime.now(UTC)
    gates = [SimpleNamespace(action="RELEASE_SUPPLIER_PAYOUT", deal_id="D1", created_at=now - timedelta(hours=30)),
             SimpleNamespace(action="SEND_NEGOTIATION_EMAIL", deal_id="D2", created_at=now - timedelta(hours=50)),
             SimpleNamespace(action="RELEASE_ESCROW_TRANCHE", deal_id="D3", created_at=now - timedelta(hours=2))]

    class _Hitl:
        async def list_by_status(self, status):
            return gates

    class _Exc:
        async def list_all(self, limit=0):
            return [SimpleNamespace(status="OPEN") for _ in range(12)]
    monkeypatch.setattr("services.hitl_approval.repository.get_repository", lambda: _Hitl())
    monkeypatch.setattr("services.exception_resolver.repository.get_repository", lambda: _Exc())
    alerts = await fm.business_alerts(now)
    assert alerts[0].startswith("MONEY GATES WAITING: 1 money gate") and "D1" in alerts[0]
    assert alerts[1] == "EXCEPTIONS PILING UP: 12 open or escalated (Exceptions page)"


async def test_business_alert_once_per_period_even_when_the_count_changes():
    sent = []

    async def send(m):
        sent.append(m)
    await fm.check_and_alert(now=10.0, send=send, business=["EXCEPTIONS PILING UP: 12 open or escalated (Exceptions page)"])
    await fm.check_and_alert(now=20.0, send=send, business=["EXCEPTIONS PILING UP: 13 open or escalated (Exceptions page)"])
    assert len(sent) == 1
