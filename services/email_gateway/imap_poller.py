"""IMAP inbox poller — async Gmail polling via aioimaplib (inbound transport).

``ImapPoller`` instances run as background asyncio tasks at startup — one per
distinct reply folder (a single shared poller when the buyer and supplier
folders are the same). Each poll cycle scans the last ``IMAP_LOOKBACK_DAYS``
of the folder REGARDLESS of the ``\\Seen`` flag — a Gmail filter's "mark as
read" or a human reading the thread in Gmail must never hide a reply from the
pipeline (UNSEEN-only polling silently skipped exactly those replies). New
messages are identified by Message-ID: an in-memory per-poller cache skips
already-forwarded ids cheaply, and the Email Processor's durable message_id
deduplication is the authoritative guard (covers restarts). The account's own
outbound copies (Gmail labels sent mail into the same folder) are filtered out
by From address. Each new message is parsed into ``InboundRawEmail`` (bodies +
attachments) and forwarded to a callback — the Email Processor's
``process_inbound`` (in-process) or its receiving API over HTTP. The gateway
does not persist or interpret the email; that is the Processor's job.

Poll interval is configurable via ``IMAP_POLL_INTERVAL_SECONDS`` (default 900s =
15 min). If ``IMAP_USER`` is not set, polling is silently disabled and the
gateway still serves outbound sends.
"""
from __future__ import annotations

import asyncio
import base64
import email
import logging
import re
from datetime import datetime, timedelta, timezone
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
        self._forwarded: dict[str, None] = {}
        """Message-IDs already forwarded by THIS poller instance (insertion-
        ordered, pruned at _FORWARDED_CACHE_MAX). Cheap first-line dedup so the
        date-window scan doesn't re-download bodies every cycle; the Email
        Processor's durable message_id dedup is the authoritative guard and
        covers process restarts."""

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
                async for raw_email in self._fetch_new():
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

    _FORWARDED_CACHE_MAX = 2000

    # IMAP SINCE dates use fixed English month abbreviations — never strftime %b,
    # which is locale-dependent.
    _IMAP_MONTHS = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )

    @classmethod
    def _imap_since_date(cls, lookback_days: int) -> str:
        d = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        return f"{d.day:02d}-{cls._IMAP_MONTHS[d.month - 1]}-{d.year}"

    def _own_addresses(self) -> set[str]:
        """The account's own sending identities — their folder copies are our
        outbound mail, never inbound to process."""
        return {
            a.strip().lower()
            for a in (settings.imap_user, settings.gmail_sender_address)
            if a and a.strip()
        }

    def _remember_forwarded(self, message_id: str) -> None:
        self._forwarded[message_id] = None
        if len(self._forwarded) > self._FORWARDED_CACHE_MAX:
            for key in list(self._forwarded)[: self._FORWARDED_CACHE_MAX // 2]:
                del self._forwarded[key]

    @staticmethod
    def _parse_envelope(blob: str) -> tuple[str, str, bool]:
        """(message_id, from_address, seen) from a FLAGS+headers fetch blob."""
        from email.utils import parseaddr

        msg_id = from_addr = ""
        for line in blob.splitlines():
            low = line.lower()
            if low.startswith("message-id:"):
                msg_id = line.split(":", 1)[1].strip()
            elif low.startswith("from:"):
                _, from_addr = parseaddr(line.split(":", 1)[1].strip())
        flags_match = re.search(r"FLAGS \(([^)]*)\)", blob)
        seen = bool(flags_match and "\\Seen" in flags_match.group(1))
        return msg_id, from_addr.lower(), seen

    async def _fetch_new(self) -> AsyncIterator[InboundRawEmail]:
        """Scan the recent window and yield messages not yet forwarded.

        Deliberately SEEN-flag-agnostic: replies routed through a Gmail label
        whose filter marks them read (or read by a human in Gmail) arrive
        already \\Seen, so an UNSEEN-only search skips them forever — exactly
        the failure observed in production. Instead, SEARCH SINCE the lookback
        window and identify new mail by Message-ID (in-memory cache here, the
        Processor's durable message_id dedup as the authoritative guard). A
        cheap headers-first pass skips our own outbound copies and known ids
        without downloading bodies. Messages WITHOUT a Message-ID cannot be
        deduplicated, so only those fall back to the old rule (skip if already
        \\Seen). Forwarded messages are still marked \\Seen for mailbox tidiness.
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

            since = self._imap_since_date(settings.imap_lookback_days)
            status, data = await imap.search("SINCE", since)
            if status != "OK" or not data[0].strip():
                logger.debug(
                    "ImapPoller[%s]: no messages since %s in folder '%s'",
                    self._label, since, self._folder,
                )
                return

            uids = [u.decode() if isinstance(u, bytes) else u for u in data[0].split()]
            own = self._own_addresses()
            new_count = 0
            for uid in uids:
                try:
                    # Headers-first: identify the message before paying for the body.
                    head_status, head_data = await imap.fetch(
                        uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)])"
                    )
                    if head_status != "OK":
                        continue
                    blob = b"".join(
                        p for p in head_data if isinstance(p, (bytes, bytearray))
                    ).decode("utf-8", errors="replace")
                    msg_id, from_addr, seen = self._parse_envelope(blob)

                    if from_addr in own:
                        continue  # our own outbound copy labeled into this folder
                    if msg_id and msg_id in self._forwarded:
                        continue  # already forwarded this session
                    if not msg_id and seen:
                        continue  # no id to dedup on — old UNSEEN rule applies

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
                    if msg_id:
                        self._remember_forwarded(msg_id)
                    # Tidiness only — no longer load-bearing for dedup.
                    await imap.store(uid, "+FLAGS", "(\\Seen)")
                    new_count += 1
                    yield raw_email
                except Exception as exc:
                    logger.error(
                        "ImapPoller[%s]: error processing uid=%s: %s",
                        self._label, uid, exc,
                    )
            if new_count:
                logger.info(
                    "ImapPoller[%s]: forwarded %d new message(s) from '%s' (window since %s)",
                    self._label, new_count, self._folder, since,
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
