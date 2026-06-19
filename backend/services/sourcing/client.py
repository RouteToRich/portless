"""REST client for the Sourcing service."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class SourcingClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.sourcing_url)

    async def health(self) -> Any:
        return await self.get("/sourcing/health")

    async def dispatch_rfq(self, payload: dict[str, Any]) -> Any:
        return await self.post("/sourcing/rfq", json=payload)

    async def receive_quote(self, payload: dict[str, Any]) -> Any:
        return await self.post("/sourcing/quotes", json=payload)
