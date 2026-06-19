"""REST client for the Escrow-Guard service."""
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

    async def request_payout(self, payload: dict[str, Any]) -> Any:
        return await self.post("/escrow-guard/payouts", json=payload)
