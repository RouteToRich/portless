"""Daily reconciliation of the books against the providers (todo: daily reconciliation job)."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from shared.orchestration import provider_reconciliation as pr


@pytest.fixture(autouse=True)
def _reset():
    pr.reset_for_tests()
    yield
    pr.reset_for_tests()


def _hold(funded=50000.0, status="FUNDED", released=("T1",)):
    tr = [SimpleNamespace(tranche_id=t, status="RELEASED" if t in released else "PENDING") for t in ("T1", "T2", "T3", "T4")]
    return SimpleNamespace(funded_amount=funded, status=status, tranches=tr)


def test_escrow_matches_the_provider():
    assert pr.compare_escrow(_hold(), {"funded": True, "funded_amount": 50000.0, "released": ["T1"]}) == []


def test_every_kind_of_escrow_difference():
    diffs = pr.compare_escrow(_hold(released=("T1", "T2")), {"funded": True, "funded_amount": 30000.0, "released": ["T1", "T3"]})
    kinds = sorted((d["kind"], d.get("tranche_id")) for d in diffs)
    assert kinds == [("funded_amount", None), ("tranche_not_booked", "T3"), ("tranche_not_paid_by_provider", "T2")]
    unfunded = pr.compare_escrow(_hold(), {"funded": False, "released": []})
    assert unfunded[0]["kind"] == "funded_amount" and "does not" in unfunded[0]["detail"]


def test_payout_differences():
    p = SimpleNamespace(payout_id="PAY-1", status="PROCESSING")
    assert pr.compare_payout(p, {"status": "COMPLETED"})[0]["kind"] == "payout_completed_not_booked"
    done = SimpleNamespace(payout_id="PAY-2", status="COMPLETED")
    assert pr.compare_payout(done, {"status": "FAILED"})[0]["kind"] == "payout_booked_but_failed"
    assert pr.compare_payout(done, {"status": "COMPLETED"}) == [] and pr.compare_payout(done, {}) == []


async def test_run_reads_providers_alerts_once_and_reports_unreadable(monkeypatch):
    from services.escrow_guard import service as eg
    holds = [SimpleNamespace(deal_id="D1", escrow_id="E1", provider="tazapay", ext_ref="TZ1", status="FUNDED",
                             funded_amount=50000.0, tranches=_hold().tranches),
             SimpleNamespace(deal_id="D2", escrow_id="E2", provider="escrow_com", ext_ref="EC2", status="FUNDED",
                             funded_amount=1.0, tranches=[]),
             SimpleNamespace(deal_id="D3", escrow_id="E3", provider="stripe", ext_ref="S3", status="FUNDED",
                             funded_amount=1.0, tranches=[])]

    class _Repo:
        async def list_holds(self):
            return holds

        async def list_payouts_by_deal(self, deal_id):
            return [SimpleNamespace(payout_id="PAY-1", deal_id=deal_id, status="PROCESSING", ext_ref="PN1")] if deal_id == "D1" else []

    class _Conn:
        def __init__(self, provider):
            self.provider = provider

        async def status(self, ref):
            if self.provider == "escrow_com":
                raise RuntimeError("down")
            return {}

        def interpret_status(self, body, tranches, event):
            return {"funded": True, "funded_amount": 50000.0, "released": ["T1", "T2"]}

    class _Payout:
        async def status(self, ref):
            return {"status": "COMPLETED"}

    events = []

    class _Pub:
        async def publish(self, e):
            events.append(e)
    monkeypatch.setattr("services.escrow_guard.repository.get_repository", lambda: _Repo())
    monkeypatch.setattr(eg, "_payment_connector", lambda p: _Conn(p))
    monkeypatch.setattr(pr, "_payout_connector", lambda: _Payout())
    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    now = datetime(2026, 11, 18, 3, tzinfo=UTC)
    report = await pr.run(now=now)
    assert report["escrows_checked"] == 1 and report["payouts_checked"] == 1
    assert {d["kind"] for d in report["differences"]} == {"tranche_not_booked", "payout_completed_not_booked"}
    assert report["unreadable"][0]["deal_id"] == "D2"
    assert len(events) == 2 and all(e.payload["stage"].startswith("RECONCILIATION:") for e in events)
    await pr.run(now=now)
    assert len(events) == 2  # same differences, same day: no second alert
    assert await pr.due_daily(now) is None  # already ran today
