"""A money-moving request re-checks a token older than AUTH_MONEY_INTROSPECTION_CACHE_SECONDS (todo:
token revocation delay for money approvers)."""
from __future__ import annotations

import pytest

from shared.auth import identity as idn
from shared.auth.dependencies import moves_money


@pytest.fixture
def gateway(monkeypatch):
    calls = []

    async def ask(token):
        calls.append(token)
        return {"user_id": "u1", "email": "cfo@portless.in", "name": "CFO", "account_id": None}
    monkeypatch.setattr(idn, "ask_gateway", ask)
    idn.clear_cache()
    yield calls
    idn.clear_cache()


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/hitl-approval/approvals/APPR-1/resolve", True),
    ("POST", "/escrow-guard/escrows/E1/release", True),
    ("POST", "/escrow-guard/payouts", True),
    ("POST", "/escrow-guard/escrows/E1/tranches/T1/provider-confirmed", True),
    ("POST", "/hitl-approval/kill-switch", True),
    ("GET", "/hitl-approval/approvals/APPR-1", False),
    ("POST", "/deals/D1/notes", False),
])
def test_which_requests_move_money(method, path, expected):
    assert moves_money(method, path) is expected


async def test_old_cached_check_is_asked_again_for_money(gateway, monkeypatch):
    monkeypatch.setattr(idn.time, "monotonic", lambda: 1000.0)
    await idn.identity_for_token("tok")
    assert len(gateway) == 1
    monkeypatch.setattr(idn.time, "monotonic", lambda: 1003.0)
    await idn.identity_for_token("tok", max_age=5)        # 3 s old: fresh enough
    await idn.identity_for_token("tok")                    # normal request: cache
    assert len(gateway) == 1
    monkeypatch.setattr(idn.time, "monotonic", lambda: 1010.0)
    await idn.identity_for_token("tok", max_age=5)        # 10 s old: asked again
    assert len(gateway) == 2
