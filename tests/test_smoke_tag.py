"""The after-deploy smoke set: 12 fast P0 catalogue cases tagged @smoke, runnable alone (--tag smoke)."""
from __future__ import annotations

import argparse
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_twelve_p0_cases_are_tagged_smoke():
    text = (ROOT / "docs/testing/TEST_CASES.md").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if "@smoke" in ln]
    assert len(lines) == 12 and all("@P0" in ln and "@ui" not in ln for ln in lines)


def test_worker_selects_by_tag():
    from types import SimpleNamespace

    from services.automation.worker import _select
    cases = [SimpleNamespace(tc_id="TC-A-1", phase="", priority="P0", tags=["@P0", "@smoke"]),
             SimpleNamespace(tc_id="TC-A-2", phase="", priority="P0", tags=["@P0"])]
    args = argparse.Namespace(phase="", priority="", only="", tag="smoke")
    assert [c.tc_id for c in _select(cases, args)] == ["TC-A-1"]
    args.tag = ""
    assert len(_select(cases, args)) == 2
    assert re.search(r"--tag", (ROOT / "services/automation/worker.py").read_text(encoding="utf-8"))
