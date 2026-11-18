"""Journal export in Tally / Zoho Books import layouts (services/escrow_guard/journal.py)."""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import pytest

from services.escrow_guard.journal import DealJournal, JournalEntry, accounting_csv, ledger_name

AT = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


def _journal():
    return DealJournal(deal_id="D1", currency="USD", generated_at=AT, entries=[
        JournalEntry(seq=1, at=AT, event="escrow_funded", debit="escrow_trust", credit="buyer_deposits",
                     amount=50000.0, currency="USD", ref="TZP-1"),
        JournalEntry(seq=2, at=AT, event="tranche_released", debit="portless_cash", credit="escrow_trust",
                     amount=14850.0, currency="USD", ref="T1", memo="net of fee"),
    ])


def test_tally_layout_has_a_debit_and_a_credit_row_per_entry(monkeypatch):
    monkeypatch.setenv("JOURNAL_LEDGER_PORTLESS_CASH", "HDFC Current A/c 1234")
    rows = list(csv.DictReader(io.StringIO(accounting_csv([_journal()], "tally"))))
    assert len(rows) == 4 and rows[0]["Date"] == "18-11-2026" and rows[0]["Voucher Type"] == "Journal"
    assert (rows[2]["Ledger Name"], rows[2]["Debit"], rows[3]["Credit"]) == ("HDFC Current A/c 1234", "14850.00", "14850.00")
    assert rows[2]["Voucher Number"] == rows[3]["Voucher Number"] == "D1-002"
    assert sum(float(r["Debit"] or 0) for r in rows) == sum(float(r["Credit"] or 0) for r in rows)


def test_zoho_layout():
    rows = list(csv.DictReader(io.StringIO(accounting_csv([_journal()], "zoho"))))
    assert rows[0]["Journal Date"] == "2026-11-18" and rows[0]["Account"] == ledger_name("escrow_trust")
    assert rows[1]["Credits"] == "50000.00" and rows[1]["Currency"] == "USD" and rows[1]["Reference Number"] == "TZP-1"


def test_unknown_target():
    with pytest.raises(ValueError):
        accounting_csv([_journal()], "sap")
