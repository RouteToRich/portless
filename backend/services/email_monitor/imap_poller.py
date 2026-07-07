"""IMAP inbox poller — async Gmail polling via aioimaplib.

Requires:
    pip install aioimaplib

Two ``ImapPoller`` instances run as background asyncio tasks at startup —
one for the buyer reply folder and one for the supplier reply folder.
Each poll cycle fetches all UNSEEN messages, marks them SEEN immediately,
parses them into ``InboundRawEmail``, and forwards them to
``service.process_inbound()`` which stores the DB record and triggers the
LangGraph negotiation orchestrator via ``run_round()``.

Poll interval is configurable via ``IMAP_MONITOR_POLL_INTERVAL_SECONDS``
(default: 600 s = 10 min).  Set to a lower value (e.g. 300) for faster
round-trip response in production.

If ``IMAP_MONITOR_USER`` is not set, polling is silently disabled and the
service falls back to webhook-only or manual-submission mode.
"""
from __future__ import annotations

import asyncio
import email
import logging
from datetime import datetime, timezone
from email.header import decode_header
from typing import AsyncIterator

from .config import settings
from .schemas import InboundEmailSource, InboundRawEmail

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Email parsing helpers (complete — no changes needed)
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
        source=InboundEmailSource.IMAP,
        received_at=datetime.now(timezone.utc),
        raw_headers={k: v for k, v in msg.items()},
    )


# ---------------------------------------------------------------------------
# IMAP poller
# ---------------------------------------------------------------------------

class ImapPoller:
    """Polls one Gmail IMAP folder for unseen messages and forwards them
    to a processing callback (typically ``service.process_inbound``).

    Two instances are started at app startup — one for the buyer folder
    and one for the supplier folder (see config.py for Gmail label setup).
    When both folders are the same (default: INBOX) the service-layer
    message_id deduplication ensures each email is processed exactly once.

    Use the factory helpers rather than instantiating directly::

        buyer_poller    = create_buyer_poller(service.process_inbound)
        supplier_poller = create_supplier_poller(service.process_inbound)
        await buyer_poller.start()
        await supplier_poller.start()
        ...
        await buyer_poller.stop()
        await supplier_poller.stop()
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
        """Passed to process_inbound as party_override when the folder is
        dedicated to one party (buyer or supplier)."""
        self._label = label
        self._running = False
        self._task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background polling loop if IMAP credentials are configured."""
        if not settings.imap_monitor_user:
            logger.info(
                "ImapPoller[%s]: IMAP_MONITOR_USER not set — polling disabled. "
                "Set IMAP_MONITOR_USER + IMAP_MONITOR_PASSWORD in .env, "
                "or use webhooks via /email-monitor/inbound/sendgrid or /mailgun.",
                self._label,
            )
            return
        if not settings.imap_monitor_password:
            logger.warning(
                "ImapPoller[%s]: IMAP_MONITOR_USER is set but IMAP_MONITOR_PASSWORD "
                "is empty — polling disabled. Provide a Gmail App Password.",
                self._label,
            )
            return

        logger.info(
            "ImapPoller[%s]: starting — host=%s user=%s folder=%s interval=%ds",
            self._label,
            settings.imap_monitor_host,
            settings.imap_monitor_user,
            self._folder,
            settings.imap_monitor_poll_interval_seconds,
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

            await asyncio.sleep(settings.imap_monitor_poll_interval_seconds)

    async def _fetch_unseen(self) -> AsyncIterator[InboundRawEmail]:
        """Connect to IMAP, fetch UNSEEN messages, mark them SEEN, and yield each.

        Uses aioimaplib for async SSL IMAP4 access (Gmail imap.gmail.com:993).
        Marks each fetched message as \\Seen immediately so the next poll cycle
        does not re-process it.  The caller (service.process_inbound) handles
        message-id deduplication as a second safety net.
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
            host=settings.imap_monitor_host,
            port=settings.imap_monitor_port,
            timeout=_CONNECT_TIMEOUT,
        )
        try:
            await imap.wait_hello_from_server()
            await imap.login(settings.imap_monitor_user, settings.imap_monitor_password)
            await imap.select(self._folder)

            status, data = await imap.search("UNSEEN")
            if status != "OK" or not data[0].strip():
                logger.debug(
                    "ImapPoller[%s]: no unseen messages in folder '%s'",
                    self._label, self._folder,
                )
                return

            uids = data[0].split()
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
                    raw_bytes = msg_data[1]
                    if not isinstance(raw_bytes, bytes):
                        # aioimaplib may return the literal string when the
                        # message body is inline rather than a bytes segment.
                        raw_bytes = str(raw_bytes).encode("utf-8", errors="replace")
                    msg = email.message_from_bytes(raw_bytes)
                    raw_email = _mime_to_raw_email(msg)
                    # Mark SEEN immediately — prevents double-processing on
                    # the next poll even if this process crashes mid-batch.
                    await imap.store(uid, "+FLAGS", "(\\\\Seen)")
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
# Convenience factories — use these instead of instantiating ImapPoller directly
# ---------------------------------------------------------------------------

def create_buyer_poller(callback) -> ImapPoller:  # type: ignore[type-arg]
    """Return an ImapPoller that reads from the buyer reply folder.

    Set IMAP_MONITOR_BUYER_FOLDER in .env to a dedicated Gmail label
    (e.g. PortLess-Buyers) to segregate buyer replies from supplier replies.
    """
    return ImapPoller(
        on_email_received=callback,
        folder=settings.imap_monitor_buyer_folder,
        party_hint="buyer",
        label="buyer",
    )


def create_supplier_poller(callback) -> ImapPoller:  # type: ignore[type-arg]
    """Return an ImapPoller that reads from the supplier reply folder.

    Set IMAP_MONITOR_SUPPLIER_FOLDER in .env to a dedicated Gmail label
    (e.g. PortLess-Suppliers) to segregate supplier replies from buyer replies.
    """
    return ImapPoller(
        on_email_received=callback,
        folder=settings.imap_monitor_supplier_folder,
        party_hint="supplier",
        label="supplier",
    )
