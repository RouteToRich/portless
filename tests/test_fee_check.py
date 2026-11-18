"""Actual fees vs the fees priced in (shared/orchestration/fee_check.py)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.orchestration import fee_check as fc


@pytest.fixture(autouse=True)
def world(monkeypatch):
    fc.set_collection_for_tests(None)
    ledger = SimpleNamespace(deal_id="D1", released_total=40000.0, payouts_total=30000.0, fees_total=600.0)
    monkeypatch.setattr("services.escrow_guard.service.get_deal_ledger", AsyncMock(return_value=ledger))
    monkeypatch.setattr("services.escrow_guard.service.list_deal_ledgers", AsyncMock(return_value=[ledger]))
    monkeypatch.setattr(fc, "_assumed", AsyncMock(return_value={"escrow": 1.0, "payout": 0.0, "bank": 0.0}))
    yield ledger
    fc.set_collection_for_tests(None)


async def test_the_providers_quoted_fee_counts_until_a_statement_is_recorded():
    out = await fc.check("D1")
    escrow = next(l for l in out["lines"] if l["kind"] == "escrow")
    assert escrow["actual_pct"] == 1.5 and escrow["source"] == "provider quote" and escrow["over"]
    assert out["over_usd"] == 200.0  # 600 charged vs 400 assumed on 40,000 released


async def test_statements_replace_the_quote_and_bank_charges_are_caught():
    await fc.record("D1", "escrow", 380.0, ref="TZP-STMT-11", by="ops")
    await fc.record("D1", "bank", 45.0, ref="SWIFT charge", by="ops")
    out = await fc.check("D1")
    by = {l["kind"]: l for l in out["lines"]}
    assert not by["escrow"]["over"] and by["escrow"]["source"] == "statement"
    assert by["bank"]["over"] and by["bank"]["over_usd"] == 45.0
    assert await fc.over_all() == [{"deal_id": "D1", "over_usd": 45.0, "kinds": ["bank"]}]


async def test_refusals():
    with pytest.raises(ValueError):
        await fc.record("D1", "lunch", 10.0, ref=None, by="ops")
    with pytest.raises(ValueError):
        await fc.record("D1", "bank", -1.0, ref=None, by="ops")


def test_digest_line():
    from shared.orchestration import digest
    text = digest.render_digest_text({"generated_at": "2026-11-19T06:00", "fees_over": [
        {"deal_id": "D1", "over_usd": 245.0, "kinds": ["escrow", "bank"]}]})
    assert "FEES ABOVE THE PRICE (1 deal(s)): D1 +245 USD (escrow/bank)" in text
