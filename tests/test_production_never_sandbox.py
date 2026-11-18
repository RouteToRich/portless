"""Production never runs on stubs or the dev auto-flow (todo: make sure production never sets
INTEGRATION_MODE=sandbox, the *_SANDBOX_PROVIDERS or AUTO_FLOW_ENABLED)."""
from __future__ import annotations

import pytest

from shared import readiness


def _failing(report):
    return {c["id"] for c in report["checks"] if c["status"] == "fail"}


@pytest.mark.parametrize("env,check", [
    ({"INTEGRATION_MODE": "sandbox"}, "integration_mode"),
    ({"AUTO_FLOW_ENABLED": "true"}, "auto_flow_off"),
])
def test_each_setting_fails_readiness_and_stops_a_production_boot(monkeypatch, env, check):
    monkeypatch.setenv("ENV_PROFILE", "production")
    monkeypatch.delenv("READINESS_ENFORCE", raising=False)
    monkeypatch.setenv("INTEGRATION_MODE", "live")
    monkeypatch.delenv("AUTO_FLOW_ENABLED", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    report = readiness.evaluate()
    assert check in _failing(report) and report["enforced"] is True
    with pytest.raises(readiness.NotProductionReady):
        readiness.assert_ready_or_log()


@pytest.mark.parametrize("var", ["QC_SANDBOX_PROVIDERS", "ESCROW_SANDBOX_PROVIDERS", "CARRIER_SANDBOX_PROVIDERS"])
def test_a_simulator_in_sandbox_mode_fails_too(monkeypatch, var):
    monkeypatch.setenv("ENV_PROFILE", "production")
    monkeypatch.setenv("INTEGRATION_MODE", "sandbox")
    monkeypatch.setenv(var, "qima,tazapay,maersk")
    report = readiness.evaluate()
    failing = [c for c in report["checks"] if c["status"] == "fail" and var in (c.get("detail") or "")]
    assert failing, f"{var} is not reported"
