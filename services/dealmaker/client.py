"""REST client for the DealMaker service.

Used by other services (e.g. DocuGen) when running in microservice mode.
In the combined monolith, call service functions directly via in-process import.
"""
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
        """POST /dealmaker/pi — issue a Proforma Invoice."""
        return await self.post("/dealmaker/pi", json=payload)

    async def accept_pi(self, pi_id: str, payload: dict[str, Any]) -> Any:
        """POST /dealmaker/pi/{pi_id}/accept — record buyer acceptance."""
        return await self.post(f"/dealmaker/pi/{pi_id}/accept", json=payload)

    async def get_pi(self, pi_id: str) -> Any:
        """GET /dealmaker/pi/{pi_id}."""
        return await self.get(f"/dealmaker/pi/{pi_id}")

    async def issue_po(self, payload: dict[str, Any]) -> Any:
        """POST /dealmaker/po — issue a Purchase Order."""
        return await self.post("/dealmaker/po", json=payload)

    async def get_po(self, po_id: str) -> Any:
        """GET /dealmaker/po/{po_id}."""
        return await self.get(f"/dealmaker/po/{po_id}")

    async def get_deal_documents(self, deal_id: str) -> Any:
        """GET /dealmaker/deals/{deal_id}/documents."""
        return await self.get(f"/dealmaker/deals/{deal_id}/documents")
