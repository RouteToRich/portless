"""REST client for the DealMaker service."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class DealMakerClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.dealmaker_url)

    async def health(self) -> Any:
        return await self.get("/dealmaker/health")

    async def issue_pi(self, payload: dict[str, Any]) -> Any:
        return await self.post("/dealmaker/pi", json=payload)

    async def issue_po(self, payload: dict[str, Any]) -> Any:
        return await self.post("/dealmaker/po", json=payload)
