"""HITL Approval service — business logic.

Approval creation
-----------------
  create_approval()
    Validates the action; applies autonomy-level logic:
      L0 → persist PENDING, notify UI, wait for human
      L1 → persist PENDING, notify UI, schedule timeout
      L2 → auto-approve immediately, notify UI (non-blocking)
      L3 → caller should never reach here; auto-approve and log warning
    Emits approval.requested (L0/L1) or skips (L2/L3 auto-approval notified
    through a different channel so the event stream stays clean).

Approval resolution
-------------------
  resolve_approval()
    Human (or timeout watcher) resolves APPROVE or REJECT.
    APPROVE → execute saga callback via callbacks.execute_callback()
            → emit approval.resolved
    REJECT  → emit deal.exception + approval.resolved

Timeout watcher
---------------
  run_timeout_watcher()
    Background coroutine; polls repository every N seconds for PENDING
    approvals past their expires_at; auto-resolves with default_on_timeout.
    Called from the FastAPI lifespan; cancelled on shutdown.

Exceptions
----------
  ApprovalNotFoundError  404
  ApprovalAlreadyResolved 409
  InvalidAction          422
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from shared.bus import get_publisher
from shared.domain.enums import ApprovalAction
from shared.domain.events import DomainEvent, EventTopic

from .callbacks import SOFT_REJECT_ACTIONS, execute_callback, execute_reject_callback
from .config import settings
from .notification import notify
from .repository import get_repository
from .schemas import (
    ApprovalSignature,
    ApprovalRecord,
    ApprovalResponse,
    CreateApprovalRequest,
    ResolveApprovalRequest,
)

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {a.value for a in ApprovalAction}

# Actions where a REJECT is a deliberate operator decision, not a deal
# blocker — no DEAL_EXCEPTION event should be emitted for these.
# CONFIRM_NEGOTIATION_AGREEMENT is soft: rejecting it means "keep negotiating"
# (the session reopens via its reject callback), not a blocked deal. The set is
# owned by callbacks.py (every soft action has a reject handler there).
_SOFT_REJECT_ACTIONS = SOFT_REJECT_ACTIONS


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ApprovalNotFoundError(Exception):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"Approval {approval_id!r} not found")


class ApprovalAlreadyResolved(Exception):
    def __init__(self, approval_id: str, status: str) -> None:
        super().__init__(f"Approval {approval_id!r} is already {status!r}")


class SameApproverError(Exception):
    """Four-eyes: the second APPROVE must come from a different operator."""

    def __init__(self, approval_id: str, approver: str) -> None:
        super().__init__(f"Approval {approval_id!r} already carries {approver!r}'s approval — a different operator must countersign")
        self.approval_id = approval_id
        self.approver = approver


class ApproverNotProvenError(Exception):
    """A two-approver gate was decided by a caller with no proven identity."""

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"Approval {approval_id!r} needs two signed-in approvers — sign in to decide it")


class ComplianceBlockedError(Exception):
    """A money-release gate cannot be approved while a block-level hold is open."""

    def __init__(self, approval_id: str, blocks: list[str]) -> None:
        super().__init__(
            f"Approval {approval_id!r} cannot be approved while block-level compliance holds are open: "
            f"{', '.join(blocks)}. Resolve them, or reject this gate."
        )


class GateNotApproved(Exception):
    """An HTTP call tried to move money without the approved gate behind it."""


def four_eyes_threshold_usd() -> float | None:
    """``FOUR_EYES_THRESHOLD_USD``: money gates at/above it need two approvers."""
    import os
    raw = os.getenv("FOUR_EYES_THRESHOLD_USD", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def four_eyes_required(action: str, financial_impact_usd: float | None) -> bool:
    threshold = four_eyes_threshold_usd()
    if threshold is None or financial_impact_usd is None:
        return False
    try:
        from shared.orchestration.autonomy_policy import ACTION_CLASSES
        cls = next((c for c in ACTION_CLASSES if c.action == action), None)
        money = bool(cls and getattr(cls, "money_moving", False))
    except Exception:  # noqa: BLE001
        money = action in ("RELEASE_ESCROW_TRANCHE", "RELEASE_SUPPLIER_PAYOUT", "CONFIRM_BUYER_ACCEPTANCE", "REFUND_ESCROW")
    return money and float(financial_impact_usd) >= threshold


class InvalidAction(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return f"APPR-{uuid4().hex[:10].upper()}"


def _deep_link(approval_id: str) -> str:
    return f"{settings.ui_base_url}/approvals/{approval_id}"


async def _emit(topic: EventTopic, deal_id: str, payload: dict) -> None:
    try:
        await get_publisher().publish(DomainEvent(topic=topic, deal_id=deal_id, payload=payload))
    except Exception as exc:
        logger.warning("HitlApproval: emit %s failed: %s", topic, exc)


# ---------------------------------------------------------------------------
# Create approval
# ---------------------------------------------------------------------------

async def create_approval(req: CreateApprovalRequest) -> ApprovalResponse:
    """Create a HITL approval request, applying autonomy-level gate logic.

    Idempotent on ``idempotency_key``.
    """
    repo = get_repository()

    # Idempotency
    if req.idempotency_key:
        existing = await repo.get_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return ApprovalResponse.from_record(existing)

    if req.action not in _VALID_ACTIONS:
        raise InvalidAction(
            f"Unknown action {req.action!r}. Valid: {sorted(_VALID_ACTIONS)}"
        )

    now = datetime.now(timezone.utc)
    approval_id = _new_id()

    # Resolve expires_at
    expires_at: datetime | None = None
    if req.expires_at:
        try:
            expires_at = datetime.fromisoformat(req.expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except ValueError:
            expires_at = None
    elif req.autonomy_level == "L1":
        expires_at = now + timedelta(seconds=settings.l1_default_timeout_seconds)

    # Four-eyes: a money gate at/above the threshold is never auto-approved and
    # needs two distinct operators, whatever the policy resolved.
    required_approvals = 1
    autonomy_level = req.autonomy_level
    risk_flags = list(req.risk_flags)
    if four_eyes_required(req.action, req.financial_impact_usd):
        required_approvals = 2
        if "four_eyes_required" not in risk_flags:
            risk_flags.append("four_eyes_required")
        if autonomy_level in ("L2", "L3"):
            logger.info(
                "HitlApproval: %s for deal %s (%.2f USD) needs two approvers — %s auto-approval overridden to L0",
                req.action, req.deal_id, req.financial_impact_usd or 0.0, autonomy_level,
            )
            autonomy_level = "L0"
            expires_at = None

    # Paused platform: nothing approves itself; the gate waits for a person.
    from shared.orchestration import kill_switch
    if autonomy_level != "L0" and await kill_switch.is_paused():
        risk_flags.append("platform_paused")
        autonomy_level = "L0"
        expires_at = None

    # ---------------------------------------------------------------------------
    # L2 / L3: auto-approve immediately, skip human gate
    # ---------------------------------------------------------------------------
    if autonomy_level in ("L2", "L3"):
        if req.autonomy_level == "L3":
            logger.warning(
                "HitlApproval: L3 approval request for action=%s deal=%s — "
                "caller should not reach HITL at L3; auto-approving.",
                req.action, req.deal_id,
            )

        record = ApprovalRecord(
            approval_id=approval_id,
            deal_id=req.deal_id,
            action=req.action,
            summary=req.summary,
            financial_impact_usd=req.financial_impact_usd,
            risk_flags=req.risk_flags,
            autonomy_level=req.autonomy_level,
            status="AUTO_APPROVED",
            expires_at=expires_at,
            default_on_timeout=req.default_on_timeout,
            deep_link=_deep_link(approval_id),
            callback_payload=req.callback_payload,
            decision="APPROVE",
            resolved_by=f"system:auto_{req.autonomy_level.lower()}",
            resolved_at=now,
            resolution_note=f"Auto-approved by autonomy level {req.autonomy_level}",
            created_at=now,
            updated_at=now,
            idempotency_key=req.idempotency_key,
        )
        await repo.save(record)
        response = ApprovalResponse.from_record(record)

        # Notify UI (non-blocking) — operator sees it was auto-approved
        await notify("approval_created", response.model_dump(mode="json"))

        # Execute saga callback
        asyncio.create_task(
            execute_callback(req.action, req.deal_id, req.callback_payload, approval_id)
        )

        await _emit(EventTopic.APPROVAL_RESOLVED, req.deal_id, {
            "approval_id": approval_id,
            "action": req.action,
            "decision": "APPROVE",
            "resolved_by": f"system:auto_{req.autonomy_level.lower()}",
        })
        return response

    # ---------------------------------------------------------------------------
    # L0 / L1: create PENDING approval, wait for human
    # ---------------------------------------------------------------------------
    record = ApprovalRecord(
        approval_id=approval_id,
        deal_id=req.deal_id,
        action=req.action,
        summary=req.summary,
        financial_impact_usd=req.financial_impact_usd,
        risk_flags=risk_flags,
        autonomy_level=autonomy_level,
        status="PENDING",
        expires_at=expires_at,
        default_on_timeout=req.default_on_timeout,
        deep_link=_deep_link(approval_id),
        callback_payload=req.callback_payload,
        required_approvals=required_approvals,
        created_at=now,
        updated_at=now,
        idempotency_key=req.idempotency_key,
    )
    await repo.save(record)
    response = ApprovalResponse.from_record(record)

    # Push to UI via SSE + webhook
    await notify("approval_created", response.model_dump(mode="json"))

    await _emit(EventTopic.APPROVAL_REQUESTED, req.deal_id, {
        "approval_id": approval_id,
        "action": req.action,
        "autonomy_level": autonomy_level,
        "deep_link": record.deep_link,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "required_approvals": required_approvals,
    })

    logger.info(
        "HitlApproval: approval %s created (action=%s deal=%s autonomy=%s approvers=%d)",
        approval_id, req.action, req.deal_id, autonomy_level, required_approvals,
    )
    return response


# ---------------------------------------------------------------------------
# Resolve approval (human decision)
# ---------------------------------------------------------------------------

async def resolve_approval(
    approval_id: str,
    req: ResolveApprovalRequest,
    *,
    approver_proven: bool = True,
) -> ApprovalResponse:
    """Human operator approves or rejects a PENDING approval.

    APPROVE → execute saga callback, emit approval.resolved
    REJECT  → emit deal.exception + approval.resolved
    ``approver_proven=False``: the HTTP caller's identity could not be checked.
    """
    repo = get_repository()
    record = await repo.get(approval_id)
    if record is None:
        raise ApprovalNotFoundError(approval_id)
    if record.status != "PENDING":
        raise ApprovalAlreadyResolved(approval_id, record.status)
    if record.required_approvals > 1 and not approver_proven:
        raise ApproverNotProvenError(approval_id)

    # Refused before any signature is taken, so the gate stays open for a retry.
    from shared.orchestration import compliance, kill_switch
    if req.decision == "APPROVE":
        await kill_switch.assert_running(f"Approving {record.action}")
    if req.decision == "APPROVE" and record.action in compliance.MONEY_RELEASE_ACTIONS:
        blocks = await compliance.open_blocks(record.deal_id)
        if blocks:
            raise ComplianceBlockedError(approval_id, blocks)

    now = datetime.now(timezone.utc)
    new_status = "APPROVED" if req.decision == "APPROVE" else "REJECTED"

    # Four-eyes: collect distinct approvers; the gate resolves on the last one.
    signatures = list(record.approvals)
    if req.decision == "APPROVE" and record.required_approvals > 1:
        if any(s.approved_by == req.resolved_by for s in signatures):
            raise SameApproverError(approval_id, req.resolved_by)
        signatures.append(ApprovalSignature(approved_by=req.resolved_by, approved_at=now, note=req.note))
        if len(signatures) < record.required_approvals:
            countersign = record.model_copy(update={
                "approvals": signatures,
                "modified_body": req.modified_body or record.modified_body,
                "updated_at": now,
            })
            await repo.save(countersign)
            response = ApprovalResponse.from_record(countersign)
            await notify("approval_countersign_needed", response.model_dump(mode="json"))
            await _emit(EventTopic.APPROVAL_REQUESTED, record.deal_id, {
                "approval_id": approval_id,
                "action": record.action,
                "autonomy_level": record.autonomy_level,
                "deep_link": record.deep_link,
                "countersign": True,
                "approvals": len(signatures),
                "required_approvals": record.required_approvals,
                "approved_by": req.resolved_by,
            })
            logger.info(
                "HitlApproval: approval %s approved by %s (%d/%d) — waiting for a second approver",
                approval_id, req.resolved_by, len(signatures), record.required_approvals,
            )
            return response

    updated = record.model_copy(update={
        "status": new_status,
        "decision": req.decision,
        "resolved_by": req.resolved_by,
        "resolved_at": now,
        "resolution_note": req.note,
        "modified_body": req.modified_body or record.modified_body,
        "approvals": signatures,
        "updated_at": now,
    })
    await repo.save(updated)
    response = ApprovalResponse.from_record(updated)

    # Notify UI
    await notify("approval_resolved", response.model_dump(mode="json"))

    if req.decision == "APPROVE":
        # Inject modified_body into callback payload so the handler can use it
        payload_for_callback = dict(record.callback_payload)
        if req.modified_body:
            payload_for_callback["modified_body"] = req.modified_body

        asyncio.create_task(
            execute_callback(record.action, record.deal_id, payload_for_callback, approval_id)
        )
        await _emit(EventTopic.APPROVAL_RESOLVED, record.deal_id, {
            "approval_id": approval_id,
            "action": record.action,
            "decision": "APPROVE",
            "resolved_by": req.resolved_by,
        })
    else:
        if record.action in _SOFT_REJECT_ACTIONS:
            # Soft reject (e.g. email draft) — call the reject callback to update
            # the draft status, but do NOT raise a deal exception.
            asyncio.create_task(
                execute_reject_callback(
                    record.action, record.deal_id, record.callback_payload, approval_id
                )
            )
        else:
            # Hard reject: surface as deal exception for downstream handling
            await _emit(EventTopic.DEAL_EXCEPTION, record.deal_id, {
                "source": "hitl_approval",
                "approval_id": approval_id,
                "action": record.action,
                "decision": "REJECT",
                "resolved_by": req.resolved_by,
                "note": req.note,
                "action_required": "deal_flow_blocked_by_operator_rejection",
            })
        await _emit(EventTopic.APPROVAL_RESOLVED, record.deal_id, {
            "approval_id": approval_id,
            "action": record.action,
            "decision": "REJECT",
            "resolved_by": req.resolved_by,
        })

    logger.info(
        "HitlApproval: approval %s resolved %s by %s",
        approval_id, req.decision, req.resolved_by,
    )
    return response


# ---------------------------------------------------------------------------
# Internal auto-resolve (called by timeout watcher)
# ---------------------------------------------------------------------------

async def _auto_resolve(record: ApprovalRecord, decision: str, reason: str) -> None:
    """Auto-resolve a PENDING approval (timeout or system trigger)."""
    repo = get_repository()
    now = datetime.now(timezone.utc)
    new_status = "APPROVED" if decision == "APPROVE" else "TIMED_OUT"

    updated = record.model_copy(update={
        "status": new_status,
        "decision": decision,
        "resolved_by": "system:timeout",
        "resolved_at": now,
        "resolution_note": reason,
        "updated_at": now,
    })
    await repo.save(updated)
    response = ApprovalResponse.from_record(updated)
    await notify("approval_resolved", response.model_dump(mode="json"))

    if decision == "APPROVE":
        await execute_callback(
            record.action, record.deal_id, record.callback_payload, record.approval_id
        )
        await _emit(EventTopic.APPROVAL_RESOLVED, record.deal_id, {
            "approval_id": record.approval_id,
            "action": record.action,
            "decision": "APPROVE",
            "resolved_by": "system:timeout",
        })
    else:
        await _emit(EventTopic.DEAL_EXCEPTION, record.deal_id, {
            "source": "hitl_approval",
            "approval_id": record.approval_id,
            "action": record.action,
            "decision": "REJECT",
            "resolved_by": "system:timeout",
            "action_required": "deal_flow_blocked_by_timeout_rejection",
        })

    logger.info(
        "HitlApproval: approval %s auto-resolved %s (reason: %s)",
        record.approval_id, decision, reason,
    )


# ---------------------------------------------------------------------------
# Background timeout watcher
# ---------------------------------------------------------------------------

async def run_timeout_watcher() -> None:
    """Coroutine that periodically auto-resolves expired L1 approvals.

    Run as a background task from the service lifespan.
    """
    poll = settings.timeout_poll_interval_seconds
    logger.info("HitlApproval: timeout watcher started (interval=%ds)", poll)
    while True:
        try:
            await asyncio.sleep(poll)
            from shared.orchestration import kill_switch
            if await kill_switch.is_paused():
                continue
            now_iso = datetime.now(timezone.utc).isoformat()
            repo = get_repository()
            expired = await repo.list_pending_expired(now_iso)
            for record in expired:
                decision = record.default_on_timeout
                await _auto_resolve(
                    record,
                    decision,
                    f"Auto-resolved after timeout (default={decision})",
                )
        except asyncio.CancelledError:
            logger.info("HitlApproval: timeout watcher stopped")
            return
        except Exception as exc:
            logger.error("HitlApproval: timeout watcher error: %s", exc)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

async def get_approval(approval_id: str) -> ApprovalResponse:
    repo = get_repository()
    record = await repo.get(approval_id)
    if record is None:
        raise ApprovalNotFoundError(approval_id)
    return ApprovalResponse.from_record(record)


async def assert_gate_approved(approval_id: str | None, action: str, expect: dict[str, Any]) -> None:
    """Money moves over HTTP only as the execution of an approved gate: it must
    exist, be approved, be for this action, and name exactly this money."""
    if not approval_id:
        raise GateNotApproved(f"{action} needs an approved gate — it cannot be called directly")
    try:
        approval = await get_approval(approval_id)
    except ApprovalNotFoundError as exc:
        raise GateNotApproved(f"Approval {approval_id!r} not found") from exc
    if approval.action != action:
        raise GateNotApproved(f"Approval {approval_id!r} is for {approval.action}, not {action}")
    if approval.status not in ("APPROVED", "AUTO_APPROVED"):
        raise GateNotApproved(f"Approval {approval_id!r} is {approval.status}, not approved")
    payload = approval.callback_payload or {}
    for field, value in expect.items():
        if payload.get(field) != value:
            raise GateNotApproved(f"Approval {approval_id!r} does not cover this {field}")


async def list_approvals(
    status: str | None = None,
    deal_id: str | None = None,
) -> list[ApprovalResponse]:
    repo = get_repository()
    if deal_id:
        records = await repo.list_by_deal(deal_id)
        if status:
            records = [r for r in records if r.status == status]
    else:
        records = await repo.list_by_status(status)
    return [ApprovalResponse.from_record(r) for r in records]
