"""REST client for the Deals service.

Used by agents and other services that need to create deals, drive status
transitions, or read match scores — especially the DealMaker agent and the
Negotiation Agent.

Typical call flow (DealMaker → Deals)
--------------------------------------
1. DealMaker detects a Trade-Min opportunity
2. Calls ``create_deal(opportunity_id=...)`` — Deals service scores all sellers
3. Polls ``get_deal(deal_id)`` until ``status == MATCHING``
4. Calls ``transition_status(deal_id, "NEGOTIATING")`` to hand off to the
   Negotiation Agent
5. After negotiation, Negotiation Agent calls ``update_financials`` with the
   agreed price then ``transition_status(deal_id, "NEGOTIATION_AGREED")``
"""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class DealsClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.deals_url)

    # ── meta ──────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        return await self.get("/deals/health")

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def list_deals(
        self,
        limit: int = 100,
        status: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List deals, optionally filtered by status and/or operator."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if operator_id:
            params["operator_id"] = operator_id
        return await self.get("/deals", params=params)

    async def create_deal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new deal.  Pass ``opportunity_id`` to trigger ME scoring."""
        return await self.post("/deals", json=payload)

    async def get_deal(self, deal_id: str) -> dict[str, Any]:
        """Fetch a single deal by ID."""
        return await self.get(f"/deals/{deal_id}")

    # ── Status transitions ─────────────────────────────────────────────────

    async def transition_status(
        self,
        deal_id: str,
        target_status: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Transition a deal to a new lifecycle state.

        Raises ``httpx.HTTPStatusError`` with status 422 when the transition
        is not permitted by the state machine.
        """
        return await self.patch(
            f"/deals/{deal_id}/status",
            json={"target_status": target_status, "reason": reason},
        )

    # Keep old method name as an alias for callers using the previous API
    async def update_status(
        self,
        deal_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.patch(f"/deals/{deal_id}/status", json=payload)

    # ── Financial patch ────────────────────────────────────────────────────

    async def update_financials(
        self,
        deal_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Patch financial fields (est_margin, winning price, CMS score, etc.)."""
        return await self.patch(f"/deals/{deal_id}/financials", json=payload)

    # ── Match Engine results sub-resource ─────────────────────────────────

    async def get_match_scores(self, deal_id: str) -> list[dict[str, Any]]:
        """Return all Match Engine scores for a deal's seller candidates.

        The list is sorted by composite score descending.
        ``decision`` is ``SOURCE`` (viable) or ``DISCARD``.
        """
        return await self.get(f"/deals/{deal_id}/match-scores")
