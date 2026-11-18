"""Repeat orders: reorder from a completed deal, reminders before the usual order date."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shared.orchestration import reorders as ro

NOW = datetime.now(UTC)


def _d(deal_id, status, updated_days_ago, buyer="Gulf Foods", hs="10063020"):
    return SimpleNamespace(deal_id=deal_id, status=status, hs_code=hs, market="AE",
                           updated_at=NOW - timedelta(days=updated_days_ago),
                           parties=[SimpleNamespace(role="buyer", legal_name=buyer, email=f"buy@{buyer[:4].lower()}.ae")])


def test_usual_gap_from_past_orders(monkeypatch):
    monkeypatch.delenv("REORDER_REMIND_DAYS", raising=False)
    deals = [_d("D1", "PAID", 125), _d("D2", "PAID", 65)]   # every 60 days → next ~5 days ago
    (row,) = ro.due_from(deals, NOW)
    assert row["deal_id"] == "D2" and row["usual_gap_days"] == 60 and row["orders"] == 2 and row["overdue_days"] == 5


def test_not_yet_due_or_already_reordered(monkeypatch):
    assert ro.due_from([_d("D1", "PAID", 10)], NOW) == []            # default 90 days: not near yet
    assert ro.due_from([_d("D1", "PAID", 88)], NOW)[0]["usual_gap_days"] == 90
    open_reorder = [_d("D1", "PAID", 88), _d("D3", "MATCHING", 1)]
    assert ro.due_from(open_reorder, NOW) == []                       # a reorder is already open
    other_product = [_d("D1", "PAID", 88), _d("D3", "MATCHING", 1, hs="09042211")]
    assert len(ro.due_from(other_product, NOW)) == 1


async def test_reorder_needs_a_completed_deal(monkeypatch):
    from services.deals import service as ds
    from services.deals.schemas import DealStatus

    async def get(deal_id):
        return SimpleNamespace(deal_id=deal_id, status=DealStatus.NEGOTIATING, parties=[])
    monkeypatch.setattr(ds, "get_deal", get)
    with pytest.raises(ValueError, match="only a completed"):
        await ro.reorder("D1", by="ops")


async def test_reorder_copies_parties_quantity_and_price(monkeypatch):
    from services.deals.repository import InMemoryFallbackRepository
    from services.deals.schemas import DealStatus
    from shared.domain.models import Party
    from tests.factories import make
    from services.deals.schemas import DealRecord
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "false")
    old = make(DealRecord, deal_id="DEAL-OLD", hs_code="10063020", market="AE", status=DealStatus.PAID, quantity_mt=25.0,
               buyer_target_price_usd=950.0, org_id="portless",
               parties=[Party(role="buyer", legal_name="Gulf Foods", country="AE", email="buy@gulf.ae"),
                        Party(role="supplier", legal_name="Mill One", country="IN", email="sales@mill.in")])
    await repo.save(old)
    new = await ro.reorder("DEAL-OLD", by="ops@portless.in")
    assert new.deal_id != "DEAL-OLD" and new.reorder_of == "DEAL-OLD"
    assert new.quantity_mt == 25.0 and new.buyer_target_price_usd == 950.0
    assert {getattr(p.role, "value", p.role) for p in new.parties} == {"buyer", "supplier"}
    assert getattr(new.status, "value", new.status) == "MATCHING"
