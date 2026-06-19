"""REST client for the Negotiator service."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class NegotiatorClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.negotiator_url)

    async def health(self) -> Any:
        return await self.get("/negotiator/health")

    async def start(self, payload: dict[str, Any]) -> Any:
        return await self.post("/negotiator/negotiations", json=payload)

    async def run_round(self, negotiation_id: str, payload: dict[str, Any]) -> Any:
        return await self.post(f"/negotiator/negotiations/{negotiation_id}/round", json=payload)

    async def get_negotiation(self, negotiation_id: str) -> Any:
        return await self.get(f"/negotiator/negotiations/{negotiation_id}")
