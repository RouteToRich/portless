"""Coverage gate (todo: fail the build below the overall floor and below the floor of each money
module). Reads coverage.py's JSON report:

  pytest --cov=agents --cov=services --cov=shared --cov=app --cov-branch --cov-report=json:coverage.json
  python scripts/coverage_gate.py coverage.json

``OVERALL_MIN`` and ``MONEY_MIN`` are ratchets: set just under today's numbers so the build fails
when coverage drops, raised as tests are added (target: 95 % overall, 100 % on the money modules).
Exit 0 when every floor holds, 1 otherwise (each miss printed).
"""
from __future__ import annotations

import json
import sys

OVERALL_MIN = 90.0
"""Line + branch coverage of the product code (services/automation excluded — a test runner)."""

MONEY_MIN: dict[str, float] = {
    "services/escrow_guard/service.py": 93.0,
    "services/escrow_guard/journal.py": 91.0,
    "shared/orchestration/trade_costs.py": 94.0,
    "agents/negotiation_agent/pricing.py": 97.0,
    "shared/currency.py": 85.0,
    "shared/orchestration/supplier_commitment.py": 84.0,
    "shared/orchestration/compliance.py": 96.0,
    "shared/orchestration/chargebacks.py": 79.0,
    "shared/orchestration/escrow_interest.py": 70.0,
    "shared/fx_rates.py": 80.0,
}

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
    for path, floor in MONEY_MIN.items():
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
