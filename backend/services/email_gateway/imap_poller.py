"""IMAP inbox poller — async Gmail polling via aioimaplib (inbound transport).

Two ``ImapPoller`` instances run as background asyncio tasks at startup — one
for the buyer reply folder and one for the supplier reply folder. Each poll
cycle fetches all UNSEEN messages, marks them SEEN immediately, parses them into
``InboundRawEmail`` (bodies + attachments), and forwards each to a callback —
the Email Processor's ``process_inbound`` (in-process) or its receiving API over
HTTP. The gateway does not persist or interpret the email; that is the
Processor's job.

Poll interval is configurable via ``IMAP_POLL_INTERVAL_SECONDS`` (default 900s =
15 min). If ``IMAP_USER`` is not set, polling is silently disabled and the
gateway still serves outbound sends.
"""
from __future__ import annotations

import asyncio
import base64
import email
import logging
from datetime import datetime, timezone
from email.header import decode_header
from typing import AsyncIterator

from services.email_processor.schemas import (
    EmailAttachment,
    InboundEmailSource,
    InboundRawEmail,
)

from .config import settings

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------

def _decode_header_value(raw: str) -> str:
    """Decode RFC 2047-encoded header values (e.g. =?UTF-8?B?...?=)."""
    parts = decode_header(raw)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return " ".join(decoded)


