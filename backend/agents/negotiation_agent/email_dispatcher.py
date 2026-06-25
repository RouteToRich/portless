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

IMAP inbox monitoring
---------------------
InboxMonitor is a skeleton — see module-level TODO for implementation notes.
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
        mime["Subject"] = msg.subject
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


# ---------------------------------------------------------------------------
# IMAP inbox monitor (placeholder)
# ---------------------------------------------------------------------------

class InboxMonitor:
    """Polls a Gmail INBOX for buyer/supplier replies.

    Currently a skeleton — implement with aioimaplib when ready.

    TODO (production):
      - Open async IMAP SSL connection to imap.gmail.com:993 using aioimaplib
      - Poll IMAP_INBOX_FOLDER every IMAP_POLL_INTERVAL_SECONDS seconds
      - Match emails by In-Reply-To / References header to negotiation_id
      - Extract counter_price from structured body or route to HITL for parsing
      - Call service.run_round() with extracted RoundRequest
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not settings.imap_host:
            logger.info(
                "InboxMonitor: IMAP_HOST not set — Gmail inbox monitoring disabled."
            )
            return

        logger.info(
            "InboxMonitor: Gmail IMAP configured (host=%s folder=%s interval=%ds) "
            "— polling NOT yet implemented (placeholder).",
            settings.imap_host,
            settings.imap_inbox_folder,
            settings.imap_poll_interval_seconds,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("InboxMonitor stopped.")
