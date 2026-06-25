"""Business logic for the Exception-Resolver service.

Every deal.exception event or direct API call routes through here.
The LangGraph agent (agent.py) handles classification and remediation;
this layer handles persistence, event emission, and lifecycle management.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from shared.bus.publisher import get_publisher
from shared.domain.events import DomainEvent, EventTopic
from .agent import run_agent
from .repository import get_repository
from .schemas import (
    ExceptionRecord,
    ExceptionResponse,
    ExceptionStatus,
    RaiseExceptionRequest,
    RemediationDecision,
    RemediationStep,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ExceptionNotFoundError(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _emit(topic: EventTopic, deal_id: str, payload: dict) -> None:
    try:
        await get_publisher().publish(DomainEvent(topic=topic, deal_id=deal_id, payload=payload))
    except Exception as exc:
        logger.warning("Event publish failed (topic=%s): %s", topic.value, exc)


def _build_steps(remediation_steps: list[str]) -> list[RemediationStep]:
    return [
        RemediationStep(step=i + 1, description=desc)
        for i, desc in enumerate(remediation_steps)
    ]


# ---------------------------------------------------------------------------
# Core service operations
# ---------------------------------------------------------------------------

async def raise_exception(payload: RaiseExceptionRequest) -> ExceptionResponse:
    """Classify a deal.exception event and execute the remediation plan.

    Flow:
      1. Persist OPEN record (crash-safe audit trace).
      2. Transition to ANALYZING, run LangGraph agent.
      3. Persist analysis + steps, transition to REMEDIATING.
      4. Execute decision (AUTO_FIX / ESCALATE / CANCEL / MONITOR).
      5. Update final status; emit domain events.
    """
    repo = get_repository()

    # 1. Create audit record immediately
    record = ExceptionRecord(
        deal_id=payload.deal_id,
        source_event=payload.source_event,
        source_service=payload.source_service,
        raw_detail=payload.detail,
        context=payload.context,
        status=ExceptionStatus.OPEN,
    )
    await repo.save(record)
    logger.info("ExceptionResolver: created %s for deal %s", record.exception_id, payload.deal_id)

    # Emit so other services know an exception was raised
    await _emit(
        EventTopic.DEAL_EXCEPTION,
        payload.deal_id,
        {
            "exception_id": record.exception_id,
            "source_event": payload.source_event,
            "source_service": payload.source_service,
            "status": ExceptionStatus.OPEN.value,
        },
    )

    # 2. Transition to ANALYZING
    record.status = ExceptionStatus.ANALYZING
    record.updated_at = datetime.now(timezone.utc)
    await repo.save(record)

    # 3. Run agent
    try:
        analysis, exec_result = await run_agent(record)
    except Exception as exc:
        logger.error("ExceptionAgent crashed for %s: %s", record.exception_id, exc)
        record.status = ExceptionStatus.UNRESOLVABLE
        record.resolution_note = f"Agent crashed: {exc}"
        record.updated_at = datetime.now(timezone.utc)
        await repo.save(record)
        return ExceptionResponse.from_record(record)

    # 4. Persist analysis
    if analysis:
        record.analysis = analysis
        record.category = analysis.category
        record.severity = analysis.severity
        record.steps = _build_steps(analysis.remediation_steps)

    record.status = ExceptionStatus.REMEDIATING
    record.updated_at = datetime.now(timezone.utc)
    await repo.save(record)

    # 5. Mark step outcomes based on execution result
    _apply_execution_result(record, analysis, exec_result)

    record.updated_at = datetime.now(timezone.utc)
    await repo.save(record)

    # 6. Emit final domain event
    await _emit(
        EventTopic.DEAL_EXCEPTION,
        payload.deal_id,
        {
            "exception_id": record.exception_id,
            "status": record.status.value,
            "category": record.category.value if record.category else None,
            "severity": record.severity.value if record.severity else None,
            "decision": analysis.decision.value if analysis else None,
            "approval_id": record.approval_id,
        },
    )

    return ExceptionResponse.from_record(record)


def _apply_execution_result(
    record: ExceptionRecord,
    analysis,
    exec_result: dict,
) -> None:
    """Update record status, steps, and approval_id from the agent execution result."""
    success = exec_result.get("success", False)
    action = exec_result.get("action", "")

    if analysis is None:
        record.status = ExceptionStatus.UNRESOLVABLE
        record.resolution_note = "Agent produced no analysis."
        return

    decision = analysis.decision

    if decision == RemediationDecision.MONITOR:
        record.status = ExceptionStatus.RESOLVED
        record.resolution_note = "Logged for monitoring — no immediate action required."
        record.resolved_at = datetime.now(timezone.utc)
        _mark_steps_done(record)
        return

    if decision == RemediationDecision.AUTO_FIX and success:
        record.status = ExceptionStatus.RESOLVED
        record.resolution_note = f"Auto-fixed: {action}"
        record.resolved_at = datetime.now(timezone.utc)
        record.auto_fix_attempts += 1
        _mark_steps_done(record)
        return

    if decision in (RemediationDecision.ESCALATE, RemediationDecision.CANCEL) or (
        decision == RemediationDecision.AUTO_FIX and not success
    ):
        approval_id = exec_result.get("approval_id")
        if approval_id:
            record.approval_id = approval_id
            record.status = ExceptionStatus.ESCALATED
            record.resolution_note = f"Escalated to HITL approval {approval_id}."
        else:
            # Escalation itself failed
            record.status = ExceptionStatus.UNRESOLVABLE
            record.resolution_note = (
                f"Escalation failed: {exec_result.get('error', 'unknown error')}. "
                "Manual intervention required."
            )
        if decision == RemediationDecision.AUTO_FIX:
            record.auto_fix_attempts += 1
        return

    record.status = ExceptionStatus.UNRESOLVABLE
    record.resolution_note = "No matching resolution path — manual intervention required."


def _mark_steps_done(record: ExceptionRecord) -> None:
    now = datetime.now(timezone.utc)
    for step in record.steps:
        step.status = "DONE"
        step.executed_at = now
        step.result = "Completed as part of automated resolution."


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

async def get_exception(exception_id: str) -> ExceptionResponse:
    record = await get_repository().get(exception_id)
    if record is None:
        raise ExceptionNotFoundError(exception_id)
    return ExceptionResponse.from_record(record)


async def list_exceptions(
    deal_id: str | None = None,
    status: ExceptionStatus | None = None,
    limit: int = 100,
) -> list[ExceptionResponse]:
    repo = get_repository()

    if deal_id:
        records = await repo.list_by_deal(deal_id)
    elif status:
        records = await repo.list_by_status(status)
    else:
        records = await repo.list_all(limit=limit)

    return [ExceptionResponse.from_record(r) for r in records]
