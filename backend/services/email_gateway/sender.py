"""Outbound SMTP transport for the Email Gateway.

Sends one email at a time via Gmail SMTP (aiosmtplib + STARTTLS). Falls back to
log-only mode when GMAIL_SENDER_ADDRESS / GMAIL_APP_PASSWORD are unset, so the
service starts cleanly in dev/CI without credentials.

Knows nothing about negotiations, HITL, or email history — the negotiation flow
owns *what* and *when* to send and calls ``send_email()`` (in-process) or
``POST /email-gateway/send`` (over HTTP) to send.
"""
from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import settings
from .schemas import SendEmailRequest, SendEmailResult, SendOutcome

logger = logging.getLogger(__name__)


class EmailSendError(RuntimeError):
    """Raised when a configured SMTP send fails. The router maps this to 502;
    in-process callers catch it to mark the email FAILED."""


def _build_message_id(email_id: str, negotiation_id: str) -> str:
    if email_id and negotiation_id:
        return f"<{email_id}.{negotiation_id}@{settings.portless_email_domain}>"
    return ""


def _build_mime(req: SendEmailRequest, message_id: str) -> MIMEMultipart:
    mime = MIMEMultipart("alternative")
    mime["From"] = f"{settings.smtp_from_name} <{settings.gmail_sender_address}>"
    mime["To"] = req.to_address
    # Append [NEG:...] so the Email Processor can map replies via subject-line
    # fallback when In-Reply-To / References headers are stripped by the client.
    subject = req.subject
    if req.negotiation_id:
        subject += f" [NEG:{req.negotiation_id}]"
    mime["Subject"] = subject
    if message_id:
        mime["Message-ID"] = message_id
    if req.cc:
        mime["Cc"] = ", ".join(req.cc)
    mime.attach(MIMEText(req.body, "plain", "utf-8"))
    return mime


async def send_email(req: SendEmailRequest) -> SendEmailResult:
    """Transport one email. Returns SENT (delivered) or LOGGED (no creds).

    Raises EmailSendError when credentials are configured but the SMTP send
    fails, so the caller can record a FAILED status.
    """
    message_id = _build_message_id(req.email_id, req.negotiation_id)
    enabled = bool(settings.gmail_sender_address and settings.gmail_app_password)

    if not enabled:
        logger.info(
            "[EMAIL LOG] negotiation_id=%s round=%d to=%s\nSubject: %s\n---\n%s\n---",
            req.negotiation_id, req.round_no, req.to_address, req.subject, req.body,
        )
        return SendEmailResult(
            outcome=SendOutcome.LOGGED,
            message_id=message_id,
            detail="Gmail not configured — message logged only.",
        )

    try:
        import aiosmtplib  # type: ignore[import]
    except ImportError as exc:
        raise EmailSendError(
            "aiosmtplib is required for SMTP dispatch. Run: pip install aiosmtplib"
        ) from exc

    mime = _build_mime(req, message_id)
    try:
        await aiosmtplib.send(
            mime,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.gmail_sender_address,
            password=settings.gmail_app_password,
            start_tls=True,
        )
    except Exception as exc:
        logger.error(
            "SMTP send failed for negotiation_id=%s round=%d to=%s: %s",
            req.negotiation_id, req.round_no, req.to_address, exc,
        )
        raise EmailSendError(str(exc)) from exc

    logger.info(
        "Email sent via Gmail: negotiation_id=%s round=%d to=%s",
        req.negotiation_id, req.round_no, req.to_address,
    )
    return SendEmailResult(outcome=SendOutcome.SENT, message_id=message_id)
