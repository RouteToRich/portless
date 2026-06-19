"""REST client for the Shield-Link service."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class ShieldLinkClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.shield_link_url)

    async def health(self) -> Any:
        return await self.get("/shield-link/health")

    async def book_qc(self, payload: dict[str, Any]) -> Any:
        return await self.post("/shield-link/qc", json=payload)

    async def book_freight(self, payload: dict[str, Any]) -> Any:
        return await self.post("/shield-link/freight", json=payload)

    async def file_cha(self, payload: dict[str, Any]) -> Any:
        return await self.post("/shield-link/cha", json=payload)
