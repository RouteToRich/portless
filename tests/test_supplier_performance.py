"""Supplier performance from real deals (QC pass rate, ready on time, disputes, price kept) in the
directory score, and the view cache that serves the computed views (shared/orchestration/
supplier_performance.py, shared/view_cache.py)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from shared import view_cache
from shared.orchestration import supplier_performance as sp

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def _deal(deal_id, status, history=(), offers=()):
    party = SimpleNamespace(role="supplier", legal_name="Shree Spices Pvt Ltd", website="https://shreespices.in",
                            email="sales@shreespices.in", party_id="P1")
    return SimpleNamespace(deal_id=deal_id, status=status, parties=[party],
                           status_history=[SimpleNamespace(status=h) for h in history], supplier_offers=list(offers))


def _qc(result, date, kind="PSI"):
    return SimpleNamespace(order_type="QC", inspection_type=kind, qc_result=result, inspection_date=date)


def test_deal_facts_qc_ready_dispute_price():
    offer = SimpleNamespace(email="sales@shreespices.in", company_name="Shree Spices", fob_price_usd_per_mt=1000.0,
                            price_basis="quoted", received_at=NOW)
    deal = _deal("D1", "PAID", history=("PO_ISSUED", "QC_FAILED", "DISPUTE", "QC_PASSED", "PAID"), offers=[offer])
    commitment = SimpleNamespace(qc_ready_date="2026-10-01", confirmed_price=1040.0)
    f = sp.deal_facts(deal, [_qc("FAIL", "2026-10-06"), _qc("PASS", "2026-10-12"), _qc("PASS", "2026-09-20", "DUPRO")], commitment)
    assert f["qc_failed"] == 1 and f["qc_passed"] == 1  # DUPRO not counted
    assert f["ready"] == "late" and f["late_days"] == 5
    assert f["dispute"] and f["completed"] and f["price"] == "requoted_up" and f["price_change_pct"] == 4.0


def test_summary_rates():
    rows = [
        {"deal_id": "D1", "qc_passed": 1, "qc_failed": 1, "dispute": True, "completed": True, "cancelled": False,
         "ready": "late", "price": "requoted_up"},
        {"deal_id": "D2", "qc_passed": 2, "qc_failed": 0, "dispute": False, "completed": True, "cancelled": False,
         "ready": "on_time", "price": "kept"},
    ]
    s = sp.summarise(rows)
    assert s["qc_pass_rate"] == 0.75 and s["late_ready"] == 1 and s["on_time_ready"] == 1
    assert s["disputes"] == 1 and s["requoted_up"] == 1 and s["completed"] == 2


async def test_compute_groups_deals_by_company(monkeypatch):
    deals = [_deal("D1", "PAID"), _deal("D2", "QC_PASSED")]

    class DealRepo:
        async def list_active(self, limit=100):
            return deals

    class SlRepo:
        async def list_by_deal(self, deal_id):
            return [_qc("PASS" if deal_id == "D1" else "FAIL", "2026-10-02")]

    monkeypatch.setattr("services.deals.repository.get_repository", lambda: DealRepo())
    monkeypatch.setattr("services.shield_link.repository.get_repository", lambda: SlRepo())

    async def no_commitment(deal_id):
        return None

    monkeypatch.setattr("shared.orchestration.supplier_commitment.get", no_commitment)
    perf = await sp.compute()
    summary = perf["d:shreespices.in"]
    assert summary["deals"] == 2 and summary["qc_pass_rate"] == 0.5 and summary["completed"] == 1


def test_score_uses_performance():
    from services.trade_min.relationships import score_of
    from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation
    rec = CounterpartyRecord.model_construct(id="supplier:x", role="supplier", name="X", do_not_contact=False,
                                             validation=CounterpartyValidation(status="verified"), email=None,
                                             fit_score=None, flags=[], dnc_reason=None)
    good, _ = score_of(rec, {"stage": "traded", "performance": {"qc_inspections": 4, "qc_passed": 4, "qc_pass_rate": 1.0}}, None)
    bad, basis = score_of(rec, {"stage": "traded", "performance": {"qc_inspections": 2, "qc_passed": 0, "qc_pass_rate": 0.0,
                                                                   "late_ready": 2, "disputes": 1, "requoted_up": 1}}, None)
    assert good - bad == 20 + 10 + 10 + 5
    assert any("QC passed 0 of 2" in b for b in basis) and any("dispute" in b for b in basis)


async def test_view_cache_hits_until_a_deal_changes(monkeypatch):
    monkeypatch.setenv("VIEW_CACHE_SECONDS", "60")
    view_cache.clear()
    calls = []

    async def compute():
        calls.append(1)
        return len(calls)

    assert await view_cache.get_or_compute("v", compute) == 1
    assert await view_cache.get_or_compute("v", compute) == 1
    from services.deals.repository import InMemoryFallbackRepository as InMemoryDealRepository
    from services.deals.schemas import DealRecord
    from tests.factories import make
    await InMemoryDealRepository().save(make(DealRecord))  # any deal write invalidates
    assert await view_cache.get_or_compute("v", compute) == 2
    monkeypatch.setenv("VIEW_CACHE_SECONDS", "0")
    assert await view_cache.get_or_compute("v", compute) == 3
    view_cache.clear()


async def test_view_cache_shares_one_computation():
    import asyncio
    view_cache.clear()
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(0.01)
        return "x"

    out = await asyncio.gather(*(view_cache.get_or_compute("s", slow, ttl=60) for _ in range(5)))
    assert out == ["x"] * 5 and len(calls) == 1
    view_cache.clear()
