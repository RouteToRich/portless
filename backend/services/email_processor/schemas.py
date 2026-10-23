"""Pydantic models for the Email Processor service."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class InboundEmailSource(str, Enum):
    IMAP = "imap"
    SENDGRID_WEBHOOK = "sendgrid_webhook"
    MAILGUN_WEBHOOK = "mailgun_webhook"
    MANUAL = "manual"


class MappingConfidence(str, Enum):
    HEADER = "header"
    """Matched via In-Reply-To or References email threading header."""
    SUBJECT_TOKEN = "subject_token"
    """Matched via [NEG:{negotiation_id}] token appended to every outbound subject."""
    MANUAL_OVERRIDE = "manual_override"
    """Operator supplied negotiation_id explicitly (manual submission)."""
    AMBIGUOUS = "ambiguous"
    """Sender email matches multiple active negotiations — operator must pick one."""
    UNMATCHED = "unmatched"
    """No mapping strategy succeeded."""


class InboundEmailStatus(str, Enum):
    RECEIVED = "received"
    """Parsed and stored; mapping attempt in progress."""
    MAPPED = "mapped"
    """Negotiation session identified."""
    PROCESSED = "processed"
    """run_round() called successfully; negotiation advanced."""
    NEEDS_OPERATOR = "needs_operator"
    """Price could not be extracted OR run_round() failed; HITL approval created."""
    AMBIGUOUS = "ambiguous"
    """Sender matched multiple active negotiations; operator must resolve."""
    UNMATCHED = "unmatched"
    """No active negotiation found for this email."""
    REJECTED = "rejected"
    """Operator marked as irrelevant / spam."""
    SELF_ECHO = "self_echo"
    """From address matches PortLess's own outbound sender — a copy of our own
    sent mail landed back in the polled inbox/folder (shared send+poll mailbox,
    an auto-forward rule, etc.), not a real reply. Never advances a round."""


# ---------------------------------------------------------------------------
# Attachment
# ---------------------------------------------------------------------------

class EmailAttachment(BaseModel):
    """A single email attachment captured from any inbound source."""

    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0
    """Size of the decoded attachment in bytes."""
    content_b64: str = ""
    """Base64-encoded attachment bytes. May be empty when only metadata is captured."""


# ---------------------------------------------------------------------------
# Unified inbound email (normalised from any source)
# ---------------------------------------------------------------------------

class InboundRawEmail(BaseModel):
    from_address: str
    to_address: str
    subject: str
    body_text: str
    body_html: str = ""
    message_id: str = ""
    """The email's own Message-ID header value."""
    in_reply_to: str = ""
    """In-Reply-To header value — the Message-ID this email is replying to."""
    references: list[str] = Field(default_factory=list)
    """All Message-IDs from the References header (email thread chain)."""
    attachments: list[EmailAttachment] = Field(default_factory=list)
    """Attachments extracted from the email (PDF/Excel quotes, packing lists, …)."""
    source: InboundEmailSource
    received_at: datetime
    raw_headers: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Mapping result (ephemeral — not persisted directly)
# ---------------------------------------------------------------------------

class MappingResult(BaseModel):
    negotiation_id: str | None = None
    rfq_id: str | None = None
    """Set when the e-mail is a supplier's reply to an ``[RFQ:<id>]`` request
    (sourcing stage) rather than a negotiation round."""
    party: str | None = None
    """'buyer' | 'supplier' — derived from which party was addressed in the outbound email."""
    confidence: MappingConfidence
    candidates: list[str] = Field(default_factory=list)
    """Populated when confidence=AMBIGUOUS."""
    reason: str = ""
    session_status: str | None = None
    """``"AGREED"`` when the mapped session is already closed, i.e. this is a
    post-agreement reply (the buyer answering the Proforma Invoice email, the
    supplier acknowledging the PO) — the processor routes it to the deal-stage
    handler instead of ``run_round()``. None for a live negotiation."""


# ---------------------------------------------------------------------------
# Persisted inbound email record
# ---------------------------------------------------------------------------

class InboundEmailRecord(BaseModel):
    inbound_id: str
    from_address: str
    to_address: str
    subject: str
    body_text: str
    body_html: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = Field(default_factory=list)
    attachments: list[EmailAttachment] = Field(default_factory=list)
    source: InboundEmailSource
    status: InboundEmailStatus
    negotiation_id: str | None = None
    deal_id: str | None = None
    party: str | None = None
    mapping_confidence: MappingConfidence | None = None
    extracted_price: float | None = None
    """Counter-price extracted from email body. None if extraction failed."""
    hitl_approval_id: str | None = None
    """Set when status=NEEDS_OPERATOR or AMBIGUOUS — links to the HITL approval."""
    ambiguous_candidates: list[str] = Field(default_factory=list)
    """Filled when status=AMBIGUOUS — all matching negotiation IDs."""
    error: str | None = None
    received_at: datetime
    processed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Processing result
# ---------------------------------------------------------------------------

class ProcessingResult(BaseModel):
    inbound_id: str
    status: InboundEmailStatus
    negotiation_id: str | None = None
    round_no: int | None = None
    message: str = ""


# ---------------------------------------------------------------------------
# Webhook payload shapes
# ---------------------------------------------------------------------------

class SendGridInboundPayload(BaseModel):
    """
    Shape of a SendGrid Inbound Parse webhook POST (multipart/form-data).
    The router reads raw form fields directly; this model documents the fields.
    """
    from_: str = Field(alias="from", default="")
    to: str = ""
    subject: str = ""
    text: str = ""
    html: str = ""
    headers: str = ""
    envelope: str = ""
    charsets: str = ""
    spam_score: str = ""
    dkim: str = ""
    spf: str = ""

    class Config:
        populate_by_name = True


class MailgunInboundPayload(BaseModel):
    """
    Shape of a Mailgun Routes inbound forward webhook POST (multipart/form-data).
    The router reads raw form fields directly; this model documents the fields.
    """
    From: str = ""
    To: str = ""
    Subject: str = ""
    body_plain: str = Field(alias="body-plain", default="")
    body_html: str = Field(alias="body-html", default="")
    message_headers: str = Field(alias="message-headers", default="[]")
    timestamp: str = ""
    token: str = ""
    signature: str = ""

    class Config:
        populate_by_name = True


class ManualInboundRequest(BaseModel):
    """
    Operator manually submits an inbound email — useful when IMAP/webhooks
    are not configured or when replaying a message from another channel.
    """
    from_address: str
    subject: str
    body_text: str
    attachments: list[EmailAttachment] = Field(default_factory=list)
    negotiation_id: str | None = None
    """Supply to skip automatic mapping entirely."""
    party: str | None = None
    """'buyer' or 'supplier' — required when negotiation_id is provided."""
    counter_price: float | None = None
    """Supply to skip automatic price extraction."""


class InboundApiRequest(BaseModel):
    """Body for ``POST /email-processor/inbound`` — a fetched email forwarded by
    the Email Gateway's IMAP poller or any external fetcher (cron job, lambda).

    This is the "receiving API" the transport calls with each fetched email
    (sender, subject, content, attachments). The optional override fields carry
    the IMAP folder party-hint or an operator-supplied negotiation id/price so
    the receiver can skip auto-mapping / auto-price-extraction when known.
    """
    email: InboundRawEmail
    negotiation_id_override: str | None = None
    party_override: str | None = None
    counter_price_override: float | None = None
