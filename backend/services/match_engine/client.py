"""REST client for the Match Engine service."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class MatchEngineClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.match_engine_url)

    async def health(self) -> Any:
        return await self.get("/match-engine/health")

    async def score(self, payload: dict[str, Any]) -> Any:
        return await self.post("/match-engine/score", json=payload)

    async def list_scores(self) -> Any:
        return await self.get("/match-engine/scores")

    async def get_score(self, deal_id: str) -> Any:
        return await self.get(f"/match-engine/scores/{deal_id}")
