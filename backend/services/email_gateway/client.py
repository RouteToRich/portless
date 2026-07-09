"""REST client for the Email Gateway service (outbound send).

Used by the Negotiation Agent (microservice mode) to send an approved email
without importing SMTP logic. In the combined monolith the negotiation flow
imports ``sender.send_email`` directly.

Usage example::

    from services.email_gateway.client import EmailGatewayClient
    from services.email_gateway.schemas import SendEmailRequest

    client = EmailGatewayClient()
    result = await client.send_email(SendEmailRequest(
        to_address="supplier@example.com", subject="Re: Quote", body="...",
        negotiation_id=neg_id, round_no=2, email_id=email_id,
    ))
"""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings

from .schemas import SendEmailRequest, SendEmailResult


class EmailGatewayClient(BaseServiceClient):
    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(base_url or settings.email_gateway_url)

    async def health(self) -> Any:
        return await self.get("/email-gateway/health")

    async def send_email(self, req: SendEmailRequest) -> SendEmailResult:
        data = await self.post("/email-gateway/send", json=req.model_dump(mode="json"))
        return SendEmailResult(**data)
