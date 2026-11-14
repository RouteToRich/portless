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
    "QIMA_API_ACCESS_TOKEN", "QIMA_ACCOUNT", "QIMA_PASSWORD", "QIMA_PASSWORD_MD5", "SGS_BOOKING_EMAIL",
    "BUREAU_VERITAS_BOOKING_EMAIL", "INTERTEK_API_KEY", "TUV_SUD_API_KEY", "QC_SANDBOX_PROVIDERS",
    "INTEGRATION_MODE", "ESCROW_PROVIDERS_ENABLED", "ESCROW_DEFAULT_PROVIDER", "ESCROW_SANDBOX_PROVIDERS",
    "PORTLESS_PUBLIC_BASE_URL", "ESCROW_PROOF_LINK_SECRET",
    "ICEGATE_API_KEY", "LLM_GATEWAY_URL", "LLM_GATEWAY_TOKEN", "LLM_GATEWAY_EMAIL",
    "LLM_GATEWAY_PASSWORD", "ENV_PROFILE", "READINESS_ENFORCE",
    "AUTH_GATEWAY_URL", "PORTLESS_ACCOUNT_ID", "LLM_GATEWAY_ACCOUNT_ID", "AUTH_TRUST_GATEWAY_HEADERS",
]


@pytest.fixture
def bare_env(monkeypatch):
    # Import first: shared.config's load_dotenv would otherwise re-fill the
    # variables deleted below from the developer's .env.
    import shared.config  # noqa: F401
    for v in _ALL_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("APIFY_TOKEN", "")
    monkeypatch.setenv("APIFY_TOKEN_AUTODISCOVER", "false")
    for v in ("COMPLIANCE_ENABLED", "COMPLIANCE_LOADING_CHECK_REQUIRED", "COMPLIANCE_UNCHECKED_SCREENING"):
        monkeypatch.delenv(v, raising=False)  # conftest pins compliance off for the pipeline tests
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
    monkeypatch.setenv("ESCROW_COM_API_KEY", "ek")
    monkeypatch.setenv("ESCROW_COM_EMAIL", "escrow@portless.io")
    monkeypatch.setenv("PAYONEER_WEBHOOK_SECRET", "w2")
    monkeypatch.setenv("PAYONEER_CLIENT_ID", "p")
    monkeypatch.setenv("PAYONEER_CLIENT_SECRET", "ps")
    monkeypatch.setenv("COMTRADE_SUBSCRIPTION_KEY", "c")
    monkeypatch.setenv("GMAIL_SENDER_ADDRESS", "ops@portless.io")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("PORTLESS_EMAIL_DOMAIN", "portless.io")
    monkeypatch.setenv("AUTH_GATEWAY_URL", "https://gateway.example")
    from services.dealmaker.config import settings as dm
    monkeypatch.setattr(dm, "portless_entity_iec", "AAAPL1234A")
    monkeypatch.setattr(dm, "portless_entity_gstin", "27AAAPL1234A1Z5")
    for name, value in (("PORTLESS_ADDRESS", "1 Anna Salai, Chennai"), ("PORTLESS_IEC", "AAAPL1234A"),
                        ("PORTLESS_GSTIN", "27AAAPL1234A1Z5"), ("PORTLESS_LUT_ARN", "AD270326000123X"),
                        ("PORTLESS_AD_CODE", "6390005"), ("PORTLESS_CONTRACT_LAW", "Laws of India")):
        monkeypatch.setenv(name, value)


def test_dev_profile_reports_but_is_not_enforced(bare_env):
    report = readiness.evaluate()
    assert report["profile"] == "dev" and report["enforced"] is False and report["ready"] is False
    ids = {c["id"]: c["status"] for c in report["checks"]}
    assert ids["escrow_provider"] == "fail" and ids["money_webhook_secrets"] == "fail"
    assert ids["apify_token"] == "warn" and ids["qc_provider"] == "warn"  # warn-only outside production
    assert ids["money_ceiling"] == "ok" and ids["auto_flow_off"] == "ok"
    assert ids["auth_gateway"] == "fail" and ids["auth_account"] == "warn" and ids["auth_header_trust"] == "ok"
    readiness.assert_ready_or_log()  # logs only


def test_production_profile_escalates_and_enforces(bare_env):
    bare_env.setenv("ENV_PROFILE", "production")
    report = readiness.evaluate()
    ids = {c["id"]: c["status"] for c in report["checks"]}
    assert ids["apify_token"] == "fail" and ids["qc_provider"] == "fail" and ids["event_bus"] == "fail"
    assert ids["auth_account"] == "fail"
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


def test_missing_exporter_facts_fail_and_name_what_is_missing(bare_env):
    _fill(bare_env)
    bare_env.delenv("PORTLESS_LUT_ARN")
    bare_env.delenv("PORTLESS_CONTRACT_LAW")
    checks = {c["id"]: c for c in readiness.evaluate()["checks"]}
    assert checks["documents_exporter"]["status"] == "fail" and "PORTLESS_LUT_ARN" in checks["documents_exporter"]["detail"]
    assert checks["documents_contract_law"]["status"] == "warn"


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


def test_auth_checks(bare_env):
    _fill(bare_env)
    bare_env.delenv("AUTH_GATEWAY_URL")
    bare_env.setenv("LLM_GATEWAY_URL", "https://gateway.example")  # the same gateway, as fallback
    bare_env.setenv("LLM_GATEWAY_ACCOUNT_ID", "acc-1")
    bare_env.setenv("AUTH_TRUST_GATEWAY_HEADERS", "true")
    ids = {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}
    assert ids["auth_gateway"] == "ok" and ids["auth_account"] == "ok"
    assert ids["auth_header_trust"] == "warn"


