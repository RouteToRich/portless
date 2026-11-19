"""Money modules to 100 % (part 2): reference FX rates and the double-entry journal."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


class _Broken:
    def find(self, *a, **k):
        raise RuntimeError("store down")


@pytest.fixture(autouse=True)
def _clean():
    from shared import fx_rates
    fx_rates.reset_for_tests()
    yield
    fx_rates.reset_for_tests()


# ── shared/fx_rates.py ─────────────────────────────────────────────────────

def test_fx_settings_and_freshness(monkeypatch):
    from shared import fx_rates
    monkeypatch.setenv("FX_REFERENCE_MAX_AGE_DAYS", "x")
    assert fx_rates._f("FX_REFERENCE_MAX_AGE_DAYS", 7) == 7
    assert fx_rates._fresh({"as_of": "not a date"}) is False
    assert fx_rates._fresh({"as_of": "2026-11-18T00:00:00"}, NOW) is True  # naive date read as UTC
    monkeypatch.setenv("FX_RATE_EUR", "abc")
    assert fx_rates._setting("EUR") is None
    with patch("shared.mongo.collection", side_effect=RuntimeError("no mongo")):
        assert fx_rates._collection() is None


async def test_fx_record_in_mongo_with_audit_failure_and_refusals():
    from shared import fx_rates
    with pytest.raises(ValueError, match="number"):
        await fx_rates.record("EUR", "abc", source="ECB", by="ops")
    col = AsyncMongoMockClient()["t"]["fx_reference_rates"]
    with patch.object(fx_rates, "_collection", return_value=col), \
            patch("shared.audit_log.record", side_effect=RuntimeError("audit down")):
        row = await fx_rates.record("EUR", 0.92, source="ECB", by="ops", as_of=NOW)
        assert row["rate"] == 0.92
        fx_rates._cache.clear()  # noqa: SLF001
        assert (await fx_rates.latest("EUR"))["rate"] == 0.92  # read back from Mongo
        assert [r["currency"] for r in await fx_rates.history("EUR")] == ["EUR"]
        await fx_rates.warm()
    with patch.object(fx_rates, "_collection", return_value=_Broken()):
        assert await fx_rates.latest("EUR") is None
        assert await fx_rates.history() == []


async def test_fx_feed_refusals():
    from shared import fx_rates
    with patch.dict("os.environ", {"FX_REFERENCE_URL": " "}):
        assert (await fx_rates.refresh_from_source())["reason"] == "FX_REFERENCE_URL is empty"
    feed = httpx.MockTransport(lambda r: httpx.Response(200, json=["not", "a", "dict"]))
    assert (await fx_rates.refresh_from_source(transport=feed))["reason"] == "feed answered without rates"


# ── services/escrow_guard/journal.py ───────────────────────────────────────

def _hold(**kw):
    base = dict(deal_id="D", escrow_id="E", provider="tazapay", currency="USD", amount=10000.0, funded_amount=0.0,
                funded_at=NOW, ext_ref="X", shortfall=0.0, status="PENDING", tranches=[], refunded_total=0.0,
                provider_payload={}, updated_at=NOW, fee_pct=None, released_total=0.0, fees_total=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _ledger(**kw):
    base = dict(supplier_owed=None, payouts=[], portless_costs={}, portless_costs_basis={}, outstanding=0.0, issues=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_journal_nothing_funded_and_interest_with_a_bad_date():
    from services.escrow_guard.journal import journal_from
    j0 = journal_from(_hold(), _ledger(portless_costs={"freight": 120.0}, portless_costs_basis={"freight": "quote"}))
    assert j0.entries[0].event == "trade_cost:freight" and "(quote)" in j0.entries[0].memo
    j = journal_from(_hold(), _ledger(), interest=[
        {"interest_id": "I1", "amount_usd": 5.0, "belongs_to": "buyer", "at": "garbage", "ref": "ST", "period": None},
        {"interest_id": "I2", "amount_usd": 0.0, "belongs_to": "portless", "at": NOW.isoformat()}])
    assert [e.event for e in j.entries] == ["escrow_interest:I1"]  # a zero amount posts nothing
    assert j.entries[0].at is None and j.entries[0].credit == "buyer_deposits"


def test_fee_share_from_recorded_fees():
    from services.escrow_guard.journal import _fee_share
    assert _fee_share(_hold(fee_pct=1.5), 1000.0) == 15.0
    assert _fee_share(_hold(released_total=2000.0, fees_total=30.0), 1000.0) == 15.0
    assert _fee_share(_hold(), 1000.0) == 0.0


def test_accounting_csv_layouts(monkeypatch):
    from services.escrow_guard.journal import DealJournal, JournalEntry, accounting_csv, ledger_name
    monkeypatch.setenv("JOURNAL_LEDGER_PORTLESS_CASH", "HDFC Current A/c 1234")
    assert ledger_name("portless_cash") == "HDFC Current A/c 1234"
    j = DealJournal(deal_id="D1", currency="USD", generated_at=NOW, entries=[
        JournalEntry(seq=1, at=NOW, event="escrow_funded", debit="portless_cash", credit="buyer_deposits",
                     amount=100.0, currency="USD", ref="R1", memo="funded"),
        JournalEntry(seq=2, at=None, event="fee", debit="escrow_fees", credit="portless_cash", amount=1.0, currency="USD")])
    tally = accounting_csv([j], "tally").splitlines()
    assert tally[0].startswith("Date,Voucher Type") and tally[1].startswith("19-11-2026,Journal,D1-001,HDFC Current")
    assert tally[3].startswith(",Journal,D1-002")  # no date → empty cell
    zoho = accounting_csv([j], "zoho").splitlines()
    assert zoho[1].startswith("2026-11-19,D1-001,R1") and zoho[3].startswith(",D1-002,D1")
    with pytest.raises(ValueError):
        accounting_csv([j], "quickbooks")


async def test_build_all_journals_skips_broken_deals():
    from services.escrow_guard import journal
    holds = [SimpleNamespace(deal_id="A"), SimpleNamespace(deal_id="B")]
    repo = SimpleNamespace(list_holds=AsyncMock(return_value=holds))
    ok = journal.DealJournal(deal_id="A", currency="USD", generated_at=NOW)
    with patch("services.escrow_guard.repository.get_repository", return_value=repo), \
            patch.object(journal, "build_journal", new=AsyncMock(side_effect=[ok, RuntimeError("no hold")])):
        assert [j.deal_id for j in await journal.build_all_journals()] == ["A"]


async def test_build_journal_without_a_po_store():
    from services.escrow_guard import journal
    repo = SimpleNamespace(get_hold_by_deal=AsyncMock(return_value=_hold()))
    with patch("services.escrow_guard.repository.get_repository", return_value=repo), \
            patch("services.escrow_guard.service.get_deal_ledger", new=AsyncMock(return_value=_ledger())), \
            patch("services.dealmaker.repository.get_repository", side_effect=RuntimeError("down")):
        j = await journal.build_journal("D")
    assert j.deal_id == "D" and j.entries == []
