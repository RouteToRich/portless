"""Suite markers, applied by file at collection (tests/conftest.py).

    pytest -m smoke                  # boot, readiness, state machine, source hygiene (seconds)
    pytest -m money                  # escrow, payouts, tranches, ledger / journal, margins, money gates
    pytest -m "unit and not slow"    # the quick inner loop
    pytest -m integration            # HTTP routes, repositories on mongomock, lifespans, simulators, automation
    pytest -m slow                   # tests that take seconds each (the trade-min scan tests)
    pytest -m regression             # everything (the full suite)

Every test is ``regression`` and either ``unit`` or ``integration``; ``smoke``, ``money`` and
``slow`` are added on top. A new test file gets ``unit`` + ``regression`` until it is listed here.
"""
from __future__ import annotations

from pathlib import PurePath

MARKERS = {
    "unit": "fast, in-process tests of one module (everything not marked integration)",
    "integration": "HTTP routes, repositories, app lifespans, provider simulators, automation worker",
    "smoke": "the minimum run after a deploy: boot, readiness, state machine, source hygiene",
    "regression": "the whole suite",
    "slow": "tests that take seconds each",
    "money": "escrow, payouts, tranches, ledger and journal, margins, money gates",
}

SMOKE = {
    "test_health.py", "test_readiness.py", "test_state_machine.py", "test_source_hygiene.py",
    "test_auth_middleware.py",
}
MONEY = {
    "test_escrow_guard.py", "test_escrow_money_chain.py", "test_escrow_http_and_providers.py",
    "test_escrow_providers.py", "test_money_continuations.py", "test_supplier_retention.py",
    "test_trade_costs.py", "test_closed_margin_alert.py", "test_compliance_money_block.py",
    "test_approval_gate_enforcement.py", "test_platform_controls.py", "test_dealmaker.py",
    "test_batch_c.py", "test_supplier_commitment.py",
}
INTEGRATION = {
    "test_health.py", "test_routes_and_clients.py", "test_repositories_contract.py",
    "test_startup_and_lifespans.py", "test_deals_http_and_callbacks.py", "test_escrow_http_and_providers.py",
    "test_escrow_providers.py", "test_maersk_freight.py", "test_qc_inspection.py", "test_automation.py",
    "test_automation_continuations.py", "test_automation_deployed.py", "test_trade_min.py",
    "test_connectors_and_imap.py", "test_trade_data_and_infra.py",
}
SLOW_FILES = {"test_trade_min.py"}


def markers_for(path: str | PurePath) -> list[str]:
    """Marker names for a test file (by its file name)."""
    name = PurePath(path).name
    marks = ["regression", "integration" if name in INTEGRATION else "unit"]
    if name in SMOKE:
        marks.append("smoke")
    if name in MONEY:
        marks.append("money")
    if name in SLOW_FILES:
        marks.append("slow")
    return marks
