"""REST client for the Shield-Link service.

Used by other services to interact with Shield-Link over HTTP (microservice mode).
In combined-monolith mode, services can call shield_link.service directly.
"""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class ShieldLinkClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.shield_link_url)

    async def health(self) -> Any:
        return await self.get("/shield-link/health")

    async def list_qc_providers(self) -> Any:
        return await self.get("/shield-link/qc/providers")

    async def quote_qc(self, payload: dict[str, Any]) -> Any:
        return await self.post("/shield-link/qc/quote", json=payload)

    async def book_qc(self, payload: dict[str, Any]) -> Any:
        return await self.post("/shield-link/qc", json=payload)

    async def quote_freight(self, payload: dict[str, Any]) -> Any:
        return await self.post("/shield-link/freight/quote", json=payload)

    async def book_freight(self, payload: dict[str, Any]) -> Any:
        return await self.post("/shield-link/freight", json=payload)

    async def file_cha(self, payload: dict[str, Any]) -> Any:
        return await self.post("/shield-link/cha", json=payload)

    async def get_tpo(self, tpo_id: str) -> Any:
        return await self.get(f"/shield-link/tpo/{tpo_id}")

    async def get_deal_orders(self, deal_id: str) -> Any:
        return await self.get(f"/shield-link/deals/{deal_id}/orders")
