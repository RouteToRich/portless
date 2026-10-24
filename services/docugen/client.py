"""REST client for the DocuGen service.

Used by other services when running in microservice mode.
In the combined monolith, call service functions directly via in-process import.
"""
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
        """POST /docugen/documents — render a trade document."""
        return await self.post("/docugen/documents", json=payload)

    async def get_document(self, doc_id: str) -> Any:
        """GET /docugen/documents/{doc_id}."""
        return await self.get(f"/docugen/documents/{doc_id}")

    async def list_deal_documents(self, deal_id: str) -> Any:
        """GET /docugen/deals/{deal_id}/documents."""
        return await self.get(f"/docugen/deals/{deal_id}/documents")

    async def file_customs(self, payload: dict[str, Any]) -> Any:
        """POST /docugen/customs/file — trigger ICEGATE Shipping Bill filing."""
        return await self.post("/docugen/customs/file", json=payload)
