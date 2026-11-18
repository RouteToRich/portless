"""Export proceeds realised per Shipping Bill, and the manual CHA filing (DEF-17, RBI time limit)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.orchestration import realisation as rz

TODAY = date(2026, 11, 18)


@pytest.mark.parametrize("realised,sb,status", [
    (25000.0, date(2026, 6, 1), "realised"),
    (10000.0, date(2026, 6, 1), "open"),          # deadline 2027-02-26
    (10000.0, date(2026, 2, 28), "due_soon"),     # deadline 2026-11-25
    (10000.0, date(2026, 1, 1), "overdue"),
    (10000.0, None, "open"),
])
def test_assess(monkeypatch, realised, sb, status):
    monkeypatch.delenv("REALISATION_DAYS", raising=False)
    out = rz.assess(25000.0, realised, sb, TODAY)
    assert out["status"] == status
    if status != "realised":
        assert out["outstanding"] == 15000.0


async def test_for_deal_reads_the_shipping_bills_and_skips_stubs(monkeypatch):
    import services.escrow_guard.repository as eg
    import services.shield_link.service as sl
    orders = [SimpleNamespace(order_type="CHA", status="COMPLETED", ext_ref="SB-1234567", provider="cha_manual",
                              provider_payload={"shipping_bill_date": "2026-03-01"}, created_at=datetime.now(UTC)),
              SimpleNamespace(order_type="CHA", status="COMPLETED", ext_ref="STUB", provider="icegate",
                              provider_payload={"stub": True}, created_at=datetime.now(UTC))]
    monkeypatch.setattr(sl, "list_deal_orders", AsyncMock(return_value=SimpleNamespace(orders=orders)))
    monkeypatch.setattr(eg, "get_repository", lambda: SimpleNamespace(get_hold_by_deal=AsyncMock(
        return_value=SimpleNamespace(amount=25000.0, released_total=17500.0, currency="USD"))))
    out = await rz.for_deal("D1", TODAY)
    (sb,) = out["shipping_bills"]
    assert sb["shipping_bill_number"] == "SB-1234567" and sb["outstanding"] == 7500.0 and sb["deadline"] == "2026-11-26"
    assert sb["status"] == "due_soon"


async def test_manual_customs_filing_is_recorded_once(monkeypatch):
    from services.shield_link import service as sl
    from services.shield_link.repository import InMemoryShieldLinkRepository
    import services.shield_link.repository as repo_mod
    repo_mod._repository = InMemoryShieldLinkRepository()
    monkeypatch.setattr(sl, "_fetch_deal", AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(value="IN_TRANSIT"))))
    emitted = AsyncMock()
    monkeypatch.setattr(sl, "_emit", emitted)
    try:
        a = await sl.record_manual_customs("D1", shipping_bill_number="SB-9", shipping_bill_date="2026-11-10",
                                          let_export_order_date="2026-11-11", port="INNSA1", by="ops@portless.in")
        b = await sl.record_manual_customs("D1", shipping_bill_number="SB-9", shipping_bill_date="2026-11-10",
                                          let_export_order_date=None, port=None, by="ops@portless.in")
        assert a.tpo_id == b.tpo_id and a.status == "COMPLETED"
        assert emitted.await_count == 1 and emitted.await_args.args[2]["manual"] is True
    finally:
        repo_mod._repository = None