def _extract_bodies(msg: email.message.Message) -> tuple[str, str]:
    """Return (plain_text, html_text) from a MIME message."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                raw = part.get_payload(decode=True)
                if raw:
                    plain = raw.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
            elif ct == "text/html" and not html:
                raw = part.get_payload(decode=True)
                if raw:
                    html = raw.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
    else:
        raw = msg.get_payload(decode=True)
        if raw:
            text = raw.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text
    return plain, html


def _parse_references(raw: str) -> list[str]:
    """Split the References header into individual Message-ID tokens."""
    return [r.strip() for r in raw.split() if r.strip().startswith("<")]


def _extract_attachments(msg: email.message.Message) -> list[EmailAttachment]:
    """Return every attachment part (filename or Content-Disposition: attachment)."""
    attachments: list[EmailAttachment] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get("Content-Disposition") or "").lower()
        if not filename and "attachment" not in disposition:
            continue  # inline body part, not an attachment
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        attachments.append(
            EmailAttachment(
                filename=_decode_header_value(filename) if filename else "attachment",
                content_type=part.get_content_type(),
                size=len(payload),
                content_b64=base64.b64encode(payload).decode("ascii"),
            )
        )
    return attachments


def _mime_to_raw_email(msg: email.message.Message) -> InboundRawEmail:
    """Convert a stdlib email.message.Message into InboundRawEmail."""
    from email.utils import parseaddr

    plain, html = _extract_bodies(msg)
    _, from_addr = parseaddr(msg.get("From", ""))
    _, to_addr = parseaddr(msg.get("To", ""))

    return InboundRawEmail(
        from_address=from_addr,
        to_address=to_addr,
        subject=_decode_header_value(msg.get("Subject", "")),
        body_text=plain,
        body_html=html,
        message_id=msg.get("Message-ID", "").strip(),
        in_reply_to=msg.get("In-Reply-To", "").strip(),
        references=_parse_references(msg.get("References", "")),
        attachments=_extract_attachments(msg),
        source=InboundEmailSource.IMAP,
        received_at=datetime.now(timezone.utc),
        raw_headers={k: v for k, v in msg.items()},
    )


# ---------------------------------------------------------------------------
# IMAP poller
# ---------------------------------------------------------------------------

class ImapPoller:
    """Polls one Gmail IMAP folder for unseen messages and forwards them to a
    processing callback (typically the Email Processor's ``process_inbound``).

    Two instances are started at app startup — one for the buyer folder and one
    for the supplier folder. When both folders are the same (default: INBOX) the
    Processor's message_id deduplication ensures each email is processed once.
    """

    def __init__(
        self,
        on_email_received,  # type: ignore[type-arg]
        folder: str = "INBOX",
        party_hint: str | None = None,
        label: str = "default",
    ) -> None:
        self._callback = on_email_received
        self._folder = folder
        self._party_hint = party_hint
        """Passed to the callback as party_override when the folder is dedicated
        to one party (buyer or supplier)."""
        self._label = label
        self._running = False
        self._task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background polling loop if IMAP credentials are configured."""
        if not settings.imap_user:
            logger.info(
                "ImapPoller[%s]: IMAP_USER not set — polling disabled. "
                "Set IMAP_USER + IMAP_PASSWORD in .env to enable inbound polling.",
                self._label,
            )
            return
        if not settings.imap_password:
            logger.warning(
                "ImapPoller[%s]: IMAP_USER is set but IMAP_PASSWORD is empty — "
                "polling disabled. Provide a Gmail App Password.",
                self._label,
            )
            return

        logger.info(
            "ImapPoller[%s]: starting — host=%s user=%s folder=%s interval=%ds",
            self._label,
            settings.imap_host,
            settings.imap_user,
            self._folder,
            settings.imap_poll_interval_seconds,
        )
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the polling loop and clean up."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ImapPoller[%s] stopped.", self._label)

    # ── Internal loop ──────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        consecutive_errors = 0
        while self._running:
            try:
                async for raw_email in self._fetch_unseen():
                    try:
                        if self._party_hint:
                            await self._callback(raw_email, party_override=self._party_hint)
                        else:
                            await self._callback(raw_email)
                    except Exception as exc:
                        logger.error("ImapPoller[%s] callback failed: %s", self._label, exc)
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                backoff = min(60 * consecutive_errors, 600)
                logger.error(
                    "ImapPoller[%s]: fetch error (attempt %d, retrying in %ds): %s",
                    self._label, consecutive_errors, backoff, exc,
                )
                await asyncio.sleep(backoff)
                continue

            await asyncio.sleep(settings.imap_poll_interval_seconds)

    async def _fetch_unseen(self) -> AsyncIterator[InboundRawEmail]:
        """Connect to IMAP, fetch UNSEEN messages, mark them SEEN, and yield each.

        Uses aioimaplib for async SSL IMAP4 access (Gmail imap.gmail.com:993).
        Marks each fetched message as \\Seen immediately so the next poll cycle
        does not re-process it. The Processor handles message-id deduplication as
        a second safety net.
        """
        try:
            import aioimaplib  # type: ignore[import]
        except ImportError:
            logger.error(
                "ImapPoller[%s]: aioimaplib not installed — run: pip install aioimaplib",
                self._label,
            )
            return

        imap = aioimaplib.IMAP4_SSL(
            host=settings.imap_host,
            port=settings.imap_port,
            timeout=_CONNECT_TIMEOUT,
        )
        try:
            await imap.wait_hello_from_server()
            await imap.login(settings.imap_user, settings.imap_password)
            await imap.select(self._folder)

            status, data = await imap.search("UNSEEN")
            if status != "OK" or not data[0].strip():
                logger.debug(
                    "ImapPoller[%s]: no unseen messages in folder '%s'",
                    self._label, self._folder,
                )
                return

            uids = [u.decode() if isinstance(u, bytes) else u for u in data[0].split()]
            logger.info(
                "ImapPoller[%s]: found %d unseen message(s) in '%s'",
                self._label, len(uids), self._folder,
            )
            for uid in uids:
                try:
                    fetch_status, msg_data = await imap.fetch(uid, "(RFC822)")
                    if fetch_status != "OK" or len(msg_data) < 2:
                        logger.warning(
                            "ImapPoller[%s]: fetch failed for uid=%s status=%s",
                            self._label, uid, fetch_status,
                        )
                        continue
                    raw_bytes = next(
                        (part for part in msg_data if isinstance(part, (bytes, bytearray)) and len(part) > 100),
                        None,
                    )
                    if raw_bytes is None:
                        logger.warning(
                            "ImapPoller[%s]: no message body in fetch response for uid=%s",
                            self._label, uid,
                        )
                        continue
                    msg = email.message_from_bytes(bytes(raw_bytes))
                    raw_email = _mime_to_raw_email(msg)
                    # Mark SEEN immediately — prevents double-processing on the
                    # next poll even if this process crashes mid-batch.
                    await imap.store(uid, "+FLAGS", "(\\Seen)")
                    yield raw_email
                except Exception as exc:
                    logger.error(
                        "ImapPoller[%s]: error processing uid=%s: %s",
                        self._label, uid, exc,
                    )
        finally:
            try:
                await imap.logout()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Convenience factories
# ---------------------------------------------------------------------------

def create_buyer_poller(callback) -> ImapPoller:  # type: ignore[type-arg]
    """Return an ImapPoller reading from the buyer reply folder (IMAP_BUYER_FOLDER)."""
    return ImapPoller(
        on_email_received=callback,
        folder=settings.imap_buyer_folder,
        party_hint="buyer",
        label="buyer",
    )


def create_supplier_poller(callback) -> ImapPoller:  # type: ignore[type-arg]
    """Return an ImapPoller reading from the supplier reply folder (IMAP_SUPPLIER_FOLDER)."""
    return ImapPoller(
        on_email_received=callback,
        folder=settings.imap_supplier_folder,
        party_hint="supplier",
        label="supplier",
    )
