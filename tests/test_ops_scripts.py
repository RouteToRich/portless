"""Uptime check, response-time budgets and coverage gate scripts (scripts/uptime_check.py,
scripts/perf_budget.py, scripts/coverage_gate.py) and their workflows."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_uptime_healthy_and_stale_and_down():
    up = _load("uptime_check")

    def healthy(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        assert request.headers["authorization"] == "Bearer T"
        return httpx.Response(200, json={"ok": True, "watchers": [{"name": "deal_watcher", "stale": False}], "stale": []})

    assert await up.check("https://b", "T", transport=httpx.MockTransport(healthy)) == []

    def stale(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, json={"watchers": [{"name": "escrow_watcher", "stale": True}], "stale": ["escrow_watcher"]})

    assert await up.check("https://b", "T", transport=httpx.MockTransport(stale)) == ["stale watcher(s): escrow_watcher"]

    def down(request):
        raise httpx.ConnectError("refused")

    out = await up.check("https://b", "T", transport=httpx.MockTransport(down))
    assert out[0].startswith("/health unreachable")


async def test_uptime_alert_only_with_telegram():
    up = _load("uptime_check")
    sent = []

    def tg(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    assert await up.alert("x", None, None) is False
    assert await up.alert("x", "BOT", "42", transport=httpx.MockTransport(tg)) is True
    assert sent == ["/botBOT/sendMessage"]


async def test_perf_budget_over_and_within():
    perf = _load("perf_budget")

    def ok(request):
        return httpx.Response(200, json={})

    out = await perf.measure("https://b", "T", runs=2, transport=httpx.MockTransport(ok))
    assert out["over"] == [] and out["failed"] == [] and len(out["rows"]) == len(perf.BUDGETS_MS)
    out = await perf.measure("https://b", "T", runs=1, scale=0.0, transport=httpx.MockTransport(ok))
    assert len(out["over"]) == len(perf.BUDGETS_MS)  # a zero budget: everything is over

    def broken(request):
        return httpx.Response(500)

    out = await perf.measure("https://b", "T", runs=1, transport=httpx.MockTransport(broken))
    assert out["failed"] and all("HTTP 500" in f for f in out["failed"])


def test_coverage_gate():
    gate = _load("coverage_gate")

    def f(cov, tot):
        return {"summary": {"covered_lines": cov, "num_statements": tot, "covered_branches": 0, "num_branches": 0}}

    report = {"files": {p: f(100, 100) for p in [*gate.MONEY_MIN, *gate.FULL]} | {"services/automation/x.py": f(0, 1000)}}
    assert gate.gate(report) == []  # automation excluded from the overall figure
    report["files"]["services/escrow_guard/service.py"] = f(50, 100)
    misses = gate.gate(report)
    assert any("escrow_guard/service.py: 50.0%" in m for m in misses)
    report["files"]["services/automation/safety.py"] = f(99, 100)  # the guard is held at 100 % too
    assert any("automation/safety.py: 99.0%" in m for m in gate.gate(report))


def test_workflows_wired():
    uptime = yaml.safe_load((ROOT / ".github/workflows/uptime.yml").read_text(encoding="utf-8"))
    assert uptime[True]["schedule"][0]["cron"] == "*/10 * * * *"  # "on" parses as True
    nightly = yaml.safe_load((ROOT / ".github/workflows/nightly-automation.yml").read_text(encoding="utf-8"))
    assert "performance" in nightly["jobs"]
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts/coverage_gate.py" in ci
