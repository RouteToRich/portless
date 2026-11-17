"""A closed deal whose realised margin is below its margin floor raises deal.exception
(MARGIN_BREACH) once — services/escrow_guard/service.py::_check_closed_margin."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.escrow_guard import service as eg
from services.escrow_guard.schemas import EscrowHoldRecord, PayoutRecord, TrancheSpec
from tests.factories import make


def _hold(released: bool = True) -> EscrowHoldRecord:
    tranches = [make(TrancheSpec, tranche_id=f"T{i}", amount=a, status="RELEASED" if released or i < 3 else "PENDING")
                for i, a in enumerate((3000.0, 4000.0, 2000.0, 1000.0), start=1)]
    return make(EscrowHoldRecord, deal_id="D1", escrow_id="E1", amount=10000.0, status="RELEASED", tranches=tranches)


def _payouts(paid: float, owed: float = 8500.0) -> list[PayoutRecord]:
    return [make(PayoutRecord, deal_id="D1", payout_id="P1", amount=paid, status="COMPLETED", total_owed=owed)]


@pytest.fixture
def wired(monkeypatch):
    eg._margin_alerted.clear()
    emit = AsyncMock()
    monkeypatch.setattr(eg, "_emit", emit)
    deal = SimpleNamespace(margin_floor_pct=8.0, est_margin_pct=12.5, est_margin_usd=1250.0)
    monkeypatch.setattr("services.deals.service.get_deal", AsyncMock(return_value=deal))
    yield SimpleNamespace(emit=emit, deal=deal)
    eg._margin_alerted.clear()


async def test_a_closed_deal_below_the_floor_raises_once(wired):
    raised = await eg._check_closed_margin("D1", _hold(), _payouts(8500.0), 450.0, 4.5, {"ocean_freight": 600.0})
    assert raised is True
    topic, deal_id, payload = wired.emit.await_args.args
    assert topic == eg.EventTopic.DEAL_EXCEPTION and deal_id == "D1"
    assert payload["stage"] == "PAID:margin_below_floor" and payload["category"] == "MARGIN_BREACH"
    assert payload["margin_floor_pct"] == 8.0 and payload["est_margin_pct"] == 12.5
    assert payload["portless_costs"] == {"ocean_freight": 600.0}
    assert "4.50%" in payload["error"] and "8.00%" in payload["error"] and "estimated 12.50%" in payload["error"]
    assert await eg._check_closed_margin("D1", _hold(), _payouts(8500.0), 450.0, 4.5, {}) is False
    assert wired.emit.await_count == 1


async def test_a_margin_at_or_above_the_floor_is_quiet(wired):
    assert await eg._check_closed_margin("D1", _hold(), _payouts(8500.0), 800.0, 8.0, {}) is False
    wired.emit.assert_not_awaited()


async def test_never_judged_before_the_last_tranche(wired):
    assert await eg._check_closed_margin("D1", _hold(released=False), _payouts(8500.0), 10.0, 0.1, {}) is False
    wired.emit.assert_not_awaited()


async def test_never_judged_before_the_supplier_is_paid_in_full(wired):
    # 1,000 still owed to the supplier: the margin looks better than it will be, but it is not final either.
    assert await eg._check_closed_margin("D1", _hold(), _payouts(7500.0), 50.0, 0.5, {}) is False
    wired.emit.assert_not_awaited()


async def test_the_default_floor_applies_when_the_deal_has_none(wired, monkeypatch):
    wired.deal.margin_floor_pct = None
    monkeypatch.setenv("DEALMAKER_PO_MARGIN_FLOOR_PCT", "6")
    assert await eg._check_closed_margin("D1", _hold(), _payouts(8500.0), 700.0, 7.0, {}) is False
    assert await eg._check_closed_margin("D1", _hold(), _payouts(8500.0), 500.0, 5.0, {}) is True
    assert wired.emit.await_args.args[2]["margin_floor_pct"] == 6.0


async def test_the_margin_patch_runs_the_check(monkeypatch, wired):
    hold, payouts = _hold(), _payouts(8500.0)
    repo = SimpleNamespace(get_hold_by_deal=AsyncMock(return_value=hold),
                           list_payouts_by_deal=AsyncMock(return_value=payouts))
    monkeypatch.setattr(eg, "get_repository", lambda: repo)
    monkeypatch.setattr(eg, "_portless_costs", AsyncMock(return_value=({"ocean_freight": 900.0}, {})))
    monkeypatch.setattr("services.deals.service.update_financials", AsyncMock())
    await eg._patch_deal_realized_margin("D1")
    # net received 10,000 (stub tranches, no fee) − 8,500 paid − 900 costs = 600 → 6 % < 8 %
    payload = wired.emit.await_args.args[2]
    assert payload["realized_margin_pct"] == pytest.approx(round((eg._net_received(hold) - 9400.0) / 100.0, 2))
    assert payload["stage"] == "PAID:margin_below_floor"
