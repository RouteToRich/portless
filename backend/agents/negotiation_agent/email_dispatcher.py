"""Email dispatch for the Negotiation Agent — Gmail SMTP via aiosmtplib.

Outbound channel
----------------
Uses Gmail SMTP (smtp.gmail.com:587, STARTTLS).
Requires a Gmail App Password — NOT the regular account password.

How to create a Gmail App Password
-----------------------------------
1. Enable 2-Step Verification on the sending Gmail account:
   https://myaccount.google.com/security
2. Go to: https://myaccount.google.com/apppasswords
3. Select app: "Mail" / device: "Other (custom)" → name it "PortLess Negotiation"
4. Copy the 16-character password into GMAIL_APP_PASSWORD in .env

Configuration (.env)
---------------------
  GMAIL_SENDER_ADDRESS=your-gmail@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

Leave GMAIL_SENDER_ADDRESS empty to disable real sending — messages are logged
only, so local dev and CI work without any credentials.

Inbound email monitoring
------------------------
Inbox monitoring has been extracted to services/email_monitor/.
Configure IMAP polling or provider webhooks (SendGrid / Mailgun) there.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import settings

logger = logging.getLogger(__name__)

_GMAIL_SMTP_HOST = "smtp.gmail.com"
_GMAIL_SMTP_PORT = 587  # STARTTLS
_PORTLESS_DOMAIN = "portless.io"


# ---------------------------------------------------------------------------
# Outbound email message
# ---------------------------------------------------------------------------

@dataclass
class EmailMessage:
    to_address: str
    subject: str
    body: str
    negotiation_id: str
    round_no: int
    email_id: str = ""
    """Set to EmailHistoryRecord.email_id so Message-ID is embedded for reply threading."""
    cc: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Gmail SMTP dispatcher
# ---------------------------------------------------------------------------

class EmailDispatcher:
    """Sends negotiation messages via Gmail SMTP (aiosmtplib + STARTTLS).

    Falls back to logging-only mode when GMAIL_SENDER_ADDRESS is not set,
    so the service starts cleanly in dev/CI without any credentials.
    """

    def __init__(self) -> None:
        self._sender = settings.gmail_sender_address
        self._password = settings.gmail_app_password
        self._enabled = bool(self._sender and self._password)
        if not self._enabled:
            logger.info(
                "EmailDispatcher: Gmail not configured "
                "(GMAIL_SENDER_ADDRESS or GMAIL_APP_PASSWORD missing) "
                "— messages will be logged only."
            )

    def _build_mime(self, msg: EmailMessage) -> MIMEMultipart:
        mime = MIMEMultipart("alternative")
        mime["From"] = f"{settings.smtp_from_name} <{self._sender}>"
        mime["To"] = msg.to_address
        # Append [NEG:...] token so the Email Monitor can map replies via
        # subject-line fallback when In-Reply-To/References headers are lost.
        subject = msg.subject
        if msg.negotiation_id:
            subject += f" [NEG:{msg.negotiation_id}]"
        mime["Subject"] = subject
        # Embed a structured Message-ID so genuine replies carry In-Reply-To
        # that the Email Monitor mapper can parse unambiguously.
        if msg.email_id and msg.negotiation_id:
            mime["Message-ID"] = f"<{msg.email_id}.{msg.negotiation_id}@{_PORTLESS_DOMAIN}>"
        if msg.cc:
            mime["Cc"] = ", ".join(msg.cc)
        mime.attach(MIMEText(msg.body, "plain", "utf-8"))
        return mime

    async def send(self, msg: EmailMessage) -> None:
        if not self._enabled:
            logger.info(
                "[EMAIL LOG] negotiation_id=%s round=%d to=%s\nSubject: %s\n---\n%s\n---",
                msg.negotiation_id,
                msg.round_no,
                msg.to_address,
                msg.subject,
                msg.body,
            )
            return

        try:
            import aiosmtplib  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "aiosmtplib is required for Gmail email dispatch. "
                "Run: pip install aiosmtplib"
            ) from exc

        mime = self._build_mime(msg)
        try:
            await aiosmtplib.send(
                mime,
                hostname=_GMAIL_SMTP_HOST,
                port=_GMAIL_SMTP_PORT,
                username=self._sender,
                password=self._password,
                start_tls=True,
            )
            logger.info(
                "Email sent via Gmail: negotiation_id=%s round=%d to=%s",
                msg.negotiation_id,
                msg.round_no,
                msg.to_address,
            )
        except Exception as exc:
            logger.error(
                "Gmail send failed for negotiation_id=%s round=%d to=%s: %s",
                msg.negotiation_id,
                msg.round_no,
                msg.to_address,
                exc,
            )
            raise


# InboxMonitor has been extracted to services/email_monitor/.
# See services/email_monitor/imap_poller.py (ImapPoller) and
# services/email_monitor/router.py for webhook endpoints.
