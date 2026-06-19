"""REST client for the Trade-Min service."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class TradeMinClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.trade_min_url)

    async def health(self) -> Any:
        return await self.get("/trade-min/health")

    async def scan(self, payload: dict[str, Any]) -> Any:
        return await self.post("/trade-min/scan", json=payload)

    async def list_opportunities(self) -> Any:
        return await self.get("/trade-min/opportunities")

    async def get_opportunity(self, opportunity_id: str) -> Any:
        return await self.get(f"/trade-min/opportunities/{opportunity_id}")
