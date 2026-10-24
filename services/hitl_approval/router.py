"""HITL Approval HTTP router.

Routes:
  POST /hitl-approval/approvals                      Create approval request
  GET  /hitl-approval/approvals                      List (filter by status/deal_id)
  GET  /hitl-approval/approvals/{approval_id}        Get single approval
  POST /hitl-approval/approvals/{approval_id}/resolve Operator resolves APPROVE|REJECT
  GET  /hitl-approval/stream                         SSE stream for UI notifications
  GET  /hitl-approval/health

SSE stream contract (text/event-stream)
---------------------------------------
  On connect   → replays recent buffered events so the UI catches up
  Ongoing      → pushes `approval_created` and `approval_resolved` events
  Heartbeat    → `: heartbeat` comment every 30s to keep connection alive

Event JSON shape:
  {
    "event_type": "approval_created" | "approval_resolved",
    "occurred_at": "2026-06-25T10:00:00Z",
    "approval":    { ...ApprovalResponse fields... }
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .notification import deregister_sse_client, get_recent_events, register_sse_client
from .schemas import (
    ApprovalResponse,
    CreateApprovalRequest,
    ResolveApprovalRequest,
)
from .service import (
    ApprovalAlreadyResolved,
    SameApproverError,
    ApprovalNotFoundError,
    InvalidAction,
    create_approval,
    get_approval,
    list_approvals,
    resolve_approval,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hitl-approval", tags=["hitl-approval"])


# ---------------------------------------------------------------------------
# Autonomy policy (per-action L0–L3) — backs the Autonomy Settings page
# ---------------------------------------------------------------------------

class GateOverride(BaseModel):
    """Per-action gate settings; omit a key to leave it, null to clear it."""
    level: str | None = None
    timeout_seconds: int | None = None
    default_on_timeout: str | None = None
    auto_approve_below_usd: float | None = None


class AutonomyPolicyUpdate(BaseModel):
    levels: dict[str, str | None] = Field(default_factory=dict)
    """action → L0|L1|L2|L3; null clears the level override for that action."""
    gates: dict[str, GateOverride | None] = Field(default_factory=dict)
    """action → timeout / on-timeout / value-threshold overrides; null clears all of them."""
    money_auto_approve_below_usd: float | None = None
    """Global value threshold for money-moving gates; only applied when
    ``set_money_threshold`` is true (so a plain level save never touches it)."""
    set_money_threshold: bool = False
    updated_by: str = "operator"


def _policy_view() -> dict[str, Any]:
    from shared.orchestration.autonomy_policy import (
        effective_policy, money_auto_approve_below_usd, money_ceiling,
    )
    return {
        "money_ceiling": money_ceiling(),
        "money_auto_approve_below_usd": money_auto_approve_below_usd(),
        "actions": list(effective_policy().values()),
    }


@router.get("/autonomy-policy")
async def api_get_autonomy_policy() -> dict[str, Any]:
    """Effective autonomy level, L1 timeout, timeout default and value threshold
    per action class, and where each one comes from (override / env / default /
    money ceiling / value threshold)."""
    return _policy_view()


@router.put("/autonomy-policy")
async def api_put_autonomy_policy(req: AutonomyPolicyUpdate) -> dict[str, Any]:
    """Set per-action overrides (persisted in Mongo when configured)."""
    from shared.orchestration.autonomy_policy import set_policy

    gates = {
        a: (g.model_dump(exclude_unset=True) if g is not None else None)
        for a, g in req.gates.items()
    }
    try:
        await set_policy(
            req.levels, updated_by=req.updated_by, gates=gates,
            money_auto_approve_below_usd=(
                req.money_auto_approve_below_usd if req.set_money_threshold else "__unchanged__"
            ),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _policy_view()


# ---------------------------------------------------------------------------
# Approvals CRUD
# ---------------------------------------------------------------------------

@router.post("/approvals", response_model=ApprovalResponse, status_code=status.HTTP_201_CREATED)
async def api_create_approval(req: CreateApprovalRequest) -> ApprovalResponse:
    """Create a HITL approval request.

    Autonomy-level gate:
    - L0: Persists PENDING, notifies UI via SSE/webhook, blocks until resolved.
    - L1: Persists PENDING, notifies UI, auto-resolves after expires_at.
    - L2: Auto-approves immediately, notifies UI (non-blocking operator view).
    - L3: Auto-approves and logs (caller should avoid HITL at L3).
    """
    try:
        return await create_approval(req)
    except InvalidAction as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/approvals", response_model=list[ApprovalResponse])
async def api_list_approvals(
    status_filter: str | None = Query(default=None, alias="status"),
    deal_id: str | None = Query(default=None),
) -> list[ApprovalResponse]:
    """List approvals, optionally filtered by ``status`` and/or ``deal_id``."""
    return await list_approvals(status=status_filter, deal_id=deal_id)


@router.get("/approvals/{approval_id}", response_model=ApprovalResponse)
async def api_get_approval(approval_id: str) -> ApprovalResponse:
    """Deep-link target for the approval UI page."""
    try:
        return await get_approval(approval_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/approvals/{approval_id}/resolve", response_model=ApprovalResponse)
async def api_resolve_approval(
    approval_id: str,
    req: ResolveApprovalRequest,
) -> ApprovalResponse:
    """Operator approves or rejects a PENDING approval.

    APPROVE → saga callback executed; emits approval.resolved.
    REJECT  → emits deal.exception + approval.resolved.
    """
    try:
        return await resolve_approval(approval_id, req)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ApprovalAlreadyResolved as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    except SameApproverError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))


# ---------------------------------------------------------------------------
# Daily operator digest
# ---------------------------------------------------------------------------

@router.get("/digest")
async def api_digest(format: str = "json") -> Any:
    """Preview today's operator digest (pending gates, deals per stage, stalls,
    open exceptions, money, Apify spend). ``?format=text`` renders the message."""
    from fastapi.responses import PlainTextResponse
    from shared.orchestration.digest import build_digest, render_digest_text
    d = await build_digest()
    if format == "text":
        return PlainTextResponse(render_digest_text(d))
    return {**d, "text": render_digest_text(d)}


@router.post("/digest/send")
async def api_digest_send() -> dict[str, Any]:
    """Send the digest now on every configured channel (Telegram / e-mail)."""
    from shared.orchestration.digest import send_daily_digest
    return await send_daily_digest()


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

async def _event_generator(request: Request) -> AsyncGenerator[str, None]:
    client_id, queue = register_sse_client()
    try:
        # Replay recent events on connect so the UI catches up
        for event in get_recent_events():
            yield f"data: {json.dumps(event, default=str)}\n\n"

        # Stream live events
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {json.dumps(event, default=str)}\n\n"
            except asyncio.TimeoutError:
                # Heartbeat keeps the connection alive through proxies
                yield ": heartbeat\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        deregister_sse_client(client_id)


@router.get("/stream")
async def api_sse_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time approval notifications.

    The UI app connects once and receives all approval_created and
    approval_resolved events in real time.  Recent events are replayed
    on reconnect so the UI stays consistent after a disconnect.

    **Usage (JavaScript):**
    ```js
    const es = new EventSource('/hitl-approval/stream');
    es.onmessage = (e) => {
      const event = JSON.parse(e.data);
      if (event.event_type === 'approval_created') showApprovalBanner(event.approval);
      if (event.event_type === 'approval_resolved') updateApprovalStatus(event.approval);
    };
    ```
    """
    return StreamingResponse(
        _event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict[str, Any]:
    from .notification import _sse_clients
    return {
        "service": "hitl-approval",
        "status": "ok",
        "connected_sse_clients": len(_sse_clients),
    }
