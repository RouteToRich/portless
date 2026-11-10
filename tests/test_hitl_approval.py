"""Tests for HITL Approval service.

Covers:
  - L0 create (PENDING)
  - L1 create (PENDING with expires_at)
  - L2 create (AUTO_APPROVED immediately)
  - L3 create (AUTO_APPROVED with warning)
  - Invalid action rejected
  - Idempotency on idempotency_key
  - Human APPROVE → callback fired, approval.resolved emitted
  - Human REJECT → deal.exception emitted, approval.resolved emitted
  - Resolve non-existent approval → 404
  - Resolve already-resolved approval → 409
  - Timeout watcher fires auto-resolve on expired L1 approvals
  - list_approvals filter by status and deal_id
  - SSE: callback APPROVE triggers execute_callback
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from services.hitl_approval.schemas import (
    CreateApprovalRequest,
    ResolveApprovalRequest,
)
from services.hitl_approval.service import (
    ApprovalAlreadyResolved,
    ApprovalNotFoundError,
    InvalidAction,
    _auto_resolve,
    create_approval,
    get_approval,
    list_approvals,
    resolve_approval,
    run_timeout_watcher,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_in_memory_repo(monkeypatch):
    """Wire in-memory repository and silence SSE/webhook/event side effects."""
    import services.hitl_approval.repository as repo_mod
    repo_mod._repository = InMemoryHitlApprovalRepository()
    repo_mod._motor_client = None
    yield
    repo_mod._repository = None


@pytest.fixture(autouse=True)
def silence_side_effects(monkeypatch):
    """Stub out notification, event bus, and callbacks for unit tests."""
    monkeypatch.setattr(
        "services.hitl_approval.service.notify",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "services.hitl_approval.service._emit",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "services.hitl_approval.service.execute_callback",
        AsyncMock(return_value={"stub": True}),
    )


# ---------------------------------------------------------------------------
# Idempotency under concurrency
# ---------------------------------------------------------------------------

class _SlowLookupRepo(InMemoryHitlApprovalRepository):
    """The key lookup yields like a database round-trip, so two callers
    overlap between "no approval with this key" and the save."""

    async def get_by_idempotency_key(self, key):
        found = await super().get_by_idempotency_key(key)
        await asyncio.sleep(0.01)  # the answer travels back while the other caller reads too
        return found


class TestConcurrentIdempotency:
    @pytest.mark.asyncio
    async def test_same_key_at_the_same_time_creates_one_approval(self):
        import services.hitl_approval.repository as repo_mod
        repo = _SlowLookupRepo()
        repo_mod._repository = repo
        req = CreateApprovalRequest(deal_id="DEAL-01", action="RELEASE_SUPPLIER_PAYOUT", autonomy_level="L0",
                                    financial_impact_usd=17726.10, idempotency_key="auto-payout-gate-DEAL-01")
        a, b = await asyncio.gather(create_approval(req), create_approval(req))
        assert a.approval_id == b.approval_id
        assert len(await repo.list_by_deal("DEAL-01")) == 1

    @pytest.mark.asyncio
    async def test_different_keys_are_not_serialised_into_one(self):
        import services.hitl_approval.repository as repo_mod
        repo = _SlowLookupRepo()
        repo_mod._repository = repo
        reqs = [CreateApprovalRequest(deal_id="DEAL-02", action="BOOK_FREIGHT", autonomy_level="L0", idempotency_key=k)
                for k in ("k1", "k2")]
        a, b = await asyncio.gather(*(create_approval(r) for r in reqs))
        assert a.approval_id != b.approval_id
        assert len(await repo.list_by_deal("DEAL-02")) == 2

    @pytest.mark.asyncio
    async def test_locks_are_released_after_use(self):
        from services.hitl_approval import service as hitl
        await create_approval(CreateApprovalRequest(deal_id="DEAL-03", action="BOOK_FREIGHT",
                                                    autonomy_level="L0", idempotency_key="k3"))
        assert "k3" not in hitl._key_locks and "k3" not in hitl._key_waiters


# ---------------------------------------------------------------------------
# Create approval — L0
# ---------------------------------------------------------------------------

class TestCreateApprovalL0:
    @pytest.mark.asyncio
    async def test_creates_pending(self):
        req = CreateApprovalRequest(
            deal_id="DEAL-HITL-01",
            action="ISSUE_PO",
            summary="Approve PO issuance",
            autonomy_level="L0",
        )
        result = await create_approval(req)
        assert result.status == "PENDING"
        assert result.approval_id.startswith("APPR-")
        assert result.deep_link is not None
        assert "APPR-" in result.deep_link

    @pytest.mark.asyncio
    async def test_no_expires_at_for_l0(self):
        req = CreateApprovalRequest(deal_id="DEAL-01", action="BOOK_FREIGHT", autonomy_level="L0")
        result = await create_approval(req)
        assert result.expires_at is None

    @pytest.mark.asyncio
    async def test_emits_approval_requested(self):
        from services.hitl_approval import service as svc
        req = CreateApprovalRequest(deal_id="DEAL-01", action="BOOK_FREIGHT", autonomy_level="L0")
        await create_approval(req)
        svc._emit.assert_awaited_once()
        call_args = svc._emit.call_args[0]
        assert call_args[0].value == "approval.requested"

    @pytest.mark.asyncio
    async def test_notifies_sse(self):
        from services.hitl_approval import service as svc
        req = CreateApprovalRequest(deal_id="DEAL-01", action="ISSUE_PO", autonomy_level="L0")
        await create_approval(req)
        svc.notify.assert_awaited_once()
        event_type = svc.notify.call_args[0][0]
        assert event_type == "approval_created"

    @pytest.mark.asyncio
    async def test_idempotent(self):
        req = CreateApprovalRequest(
            deal_id="DEAL-01", action="ISSUE_PO", autonomy_level="L0",
            idempotency_key="idem-hitl-1",
        )
        r1 = await create_approval(req)
        r2 = await create_approval(req)
        assert r1.approval_id == r2.approval_id

    @pytest.mark.asyncio
    async def test_invalid_action_rejected(self):
        req = CreateApprovalRequest(deal_id="DEAL-01", action="UNKNOWN_ACTION")
        with pytest.raises(InvalidAction):
            await create_approval(req)


# ---------------------------------------------------------------------------
# Create approval — L1
# ---------------------------------------------------------------------------

class TestCreateApprovalL1:
    @pytest.mark.asyncio
    async def test_creates_pending_with_expiry(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        req = CreateApprovalRequest(
            deal_id="DEAL-01", action="ISSUE_PO",
            autonomy_level="L1", expires_at=future, default_on_timeout="APPROVE",
        )
        result = await create_approval(req)
        assert result.status == "PENDING"
        assert result.expires_at is not None

    @pytest.mark.asyncio
    async def test_default_expiry_when_not_supplied(self):
        req = CreateApprovalRequest(deal_id="DEAL-01", action="BOOK_FREIGHT", autonomy_level="L1")
        result = await create_approval(req)
        assert result.expires_at is not None
        assert result.expires_at > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Create approval — L2 / L3
# ---------------------------------------------------------------------------

class TestCreateApprovalL2L3:
    @pytest.mark.asyncio
    async def test_l2_auto_approves(self):
        req = CreateApprovalRequest(deal_id="DEAL-01", action="ISSUE_PO", autonomy_level="L2")
        result = await create_approval(req)
        assert result.status == "AUTO_APPROVED"
        assert result.decision == "APPROVE"

    @pytest.mark.asyncio
    async def test_l2_fires_callback(self):
        from services.hitl_approval import service as svc
        req = CreateApprovalRequest(
            deal_id="DEAL-01", action="BOOK_FREIGHT", autonomy_level="L2",
            callback_payload={"deal_id": "DEAL-01"},
        )
        await create_approval(req)
        # Give asyncio.create_task a chance to run
        await asyncio.sleep(0)
        svc.execute_callback.assert_awaited()

    @pytest.mark.asyncio
    async def test_l3_auto_approves_with_warning(self, caplog):
        import logging
        req = CreateApprovalRequest(deal_id="DEAL-01", action="FILE_CUSTOMS", autonomy_level="L3")
        with caplog.at_level(logging.WARNING, logger="services.hitl_approval.service"):
            result = await create_approval(req)
        assert result.status == "AUTO_APPROVED"
        assert any("L3" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Resolve approval
# ---------------------------------------------------------------------------

class TestResolveApproval:
    async def _pending(self, deal_id="DEAL-RESOLVE-01", action="ISSUE_PO") -> str:
        req = CreateApprovalRequest(deal_id=deal_id, action=action, autonomy_level="L0")
        result = await create_approval(req)
        return result.approval_id

    @pytest.mark.asyncio
    async def test_approve_sets_status(self):
        aid = await self._pending()
        result = await resolve_approval(
            aid, ResolveApprovalRequest(decision="APPROVE", resolved_by="operator@portless.io")
        )
        assert result.status == "APPROVED"
        assert result.decision == "APPROVE"
        assert result.resolved_by == "operator@portless.io"

    @pytest.mark.asyncio
    async def test_approve_fires_callback(self):
        from services.hitl_approval import service as svc
        svc.execute_callback.reset_mock()
        aid = await self._pending(action="BOOK_FREIGHT")
        await resolve_approval(
            aid, ResolveApprovalRequest(decision="APPROVE", resolved_by="op@portless.io")
        )
        await asyncio.sleep(0)
        svc.execute_callback.assert_awaited()

    @pytest.mark.asyncio
    async def test_approve_emits_approval_resolved(self):
        from services.hitl_approval import service as svc
        svc._emit.reset_mock()
        aid = await self._pending()
        await resolve_approval(aid, ResolveApprovalRequest(decision="APPROVE", resolved_by="op"))
        topics = [call[0][0].value for call in svc._emit.call_args_list]
        assert "approval.resolved" in topics

    @pytest.mark.asyncio
    async def test_reject_emits_deal_exception(self):
        from services.hitl_approval import service as svc
        svc._emit.reset_mock()
        aid = await self._pending()
        result = await resolve_approval(
            aid, ResolveApprovalRequest(decision="REJECT", resolved_by="op", note="Margin too low")
        )
        assert result.status == "REJECTED"
        topics = [call[0][0].value for call in svc._emit.call_args_list]
        assert "deal.exception" in topics
        assert "approval.resolved" in topics

    @pytest.mark.asyncio
    async def test_reject_does_not_fire_callback(self):
        from services.hitl_approval import service as svc
        svc.execute_callback.reset_mock()
        aid = await self._pending()
        await resolve_approval(aid, ResolveApprovalRequest(decision="REJECT", resolved_by="op"))
        await asyncio.sleep(0)
        svc.execute_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_found_raises(self):
        with pytest.raises(ApprovalNotFoundError):
            await resolve_approval("APPR-NOPE", ResolveApprovalRequest(decision="APPROVE", resolved_by="op"))

    @pytest.mark.asyncio
    async def test_already_resolved_raises(self):
        aid = await self._pending()
        await resolve_approval(aid, ResolveApprovalRequest(decision="APPROVE", resolved_by="op"))
        with pytest.raises(ApprovalAlreadyResolved):
            await resolve_approval(aid, ResolveApprovalRequest(decision="REJECT", resolved_by="op"))

    @pytest.mark.asyncio
    async def test_resolve_notifies_sse(self):
        from services.hitl_approval import service as svc
        svc.notify.reset_mock()
        aid = await self._pending()
        await resolve_approval(aid, ResolveApprovalRequest(decision="APPROVE", resolved_by="op"))
        # notify called once for create + once for resolve
        assert svc.notify.await_count >= 1
        last_event_type = svc.notify.call_args_list[-1][0][0]
        assert last_event_type == "approval_resolved"


# ---------------------------------------------------------------------------
# Timeout watcher
# ---------------------------------------------------------------------------

class TestTimeoutWatcher:
    @pytest.mark.asyncio
    async def test_auto_resolves_expired_l1_approve(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        req = CreateApprovalRequest(
            deal_id="DEAL-TW-01", action="ISSUE_PO",
            autonomy_level="L1", expires_at=past, default_on_timeout="APPROVE",
        )
        result = await create_approval(req)
        approval_id = result.approval_id

        # Directly invoke auto-resolve as the watcher would
        from services.hitl_approval.repository import get_repository
        record = await get_repository().get(approval_id)
        await _auto_resolve(record, "APPROVE", "test timeout")

        final = await get_approval(approval_id)
        assert final.status == "APPROVED"
        assert final.resolved_by == "system:timeout"

    @pytest.mark.asyncio
    async def test_auto_resolves_expired_l1_reject(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        req = CreateApprovalRequest(
            deal_id="DEAL-TW-02", action="BOOK_FREIGHT",
            autonomy_level="L1", expires_at=past, default_on_timeout="REJECT",
        )
        result = await create_approval(req)
        approval_id = result.approval_id

        from services.hitl_approval.repository import get_repository
        record = await get_repository().get(approval_id)
        await _auto_resolve(record, "REJECT", "test timeout")

        final = await get_approval(approval_id)
        assert final.status == "TIMED_OUT"

    @pytest.mark.asyncio
    async def test_watcher_cancels_cleanly(self):
        task = asyncio.create_task(run_timeout_watcher())
        await asyncio.sleep(0.05)
        task.cancel()
        # Watcher catches CancelledError internally and exits — task completes without exception
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        assert task.done()
        assert task.exception() is None


# ---------------------------------------------------------------------------
# List approvals
# ---------------------------------------------------------------------------

class TestListApprovals:
    @pytest.mark.asyncio
    async def test_filter_by_status(self):
        await create_approval(CreateApprovalRequest(deal_id="D1", action="ISSUE_PO", autonomy_level="L0"))
        await create_approval(CreateApprovalRequest(deal_id="D2", action="BOOK_FREIGHT", autonomy_level="L2"))

        pending = await list_approvals(status="PENDING")
        auto = await list_approvals(status="AUTO_APPROVED")
        assert all(a.status == "PENDING" for a in pending)
        assert all(a.status == "AUTO_APPROVED" for a in auto)

    @pytest.mark.asyncio
    async def test_filter_by_deal_id(self):
        await create_approval(CreateApprovalRequest(deal_id="DEAL-FILTER", action="ISSUE_PO", autonomy_level="L0"))
        await create_approval(CreateApprovalRequest(deal_id="DEAL-OTHER", action="BOOK_FREIGHT", autonomy_level="L0"))

        results = await list_approvals(deal_id="DEAL-FILTER")
        assert all(a.deal_id == "DEAL-FILTER" for a in results)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_empty_list(self):
        results = await list_approvals(status="PENDING", deal_id="DEAL-NEVER-EXISTS")
        assert results == []