def test_compliance_switched_off_fails_in_production(bare_env):
    _fill(bare_env)
    bare_env.setenv("COMPLIANCE_ENABLED", "false")
    bare_env.setenv("COMPLIANCE_LOADING_CHECK_REQUIRED", "false")
    bare_env.setenv("COMPLIANCE_UNCHECKED_SCREENING", "allow")
    ids = {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}
    assert ids["compliance_trade"] == ids["compliance_loading"] == ids["compliance_unscreened"] == "fail"


def test_counterparty_validation_needs_the_switch_and_live_screening(bare_env):
    from services.trade_min.config import settings as tm
    bare_env.setattr(tm, "trade_min_validation_enabled", True)
    _fill(bare_env)
    checks = {c["id"]: c for c in readiness.evaluate()["checks"]}
    assert checks["counterparty_validation"]["status"] == "warn"  # no Apify token, dev profile
    assert "APIFY_TOKEN" in checks["counterparty_validation"]["detail"]

    bare_env.setenv("ENV_PROFILE", "production")
    assert {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}["counterparty_validation"] == "fail"

    bare_env.setenv("APIFY_TOKEN", "apify_api_real")
    assert {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}["counterparty_validation"] == "ok"

    bare_env.setattr(tm, "trade_min_validation_enabled", False)
    checks = {c["id"]: c for c in readiness.evaluate()["checks"]}
    assert checks["counterparty_validation"]["status"] == "fail"
    assert "TRADE_MIN_VALIDATION_ENABLED=false" in checks["counterparty_validation"]["detail"]


def test_qc_provider_counts_the_qima_api_and_the_portal_desks(bare_env):
    def qc():
        return {c["id"]: c for c in readiness.evaluate()["checks"]}["qc_provider"]

    assert qc()["status"] == "warn"
    bare_env.setenv("QIMA_API_ACCESS_TOKEN", "t")
    bare_env.setenv("QIMA_ACCOUNT", "a")
    assert qc()["status"] == "warn"  # no password yet
    bare_env.setenv("QIMA_PASSWORD", "p")
    assert qc()["status"] == "ok" and "QIMA API" in qc()["detail"]
    for v in ("QIMA_API_ACCESS_TOKEN", "QIMA_ACCOUNT", "QIMA_PASSWORD"):
        bare_env.delenv(v)
    bare_env.setenv("SGS_BOOKING_EMAIL", "booking@sgs.invalid")
    assert qc()["status"] == "ok" and "SGS booking desk" in qc()["detail"]


def test_the_qc_sandbox_fails_in_production(bare_env):
    def sandbox():
        return {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}["qc_sandbox"]

    assert sandbox() == "ok"
    bare_env.setenv("QC_SANDBOX_PROVIDERS", "qima")
    assert sandbox() == "ok", "the simulator is never used in live mode"
    bare_env.setenv("INTEGRATION_MODE", "sandbox")
    assert sandbox() == "warn"
    bare_env.setenv("ENV_PROFILE", "production")
    assert sandbox() == "fail"


def test_sandbox_integration_mode_fails_in_production(bare_env):
    def mode():
        return {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}["integration_mode"]

    assert mode() == "ok"
    bare_env.setenv("INTEGRATION_MODE", "sandbox")
    assert mode() == "warn"
    bare_env.setenv("ENV_PROFILE", "production")
    assert mode() == "fail"


def test_a_placeholder_key_is_not_a_configured_provider(bare_env):
    def checks():
        return {c["id"]: c["status"] for c in readiness.evaluate()["checks"]}

    bare_env.setenv("TAZAPAY_API_KEY", "your_tazapay_api_key_here")
    bare_env.setenv("TAZAPAY_API_SECRET", "your_tazapay_api_secret_here")
    bare_env.setenv("COGOPORT_API_KEY", "your_cogoport_api_key_here")
    assert checks()["escrow_provider"] == "fail" and checks()["freight_provider"] != "ok"
    bare_env.setenv("TAZAPAY_API_KEY", "tzp_live_123")
    bare_env.setenv("TAZAPAY_API_SECRET", "s3cret")
    assert checks()["escrow_provider"] == "fail"  # Escrow.com is offered to buyers but has no keys
    bare_env.setenv("ESCROW_PROVIDERS_ENABLED", "tazapay")
    assert checks()["escrow_provider"] == "ok"


def test_every_offered_escrow_provider_needs_keys_and_the_sandbox_never_runs_in_production(bare_env):
    def checks():
        return {c["id"]: c for c in readiness.evaluate()["checks"]}

    bare_env.setenv("ESCROW_COM_API_KEY", "ek")
    bare_env.setenv("ESCROW_COM_EMAIL", "escrow@portless.io")
    assert "Tazapay" in checks()["escrow_provider"]["detail"]
    bare_env.setenv("TAZAPAY_API_KEY", "k")
    bare_env.setenv("TAZAPAY_API_SECRET", "s")
    assert checks()["escrow_provider"]["status"] == "ok"
    assert checks()["escrow_proof_links"]["status"] == "warn"
    bare_env.setenv("PORTLESS_PUBLIC_BASE_URL", "https://api.portless.io")
    bare_env.setenv("ESCROW_PROOF_LINK_SECRET", "p")
    assert checks()["escrow_proof_links"]["status"] == "ok"
    bare_env.setenv("INTEGRATION_MODE", "sandbox")
    bare_env.setenv("ESCROW_SANDBOX_PROVIDERS", "tazapay")
    bare_env.setenv("ENV_PROFILE", "production")
    assert checks()["escrow_sandbox"]["status"] == "fail"
