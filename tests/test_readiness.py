"""Production readiness gate (shared/readiness.py)."""
from __future__ import annotations

import pytest

from shared import readiness

_ALL_VARS = [
    "MONGO_URI", "EVENT_BUS_BACKEND", "NATS_CREDS_FILE", "TAZAPAY_API_KEY", "TAZAPAY_API_SECRET",
    "ESCROW_COM_API_KEY", "ESCROW_COM_EMAIL", "TAZAPAY_WEBHOOK_SECRET", "ESCROW_COM_WEBHOOK_SECRET",
    "STRIPE_WEBHOOK_SECRET", "PAYONEER_WEBHOOK_SECRET", "PAYONEER_CLIENT_ID", "PAYONEER_CLIENT_SECRET",
    "AUTONOMY_MONEY_CEILING", "APIFY_TOKEN", "COMTRADE_SUBSCRIPTION_KEY", "CENSUS_API_KEY",
    "AUTO_FLOW_ENABLED", "GMAIL_SENDER_ADDRESS", "GMAIL_APP_PASSWORD", "IMAP_USER",
    "IMAP_PASSWORD", "PORTLESS_EMAIL_DOMAIN", "GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET",
    "GOOGLE_DRIVE_REFRESH_TOKEN", "SGS_API_KEY", "QIMA_API_KEY", "COGOPORT_API_KEY", "FREIGHTOS_API_KEY",
    "ICEGATE_API_KEY", "LLM_GATEWAY_URL", "LLM_GATEWAY_TOKEN", "LLM_GATEWAY_EMAIL",
    "LLM_GATEWAY_PASSWORD", "ENV_PROFILE", "READINESS_ENFORCE",
]


@pytest.fixture
def bare_env(monkeypatch):
    for v in _ALL_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("APIFY_TOKEN", "")
    monkeypatch.setenv("APIFY_TOKEN_AUTODISCOVER", "false")
    from shared.config import settings
    monkeypatch.setattr(settings, "mongo_uri", "")
    from services.dealmaker.config import settings as dm
    monkeypatch.setattr(dm, "portless_entity_iec", "")
    monkeypatch.setattr(dm, "portless_entity_gstin", "")
    return monkeypatch


def _fill(monkeypatch):
    from shared.config import settings
    monkeypatch.setattr(settings, "mongo_uri", "mongodb://x")
    monkeypatch.setenv("TAZAPAY_API_KEY", "k")
    monkeypatch.setenv("TAZAPAY_API_SECRET", "s")
    monkeypatch.setenv("TAZAPAY_WEBHOOK_SECRET", "w")
    monkeypatch.setenv("PAYONEER_WEBHOOK_SECRET", "w2")
    monkeypatch.setenv("PAYONEER_CLIENT_ID", "p")
    monkeypatch.setenv("PAYONEER_CLIENT_SECRET", "ps")
    monkeypatch.setenv("COMTRADE_SUBSCRIPTION_KEY", "c")
    monkeypatch.setenv("GMAIL_SENDER_ADDRESS", "ops@portless.io")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("PORTLESS_EMAIL_DOMAIN", "portless.io")
    from services.dealmaker.config import settings as dm
    monkeypatch.setattr(dm, "portless_entity_iec", "AAAPL1234A")
    monkeypatch.setattr(dm, "portless_entity_gstin", "27AAAPL1234A1Z5")


def test_dev_profile_reports_but_is_not_enforced(bare_env):
    report = readiness.evaluate()
    assert report["profile"] == "dev" and report["enforced"] is False and report["ready"] is False
    ids = {c["id"]: c["status"] for c in report["checks"]}
    assert ids["escrow_provider"] == "fail" and ids["money_webhook_secrets"] == "fail"
    assert ids["apify_token"] == "warn" and ids["qc_provider"] == "warn"  # warn-only outside production
    assert ids["money_ceiling"] == "ok" and ids["auto_flow_off"] == "ok"
    readiness.assert_ready_or_log()  # logs only


def test_production_profile_escalates_and_enforces(bare_env):
    bare_env.setenv("ENV_PROFILE", "production")
    report = readiness.evaluate()
    ids = {c["id"]: c["status"] for c in report["checks"]}
    assert ids["apify_token"] == "fail" and ids["qc_provider"] == "fail" and ids["event_bus"] == "fail"
    assert report["enforced"] is True
    with pytest.raises(readiness.NotProductionReady):
        readiness.assert_ready_or_log()
    bare_env.setenv("READINESS_ENFORCE", "false")
    assert readiness.enforce() is False
    readiness.assert_ready_or_log()


def test_fully_configured_dev_is_ready(bare_env):
    _fill(bare_env)
    report = readiness.evaluate()
    failing = [c["id"] for c in report["checks"] if c["status"] == "fail"]
    assert failing == [] and report["ready"] is True
    assert {c["id"]: c["status"] for c in report["checks"]}["email_domain"] == "ok"


def test_money_ceiling_warns_when_raised(bare_env):
    _fill(bare_env)
    bare_env.setenv("AUTONOMY_MONEY_CEILING", "L3")
    ids = {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}
    assert ids["money_ceiling"] == "warn"


def test_auto_flow_fails(bare_env):
    _fill(bare_env)
    bare_env.setenv("AUTO_FLOW_ENABLED", "true")
    report = readiness.evaluate()
    ids = {c["id"]: c["status"] for c in report["checks"]}
    assert "mock_mode_off" not in ids and ids["auto_flow_off"] == "fail" and report["ready"] is False
