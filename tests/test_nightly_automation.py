"""Nightly automation summary and exit code (scripts/nightly_automation.py)."""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nightly_automation", ROOT / "scripts" / "nightly_automation.py")
na = importlib.util.module_from_spec(spec)
spec.loader.exec_module(na)


def test_green_run():
    run = {"run_id": "RUN-1", "status": "FINISHED", "totals": {"passed": 675, "not_automated": 186}, "safety": {}}
    text = na.summary(run, [])
    assert text.startswith("✅") and "passed 675" in text and na.exit_code(run, []) == 0


def test_failures_and_refused_recipients_fail_the_night():
    run = {"run_id": "RUN-2", "status": "FINISHED", "totals": {"passed": 1, "failed": 2},
           "safety": {"real_recipients": ["a@b.com"], "blocked_connections": ["dns x.gov.in"]}}
    failed = [{"tc_id": "TC-PAY-002", "error": "payouts_total is 0.0"}, {"tc_id": "UC-15", "error": "no RFQ"}]
    text = na.summary(run, failed)
    assert text.startswith("❌") and "TC-PAY-002" in text and "SAFETY: 1 real recipient" in text and "dns x.gov.in" in text
    assert "a@b.com" not in text  # never print the address
    assert na.exit_code(run, failed) == 1
    assert na.exit_code({"status": "FINISHED", "safety": {"real_recipients": ["x@y.z"]}}, []) == 1
    assert na.exit_code({"status": "STOPPED"}, []) == 1
