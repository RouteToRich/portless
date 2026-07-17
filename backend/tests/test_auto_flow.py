"""Tests for shared.orchestration.auto_flow.resume_in_flight.

Covers the restart-recovery gap: auto_flow's per-deal task is a bare
asyncio.create_task with no persistence of its own (see auto_flow.py's module
docstring). resume_in_flight() is the startup-time fix — it re-arms the task
for every opportunity-seeded deal still sitting in MATCHING/NEGOTIATING,
instead of leaving those deals permanently orphaned after a restart.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import DealRecord, DealStatus
from shared.orchestration import auto_flow


def _deal(deal_id: str, status: DealStatus, opportunity_id: str | None) -> DealRecord:
    now = datetime.now(timezone.utc)
    return DealRecord(
        deal_id=deal_id,
        operator_id="OP-1",
        org_id="ORG-1",
        hs_code="090422",
        market="US",
        status=status,
        opportunity_id=opportunity_id,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def _repo(monkeypatch) -> InMemoryFallbackRepository:
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository.get_repository", lambda: repo)
    return repo


async def test_resume_starts_matching_and_negotiating_opportunity_deals(_repo):
    await _repo.save(_deal("DEAL-1", DealStatus.MATCHING, "OPP-1"))
    await _repo.save(_deal("DEAL-2", DealStatus.NEGOTIATING, "OPP-2"))

    with patch("shared.orchestration.auto_flow.enabled", return_value=True):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    started = {call.args[0] for call in mock_start.call_args_list}
    assert started == {"DEAL-1", "DEAL-2"}


async def test_resume_skips_deals_without_opportunity_id(_repo):
    await _repo.save(_deal("DEAL-MANUAL", DealStatus.NEGOTIATING, None))

    with patch("shared.orchestration.auto_flow.enabled", return_value=True):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    mock_start.assert_not_called()


async def test_resume_skips_deals_in_other_statuses(_repo):
    await _repo.save(_deal("DEAL-DONE", DealStatus.PAID, "OPP-3"))
    await _repo.save(_deal("DEAL-DRAFT", DealStatus.DRAFT, "OPP-4"))

    with patch("shared.orchestration.auto_flow.enabled", return_value=True):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    mock_start.assert_not_called()


async def test_resume_is_noop_when_auto_flow_disabled(_repo):
    await _repo.save(_deal("DEAL-1", DealStatus.NEGOTIATING, "OPP-1"))

    with patch("shared.orchestration.auto_flow.enabled", return_value=False):
        with patch("shared.orchestration.auto_flow.start") as mock_start:
            await auto_flow.resume_in_flight()

    mock_start.assert_not_called()


async def test_resume_continues_past_a_scan_failure(_repo, caplog):
    """A repository error for one status must not block scanning the other."""
    await _repo.save(_deal("DEAL-OK", DealStatus.NEGOTIATING, "OPP-1"))

    original_list_active = _repo.list_active

    async def _flaky_list_active(*args, **kwargs):
        if kwargs.get("status") == DealStatus.MATCHING:
            raise RuntimeError("boom")
        return await original_list_active(*args, **kwargs)

    with patch.object(_repo, "list_active", side_effect=_flaky_list_active):
        with patch("shared.orchestration.auto_flow.enabled", return_value=True):
            with patch("shared.orchestration.auto_flow.start") as mock_start:
                await auto_flow.resume_in_flight()

    mock_start.assert_called_once_with("DEAL-OK")
