"""Every planned inspection is priced, and a re-inspection after the supplier's failed QC is
charged to the supplier (review board B-17)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.orchestration import chargebacks, trade_costs
from shared.orchestration import landed_cost


@pytest.fixture(autouse=True)
def _memory(monkeypatch):
    monkeypatch.delenv("SUPPLIER_REINSPECTION_CHARGE", raising=False)
    chargebacks.set_collection_for_tests(None)
    yield
    chargebacks.set_collection_for_tests(None)


def _sheet(types):
    return trade_costs.build(quantity_mt=25.0, market="AE", freight_usd_per_mt=None, freight_is_quote=False,
                             qc_usd_per_mt=None, qc_is_quote=False, supplier_address=None, inspection_types=types)


def test_each_planned_inspection_is_priced():
    one, three = _sheet(None), _sheet(["DUPRO", "PSI", "PEO"])
    line = {s: next(ln for ln in sh.lines if ln.key == "inspection") for s, sh in (("one", one), ("three", three))}
    per_visit = landed_cost.inspection_usd_per_shipment() / 25.0
    assert line["one"].usd_per_mt == pytest.approx(round(per_visit, 2))
    assert line["three"].usd_per_mt == pytest.approx(round(per_visit * 3, 2), abs=0.02)
    assert "DUPRO + PSI + PEO" in line["three"].label and three.inspection_phases == 3
    assert three.fixed_usd_per_mt > one.fixed_usd_per_mt  # the buyer band and walk-away follow


async def test_chargeback_lowers_what_the_supplier_is_owed_once_per_booking():
    await chargebacks.record("D1", "reinspection", 350.0, reason="re-inspection #1", ref="TPO-2")
    await chargebacks.record("D1", "reinspection", 350.0, reason="again", ref="TPO-2")   # same booking: once
    assert await chargebacks.total("D1") == 350.0
    assert await chargebacks.net_owed("D1", 20_000.0) == 19_650.0
    assert await chargebacks.net_owed("D1", None) is None and await chargebacks.net_owed("D2", 100.0) == 100.0
    with pytest.raises(ValueError):
        await chargebacks.record("D1", "late_delivery", 10.0, reason="x")
    assert await chargebacks.record("D1", "reinspection", 0, reason="free") is None


async def test_reinspection_cost_prefers_the_order_fee():
    assert await chargebacks.reinspection_cost("D9", SimpleNamespace(provider_payload={"fee_usd": 410.0})) == 410.0
    assert await chargebacks.reinspection_cost("D9", None) == pytest.approx(landed_cost.inspection_usd_per_shipment())


def test_rework_notice_says_it_is_at_the_suppliers_cost():
    from shared.orchestration.notifications import _tpl_qc_rework_notice
    _, body = _tpl_qc_rework_notice({"deal_id": "D1", "supplier_name": "Mill", "ext_ref": "Q-2", "charge_usd": 350.0})
    assert "USD 350.00" in body and "deducted from your next payment" in body
    _, plain = _tpl_qc_rework_notice({"deal_id": "D1", "supplier_name": "Mill"})
    assert "deducted" not in plain


def test_journal_posts_the_chargeback_and_stays_balanced():
    from datetime import UTC, datetime, timedelta

    from services.escrow_guard.journal import journal_from
    from tests.test_batch_c import _hold, _ledger
    now = datetime.now(UTC)
    led = _ledger(supplier_owed=46000.0 - 350.0)  # the ledger shows the net entitlement
    j = journal_from(_hold(), led, now=now + timedelta(days=40),
                     chargebacks=[{"kind": "reinspection", "amount_usd": 350.0, "reason": "re-inspection #1", "ref": "TPO-2"}])
    assert j.balanced and j.debits_total == j.credits_total
    assert "supplier_chargeback:reinspection" in [e.event for e in j.entries]
    assert j.balances["supplier_payable"] == pytest.approx(-(46000.0 - 350.0 - 40000.0))


async def test_the_switch_turns_the_charge_off(monkeypatch):
    monkeypatch.setenv("SUPPLIER_REINSPECTION_CHARGE", "false")
    assert chargebacks.reinspection_charged() is False


def test_value_added_services_carry_a_service_charge(monkeypatch):
    """todo: value-added services priced explicitly — extra inspections and special packing."""
    monkeypatch.delenv("VAS_MARKUP_PCT", raising=False)
    plain, three = _sheet(None), _sheet(["DUPRO", "PSI", "PEO"])
    assert not [ln for ln in plain.lines if ln.key == "value_added_services"]
    (vas,) = [ln for ln in three.lines if ln.key == "value_added_services"]
    per_visit = landed_cost.inspection_usd_per_shipment() / 25.0
    assert vas.usd_per_mt == pytest.approx(round(per_visit * 2 * 0.15, 2), abs=0.01)
    monkeypatch.setenv("VAS_MARKUP_PCT", "0")
    assert not [ln for ln in _sheet(["DUPRO", "PSI"]).lines if ln.key == "value_added_services"]
