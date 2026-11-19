"""Coverage gate (todo: fail the build below the overall floor and below the floor of each money
module). Reads coverage.py's JSON report:

  pytest --cov=agents --cov=services --cov=shared --cov=app --cov-branch --cov-report=json:coverage.json
  python scripts/coverage_gate.py coverage.json

``OVERALL_MIN``, ``MONEY_MIN`` and ``FULL`` are ratchets: set just under today's numbers so the build fails
when coverage drops, raised as tests are added (target: 95 % overall, 100 % on the money modules).
Exit 0 when every floor holds, 1 otherwise (each miss printed).
"""
from __future__ import annotations

import json
import sys

OVERALL_MIN = 91.5
"""Line + branch coverage of the product code (services/automation excluded — a test runner)."""

MONEY_MIN: dict[str, float] = {
    "services/escrow_guard/service.py": 100.0,
    "services/escrow_guard/journal.py": 100.0,
    "shared/orchestration/trade_costs.py": 100.0,
    "agents/negotiation_agent/pricing.py": 100.0,
    "shared/currency.py": 100.0,
    "shared/orchestration/supplier_commitment.py": 100.0,
    "shared/orchestration/compliance.py": 100.0,
    "shared/orchestration/chargebacks.py": 100.0,
    "shared/orchestration/escrow_interest.py": 100.0,
    "shared/fx_rates.py": 100.0,
}
"""The money modules: 100 % line + branch (phase 1 of the 100 % target, 2026-11-19)."""

FULL: tuple[str, ...] = (
    # the isolation guard of the automation runner — protects production data
    "services/automation/safety.py",
    # modules added on 2026-11-19, written with full tests
    "shared/connectors/apify_usage.py", "shared/connectors/national_trade.py", "shared/indian_ids.py",
    "services/trade_min/source_yield.py", "shared/product_spec.py", "services/trade_min/product_facts.py",
    "services/trade_min/volume.py", "services/trade_min/markets.py", "services/trade_min/scan_plan.py",
    "shared/orchestration/sourcing_agent.py", "shared/orchestration/agent_scorecard.py",
    "shared/orchestration/inspection_options.py", "services/shield_link/freight_compare.py",
    "services/trade_min/supplier_onboarding.py", "shared/orchestration/relay_evidence.py",
    "services/trade_min/site_facts.py", "services/trade_min/company_led.py", "shared/orchestration/reorder_agent.py",
    "shared/orchestration/document_check.py", "shared/orchestration/claims.py", "shared/orchestration/outreach_ab.py",
    "services/trade_min/personal_data.py",
)
"""Modules held at 100 % — a ratchet: a module joins when it reaches 100 % and never leaves."""

EXCLUDE = ("services/automation/",)


def _pct(summary: dict) -> float:
    covered = summary.get("covered_lines", 0) + summary.get("covered_branches", 0)
    total = summary.get("num_statements", 0) + summary.get("num_branches", 0)
    return 100.0 * covered / total if total else 100.0


def gate(report: dict) -> list[str]:
    files = {k.replace("\\", "/"): v for k, v in report.get("files", {}).items()}
    product = {k: v for k, v in files.items() if not k.startswith(EXCLUDE)}
    totals = {"covered_lines": 0, "covered_branches": 0, "num_statements": 0, "num_branches": 0}
    for v in product.values():
        for key in totals:
            totals[key] += v["summary"].get(key, 0)
    misses = []
    overall = _pct(totals)
    if overall < OVERALL_MIN:
        misses.append(f"overall {overall:.1f}% < {OVERALL_MIN}%")
    for path, floor in {**MONEY_MIN, **{p: 100.0 for p in FULL}}.items():
        hit = next((v for k, v in files.items() if k.endswith(path)), None)
        if hit is None:
            misses.append(f"{path}: not in the report")
            continue
        pct = _pct(hit["summary"])
        if pct < floor:
            misses.append(f"{path}: {pct:.1f}% < {floor}%")
    return misses


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "coverage.json"
    with open(path, encoding="utf-8") as f:
        misses = gate(json.load(f))
    for m in misses:
        print("COVERAGE BELOW FLOOR:", m)
    if not misses:
        print("coverage gate: every floor holds")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
