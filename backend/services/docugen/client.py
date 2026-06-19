"""REST client for the DocuGen service."""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class DocuGenClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.docugen_url)

    async def health(self) -> Any:
        return await self.get("/docugen/health")

    async def generate_document(self, payload: dict[str, Any]) -> Any:
        return await self.post("/docugen/documents", json=payload)

    async def file_customs(self, payload: dict[str, Any]) -> Any:
        return await self.post("/docugen/customs", json=payload)
