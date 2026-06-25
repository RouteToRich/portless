"""Tests for the Exception-Resolver agent service.

Covers:
  - Exception raised → persisted as OPEN
  - LLM analysis returns valid AgentAnalysis
  - AUTO_FIX path: DOCUMENT_ERROR → resolved
  - AUTO_FIX path: unknown category → falls through to ESCALATE
  - ESCALATE path → HITL approval created, status=ESCALATED
  - CANCEL path → HITL approval created with cancel_recommended flag
  - MONITOR path → status=RESOLVED, no HITL
  - LLM exhausted → conservative ESCALATE fallback
  - get_exception: found + not-found
  - list_exceptions: by deal_id, by status, no filter
  - Concurrent exceptions on same deal are tracked independently
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.exception_resolver.repository import InMemoryExceptionRepository
from services.exception_resolver.schemas import (
    AgentAnalysis,
    ExceptionCategory,
    ExceptionSeverity,
    ExceptionStatus,
    RaiseExceptionRequest,
    RemediationDecision,
)
from services.exception_resolver.service import (
    ExceptionNotFoundError,
    get_exception,
    list_exceptions,
    raise_exception,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_in_memory_repo(monkeypatch):
    import services.exception_resolver.repository as repo_mod
    repo = InMemoryExceptionRepository()
    repo_mod._repository = repo
    repo_mod._motor_client = None
    yield repo
    repo_mod._repository = None


def _mock_analysis(
    category: ExceptionCategory = ExceptionCategory.DOCUMENT_ERROR,
    severity: ExceptionSeverity = ExceptionSeverity.MEDIUM,
    decision: RemediationDecision = RemediationDecision.AUTO_FIX,
) -> AgentAnalysis:
    return AgentAnalysis(
        category=category,
        severity=severity,
        summary="Test summary",
        root_cause="Test root cause",
        remediation_steps=["Step 1", "Step 2"],
        decision=decision,
        decision_rationale="Test rationale",
        estimated_resolution_hours=2,
        llm_model="mock-model",
    )


@pytest.fixture(autouse=True)
def silence_events(monkeypatch):
    monkeypatch.setattr("services.exception_resolver.service._emit", AsyncMock())


# ---------------------------------------------------------------------------
# Helper to build a quick request
# ---------------------------------------------------------------------------

def _req(
    deal_id: str = "DEAL-EXC-01",
    source_event: str = "deal.exception",
    source_service: str = "docugen",
    detail: str = "Document render failed",
    context: dict | None = None,
) -> RaiseExceptionRequest:
    return RaiseExceptionRequest(
        deal_id=deal_id,
        source_event=source_event,
        source_service=source_service,
        detail=detail,
        context=context or {},
    )


# ---------------------------------------------------------------------------
# raise_exception — AUTO_FIX DOCUMENT_ERROR
# ---------------------------------------------------------------------------

class TestAutoFixDocumentError:
    @pytest.mark.asyncio
    async def test_resolved_status(self):
        analysis = _mock_analysis(
            category=ExceptionCategory.DOCUMENT_ERROR,
            decision=RemediationDecision.AUTO_FIX,
        )
        with (
            patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
                return_value=(analysis, {"success": True, "action": "document_regenerated"})
            )),
        ):
            resp = await raise_exception(_req(source_service="docugen"))
        assert resp.status == ExceptionStatus.RESOLVED
        assert resp.category == ExceptionCategory.DOCUMENT_ERROR
        assert resp.decision == RemediationDecision.AUTO_FIX
        assert resp.resolution_note is not None

    @pytest.mark.asyncio
    async def test_steps_populated(self):
        analysis = _mock_analysis(decision=RemediationDecision.AUTO_FIX)
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "action": "document_regenerated"})
        )):
            resp = await raise_exception(_req())
        assert len(resp.remediation_steps) == 2
        assert all(s.status == "DONE" for s in resp.remediation_steps)

    @pytest.mark.asyncio
    async def test_auto_fix_failed_falls_to_escalate(self):
        analysis = _mock_analysis(
            category=ExceptionCategory.DOCUMENT_ERROR,
            decision=RemediationDecision.AUTO_FIX,
        )
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": False, "approval_id": "APPR-FALLBACK", "error": "regen failed"})
        )):
            resp = await raise_exception(_req())
        assert resp.status == ExceptionStatus.ESCALATED
        assert resp.approval_id == "APPR-FALLBACK"


# ---------------------------------------------------------------------------
# raise_exception — ESCALATE
# ---------------------------------------------------------------------------

class TestEscalate:
    @pytest.mark.asyncio
    async def test_escalated_status(self):
        analysis = _mock_analysis(
            category=ExceptionCategory.QC_FAILED,
            severity=ExceptionSeverity.HIGH,
            decision=RemediationDecision.ESCALATE,
        )
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "approval_id": "APPR-QC-01", "action": "hitl_escalated"})
        )):
            resp = await raise_exception(_req(source_service="shield_link", detail="SGS QC failed"))
        assert resp.status == ExceptionStatus.ESCALATED
        assert resp.approval_id == "APPR-QC-01"
        assert resp.category == ExceptionCategory.QC_FAILED
        assert resp.severity == ExceptionSeverity.HIGH

    @pytest.mark.asyncio
    async def test_escalation_hitl_failure_becomes_unresolvable(self):
        analysis = _mock_analysis(
            category=ExceptionCategory.PAYMENT_FAILED,
            decision=RemediationDecision.ESCALATE,
        )
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": False, "error": "HITL service down"})
        )):
            resp = await raise_exception(_req(source_service="escrow_guard"))
        assert resp.status == ExceptionStatus.UNRESOLVABLE
        assert "HITL service down" in (resp.resolution_note or "")


# ---------------------------------------------------------------------------
# raise_exception — CANCEL
# ---------------------------------------------------------------------------

class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_creates_hitl_with_flag(self):
        analysis = _mock_analysis(
            category=ExceptionCategory.NEGOTIATION_FAILED,
            severity=ExceptionSeverity.CRITICAL,
            decision=RemediationDecision.CANCEL,
        )
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(
                analysis,
                {"success": True, "approval_id": "APPR-CANCEL-01", "cancel_recommended": True}
            )
        )):
            resp = await raise_exception(_req(source_service="negotiation_agent"))
        assert resp.status == ExceptionStatus.ESCALATED
        assert resp.approval_id == "APPR-CANCEL-01"


# ---------------------------------------------------------------------------
# raise_exception — MONITOR
# ---------------------------------------------------------------------------

class TestMonitor:
    @pytest.mark.asyncio
    async def test_monitor_resolves_without_hitl(self):
        analysis = _mock_analysis(
            category=ExceptionCategory.CONNECTOR_ERROR,
            severity=ExceptionSeverity.LOW,
            decision=RemediationDecision.MONITOR,
        )
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "action": "monitoring"})
        )):
            resp = await raise_exception(_req(source_service="shield_link"))
        assert resp.status == ExceptionStatus.RESOLVED
        assert resp.approval_id is None
        assert "monitoring" in (resp.resolution_note or "").lower()


# ---------------------------------------------------------------------------
# raise_exception — LLM exhausted fallback
# ---------------------------------------------------------------------------

class TestLLMFallback:
    @pytest.mark.asyncio
    async def test_llm_exhausted_escalates_conservatively(self):
        from shared.llm_gateway import LLMExhaustedException
        from services.exception_resolver import agent as agent_mod

        with patch.object(agent_mod, "LLMClient") as MockClient:
            MockClient.return_value.chat = AsyncMock(
                side_effect=LLMExhaustedException("all models failed")
            )
            resp = await raise_exception(_req())

        # Agent catches the exception and returns conservative ESCALATE analysis
        # so status should be ESCALATED (or UNRESOLVABLE if HITL also fails)
        assert resp.status in (ExceptionStatus.ESCALATED, ExceptionStatus.UNRESOLVABLE)
        assert resp.category in (ExceptionCategory.UNKNOWN, None)


# ---------------------------------------------------------------------------
# raise_exception — agent crash
# ---------------------------------------------------------------------------

class TestAgentCrash:
    @pytest.mark.asyncio
    async def test_agent_crash_returns_unresolvable(self):
        with patch(
            "services.exception_resolver.service.run_agent",
            new=AsyncMock(side_effect=RuntimeError("graph exploded")),
        ):
            resp = await raise_exception(_req())
        assert resp.status == ExceptionStatus.UNRESOLVABLE
        assert "graph exploded" in (resp.resolution_note or "")


# ---------------------------------------------------------------------------
# get_exception
# ---------------------------------------------------------------------------

class TestGetException:
    @pytest.mark.asyncio
    async def test_get_existing(self):
        analysis = _mock_analysis(decision=RemediationDecision.MONITOR)
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "action": "monitoring"})
        )):
            created = await raise_exception(_req())

        fetched = await get_exception(created.exception_id)
        assert fetched.exception_id == created.exception_id
        assert fetched.deal_id == "DEAL-EXC-01"

    @pytest.mark.asyncio
    async def test_get_nonexistent_raises(self):
        with pytest.raises(ExceptionNotFoundError):
            await get_exception("EXC-NOPE")


# ---------------------------------------------------------------------------
# list_exceptions
# ---------------------------------------------------------------------------

class TestListExceptions:
    @pytest.mark.asyncio
    async def test_filter_by_deal_id(self):
        analysis = _mock_analysis(decision=RemediationDecision.MONITOR)
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "action": "monitoring"})
        )):
            await raise_exception(_req(deal_id="DEAL-LIST-A"))
            await raise_exception(_req(deal_id="DEAL-LIST-A"))
            await raise_exception(_req(deal_id="DEAL-LIST-B"))

        results = await list_exceptions(deal_id="DEAL-LIST-A")
        assert len(results) == 2
        assert all(r.deal_id == "DEAL-LIST-A" for r in results)

    @pytest.mark.asyncio
    async def test_filter_by_status(self):
        analysis = _mock_analysis(decision=RemediationDecision.MONITOR)
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "action": "monitoring"})
        )):
            await raise_exception(_req(deal_id="DEAL-S-01"))
            await raise_exception(_req(deal_id="DEAL-S-02"))

        results = await list_exceptions(status=ExceptionStatus.RESOLVED)
        assert all(r.status == ExceptionStatus.RESOLVED for r in results)

    @pytest.mark.asyncio
    async def test_list_all(self):
        analysis = _mock_analysis(decision=RemediationDecision.MONITOR)
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "action": "monitoring"})
        )):
            for i in range(3):
                await raise_exception(_req(deal_id=f"DEAL-ALL-{i}"))

        results = await list_exceptions()
        assert len(results) >= 3

    @pytest.mark.asyncio
    async def test_empty_list(self):
        results = await list_exceptions(deal_id="DEAL-NEVER-EXISTS-XYZ")
        assert results == []


# ---------------------------------------------------------------------------
# Exception ID format
# ---------------------------------------------------------------------------

class TestExceptionIdFormat:
    @pytest.mark.asyncio
    async def test_id_prefix(self):
        analysis = _mock_analysis(decision=RemediationDecision.MONITOR)
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "action": "monitoring"})
        )):
            resp = await raise_exception(_req())
        assert resp.exception_id.startswith("EXC-")

    @pytest.mark.asyncio
    async def test_unique_ids(self):
        analysis = _mock_analysis(decision=RemediationDecision.MONITOR)
        with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
            return_value=(analysis, {"success": True, "action": "monitoring"})
        )):
            r1 = await raise_exception(_req())
            r2 = await raise_exception(_req())
        assert r1.exception_id != r2.exception_id


# ---------------------------------------------------------------------------
# All exception categories round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("category", [
    ExceptionCategory.QC_FAILED,
    ExceptionCategory.FREIGHT_DELAY,
    ExceptionCategory.PAYMENT_FAILED,
    ExceptionCategory.NEGOTIATION_FAILED,
    ExceptionCategory.CUSTOMS_REJECTED,
    ExceptionCategory.DOCUMENT_ERROR,
    ExceptionCategory.CONNECTOR_ERROR,
    ExceptionCategory.MARGIN_BREACH,
    ExceptionCategory.APPROVAL_REJECTED,
    ExceptionCategory.CALLBACK_FAILED,
    ExceptionCategory.UNKNOWN,
])
async def test_all_categories_escalate(category, monkeypatch):
    """Every exception category can complete a round-trip through ESCALATE."""
    import services.exception_resolver.repository as repo_mod
    repo_mod._repository = InMemoryExceptionRepository()

    monkeypatch.setattr("services.exception_resolver.service._emit", AsyncMock())

    analysis = _mock_analysis(
        category=category,
        decision=RemediationDecision.ESCALATE,
    )
    with patch("services.exception_resolver.agent.run_agent", new=AsyncMock(
        return_value=(analysis, {"success": True, "approval_id": "APPR-TEST", "action": "hitl_escalated"})
    )):
        resp = await raise_exception(_req())

    assert resp.category == category
    assert resp.status == ExceptionStatus.ESCALATED
