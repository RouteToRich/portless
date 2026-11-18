"""Exposure limits before the PI (DEF-14)."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from shared.orchestration import exposure as ex

NOW = datetime.now(UTC)


def _deal(did, status="PI_SENT", buyer="b@gulf.ae", supplier="s@mill.in", price=1000.0, qty=20.0):
    return SimpleNamespace(deal_id=did, status=SimpleNamespace(value=status), buyer_target_price_usd=price, quantity_mt=qty,
                           updated_at=NOW, exposure_accepted_by=None,
                           parties=[SimpleNamespace(role="buyer", party_id=None, email=buyer, legal_name="Gulf"),
                                    SimpleNamespace(role="supplier", party_id=None, email=supplier, legal_name="Mill")])


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    for n in ex.LIMITS:
        monkeypatch.delenv(f"EXPOSURE_MAX_{n.upper()}_USD", raising=False)


def test_no_limits_no_breaches():
    assert ex.breaches(_deal("D0"), 1e9, [], 1e9) == []


def test_each_limit(monkeypatch):
    monkeypatch.setenv("EXPOSURE_MAX_DEAL_USD", "30000")
    monkeypatch.setenv("EXPOSURE_MAX_BUYER_USD", "50000")
    monkeypatch.setenv("EXPOSURE_MAX_SUPPLIER_USD", "100000")
    monkeypatch.setenv("EXPOSURE_MAX_DAILY_USD", "45000")
    monkeypatch.setenv("EXPOSURE_MAX_ESCROW_USD", "60000")
    open_same_buyer = _deal("D1")                                   # 20 000 open with the same buyer, PI sent today
    paid = _deal("D2", status="PAID")                               # closed: does not count
    out = {b["limit"]: b for b in ex.breaches(_deal("D0"), 35000.0, [open_same_buyer, paid], 40000.0)}
    assert set(out) == {"deal", "buyer", "daily", "escrow"}
    assert out["buyer"]["would_be_usd"] == 55000.0 and out["escrow"]["would_be_usd"] == 75000.0


async def test_accepted_exposure_is_not_checked_again(monkeypatch):
    monkeypatch.setenv("EXPOSURE_MAX_DEAL_USD", "1")
    deal = _deal("D0")
    deal.exposure_accepted_by = "cfo@portless.in"
    assert await ex.check(deal, 50000.0) == []
