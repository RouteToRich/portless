"""HTTP routes for the Email Processor service."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from starlette.datastructures import UploadFile

from . import service
from .config import settings
from .schemas import (
    EmailAttachment,
    InboundApiRequest,
    InboundEmailRecord,
    InboundEmailSource,
    InboundEmailStatus,
    InboundRawEmail,
    ManualInboundRequest,
    ProcessingResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email-processor", tags=["email-processor"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "email_processor"}


# ---------------------------------------------------------------------------
# Receiving API — structured inbound email (IMAP poller / external fetcher)
# ---------------------------------------------------------------------------

@router.post(
    "/inbound",
    response_model=ProcessingResult,
    status_code=status.HTTP_201_CREATED,
    summary="Receive a fetched inbound email (IMAP poller / external fetcher)",
)
async def inbound_api(payload: InboundApiRequest) -> ProcessingResult:
    """Receiving API — the entry point for a fetched inbound email.

    Accepts one fetched email as structured JSON (sender, subject, content,
    attachments) and runs the full pipeline: it matches the email to a **live**
    negotiation in the DB (terminal negotiations are skipped), persists the
    inbound record, extracts the counter-price, and — once the record is
    stored — triggers the negotiation orchestration agent via ``run_round()``.

    The Email Gateway's IMAP poller calls this endpoint when
    ``FORWARD_VIA_HTTP=true``; external fetchers (a standalone cron job / lambda
    that polls IMAP) can call it directly.
    """
    return await service.process_inbound(
        payload.email,
        negotiation_id_override=payload.negotiation_id_override,
        party_override=payload.party_override,
        counter_price_override=payload.counter_price_override,
    )


# ---------------------------------------------------------------------------
# Inbound webhook — SendGrid Inbound Parse
# ---------------------------------------------------------------------------

@router.post(
    "/inbound/sendgrid",
    response_model=ProcessingResult,
    summary="SendGrid Inbound Parse webhook",
)
async def inbound_sendgrid(request: Request) -> ProcessingResult:
    """
    Receives emails forwarded by SendGrid Inbound Parse.

    **SendGrid dashboard setup:**
    1. Settings → Mail Settings → Inbound Parse → Add Host & URL
       - Hostname: ``mail.portless.io`` (set this as your MX domain's MX record)
       - URL: ``https://api.portless.io/email-processor/inbound/sendgrid``
       - Enable **"POST the raw, full MIME message"**
    2. Signature verification: Settings → Mail Settings → Inbound Parse
       → Signature Verification → copy Public Key →
       set ``SENDGRID_WEBHOOK_PUBLIC_KEY`` in .env

    Signature verification is performed when ``SENDGRID_WEBHOOK_PUBLIC_KEY`` is set.
    """
    form = await request.form()

    if settings.sendgrid_webhook_public_key:
        _verify_sendgrid_signature(request)

    attachments = await _extract_form_attachments(form)
    return await service.process_inbound(_sendgrid_form_to_raw(form, attachments))


# ---------------------------------------------------------------------------
# Inbound webhook — Mailgun Routes
# ---------------------------------------------------------------------------

@router.post(
    "/inbound/mailgun",
    response_model=ProcessingResult,
    summary="Mailgun Routes inbound webhook",
)
async def inbound_mailgun(request: Request) -> ProcessingResult:
    """
    Receives emails forwarded by a Mailgun Route.

    **Mailgun dashboard setup:**
    1. Add and verify your sending domain in Mailgun.
    2. Sending → Routes → Create Route
       - Filter: ``match_recipient("portless-trade@<your-mailgun-domain>")``
       - Action: ``forward("https://api.portless.io/email-processor/inbound/mailgun")``
       - Priority: 10
    3. Settings → API Security → HTTP Webhook Signing Key →
       set ``MAILGUN_SIGNING_KEY`` in .env

    HMAC-SHA256 signature is verified when ``MAILGUN_SIGNING_KEY`` is set.
    """
    form = await request.form()

    if settings.mailgun_signing_key:
        _verify_mailgun_signature(
            signing_key=settings.mailgun_signing_key,
            timestamp=str(form.get("timestamp", "")),
            token=str(form.get("token", "")),
            signature=str(form.get("signature", "")),
        )

    attachments = await _extract_form_attachments(form)
    return await service.process_inbound(_mailgun_form_to_raw(form, attachments))


# ---------------------------------------------------------------------------
# Manual submission
# ---------------------------------------------------------------------------

@router.post(
    "/inbound/manual",
    response_model=ProcessingResult,
    status_code=status.HTTP_201_CREATED,
    summary="Manually submit an inbound email",
)
async def inbound_manual(payload: ManualInboundRequest) -> ProcessingResult:
    """
    Submit an inbound email manually from the PortLess dashboard.

    Use this when:
    - IMAP polling and provider webhooks are not yet configured
    - An email arrived via WhatsApp, phone, or another unmonitored channel
    - The operator needs to replay or correct a previously missed message

    Supply ``negotiation_id`` to skip automatic mapping.
    Supply ``counter_price`` to skip automatic price extraction.
    """
    raw = InboundRawEmail(
        from_address=payload.from_address,
        to_address=settings.portless_trade_address,
        subject=payload.subject,
        body_text=payload.body_text,
        attachments=payload.attachments,
        source=InboundEmailSource.MANUAL,
        received_at=datetime.now(timezone.utc),
    )
    return await service.process_inbound(
        raw,
        negotiation_id_override=payload.negotiation_id,
        party_override=payload.party,
        counter_price_override=payload.counter_price,
    )


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------

@router.get("/inbound", response_model=list[InboundEmailRecord])
async def list_inbound(
    status: InboundEmailStatus | None = None,
    negotiation_id: str | None = None,
    limit: int = 100,
) -> list[InboundEmailRecord]:
    """List inbound emails, optionally filtered by status or negotiation."""
    return await service.list_inbound(
        status=status, negotiation_id=negotiation_id, limit=limit
    )


@router.get("/inbound/{inbound_id}", response_model=InboundEmailRecord)
async def get_inbound(inbound_id: str) -> InboundEmailRecord:
    """Fetch a single inbound email record by its inbound_id."""
    try:
        return await service.get_inbound(inbound_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def _verify_mailgun_signature(
    signing_key: str,
    timestamp: str,
    token: str,
    signature: str,
) -> None:
    """Verify Mailgun HMAC-SHA256 webhook signature.

    Mailgun formula: ``HMAC-SHA256(signing_key, timestamp + token) == signature``
    """
    if not timestamp or not token or not signature:
        raise HTTPException(status_code=400, detail="Missing Mailgun signature fields")

    expected = hmac.new(
        signing_key.encode("utf-8"),
        msg=(timestamp + token).encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        logger.warning("Mailgun webhook: signature mismatch — request rejected")
        raise HTTPException(status_code=403, detail="Invalid Mailgun webhook signature")


def _verify_sendgrid_signature(request: Request) -> None:
    """
    Placeholder for SendGrid ECDSA webhook signature verification.

    To implement, install ``ecdsa`` (``pip install ecdsa``) and fill in::

        from ecdsa import VerifyingKey, NIST256p, BadSignatureError
        import base64

        vk = VerifyingKey.from_pem(base64.b64decode(settings.sendgrid_webhook_public_key))
        ts = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")
        sig_b64 = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
        body = await request.body()
        try:
            vk.verify(base64.b64decode(sig_b64), (ts + body.decode()).encode())
        except BadSignatureError:
            raise HTTPException(status_code=403, detail="Invalid SendGrid signature")
    """
    logger.debug("SendGrid ECDSA verification: placeholder — not yet implemented")


# ---------------------------------------------------------------------------
# Payload normalisation helpers
# ---------------------------------------------------------------------------

async def _extract_form_attachments(form) -> list[EmailAttachment]:  # type: ignore[type-arg]
    """Read every uploaded file part from a provider webhook multipart form.

    SendGrid posts attachments as ``attachment1``, ``attachment2``, … file
    parts; Mailgun as ``attachment-1``, ``attachment-2``, …. Both arrive as
    Starlette ``UploadFile`` values, so a generic scan captures either.
    """
    attachments: list[EmailAttachment] = []
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            data = await value.read()
            attachments.append(
                EmailAttachment(
                    filename=value.filename or key,
                    content_type=value.content_type or "application/octet-stream",
                    size=len(data),
                    content_b64=base64.b64encode(data).decode("ascii"),
                )
            )
    return attachments


def _parse_raw_headers(raw_block: str) -> tuple[str, str, list[str]]:
    """Parse a raw SMTP header block → (message_id, in_reply_to, references)."""
    message_id, in_reply_to = "", ""
    references: list[str] = []
    for line in raw_block.splitlines():
        lower = line.lower()
        if lower.startswith("message-id:"):
            message_id = line.split(":", 1)[1].strip()
        elif lower.startswith("in-reply-to:"):
            in_reply_to = line.split(":", 1)[1].strip()
        elif lower.startswith("references:"):
            refs_raw = line.split(":", 1)[1].strip()
            references = [r.strip() for r in refs_raw.split() if r.strip().startswith("<")]
    return message_id, in_reply_to, references


def _sendgrid_form_to_raw(
    form, attachments: list[EmailAttachment] | None = None
) -> InboundRawEmail:  # type: ignore[type-arg]
    from email.utils import parseaddr

    msg_id, in_reply_to, refs = _parse_raw_headers(str(form.get("headers", "")))
    _, from_addr = parseaddr(str(form.get("from", "")))
    return InboundRawEmail(
        from_address=from_addr or str(form.get("from", "")),
        to_address=str(form.get("to", settings.portless_trade_address)),
        subject=str(form.get("subject", "")),
        body_text=str(form.get("text", "")),
        body_html=str(form.get("html", "")),
        message_id=msg_id,
        in_reply_to=in_reply_to,
        references=refs,
        attachments=attachments or [],
        source=InboundEmailSource.SENDGRID_WEBHOOK,
        received_at=datetime.now(timezone.utc),
        raw_headers={"raw_block": str(form.get("headers", ""))},
    )


def _mailgun_form_to_raw(
    form, attachments: list[EmailAttachment] | None = None
) -> InboundRawEmail:  # type: ignore[type-arg]
    from email.utils import parseaddr

    # message-headers is a JSON array of [name, value] pairs
    headers_dict: dict[str, str] = {}
    try:
        for pair in json.loads(str(form.get("message-headers", "[]"))):
            if isinstance(pair, list) and len(pair) == 2:
                headers_dict[pair[0].lower()] = pair[1]
    except Exception:
        pass

    _, from_addr = parseaddr(str(form.get("From", "")))
    refs_raw = headers_dict.get("references", "")
    references = [r.strip() for r in refs_raw.split() if r.strip().startswith("<")]

    return InboundRawEmail(
        from_address=from_addr or str(form.get("From", "")),
        to_address=str(form.get("To", settings.portless_trade_address)),
        subject=str(form.get("Subject", "")),
        body_text=str(form.get("body-plain", "")),
        body_html=str(form.get("body-html", "")),
        message_id=headers_dict.get("message-id", ""),
        in_reply_to=headers_dict.get("in-reply-to", ""),
        references=references,
        attachments=attachments or [],
        source=InboundEmailSource.MAILGUN_WEBHOOK,
        received_at=datetime.now(timezone.utc),
        raw_headers=headers_dict,
    )
