"""Interest on escrow funds: recorded with its statement reference, PortLess's own raises the
margin and posts to interest_income, the buyer's is owed back (shared/orchestration/escrow_interest.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.orchestration import escrow_interest

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _clean():
    escrow_interest.reset_for_tests()
    yield
    escrow_interest.reset_for_tests()


async def test_record_validates_and_income_counts_only_ours():
    with pytest.raises(ValueError, match="statement reference"):
        await escrow_interest.record("D", 10, period=None, ref="", belongs_to="portless", by="ops")
    with pytest.raises(ValueError, match="above 0"):
        await escrow_interest.record("D", 0, period=None, ref="ST-1", belongs_to="portless", by="ops")
    with pytest.raises(ValueError, match="belongs_to"):
        await escrow_interest.record("D", 5, period=None, ref="ST-1", belongs_to="bank", by="ops")
    await escrow_interest.record("D", 12.5, period="Oct 2026", ref="ST-1", belongs_to="portless", by="ops")
    await escrow_interest.record("D", 4.0, period="Oct 2026", ref="ST-2", belongs_to="buyer", by="ops")
    await escrow_interest.record("OTHER", 99.0, period=None, ref="ST-3", belongs_to="portless", by="ops")
    assert await escrow_interest.income("D") == 12.5
    assert len(await escrow_interest.listed("D")) == 2


def test_journal_posts_interest_and_balances():
    from services.escrow_guard.journal import journal_from
    from tests.test_batch_c import _hold, _ledger
    interest = [{"interest_id": "INT-1", "amount_usd": 12.5, "belongs_to": "portless", "ref": "ST-1", "at": NOW.isoformat()},
                {"interest_id": "INT-2", "amount_usd": 4.0, "belongs_to": "buyer", "ref": "ST-2", "at": NOW.isoformat()}]
    j = journal_from(_hold(), _ledger(), po_issued_at=NOW + timedelta(hours=12), now=NOW + timedelta(days=40),
                     interest=interest)
    assert j.balanced and j.debits_total == j.credits_total
    assert j.balances["interest_income"] == pytest.approx(-12.5)  # credit
    events = [e.event for e in j.entries]
    assert "escrow_interest:INT-1" in events and "escrow_interest:INT-2" in events


async def test_interest_income_helper_reads_the_record():
    from services.escrow_guard import service
    await escrow_interest.record("D5", 7.25, period=None, ref="ST-9", belongs_to="portless", by="ops")
    assert await service._interest_income("D5") == 7.25
    assert await service._interest_income("D6") == 0.0


def test_route_records(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.escrow_guard import router
    from shared.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="ops", user_id="u", is_portless=True, org_id="o")
    c = TestClient(app)
    r = c.post("/escrow-guard/deals/D9/interest", json={"amount_usd": 3.2, "ref": "TZ-STMT-11", "period": "Nov"})
    assert r.status_code == 200 and r.json()["recorded"]["belongs_to"] == "portless"
    assert c.post("/escrow-guard/deals/D9/interest", json={"amount_usd": 3.2, "ref": ""}).status_code == 422
    assert len(c.get("/escrow-guard/deals/D9/interest").json()) == 1
