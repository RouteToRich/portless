"""REST client for the Exception-Resolver service."""
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
        return await self.post("/exception-resolver/exceptions", json=payload)

    async def get_exception(self, exception_id: str) -> Any:
        return await self.get(f"/exception-resolver/exceptions/{exception_id}")
