"""REST client for the HITL Approval service.

Used by other services (DealMaker, Negotiation Agent, Escrow Guard) to gate
actions behind human-in-the-loop approval when the autonomy level requires it.

Usage example (from DealMaker):
    from services.hitl_approval.client import HitlApprovalClient

    async def _gate_issue_po(deal_id, po_req, autonomy_level):
        if autonomy_level == "L0":
            client = HitlApprovalClient()
            approval = await client.create_approval({
                "deal_id": deal_id,
                "action": "ISSUE_PO",
                "autonomy_level": autonomy_level,
                "summary": "Issue Purchase Order — requires operator sign-off",
                "financial_impact_usd": po_req["buyer_price_usd"],
                "callback_payload": po_req,
            })
            # deal flow pauses until UI resolves the approval
            return approval
        # L2/L3: proceed directly
        return await issue_po(IssuePoRequest(**po_req))
"""
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
        """Create a HITL approval request.  Returns immediately for all autonomy levels."""
        return await self.post("/hitl-approval/approvals", json=payload)

    async def get_approval(self, approval_id: str) -> Any:
        return await self.get(f"/hitl-approval/approvals/{approval_id}")

    async def list_approvals(
        self,
        status: str | None = None,
        deal_id: str | None = None,
    ) -> Any:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        if deal_id:
            params["deal_id"] = deal_id
        return await self.get("/hitl-approval/approvals", params=params or None)

    async def resolve_approval(self, approval_id: str, payload: dict[str, Any]) -> Any:
        return await self.post(f"/hitl-approval/approvals/{approval_id}/resolve", json=payload)
