"""REST client for the Escrow-Guard service.

Used by other services to interact with Escrow-Guard over HTTP (microservice mode).
"""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class EscrowGuardClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.escrow_guard_url)

    async def health(self) -> Any:
        return await self.get("/escrow-guard/health")

    async def create_hold(self, payload: dict[str, Any]) -> Any:
        return await self.post("/escrow-guard/holds", json=payload)

    async def get_hold(self, escrow_id: str) -> Any:
        return await self.get(f"/escrow-guard/holds/{escrow_id}")

    async def get_hold_by_deal(self, deal_id: str) -> Any:
        return await self.get(f"/escrow-guard/deals/{deal_id}/hold")

    async def request_payout(self, payload: dict[str, Any]) -> Any:
        return await self.post("/escrow-guard/payouts", json=payload)

    async def get_payout(self, payout_id: str) -> Any:
        return await self.get(f"/escrow-guard/payouts/{payout_id}")

    async def get_deal_summary(self, deal_id: str) -> Any:
        return await self.get(f"/escrow-guard/deals/{deal_id}/summary")
