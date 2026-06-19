"""HTTP routes for the HITL Approval service."""
from __future__ import annotations

from fastapi import APIRouter

from . import service
from .schemas import (
    ApprovalResponse,
    CreateApprovalRequest,
    ResolveApprovalRequest,
)

router = APIRouter(prefix="/hitl-approval", tags=["hitl-approval"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "hitl-approval"}


@router.post("/approvals", response_model=ApprovalResponse, status_code=201)
async def create_approval(payload: CreateApprovalRequest) -> ApprovalResponse:
    return await service.create_approval(payload)


@router.get("/approvals", response_model=list[ApprovalResponse])
async def list_approvals(status: str | None = None) -> list[ApprovalResponse]:
    return await service.list_approvals(status)


@router.post("/approvals/{approval_id}/resolve", response_model=ApprovalResponse)
async def resolve_approval(approval_id: str, payload: ResolveApprovalRequest) -> ApprovalResponse:
    return await service.resolve_approval(approval_id, payload)
