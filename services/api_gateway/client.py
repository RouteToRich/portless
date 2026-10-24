"""REST client for the API Gateway service (used by UIs / external callers)."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class ApiGatewayClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.api_gateway_url)

    async def health(self) -> Any:
        return await self.get("/health")

    async def create_deal(self, payload: dict[str, Any]) -> Any:
        return await self.post("/deals", json=payload)
