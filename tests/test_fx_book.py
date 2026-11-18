"""Exchange rates on a deal and the FX gain / loss posted to the journal (shared/orchestration/fx_book.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shared.orchestration import fx_book

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _memory():
    fx_book.set_collection_for_tests(None)
    yield
    fx_book.set_collection_for_tests(None)


def test_gain_when_the_rupee_weakened_loss_when_it_strengthened():
    # 10,000 USD paid at 85 = 8.5 lakh rupees; at the PO rate 84 those rupees were worth 10,119.05 USD
    assert fx_book.gain_loss(10_000, 85.0, 84.0) == pytest.approx(119.05)
    assert fx_book.gain_loss(10_000, 83.0, 84.0) == pytest.approx(-119.05)
    assert fx_book.gain_loss(10_000, None, 84.0) == 0.0 and fx_book.gain_loss(10_000, 85.0, None) == 0.0


async def test_record_validates_and_latest_rate_wins():
    with pytest.raises(ValueError):
        await fx_book.record("D", "spot", 84.0, by="staff")
    with pytest.raises(ValueError):
        await fx_book.record("D", "po", 0, by="staff")
    with pytest.raises(ValueError, match="payout id"):
        await fx_book.record("D", "payout", 85.0, by="staff")
    await fx_book.record("D", "po", 83.0, by="staff")
    await fx_book.record("D", "po", 84.0, by="staff", note="corrected")
    await fx_book.record("D", "payout", 85.0, ref="PAY-1", by="staff")
    await fx_book.record("D", "receipt", 84.6, ref="T1", by="staff")
    await fx_book.record("OTHER", "po", 70.0, by="staff")
    r = await fx_book.rates("D")
    assert r == {"po": 84.0, "payout": {"PAY-1": 85.0}, "receipt": {"T1": 84.6}}


async def test_summary_names_what_is_missing():
    paid = [SimpleNamespace(payout_id="PAY-1", amount=10_000.0, status="COMPLETED"),
            SimpleNamespace(payout_id="PAY-2", amount=500.0, status="REQUESTED")]
    s = await fx_book.summary("D", payouts=paid)
    assert s["gain_loss_usd"] == 0.0 and len(s["missing"]) == 2
    await fx_book.record("D", "po", 84.0, by="staff")
    await fx_book.record("D", "payout", 85.0, ref="PAY-1", by="staff")
    s = await fx_book.summary("D", payouts=paid)
    assert s["missing"] == [] and s["gain_loss_usd"] == pytest.approx(119.05)
    assert s["payouts"][0]["inr"] == 850_000.0 and len(s["payouts"]) == 1


def _journal(fx):
    from services.escrow_guard.journal import journal_from
    from tests.test_batch_c import _hold, _ledger
    return journal_from(_hold(), _ledger(), po_issued_at=NOW + timedelta(hours=12), now=NOW + timedelta(days=40), fx=fx)


@pytest.mark.parametrize("rate,event,sign", [(85.0, "fx_gain:PAY-1", -1), (83.0, "fx_loss:PAY-1", 1)])
def test_journal_posts_the_gain_or_loss_and_still_balances(rate, event, sign):
    j = _journal({"po": 84.0, "payout": {"PAY-1": rate}, "receipt": {}})
    assert j.balanced and j.debits_total == j.credits_total
    assert event in [e.event for e in j.entries]
    diff = abs(fx_book.gain_loss(40_000.0, rate, 84.0))
    assert j.balances["fx_gain_loss"] == pytest.approx(sign * diff)  # credit (negative) = gain


def test_no_rates_no_fx_entries():
    j = _journal(None)
    assert not [e for e in j.entries if e.event.startswith("fx_")] and j.balances["fx_gain_loss"] == 0.0
    j = _journal({"po": 84.0, "payout": {}, "receipt": {}})
    assert not [e for e in j.entries if e.event.startswith("fx_")]


def test_routes_record_and_show():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.escrow_guard.router import router
    from shared.auth.dependencies import require_portless
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_portless] = lambda: SimpleNamespace(email="staff@portless.test", user_id="u1")
    client = TestClient(app)
    ok = client.post("/escrow-guard/deals/DEAL-FX/fx", json={"kind": "po", "rate_inr_per_usd": 84.0})
    assert ok.status_code == 200 and ok.json()["po_rate"] == 84.0
    bad = client.post("/escrow-guard/deals/DEAL-FX/fx", json={"kind": "payout", "rate_inr_per_usd": 85.0})
    assert bad.status_code == 422
    assert client.post("/escrow-guard/deals/DEAL-FX/fx", json={"kind": "po", "rate_inr_per_usd": -1}).status_code == 422
    assert client.get("/escrow-guard/deals/DEAL-FX/fx").json()["po_rate"] == 84.0


async def test_realised_margin_includes_the_fx_gain(monkeypatch):
    """Review board B-22: the gain / loss reaches the ledger's realised margin, not only the journal."""
    from services.escrow_guard import service as eg
    paid = [SimpleNamespace(payout_id="PAY-1", amount=10_000.0, status="COMPLETED")]
    assert await eg._fx_gain_loss("D", paid) == 0.0
    await fx_book.record("D", "po", 84.0, by="staff")
    await fx_book.record("D", "payout", 85.0, ref="PAY-1", by="staff")
    assert await eg._fx_gain_loss("D", paid) == pytest.approx(119.05)

    async def broken(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(fx_book, "summary", broken)
    assert await eg._fx_gain_loss("D", paid) == 0.0
