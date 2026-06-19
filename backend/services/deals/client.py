"""REST client for the Deals service, used by other services."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class DealsClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.deals_url)

    async def health(self) -> Any:
        return await self.get("/deals/health")

    async def list_deals(self, status: str | None = None) -> Any:
        params = {"status": status} if status else None
        return await self.get("/deals", params=params)

    async def create_deal(self, payload: dict[str, Any]) -> Any:
        return await self.post("/deals", json=payload)

    async def get_deal(self, deal_id: str) -> Any:
        return await self.get(f"/deals/{deal_id}")

    async def update_status(self, deal_id: str, payload: dict[str, Any]) -> Any:
        return await self.patch(f"/deals/{deal_id}/status", json=payload)
