"""Outreach / RFQ e-mails that failed in transport are retried with back-off, then raise
(shared/orchestration/send_retry.py, outreach.retry_failed_sends, sourcing.retry_failed_email_sends)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import BuyerCandidate, DealRecord, DealStatus
from shared.orchestration import outreach, send_retry

NOW = datetime.now(UTC)


def test_schedule_due_and_exhausted(monkeypatch):
    monkeypatch.delenv("SEND_RETRY_MINUTES", raising=False)
    assert send_retry.schedule_minutes() == [5.0, 30.0, 120.0]
    failed = NOW - timedelta(minutes=6)
    assert send_retry.due(1, failed, NOW) and not send_retry.due(2, failed, NOW)
    assert send_retry.due(2, NOW - timedelta(minutes=31), NOW)
    assert not send_retry.exhausted(3) and send_retry.exhausted(4) and not send_retry.due(4, None, NOW)
    monkeypatch.setenv("SEND_RETRY_MINUTES", "1")
    assert send_retry.schedule_minutes() == [1.0] and send_retry.exhausted(2)
    monkeypatch.setenv("SEND_RETRY_MINUTES", "soon")
    assert send_retry.schedule_minutes() == [5.0, 30.0, 120.0]


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", r)
    return r


def _cand(**kw):
    base = dict(candidate_id="gulf.ae", legal_name="Gulf Foods", country="AE", email="buy@gulf.ae", status="pending",
                send_error="send failed: Connection lost", send_attempts=1, send_failed_at=NOW - timedelta(minutes=10))
    base.update(kw)
    return BuyerCandidate(**base)


def _deal(cands, status=DealStatus.MATCHING):
    return DealRecord(deal_id="P1", operator_id="op", org_id="o", hs_code="090422", market="AE", status=status,
                      created_at=NOW, updated_at=NOW, buyer_candidates=cands)


async def test_a_due_failed_introduction_gets_a_new_gate(repo, monkeypatch):
    gate = AsyncMock(return_value="APPR-2")
    monkeypatch.setattr(outreach, "_gate", gate)
    deal = _deal([_cand(), _cand(candidate_id="b.ae", email="x@b.ae", send_failed_at=NOW)])  # second not due yet
    assert await outreach.retry_failed_sends(deal, now=NOW) == {"retried": 1, "gave_up": 0}
    assert gate.await_args.args[1].candidate_id == "gulf.ae" and gate.await_args.kwargs == {"followup": False}
    gate.reset_mock()
    assert (await outreach.retry_failed_sends(deal, now=NOW, force=True))["retried"] == 2


async def test_a_logged_only_send_is_never_retried(repo, monkeypatch):
    gate = AsyncMock()
    monkeypatch.setattr(outreach, "_gate", gate)
    deal = _deal([_cand(send_error=outreach.NOT_DELIVERED)])
    assert (await outreach.retry_failed_sends(deal, now=NOW, force=True))["retried"] == 0
    gate.assert_not_awaited()


async def test_after_the_last_try_the_operator_is_told_once(repo, monkeypatch):
    monkeypatch.setattr(outreach, "_gate", AsyncMock())
    raised = AsyncMock()
    monkeypatch.setattr(outreach, "_send_failed_exception", raised)
    deal = _deal([_cand(send_attempts=4)])
    await repo.save(deal)
    assert (await outreach.retry_failed_sends(deal, now=NOW))["gave_up"] == 1
    raised.assert_awaited_once()
    stored = await repo.get("P1")
    assert stored.buyer_candidates[0].send_gave_up_at is not None
    assert (await outreach.retry_failed_sends(stored, now=NOW))["gave_up"] == 0


async def test_a_successful_send_clears_the_failure(repo, monkeypatch):
    await repo.save(_deal([_cand()]))
    monkeypatch.setattr("services.email_gateway.sender.send_email",
                        AsyncMock(return_value=SimpleNamespace(outcome="sent")))
    monkeypatch.setattr("agents.negotiation_agent.email_history.record_deal_email", AsyncMock())
    await outreach.send_outreach("P1", "gulf.ae", "buy@gulf.ae", "s", "b")
    c = (await repo.get("P1")).buyer_candidates[0]
    assert c.status == "contacted" and c.send_error is None and c.send_failed_at is None


async def test_a_transport_failure_records_when(repo, monkeypatch):
    await repo.save(_deal([_cand(send_error=None, send_attempts=0, send_failed_at=None)]))
    monkeypatch.setattr("services.email_gateway.sender.send_email", AsyncMock(side_effect=ConnectionError("Connection lost")))
    with pytest.raises(ConnectionError):
        await outreach.send_outreach("P1", "gulf.ae", "buy@gulf.ae", "s", "b")
    c = (await repo.get("P1")).buyer_candidates[0]
    assert c.send_attempts == 1 and c.send_failed_at is not None and c.send_error.startswith("send failed")


class TestRfq:
    @pytest.fixture
    def sourcing(self, monkeypatch):
        from services.sourcing import repository as srepo
        from services.sourcing import service as svc
        r = srepo.InMemorySourcingRepository() if hasattr(srepo, "InMemorySourcingRepository") else None
        if r is None:
            r = next(getattr(srepo, n)() for n in dir(srepo) if n.startswith("InMemory"))
        monkeypatch.setattr(svc, "get_repository", lambda: r)
        monkeypatch.setattr(svc, "_supplier_name", AsyncMock(return_value="Mill One"))
        monkeypatch.setattr(svc, "_emit_event", AsyncMock())
        monkeypatch.setattr("agents.negotiation_agent.email_history.record_deal_email", AsyncMock())
        return SimpleNamespace(repo=r, svc=svc)

    async def test_transport_failure_is_a_failed_dispatch_not_a_portal_success(self, sourcing, monkeypatch):
        from services.sourcing.schemas import RfqRequest, SupplierRef
        monkeypatch.setattr("services.email_gateway.sender.send_email", AsyncMock(side_effect=OSError("Connection lost")))
        res = await sourcing.svc.dispatch_rfq(RfqRequest(
            deal_id="P1", hs_code="090422", quantity_mt=20,
            suppliers=[SupplierRef(supplier_id="S1", email="sales@mill.in", source="other")]))
        (d,) = res.dispatch_results
        assert d.channel == "email" and d.success is False and d.error.startswith("send failed") and d.failed_at
        assert res.status == "FAILED"

    async def test_retry_delivers_then_gives_up_after_the_schedule(self, sourcing, monkeypatch):
        from services.sourcing.schemas import RfqRequest, SupplierRef
        send = AsyncMock(side_effect=OSError("Connection lost"))
        monkeypatch.setattr("services.email_gateway.sender.send_email", send)
        res = await sourcing.svc.dispatch_rfq(RfqRequest(
            deal_id="P1", hs_code="090422", quantity_mt=20,
            suppliers=[SupplierRef(supplier_id="S1", email="sales@mill.in", source="other"),
                       SupplierRef(supplier_id="S2", email="ex@mill2.in", source="other")]))
        later = NOW + timedelta(minutes=6)
        send.side_effect = [SimpleNamespace(outcome="sent", message_id="<m1>"), OSError("still down")]
        counts = await sourcing.svc.retry_failed_email_sends("P1", now=later)
        assert counts == {"retried": 2, "delivered": 1, "gave_up": 0}
        rfq = (await sourcing.repo.list_rfqs("P1"))[0]
        assert rfq.status == "PARTIAL" and [d.success for d in rfq.dispatch_results] == [True, False]
        assert rfq.dispatch_results[1].attempts == 2
        send.side_effect = OSError("down")
        counts = await sourcing.svc.retry_failed_email_sends("P1", now=later, force=True)
        counts = await sourcing.svc.retry_failed_email_sends("P1", now=later, force=True)
        assert counts["gave_up"] == 1
        stages = [c.args[2]["stage"] for c in sourcing.svc._emit_event.await_args_list if len(c.args) > 2 and isinstance(c.args[2], dict) and "stage" in c.args[2]]
        assert stages == ["RFQ:send_failed"]
        assert (await sourcing.svc.retry_failed_email_sends("P1", now=later, force=True))["retried"] == 0
