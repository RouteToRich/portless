"""Continuation legs that had no test: negotiation start failures, the customs gate's document
listing, DUPRO / PEO results and the startup sweep (todo: product-code coverage back to 95 %)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import continuations as c


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(c, "_enabled", lambda: True)


def _deal(status="NEGOTIATING", **kw):
    return SimpleNamespace(deal_id="DEAL-C1", status=SimpleNamespace(value=status), hs_code="10063020", market="AE",
                           parties=[], opportunity_id=None, buyer_candidates=[], **kw)


def _event(topic=EventTopic.NEGOTIATION_STARTED, **payload):
    return DomainEvent(topic=topic, deal_id="DEAL-C1", payload=payload)


async def _negotiation_setup(monkeypatch, *, seed, existing=None):
    monkeypatch.setattr(c, "_get_deal_in_status", AsyncMock(return_value=_deal()))
    monkeypatch.setattr(c, "_auto_flow_owns", lambda d: False)
    monkeypatch.setattr(c, "_negotiation_seed", lambda d: seed)
    import agents.negotiation_agent.repository as neg
    monkeypatch.setattr(neg, "get_repository", lambda: SimpleNamespace(get_session_by_deal=AsyncMock(return_value=existing)))
    from shared.orchestration import outreach
    monkeypatch.setattr(outreach, "waiting_for_buyer", lambda d: False)
    emitted = AsyncMock()
    monkeypatch.setattr(c, "_emit_exception", emitted)
    return emitted


async def test_negotiation_waits_without_prices(monkeypatch):
    emitted = await _negotiation_setup(monkeypatch, seed=None)
    import agents.negotiation_agent.service as ns
    start = AsyncMock()
    monkeypatch.setattr(ns, "start_negotiation", start)
    await c.on_negotiation_started(_event())
    start.assert_not_awaited() and emitted.assert_not_awaited()


async def test_negotiation_start_failure_raises_an_exception(monkeypatch):
    emitted = await _negotiation_setup(monkeypatch, seed=(1000.0, 800.0, "AE", "SUP-1"))
    monkeypatch.setattr(c, "_winning_match", lambda d: None)
    monkeypatch.setattr(c, "_party_email", lambda d, role: None)
    monkeypatch.setattr(c, "_deal_quantity", lambda d: 25.0)
    import agents.negotiation_agent.service as ns
    monkeypatch.setattr(ns, "start_negotiation", AsyncMock(side_effect=RuntimeError("LLM down")))
    await c.on_negotiation_started(_event())
    assert emitted.await_args.args[1] == "NEGOTIATING→NEGOTIATION_STARTED"


async def test_an_open_session_is_left_alone(monkeypatch):
    await _negotiation_setup(monkeypatch, seed=(1000.0, 800.0, "AE", "SUP-1"),
                             existing=SimpleNamespace(status="ROUND", negotiation_id="NEG-1"))
    import agents.negotiation_agent.service as ns
    start = AsyncMock()
    monkeypatch.setattr(ns, "start_negotiation", start)
    await c.on_negotiation_started(_event())
    start.assert_not_awaited()


async def test_customs_gate_still_opens_when_documents_cannot_be_listed(monkeypatch):
    import services.docugen.service as dg
    monkeypatch.setattr(dg, "list_deal_documents", AsyncMock(side_effect=RuntimeError("drive down")))
    gate = AsyncMock(return_value="APPR-1")
    monkeypatch.setattr(c, "_create_gate", gate)
    await c._customs_gate(_deal("IN_TRANSIT"), reason="freight booked")
    args, kwargs = gate.await_args.args, gate.await_args.kwargs
    assert args == ("DEAL-C1", "FILE_CUSTOMS") and kwargs["callback_payload"]["documents"] == []
    assert "0 document(s)" in kwargs["summary"]


async def _phase_setup(monkeypatch, status):
    import services.deals.service as ds
    monkeypatch.setattr(ds, "get_deal", AsyncMock(return_value=_deal(status)))
    from shared.orchestration import notifications, qc_status
    monkeypatch.setattr(qc_status, "sync", AsyncMock())
    monkeypatch.setattr(notifications, "notify_inspection_phase", AsyncMock())
    published, emitted, customs = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(c, "_publish_exception", published)
    monkeypatch.setattr(c, "_emit_exception", emitted)
    monkeypatch.setattr(c, "_customs_gate", customs)
    return published, emitted, customs


async def test_peo_pass_in_transit_opens_the_customs_gate(monkeypatch):
    published, _emitted, customs = await _phase_setup(monkeypatch, "IN_TRANSIT")
    await c.on_qc_phase_result(_event(EventTopic.QC_PHASE_RESULT, inspection_type="PEO", result="PASS", provider="qima",
                                      provider_order_number="Q-9"))
    assert "Pre-customs inspection passed (qima Q-9)" in customs.await_args.kwargs["reason"]
    published.assert_not_awaited()


async def test_peo_customs_gate_failure_is_an_exception(monkeypatch):
    _published, emitted, customs = await _phase_setup(monkeypatch, "IN_TRANSIT")
    customs.side_effect = RuntimeError("gate store down")
    await c.on_qc_phase_result(_event(EventTopic.QC_PHASE_RESULT, inspection_type="PEO", result="PASS"))
    assert emitted.await_args.args[1] == "PEO_PASSED→FILE_CUSTOMS_GATE"


@pytest.mark.parametrize("phase,action", [("DUPRO", "supplier_rework_before_psi"), ("PEO", "reinspect_before_customs")])
async def test_a_failed_phase_raises_an_exception_with_the_next_step(monkeypatch, phase, action):
    published, _emitted, customs = await _phase_setup(monkeypatch, "SUPPLIER_DISPATCH_READY")
    await c.on_qc_phase_result(_event(EventTopic.QC_PHASE_RESULT, inspection_type=phase, result="FAIL", notes="moisture 15%"))
    args = published.await_args.args
    assert args[1] == f"{phase}_FAILED" and "moisture 15%" in args[2] and args[3]["action_required"] == action
    customs.assert_not_awaited()


async def test_startup_sweep_resumes_each_parked_deal(monkeypatch):
    import services.deals.repository as dr
    deals = [SimpleNamespace(deal_id="D1"), SimpleNamespace(deal_id="D2")]
    monkeypatch.setattr(dr, "get_repository", lambda: SimpleNamespace(list_active=AsyncMock(return_value=deals)))
    seen = []

    async def started(event):
        seen.append(event.deal_id)
        if event.deal_id == "D2":
            raise RuntimeError("one bad deal never stops the sweep")
    monkeypatch.setattr(c, "on_negotiation_started", started)
    await c.resume_parked_negotiations()
    assert seen == ["D1", "D2"]
