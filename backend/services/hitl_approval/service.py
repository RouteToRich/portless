"""Business logic for the HITL Approval service (skeleton — not implemented)."""
from __future__ import annotations

from .schemas import ApprovalResponse, CreateApprovalRequest, ResolveApprovalRequest


async def create_approval(payload: CreateApprovalRequest) -> ApprovalResponse:
    """Create a typed approval request and emit ``approval.requested``.

    TODO: generate approval_id + deep_link, persist, emit event, schedule the
    safe-timeout default per autonomy level.
    """
    raise NotImplementedError


async def list_approvals(status: str | None) -> list[ApprovalResponse]:
    """List approvals (optionally filtered by status). TODO: read persistence."""
    raise NotImplementedError


async def resolve_approval(approval_id: str, payload: ResolveApprovalRequest) -> ApprovalResponse:
    """Resolve an approval and emit ``approval.resolved``.

    TODO: persist decision, emit event, unblock the waiting saga.
    """
    raise NotImplementedError
