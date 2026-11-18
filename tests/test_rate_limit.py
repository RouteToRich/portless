"""Rate limit on the endpoints anyone can call without signing in (shared/rate_limit.py)."""
from __future__ import annotations

import pytest

from shared import rate_limit as rl


@pytest.fixture(autouse=True)
def _clean():
    rl.reset_for_tests()
    yield
    rl.reset_for_tests()


def test_sliding_minute_per_key():
    for i in range(3):
        assert rl.allow("ip|unsubscribe", now=100.0 + i, limit=3)[0]
    ok, retry = rl.allow("ip|unsubscribe", now=103.0, limit=3)
    assert not ok and 1 <= retry <= 58
    assert rl.allow("other|unsubscribe", now=103.0, limit=3)[0]  # another address
    assert rl.allow("ip|unsubscribe", now=160.5, limit=3)[0]      # the first hit left the window


def test_switched_off(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PUBLIC_PER_MINUTE", "0")
    assert all(rl.allow("ip|x")[0] for _ in range(500))


@pytest.mark.parametrize("path,door", [
    ("/escrow-guard/webhooks/tazapay", "webhook:tazapay"), ("/shield-link/qc/webhook", "webhook:webhook"),
    ("/escrow-guard/proof/E1/T1", "proof"), ("/trade-min/unsubscribe", "unsubscribe"),
])
def test_doors(path, door):
    assert rl.door(path) == door


def test_the_middleware_answers_429_on_the_public_door(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shared.auth.middleware import AuthMiddleware
    monkeypatch.setenv("RATE_LIMIT_PUBLIC_PER_MINUTE", "2")
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/trade-min/unsubscribe")
    async def unsub():
        return {"ok": True}

    c = TestClient(app)
    assert [c.get("/trade-min/unsubscribe").status_code for _ in range(3)] == [200, 200, 429]
    r = c.get("/trade-min/unsubscribe")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
