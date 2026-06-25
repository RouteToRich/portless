"""REST client for the Sourcing service.

Typical call flow (DealMaker → Sourcing)
-----------------------------------------
1. DealMaker receives deal.created event with status=MATCHING
2. Calls ``dispatch_rfq`` with the winning supplier from the deal's match_results
3. Supplier responds; their quote arrives via ``receive_quote`` (inbound webhook or portal)
4. Sourcing service re-scores and transitions deal → NEGOTIATING automatically
"""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class SourcingClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.sourcing_url)

    async def health(self) -> dict[str, Any]:
        return await self.get("/sourcing/health")

    # ── RFQ dispatch ──────────────────────────────────────────────────────

    async def dispatch_rfq(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch RFQs to suppliers.  ``payload.suppliers`` must be a list of
        ``{supplier_id, contact_id, source}`` dicts."""
        return await self.post("/sourcing/rfq", json=payload)

    async def get_rfq(self, rfq_id: str) -> dict[str, Any]:
        return await self.get(f"/sourcing/rfqs/{rfq_id}")

    async def list_rfqs(self, deal_id: str) -> list[dict[str, Any]]:
        return await self.get(f"/sourcing/deals/{deal_id}/rfqs")

    # ── Quote ingestion ────────────────────────────────────────────────────

    async def receive_quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ingest a supplier quote.  Triggers verification + re-scoring pipeline."""
        return await self.post("/sourcing/quotes", json=payload)

    async def get_quote(self, quote_id: str) -> dict[str, Any]:
        return await self.get(f"/sourcing/quotes/{quote_id}")

    async def list_quotes(
        self,
        deal_id: str,
        rfq_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if rfq_id:
            params["rfq_id"] = rfq_id
        return await self.get(f"/sourcing/deals/{deal_id}/quotes", params=params)

    # ── Ad-hoc verification ────────────────────────────────────────────────

    async def verify_supplier(
        self,
        supplier_id: str,
        gstin: str | None = None,
        iec: str | None = None,
        cin: str | None = None,
        udyam_number: str | None = None,
    ) -> dict[str, Any]:
        """Pre-screen a supplier with regulatory checks before issuing an RFQ."""
        params: dict[str, Any] = {}
        if gstin:
            params["gstin"] = gstin
        if iec:
            params["iec"] = iec
        if cin:
            params["cin"] = cin
        if udyam_number:
            params["udyam_number"] = udyam_number
        return await self.post(
            f"/sourcing/verify/{supplier_id}", params=params, json={}
        )
