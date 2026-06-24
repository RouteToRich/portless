"""REST client for the Negotiation Agent."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class NegotiationAgentClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.negotiation_agent_url)

    async def health(self) -> Any:
        return await self.get("/negotiation-agent/health")

    async def start(self, payload: dict[str, Any]) -> Any:
        return await self.post("/negotiation-agent/negotiations", json=payload)

    async def run_round(self, negotiation_id: str, payload: dict[str, Any]) -> Any:
        return await self.post(f"/negotiation-agent/negotiations/{negotiation_id}/round", json=payload)

    async def get_negotiation(self, negotiation_id: str) -> Any:
        return await self.get(f"/negotiation-agent/negotiations/{negotiation_id}")
