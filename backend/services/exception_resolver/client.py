"""REST client for the Exception-Resolver service.

Used by other services when they need to raise an exception programmatically:

    from services.exception_resolver.client import ExceptionResolverClient

    client = ExceptionResolverClient()
    exc = await client.raise_exception({
        "deal_id": deal_id,
        "source_event": "payout.failed",
        "source_service": "escrow_guard",
        "detail": "Payoneer payout 503 — exhausted retries",
        "context": {"payout_id": payout_id, "amount_usd": 12000},
    })
"""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class ExceptionResolverClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.exception_resolver_url)

    async def health(self) -> Any:
        return await self.get("/exception-resolver/health")

    async def raise_exception(self, payload: dict[str, Any]) -> Any:
        """Raise and analyse a deal exception. Returns ExceptionResponse."""
        return await self.post("/exception-resolver/exceptions", json=payload)

    async def get_exception(self, exception_id: str) -> Any:
        return await self.get(f"/exception-resolver/exceptions/{exception_id}")

    async def list_exceptions(
        self,
        deal_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if deal_id:
            params["deal_id"] = deal_id
        if status:
            params["status"] = status
        return await self.get("/exception-resolver/exceptions", params=params)
