"""REST client for the HITL Approval service."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class HitlApprovalClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.hitl_approval_url)

    async def health(self) -> Any:
        return await self.get("/hitl-approval/health")

    async def create_approval(self, payload: dict[str, Any]) -> Any:
        return await self.post("/hitl-approval/approvals", json=payload)

    async def list_approvals(self, status: str | None = None) -> Any:
        params = {"status": status} if status else None
        return await self.get("/hitl-approval/approvals", params=params)

    async def resolve_approval(self, approval_id: str, payload: dict[str, Any]) -> Any:
        return await self.post(f"/hitl-approval/approvals/{approval_id}/resolve", json=payload)
