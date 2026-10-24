"""REST client for the Email Processor service.

Used by the Email Gateway's IMAP pollers (when ``FORWARD_VIA_HTTP=true``) and by
any external fetcher to forward a fetched email to the receiving API, which
matches it to a live negotiation and triggers the orchestration agent.

Usage example::

    from services.email_processor.client import EmailProcessorClient

    client = EmailProcessorClient()
    result = await client.submit_inbound(raw_email, party_override="buyer")
"""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings

from .schemas import InboundApiRequest, InboundRawEmail, ProcessingResult


class EmailProcessorClient(BaseServiceClient):
    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(base_url or settings.email_processor_url)

    async def health(self) -> Any:
        return await self.get("/email-processor/health")

    async def submit_inbound(
        self,
        email: InboundRawEmail,
        *,
        negotiation_id_override: str | None = None,
        party_override: str | None = None,
        counter_price_override: float | None = None,
    ) -> ProcessingResult:
        """POST one fetched email to the receiving API.

        The keyword-only signature matches what the Gateway's ``ImapPoller``
        passes its callback (``party_override``), so this method can be used
        directly as the poller's forward callback.
        """
        body = InboundApiRequest(
            email=email,
            negotiation_id_override=negotiation_id_override,
            party_override=party_override,
            counter_price_override=counter_price_override,
        ).model_dump(mode="json")
        data = await self.post("/email-processor/inbound", json=body)
        return ProcessingResult(**data)

    async def get_inbound(self, inbound_id: str) -> Any:
        return await self.get(f"/email-processor/inbound/{inbound_id}")

    async def list_inbound(
        self,
        status: str | None = None,
        negotiation_id: str | None = None,
        limit: int = 100,
    ) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if negotiation_id:
            params["negotiation_id"] = negotiation_id
        return await self.get("/email-processor/inbound", params=params)
