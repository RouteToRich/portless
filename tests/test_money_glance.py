"""Buy / sell / profit per deal for the Deal Board (shared/orchestration/money_glance.py)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from shared.orchestration import money_glance as mg


def _deal(**kw):
    base = dict(deal_id="D1", buyer_target_price_usd=980.0, quantity_mt=25.0, winning_supplier_id="S1",
                match_results=[SimpleNamespace(supplier_id="S1", fob_price_usd=760.0)], supplier_offers=[],
                realized_margin_usd=None, est_margin_usd=3500.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _repo(monkeypatch, pi=None, po=None):
    repo = SimpleNamespace(get_pi_by_deal=AsyncMock(return_value=pi), get_po_by_deal=AsyncMock(return_value=po))
    monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: repo)


async def test_estimates_before_any_document(monkeypatch):
    _repo(monkeypatch)
    r = await mg.row(_deal())
    assert (r["sell_usd_per_mt"], r["sell_basis"], r["buy_usd_per_mt"], r["buy_basis"]) == (980.0, "estimate", 760.0, "estimate")
    assert r["profit_usd"] == 3500.0 and r["profit_basis"] == "estimate"


async def test_firm_figures_from_pi_po_and_settlement(monkeypatch):
    _repo(monkeypatch, pi=SimpleNamespace(agreed_price=1005.0, quantity_mt=24.0), po=SimpleNamespace(agreed_price=770.0))
    r = await mg.row(_deal(realized_margin_usd=4100.0))
    assert (r["sell_usd_per_mt"], r["sell_basis"], r["buy_usd_per_mt"], r["buy_basis"]) == (1005.0, "firm", 770.0, "firm")
    assert r["quantity_mt"] == 24.0 and r["profit_usd"] == 4100.0 and r["profit_basis"] == "realised"


async def test_cheapest_offer_when_no_winner(monkeypatch):
    _repo(monkeypatch)
    offers = [SimpleNamespace(fob_price_usd_per_mt=790.0), SimpleNamespace(fob_price_usd_per_mt=750.0),
              SimpleNamespace(fob_price_usd_per_mt=None)]
    r = await mg.row(_deal(winning_supplier_id=None, match_results=[], supplier_offers=offers, buyer_target_price_usd=None))
    assert r["buy_usd_per_mt"] == 750.0 and r["sell_usd_per_mt"] is None
