"""HTTP routes for the Email Gateway service (outbound send API)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from . import sender
from .schemas import SendEmailRequest, SendEmailResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email-gateway", tags=["email-gateway"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "email_gateway"}


@router.post(
    "/send",
    response_model=SendEmailResult,
    summary="Send one outbound email (called from the negotiation flow)",
)
async def send(payload: SendEmailRequest) -> SendEmailResult:
    """Transport one email via SMTP.

    Called by the negotiation flow (after HITL approval) to deliver a
    negotiation message. Returns SENT when delivered, LOGGED when running
    without credentials, or 502 when a configured SMTP send fails.
    """
    try:
        return await sender.send_email(payload)
    except sender.EmailSendError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
