"""Idempotency audit of money endpoints and webhooks (todo: replay the same webhook 2×, 10×
concurrently; two operators approve / release at the same moment → exactly one counts)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import services.hitl_approval.repository as hitl_repo_mod
from services.hitl_approval import service as hitl
from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from services.hitl_approval.schemas import CreateApprovalRequest, ResolveApprovalRequest


class SlowRepo(InMemoryHitlApprovalRepository):
    """Every read yields, like a network round-trip — so concurrent requests interleave."""

    async def get(self, approval_id):
        await asyncio.sleep(0.005)
        return await super().get(approval_id)


@pytest.fixture()
def callbacks(monkeypatch):
    hitl_repo_mod._repository = SlowRepo()
    calls: list[str] = []

    async def cb(action, deal_id, payload, approval_id):
        calls.append(approval_id)

    monkeypatch.setattr(hitl, "execute_callback", cb)
    yield calls
    hitl_repo_mod._repository = None


async def _gate(action="RELEASE_ESCROW_TRANCHE", required=1):
    g = await hitl.create_approval(CreateApprovalRequest(deal_id="D1", action=action, summary="t", autonomy_level="L0",
                                                         financial_impact_usd=1000.0, callback_payload={}))
    if required > 1:
        rec = await hitl_repo_mod._repository.get(g.approval_id)
        await hitl_repo_mod._repository.save(rec.model_copy(update={"required_approvals": required}))
    return g


async def _decide(gid, who, decision="APPROVE"):
    try:
        await hitl.resolve_approval(gid, ResolveApprovalRequest(decision=decision, resolved_by=who))
        return "ok"
    except hitl.ApprovalAlreadyResolved:
        return "conflict"


async def test_ten_operators_approve_at_once_one_callback(callbacks):
    g = await _gate()
    out = await asyncio.gather(*(_decide(g.approval_id, f"staff{i}@p") for i in range(10)))
    await asyncio.sleep(0)
    assert out.count("ok") == 1 and out.count("conflict") == 9
    assert callbacks == [g.approval_id]
    assert (await hitl.get_approval(g.approval_id)).status == "APPROVED"


async def test_approve_and_reject_race_one_decision(callbacks):
    g = await _gate()
    out = await asyncio.gather(_decide(g.approval_id, "a@p"), _decide(g.approval_id, "b@p", "REJECT"))
    assert sorted(out) == ["conflict", "ok"]
    final = await hitl.get_approval(g.approval_id)
    assert final.status in ("APPROVED", "REJECTED")
    assert len(callbacks) == (1 if final.status == "APPROVED" else 0)


async def test_four_eyes_signatures_never_lost_or_doubled(callbacks):
    g = await _gate(required=2)
    first = await asyncio.gather(_decide(g.approval_id, "cfo@p"), _decide(g.approval_id, "founder@p"))
    await asyncio.sleep(0)
    rec = await hitl.get_approval(g.approval_id)
    if first.count("ok") == 2:  # the second read saw the first signature: both count
        assert rec.status == "APPROVED" and {a.approved_by for a in rec.approvals} == {"cfo@p", "founder@p"}
        assert len(callbacks) == 1
        return
    # both read "no signature yet": exactly one signature is kept, the other approver retries
    assert first.count("ok") == 1 and rec.status == "PENDING" and len(rec.approvals) == 1 and callbacks == []
    other = "founder@p" if rec.approvals[0].approved_by == "cfo@p" else "cfo@p"
    assert await _decide(g.approval_id, other) == "ok"
    assert (await hitl.get_approval(g.approval_id)).status == "APPROVED" and len(callbacks) == 1


async def test_mongo_conditional_save():
    from mongomock_motor import AsyncMongoMockClient

    from services.hitl_approval.repository import MongoHitlApprovalRepository
    repo = MongoHitlApprovalRepository(AsyncMongoMockClient()["t"]["approvals"])
    hitl_repo_mod._repository = repo
    try:
        g = await _gate()
        rec = await repo.get(g.approval_id)
        done = rec.model_copy(update={"status": "APPROVED"})
        assert await repo.save_if_pending(done, 0) is True
        assert await repo.save_if_pending(done, 0) is False  # no longer PENDING
    finally:
        hitl_repo_mod._repository = None


# ── escrow webhooks and releases ────────────────────────────────────────────

@pytest.fixture()
def escrow_repo():
    import services.escrow_guard.repository as repo_mod
    from services.escrow_guard.repository import InMemoryEscrowGuardRepository
    repo_mod._repository = InMemoryEscrowGuardRepository()
    yield repo_mod._repository
    repo_mod._repository = None


async def test_funded_webhook_ten_times_concurrently_books_once(escrow_repo):
    from services.escrow_guard.schemas import EscrowFundedWebhookPayload
    from services.escrow_guard.service import handle_escrow_funded_webhook
    from tests.test_escrow_money_chain import _open
    emitted = []

    async def emit(topic, deal_id, payload):
        emitted.append(topic)

    with patch("services.escrow_guard.service._emit", new=emit):
        esc = await _open()
        trans = AsyncMock()
        with patch("services.escrow_guard.service._transition_deal", new=trans):
            payload = EscrowFundedWebhookPayload(ext_ref=esc.ext_ref, status="FUNDED", amount=esc.amount, event_id="ev-1")
            outs = await asyncio.gather(*(handle_escrow_funded_webhook(payload, {}, b"{}") for _ in range(10)))
    from shared.domain.events import EventTopic
    assert emitted.count(EventTopic.ESCROW_FUNDED) == 1
    assert trans.await_count == 1
    assert sum(1 for o in outs if o.get("replayed")) == 9
    hold = await escrow_repo.get_hold(esc.escrow_id)
    assert hold.funded_amount == esc.amount and hold.processed_events == ["ev-1"]


async def test_same_tranche_released_twice_at_once_moves_money_once(escrow_repo):
    from services.escrow_guard.schemas import ReleaseTrancheRequest
    from services.escrow_guard.service import release_tranche
    from tests.test_escrow_money_chain import _fund, _open

    async def emit(*a):
        return None

    with patch("services.escrow_guard.service._emit", new=emit):
        esc = await _open()
        await _fund(esc, event_id="ev-f")
        with patch("services.escrow_guard.service._fetch_deal",
                   new=AsyncMock(return_value=type("D", (), {"deal_id": "DEAL-M1", "status": type("S", (), {"value": "PO_ISSUED"})()})())):
            outs = await asyncio.gather(*(release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id="T1")) for _ in range(5)),
                                        return_exceptions=True)
    hold = await escrow_repo.get_hold(esc.escrow_id)
    t1 = next(t for t in hold.tranches if t.tranche_id == "T1")
    assert t1.status in ("RELEASED", "SUBMITTED")
    assert hold.released_total == pytest.approx(esc.amount * t1.pct) or t1.status == "SUBMITTED"
    assert sum(1 for o in outs if not isinstance(o, Exception)) >= 1
