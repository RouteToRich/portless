"""Funnel analytics (shared/orchestration/funnel.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from shared.orchestration import funnel

NOW = datetime(2026, 11, 18, tzinfo=UTC)


def _h(status, days):
    return SimpleNamespace(status=status, transitioned_at=NOW - timedelta(days=days))


def _deal(deal_id, status, created_days, history=(), cands=()):
    return SimpleNamespace(deal_id=deal_id, status=status, created_at=NOW - timedelta(days=created_days),
                           updated_at=NOW, status_history=list(history), buyer_candidates=list(cands))


async def test_counts_conversion_days_and_cost(monkeypatch):
    deals = [
        _deal("A", "PAID", 60, [_h("MATCHING", 60), _h("NEGOTIATING", 50), _h("PI_SENT", 40), _h("PAID", 10)]),
        _deal("B", "NEGOTIATING", 30, [_h("MATCHING", 30), _h("NEGOTIATING", 20)]),
        _deal("C", "MATCHING", 20, [_h("MATCHING", 20)],
              cands=[SimpleNamespace(status="interested", replied_at=NOW - timedelta(days=15))]),
        _deal("D", "MATCHING", 10, [_h("MATCHING", 10)]),
        _deal("OLD", "PAID", 400, [_h("PAID", 380)]),
    ]
    monkeypatch.setattr("services.deals.service.list_deals", AsyncMock(return_value=deals))
    opps = [SimpleNamespace(scanned_at=NOW - timedelta(days=d), scan_cost={"usd": 2.5}) for d in (5, 25, 70, 200)]
    monkeypatch.setattr("services.trade_min.repository.get_repository",
                        AsyncMock(return_value=SimpleNamespace(list_active=AsyncMock(return_value=opps))))
    monkeypatch.setattr("agents.negotiation_agent.email_history.get_email_repository",
                        lambda: SimpleNamespace(list_by_deal=AsyncMock(return_value=[SimpleNamespace(direction="outbound")])))
    out = await funnel.build(90, NOW)
    by = {s["step"]: s for s in out["steps"]}
    assert [s["count"] for s in out["steps"]] == [3, 4, 3, 2, 1, 1]
    assert by["interested"]["conversion_pct"] == 75.0 and by["paid"]["conversion_pct"] == 100.0
    assert by["matched"]["median_days"] == 10.0 and by["paid"]["median_days"] == 50.0
    assert out["cost"] == {"apify_scan_usd": 7.5, "emails_sent": 4, "apify_usd_per_paid_deal": 7.5}
    assert out["biggest_drop"]["step"] == "pi"
