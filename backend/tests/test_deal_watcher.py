"""Deal SLA watcher (shared/orchestration/deal_watcher.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import DealRecord, DealStatus
from shared.orchestration import deal_watcher as dw

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _deal(deal_id: str, status: DealStatus, age_hours: float) -> DealRecord:
    ts = NOW - timedelta(hours=age_hours)
    return DealRecord(
        deal_id=deal_id, operator_id="op", org_id="portless", hs_code="1006", market="DE",
        status=status, created_at=ts, updated_at=ts,
    )


@pytest.fixture
def wiring(monkeypatch):
    dw.reset_for_tests()
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    published: list = []

    class _Pub:
        async def publish(self, event):
            published.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    pi_reminder = AsyncMock(return_value="EMAIL-PI")
    escrow_reminder = AsyncMock(return_value="EMAIL-ESC")
    monkeypatch.setattr("shared.orchestration.notifications.notify_pi_reminder", pi_reminder)
    monkeypatch.setattr("shared.orchestration.notifications.notify_escrow_reminder", escrow_reminder)
    dm_repo = AsyncMock()
    dm_repo.get_pi_by_deal = AsyncMock(return_value=None)
    monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: dm_repo)
    eg_repo = AsyncMock()
    eg_repo.get_hold_by_deal = AsyncMock(return_value=None)
    monkeypatch.setattr("services.escrow_guard.repository.get_repository", lambda: eg_repo)
    for var in ("DEAL_WATCHER_ENABLED", "DEAL_PI_REMINDER_AFTER_HOURS",
                "DEAL_ESCROW_REMINDER_AFTER_HOURS", "DEAL_REMINDER_MAX", "DEAL_STALL_SLA_HOURS"):
        monkeypatch.delenv(var, raising=False)
    yield SimpleNamespace(
        repo=repo, published=published, pi_reminder=pi_reminder,
        escrow_reminder=escrow_reminder, dm_repo=dm_repo, eg_repo=eg_repo,
    )
    dw.reset_for_tests()


def _pi(issued_hours_ago: float, expires_in_hours: float):
    return SimpleNamespace(
        pi_id="PI-1", issued_at=NOW - timedelta(hours=issued_hours_ago),
        expires_at=NOW + timedelta(hours=expires_in_hours),
    )


class TestPiSent:
    @pytest.mark.asyncio
    async def test_first_reminder_after_window(self, wiring):
        await wiring.repo.save(_deal("D1", DealStatus.PI_SENT, 60))
        wiring.dm_repo.get_pi_by_deal.return_value = _pi(60, 108)
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts == {"scanned": 1, "reminders": 1, "exceptions": 0}
        wiring.pi_reminder.assert_awaited_once()
        assert wiring.pi_reminder.await_args.args[2] == 1  # reminder #1

    @pytest.mark.asyncio
    async def test_no_reminder_before_window(self, wiring):
        await wiring.repo.save(_deal("D1", DealStatus.PI_SENT, 10))
        wiring.dm_repo.get_pi_by_deal.return_value = _pi(10, 158)
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts["reminders"] == 0 and counts["exceptions"] == 0

    @pytest.mark.asyncio
    async def test_reminders_capped(self, wiring):
        await wiring.repo.save(_deal("D1", DealStatus.PI_SENT, 150))
        wiring.dm_repo.get_pi_by_deal.return_value = _pi(150, 18)  # 3 windows elapsed, max 2
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts["reminders"] == 0
        wiring.pi_reminder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_pi_raises_exception_once(self, wiring):
        await wiring.repo.save(_deal("D1", DealStatus.PI_SENT, 200))
        wiring.dm_repo.get_pi_by_deal.return_value = _pi(200, -32)
        await dw.sweep_stalled_deals(NOW)
        await dw.sweep_stalled_deals(NOW)
        assert len(wiring.published) == 1
        payload = wiring.published[0].payload
        assert payload["stage"] == "PI_SENT:expired"
        assert payload["source"] == "deal_watcher"
        assert payload["pi_id"] == "PI-1"
        wiring.pi_reminder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_pi_record_is_an_exception(self, wiring):
        await wiring.repo.save(_deal("D1", DealStatus.PI_SENT, 1))
        await dw.sweep_stalled_deals(NOW)
        assert wiring.published[0].payload["stage"] == "PI_SENT:missing_pi"


class TestEscrowPending:
    @pytest.mark.asyncio
    async def test_reminder_then_exception(self, wiring):
        await wiring.repo.save(_deal("D2", DealStatus.ESCROW_PENDING, 60))
        hold = SimpleNamespace(escrow_id="ESC-1", amount=30000.0, currency="USD",
                               created_at=NOW - timedelta(hours=60))
        wiring.eg_repo.get_hold_by_deal.return_value = hold
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts["reminders"] == 1
        wiring.escrow_reminder.assert_awaited_once()

        hold.created_at = NOW - timedelta(hours=160)  # past the 2nd reminder window
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts["exceptions"] == 1
        assert wiring.published[0].payload["stage"] == "ESCROW_PENDING:unfunded"


class TestGenericStall:
    @pytest.mark.asyncio
    async def test_stalled_status_past_sla(self, wiring):
        await wiring.repo.save(_deal("D3", DealStatus.SUPPLIER_DISPATCH_READY, 24 * 8))
        await wiring.repo.save(_deal("D4", DealStatus.IN_TRANSIT, 24 * 2))
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts["scanned"] == 2 and counts["exceptions"] == 1
        assert wiring.published[0].deal_id == "D3"
        assert wiring.published[0].payload["stage"] == "SUPPLIER_DISPATCH_READY:stalled"

    @pytest.mark.asyncio
    async def test_skips_terminal_negotiating_and_draft(self, wiring):
        for i, st in enumerate((DealStatus.PAID, DealStatus.CANCELLED, DealStatus.NEGOTIATING,
                                DealStatus.DRAFT, DealStatus.NEGOTIATION_FAILED)):
            await wiring.repo.save(_deal(f"D{i}", st, 24 * 30))
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts == {"scanned": 0, "reminders": 0, "exceptions": 0}

    @pytest.mark.asyncio
    async def test_disabled(self, wiring, monkeypatch):
        monkeypatch.setenv("DEAL_WATCHER_ENABLED", "false")
        await wiring.repo.save(_deal("D3", DealStatus.IN_TRANSIT, 24 * 30))
        assert await dw.sweep_stalled_deals(NOW) == {"scanned": 0, "reminders": 0, "exceptions": 0}

    @pytest.mark.asyncio
    async def test_one_bad_deal_does_not_stop_sweep(self, wiring):
        await wiring.repo.save(_deal("D1", DealStatus.PI_SENT, 200))
        await wiring.repo.save(_deal("D3", DealStatus.IN_TRANSIT, 24 * 8))
        wiring.dm_repo.get_pi_by_deal.side_effect = RuntimeError("mongo down")
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts["exceptions"] == 1
        assert wiring.published[0].deal_id == "D3"
