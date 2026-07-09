"""Pydantic models for the Email Gateway service (outbound send contract).

The inbound wire format (InboundRawEmail / EmailAttachment) is owned by the
Email Processor — the gateway imports it when forwarding fetched mail.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SendOutcome(str, Enum):
    SENT = "sent"
    """Delivered to the SMTP server."""
    LOGGED = "logged"
    """No credentials configured — message logged only (dev/CI fallback)."""


class SendEmailRequest(BaseModel):
    """One outbound email to transport. The caller (negotiation flow) owns the
    content, recipient, and threading identifiers; the gateway only sends."""

    to_address: str
    subject: str
    body: str
    negotiation_id: str = ""
    """Appended to the subject as [NEG:{id}] and embedded in the Message-ID for
    reply threading by the Email Processor."""
    round_no: int = 0
    email_id: str = ""
    """EmailHistoryRecord.email_id — used to build a unique, parseable Message-ID."""
    cc: list[str] = Field(default_factory=list)


class SendEmailResult(BaseModel):
    outcome: SendOutcome
    message_id: str = ""
    """The Message-ID header embedded on the sent email (empty if none)."""
    detail: str = ""
