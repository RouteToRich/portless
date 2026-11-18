"""DEF-18: public holidays skipped, a report whose hash differs from the inspector's CRF hash is
held, and a facility address changed after booking is raised."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.orchestration import qc_schedule as qs


def test_public_holidays_are_skipped(monkeypatch):
    monkeypatch.setenv("QC_PUBLIC_HOLIDAYS", "2026-11-09, 2026-11-10,not-a-date")
    # Fri 6 Nov + 2 working days, skipping the weekend and Mon/Tue holidays → Thu 12 Nov
    assert qs.add_business_days(date(2026, 11, 6), 2) == date(2026, 11, 12)
    assert qs.next_business_day(date(2026, 11, 7)) == date(2026, 11, 11)
    monkeypatch.delenv("QC_PUBLIC_HOLIDAYS")
    assert qs.next_business_day(date(2026, 11, 7)) == date(2026, 11, 9)


async def test_address_change_after_booking_is_raised(monkeypatch):
    import services.shield_link.service as sl
    from services.deals import service as ds
    orders = [SimpleNamespace(order_type="QC", status="BOOKED", tpo_id="TPO-QC-1"),
              SimpleNamespace(order_type="FREIGHT", status="BOOKED", tpo_id="TPO-F-1")]
    monkeypatch.setattr(sl, "list_deal_orders", AsyncMock(return_value=SimpleNamespace(orders=orders)))
    events = []

    class _Pub:
        async def publish(self, e):
            events.append(e)
    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    await ds._address_changed_after_booking("D1", "Plot 12, SIPCOT, Thanjavur 613005")
    assert events[0].payload["stage"] == "QC:address_changed_after_booking" and events[0].payload["tpo_ids"] == ["TPO-QC-1"]
    monkeypatch.setattr(sl, "list_deal_orders", AsyncMock(return_value=SimpleNamespace(orders=orders[1:])))
    await ds._address_changed_after_booking("D1", "x")
    assert len(events) == 1  # no open inspection: nothing to correct


async def test_report_not_matching_the_crf_hash_is_held(monkeypatch):
    from services.shield_link import qc
    from services.shield_link import service as sl
    from services.shield_link.repository import InMemoryShieldLinkRepository
    from services.shield_link.schemas import ThirdPartyOrderRecord
    import services.shield_link.repository as repo_mod
    from datetime import UTC, datetime
    repo_mod._repository = InMemoryShieldLinkRepository()
    now = datetime.now(UTC)
    tpo = ThirdPartyOrderRecord(tpo_id="TPO-QC-9", deal_id="D9", order_type="QC", provider="qima", status="BOOKED",
                                ext_ref="Q-9", created_at=now, updated_at=now)
    await repo_mod._repository.save(tpo)
    monkeypatch.setattr(qc, "_fetch_documents", AsyncMock(return_value=[{"name": "r.pdf", "sha256": "aaa", "file_type": "pdf"}]))
    emitted = AsyncMock()
    monkeypatch.setattr(sl, "_emit", emitted)
    trans = AsyncMock()
    monkeypatch.setattr(sl, "_transition_deal", trans)
    try:
        await qc._apply_update(tpo, source="webhook", status="REPORT_ISSUED", result="PASS", crf_hash="bbb")
        stages = [c.args[2].get("stage") for c in emitted.await_args_list]
        assert "QC_REPORT_HASH_MISMATCH" in stages
        trans.assert_not_awaited()  # never moved to QC_PASSED
    finally:
        repo_mod._repository = None
