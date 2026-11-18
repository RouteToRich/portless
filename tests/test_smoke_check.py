"""Post-deploy smoke test script (scripts/smoke_check.py) on a mocked backend."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx

_spec = importlib.util.spec_from_file_location("smoke_check", Path(__file__).resolve().parents[1] / "scripts" / "smoke_check.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def _backend(*, readiness=None, broken=(), seen=None):
    readiness = readiness or {"checks": [{"id": "mongo", "status": "ok"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.url.path, request.headers.get("authorization")))
        path = request.url.path
        if any(path.startswith(b) for b in broken):
            return httpx.Response(500, json={"detail": "boom"})
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/health/readiness":
            return httpx.Response(200, json=readiness)
        return httpx.Response(200, json=[])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_healthy_backend_passes_and_sends_the_token():
    seen = []
    report = await smoke.run("https://api.x", "tok", client=_backend(seen=seen))
    assert report["ok"] and len(report["results"]) == 1 + len(smoke.MODULES) + 1 + len(smoke.READS)
    assert ("/health/readiness", "Bearer tok") in seen and ("/deals", "Bearer tok") in seen


async def test_a_readiness_fail_or_warning_fails_the_rollout():
    fail = {"checks": [{"id": "smtp", "status": "fail"}, {"id": "four_eyes", "status": "warn"}]}
    report = await smoke.run("https://api.x", "tok", client=_backend(readiness=fail))
    assert not report["ok"] and "fail: smtp" in next(r["detail"] for r in report["results"] if r["check"] == "readiness")
    warn = {"checks": [{"id": "four_eyes", "status": "warn"}]}
    assert not (await smoke.run("https://api.x", "tok", client=_backend(readiness=warn)))["ok"]
    assert (await smoke.run("https://api.x", "tok", allow_warn=True, client=_backend(readiness=warn)))["ok"]


async def test_a_broken_module_fails():
    report = await smoke.run("https://api.x", "tok", client=_backend(broken=("/escrow-guard",)))
    failed = {r["check"] for r in report["results"] if not r["ok"]}
    assert failed == {"escrow-guard health", "read escrow ledgers"}


async def test_unreachable_backend():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    report = await smoke.run("https://api.x", None, client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    assert report["unreachable"] and not report["ok"]


def test_exit_codes(monkeypatch):
    # main() runs the async check with asyncio.run; the test replaces that seam (never
    # asyncio.run inside the suite).
    for report, code in (({"ok": True, "unreachable": False}, 0), ({"ok": False, "unreachable": False}, 1),
                         ({"ok": False, "unreachable": True}, 2)):
        monkeypatch.setattr(smoke, "_execute", lambda *a, r=report: {**r, "results": []})
        assert smoke.main(["--base", "https://api.x"]) == code
