"""Park / drop a prospect from the desk (shared/orchestration/prospect_actions.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import BuyerCandidate, DealRecord, DealStatus
from shared.orchestration import prospect_actions as pa

NOW = datetime.now(UTC)


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", r)
    return r


def _deal(deal_id="P1", status=DealStatus.MATCHING, **kw):
    return DealRecord(deal_id=deal_id, operator_id="op", org_id="o", hs_code="090422", market="AE", status=status,
                      created_at=NOW - timedelta(days=30), updated_at=NOW - timedelta(days=20), **kw)


async def test_park_keeps_the_record_and_unpark_clears_it(repo):
    await repo.save(_deal())
    deal = await pa.park("P1", "buyer at Gulfood, back in March", "ops@portless.in")
    assert deal.parked_at and deal.parked_until is None and "ops@portless.in" in deal.parked_reason
    assert pa.is_parked(deal) and (await repo.get("P1")).status == DealStatus.MATCHING
    deal = await pa.unpark("P1", "ops@portless.in")
    assert deal.parked_at is None and not pa.is_parked(deal)


async def test_a_dated_park_ends_by_itself(repo):
    await repo.save(_deal())
    deal = await pa.park("P1", "season starts in March", "ops", until=NOW + timedelta(days=3))
    assert pa.is_parked(deal, NOW) and not pa.is_parked(deal, NOW + timedelta(days=4))
    with pytest.raises(ValueError):
        await pa.park("P1", "past date", "ops", until=NOW - timedelta(days=1))


async def test_refusals(repo):
    await repo.save(_deal())
    await repo.save(_deal("D2", status=DealStatus.NEGOTIATING))
    with pytest.raises(ValueError):
        await pa.park("P1", "  ", "ops")
    with pytest.raises(pa.NotAProspect):
        await pa.park("D2", "reason", "ops")
    with pytest.raises(pa.NotAProspect):
        await pa.drop("D2", "reason", "ops")


async def test_drop_cancels_with_the_reason_and_closes_its_gates(repo, monkeypatch):
    await repo.save(_deal())
    withdraw = AsyncMock(return_value=["APPR-1"])
    monkeypatch.setattr("services.hitl_approval.service.withdraw_pending", withdraw)
    deal = await pa.drop("P1", "no buyer pays above the floor", "ops@portless.in")
    assert deal.status == DealStatus.CANCELLED
    assert "no buyer pays above the floor" in deal.status_history[-1].reason
    deal_id, actions, reason = withdraw.await_args.args
    assert deal_id == "P1" and {"CONFIRM_MATCH", "SEND_BUYER_OUTREACH", "DISPATCH_RFQ"} <= set(actions)
    assert "ops@portless.in" in reason


async def test_watcher_and_followups_skip_a_parked_prospect(repo, monkeypatch):
    from shared.orchestration import deal_watcher, outreach
    cand = BuyerCandidate(candidate_id="gulf.ae", legal_name="Gulf", country="AE", email="b@gulf.ae",
                          status="contacted", contacted_at=NOW - timedelta(days=10))
    await repo.save(_deal(buyer_candidates=[cand]))
    await pa.park("P1", "on hold", "ops")
    monkeypatch.setenv("DEAL_WATCHER_ENABLED", "true")
    monkeypatch.setattr("shared.orchestration.kill_switch.is_paused", AsyncMock(return_value=False))
    gate = AsyncMock()
    monkeypatch.setattr(outreach, "_gate", gate)
    emit = AsyncMock(return_value=True)
    monkeypatch.setattr(deal_watcher, "_emit_exception", emit)
    await deal_watcher.sweep_stalled_deals(NOW)
    assert (await outreach.sweep_followups(NOW))["followups"] == 0
    gate.assert_not_awaited()
    emit.assert_not_awaited()
    await pa.unpark("P1", "ops")
    assert (await outreach.sweep_followups(NOW))["followups"] == 1


async def test_desk_lists_parked_last_and_never_stalled(monkeypatch):
    from shared.orchestration import prospect_desk
    monkeypatch.setattr("shared.orchestration.matchmaking.build_board", AsyncMock(return_value=None))
    monkeypatch.setattr(prospect_desk, "_opportunity_profit", AsyncMock(return_value=None))
    parked = _deal("P1", parked_at=NOW, parked_reason="on hold (by ops)")
    active = _deal("P2")
    rows = await prospect_desk.desk([parked, active])
    assert [r.deal_id for r in rows] == ["P2", "P1"]
    assert rows[1].parked and not rows[1].stalled and rows[1].actions[0].kind == "unpark"


class TestRoutes:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.deals.router import router
        from shared.auth.dependencies import get_current_user
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            email="ops@portless.in", user_id="u1", is_portless=True, org_id="o", role="admin")
        return TestClient(app)

    async def test_park_unpark_drop_over_http(self, repo, monkeypatch):
        await repo.save(_deal())
        monkeypatch.setattr("services.hitl_approval.service.withdraw_pending", AsyncMock(return_value=[]))
        c = self._client()
        assert c.post("/deals/P1/prospect/park", json={"reason": "x"}).status_code == 422
        r = c.post("/deals/P1/prospect/park", json={"reason": "back in March"})
        assert r.status_code == 200 and r.json()["parked_at"]
        assert c.post("/deals/P1/prospect/unpark").json()["parked_at"] is None
        r = c.post("/deals/P1/prospect/drop", json={"reason": "no margin at any buyer price"})
        assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
        assert c.post("/deals/P1/prospect/park", json={"reason": "too late"}).status_code == 409


async def test_radar_estimate_is_backfilled_once(repo, monkeypatch):
    from shared.orchestration import prospect_desk
    old = _deal("P9", opportunity_id="OPP-1")
    has = _deal("P8", opportunity_id="OPP-2", radar_estimate={"expected_profit_usd": 1.0})
    none = _deal("P7")
    for d in (old, has, none):
        await repo.save(d)
    profit = AsyncMock(return_value={"expected_profit_usd": 5200.0, "margin_usd_per_mt": 61.0})
    monkeypatch.setattr(prospect_desk, "_opportunity_profit", profit)
    counts = await prospect_desk.backfill_radar_estimates([old, has, none])
    assert counts == {"checked": 1, "backfilled": 1, "no_opportunity_data": 0}
    stored = (await repo.get("P9")).radar_estimate
    assert stored["expected_profit_usd"] == 5200.0 and stored["backfilled_at"]
    assert (await repo.get("P8")).radar_estimate == {"expected_profit_usd": 1.0}
    profit.assert_awaited_once_with("OPP-1")


async def test_the_desk_stores_a_missing_estimate(repo, monkeypatch):
    from shared.orchestration import prospect_desk
    await repo.save(_deal("P9", opportunity_id="OPP-1"))
    monkeypatch.setattr("shared.orchestration.matchmaking.build_board", AsyncMock(return_value=None))
    monkeypatch.setattr(prospect_desk, "_opportunity_profit", AsyncMock(return_value={"expected_profit_usd": 900.0}))
    rows = await prospect_desk.desk([await repo.get("P9")])
    assert rows[0].radar_expected_profit_usd == 900.0
    assert (await repo.get("P9")).radar_estimate["expected_profit_usd"] == 900.0
