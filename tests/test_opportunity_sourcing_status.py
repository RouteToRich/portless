"""The Opportunity Radar's supplier-side status per opportunity (services/trade_min/sourcing_status.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services.trade_min import sourcing_status as ss

NOW = datetime.now(timezone.utc)


def _deal(deal_id, opp, age_days=0, hs="10063020", market="AE"):
    return SimpleNamespace(deal_id=deal_id, opportunity_id=opp, status="MATCHING", hs_code=hs, market=market,
                           created_at=NOW - timedelta(days=age_days))


def _rfq(deal_id, *suppliers):
    return SimpleNamespace(deal_id=deal_id, dispatch_results=[SimpleNamespace(supplier_id=s, success=True) for s in suppliers])


def _quote(deal_id, supplier, price):
    return SimpleNamespace(deal_id=deal_id, supplier_id=supplier, company_name=f"{supplier} Mills", unit_price_usd=price)


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(deals=[], rfqs={}, quotes={}, commitments={}, opportunities={})

    class Deals:
        async def list_active(self, limit=100, **_):
            return state.deals

    class Sourcing:
        async def list_rfqs(self, deal_id):
            return state.rfqs.get(deal_id, [])

        async def list_quotes(self, deal_id=None, rfq_id=None):
            return state.quotes.get(deal_id, [])

    async def commitment(deal_id):
        return state.commitments.get(deal_id)

    class Opportunities:
        async def get(self, oid):
            return state.opportunities.get(oid)

    async def opportunities_repo():
        return Opportunities()

    monkeypatch.setattr("services.trade_min.repository.get_repository", opportunities_repo)
    monkeypatch.setattr("services.deals.repository.get_repository", lambda: Deals())
    monkeypatch.setattr("services.sourcing.repository.get_repository", lambda: Sourcing())
    monkeypatch.setattr("shared.orchestration.supplier_commitment.get", commitment)
    return state


async def test_each_stage_is_reported(world):
    world.deals = [_deal(f"D{i}", f"O{i}", market=f"M{i}") for i in range(1, 6)]
    world.rfqs = {d: [_rfq(d, "S1", "S2")] for d in ("D2", "D3", "D4", "D5")}
    world.quotes = {d: [_quote(d, "S1", 920.0), _quote(d, "S2", 900.0)] for d in ("D3", "D4", "D5")}
    attempt = SimpleNamespace(supplier_id="S2", company_name="S2 Mills", price_usd_per_mt=900.0)
    world.commitments = {
        "D4": SimpleNamespace(status="requested", current=attempt, confirmed_price=None, confirmed_supplier_id=None),
        "D5": SimpleNamespace(status="confirmed", current=attempt, confirmed_price=905.0, confirmed_supplier_id="S2"),
    }
    out = await ss.sourcing_status(["O1", "O2", "O3", "O4", "O5", "O-none"])
    assert {k: v["stage"] for k, v in out.items()} == {
        "O1": "no_rfq", "O2": "rfq_sent", "O3": "quoted", "O4": "awaiting_confirmation", "O5": "confirmed"}
    assert out["O2"]["suppliers_asked"] == 2 and out["O2"]["quotes"] == 0
    assert (out["O3"]["best_quote_usd_per_mt"], out["O3"]["best_quote_supplier"]) == (900.0, "S2 Mills")
    assert out["O4"]["awaiting_supplier"] == "S2 Mills" and out["O4"]["requested_price_usd_per_mt"] == 900.0
    assert out["O5"]["confirmed"] is True and out["O5"]["confirmed_price_usd_per_mt"] == 905.0
    assert out["O5"]["confirmed_supplier"] == "S2 Mills"
    assert "O-none" not in out                                   # no deal → nothing to show


async def test_the_newest_deal_of_an_opportunity_counts(world):
    world.deals = [_deal("OLD", "O1", age_days=10), _deal("NEW", "O1")]
    world.rfqs = {"NEW": [_rfq("NEW", "S1")]}
    out = await ss.sourcing_status(["O1"])
    assert out["O1"]["deal_id"] == "NEW" and out["O1"]["stage"] == "rfq_sent"


async def test_no_ids_no_work(world):
    assert await ss.sourcing_status([]) == {}


async def test_the_latest_scan_of_a_corridor_shows_the_deal_of_an_earlier_scan(world):
    """The radar lists only the newest scan of a product × market; the deal hangs off an
    earlier, CONVERTED scan of the same corridor."""
    world.deals = [_deal("D-OLD-SCAN", "OPP-OLD", hs="090422", market="DE")]
    world.rfqs = {"D-OLD-SCAN": [_rfq("D-OLD-SCAN", "S1", "S2", "S3")]}
    world.opportunities = {"OPP-NEW": SimpleNamespace(hs_code="090422", market="DE"),
                           "OPP-OTHER": SimpleNamespace(hs_code="090422", market="ES")}
    out = await ss.sourcing_status(["OPP-NEW", "OPP-OTHER"])
    assert out["OPP-NEW"]["deal_id"] == "D-OLD-SCAN" and out["OPP-NEW"]["stage"] == "rfq_sent"
    assert out["OPP-NEW"]["suppliers_asked"] == 3
    assert "OPP-OTHER" not in out                                # another market → not this deal

