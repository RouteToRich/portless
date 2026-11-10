"""Email draft and send history for the Negotiation Agent.

Each outbound email drafted by the LLM and each inbound email received from
buyer/supplier is stored here.  The record progresses through:

  DRAFT → PENDING_APPROVAL  (HITL approval created)
        → SENT              (operator approved, SMTP delivered)
        → MODIFIED_SENT     (operator approved with modified body, SMTP delivered)
        → REJECTED          (operator rejected, not sent)
        → FAILED            (SMTP/delivery error after approval)
        → BLOCKED           (approved, but the send-time safety check refused it:
                             wrong / stale recipient, other party named, thread
                             moved on — see email_safety.preflight)
        → LOGGED_ONLY       (approved, no outbound mailbox configured: composed
                             and logged, NOT delivered)

SKIPPED records a deal notification that could not be drafted at all.

Inbound emails (received from buyer/supplier) start at RECEIVED and are
terminal — they are audit records only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from .config import settings

logger = logging.getLogger(__name__)


class EmailDirection(str, Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class EmailStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    SENT = "sent"
    MODIFIED_SENT = "modified_sent"
    REJECTED = "rejected"
    FAILED = "failed"
    BLOCKED = "blocked"
    """Approved, but the send-time safety check (email_safety.preflight) refused it."""
    RECEIVED = "received"
    LOGGED_ONLY = "logged_only"
    """Approved and composed, but no outbound mailbox is configured — the gateway
    only logged it; the counterparty did NOT receive it."""
    SKIPPED = "skipped"
    """A deal notification that could not even be drafted (no negotiation thread,
    no address on file for that party). Recorded so the console shows the gap."""


ISSUED_STATUSES: tuple[EmailStatus, ...] = (EmailStatus.SENT, EmailStatus.MODIFIED_SENT, EmailStatus.LOGGED_ONLY)
"""Outbound e-mails PortLess issued (delivered, or composed with no mailbox).
Negotiation logic — drafting history, preflight, reply matching — treats them
alike so a dev / automation run without a mailbox behaves as a live one; only
the display tells them apart."""
ISSUED_VALUES: tuple[str, ...] = tuple(s.value for s in ISSUED_STATUSES)


class EmailHistoryRecord(BaseModel):
    email_id: str
    negotiation_id: str
    deal_id: str
    round_no: int
    direction: EmailDirection
    party: str
    to_address: str
    from_address: str
    subject: str
    body: str
    original_body: str | None = None
    """Populated when operator modified the body before sending."""
    approval_id: str | None = None
    status: EmailStatus
    kind: str = "negotiation"
    """``negotiation`` (agent round draft) · ``followup`` · ``notification`` (templated deal e-mail)."""
    notice_kind: str | None = None
    """For ``kind == "notification"``: which deal notice (pi_issued, escrow_funded, qc_passed …)."""
    block_reason: str | None = None
    """Why the send-time safety check refused it (status BLOCKED)."""
    delivery_note: str | None = None
    """Why it did not reach the counterparty: LOGGED_ONLY (no mailbox), SKIPPED
    (no thread / address), FAILED (the transport's error)."""
    sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Repository protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class EmailHistoryRepository(Protocol):
    async def save(self, record: EmailHistoryRecord) -> None: ...
    async def get(self, email_id: str) -> EmailHistoryRecord | None: ...
    async def list_by_negotiation(self, negotiation_id: str) -> list[EmailHistoryRecord]: ...
    async def list_by_deal(self, deal_id: str) -> list[EmailHistoryRecord]: ...


# ---------------------------------------------------------------------------
# In-memory fallback (dev / CI — no MongoDB needed)
# ---------------------------------------------------------------------------

class InMemoryEmailHistoryRepository:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    async def save(self, record: EmailHistoryRecord) -> None:
        self._store[record.email_id] = record.model_dump()

    async def get(self, email_id: str) -> EmailHistoryRecord | None:
        doc = self._store.get(email_id)
        return EmailHistoryRecord(**doc) if doc else None

    async def list_by_negotiation(self, negotiation_id: str) -> list[EmailHistoryRecord]:
        return [
            EmailHistoryRecord(**d)
            for d in self._store.values()
            if d.get("negotiation_id") == negotiation_id
        ]

    async def list_by_deal(self, deal_id: str) -> list[EmailHistoryRecord]:
        return [
            EmailHistoryRecord(**d)
            for d in self._store.values()
            if d.get("deal_id") == deal_id
        ]


# ---------------------------------------------------------------------------
# MongoDB Motor repository
# ---------------------------------------------------------------------------

class MongoEmailHistoryRepository:
    def __init__(self, uri: str, db_name: str, collection: str) -> None:
        import motor.motor_asyncio as motor  # type: ignore[import]

        client: Any = motor.AsyncIOMotorClient(uri)
        self._col = client[db_name][collection]

    async def _ensure_indexes(self) -> None:
        from pymongo import ASCENDING, DESCENDING  # type: ignore[import]

        await self._col.create_index([("email_id", ASCENDING)], unique=True, background=True)
        await self._col.create_index([("negotiation_id", ASCENDING)], background=True)
        await self._col.create_index([("deal_id", ASCENDING)], background=True)
        await self._col.create_index([("created_at", DESCENDING)], background=True)

    async def save(self, record: EmailHistoryRecord) -> None:
        doc = record.model_dump()
        await self._col.replace_one({"email_id": record.email_id}, doc, upsert=True)

    async def get(self, email_id: str) -> EmailHistoryRecord | None:
        doc = await self._col.find_one({"email_id": email_id}, {"_id": 0})
        return EmailHistoryRecord(**doc) if doc else None

    async def list_by_negotiation(self, negotiation_id: str) -> list[EmailHistoryRecord]:
        cursor = self._col.find(
            {"negotiation_id": negotiation_id}, {"_id": 0}
        ).sort("created_at", 1)
        return [EmailHistoryRecord(**d) async for d in cursor]

    async def list_by_deal(self, deal_id: str) -> list[EmailHistoryRecord]:
        cursor = self._col.find(
            {"deal_id": deal_id}, {"_id": 0}
        ).sort("created_at", 1)
        return [EmailHistoryRecord(**d) async for d in cursor]


async def record_deal_email(
    *, deal_id: str, direction: str, party: str, to_address: str, from_address: str, subject: str, body: str,
    kind: str, delivered: bool = True,
) -> None:
    """Put a pre-negotiation e-mail (buyer introduction, RFQ, indicative offer and
    their replies) in the deal's history. These belong to no negotiation yet, so
    they carry an empty negotiation id and the drafting agents never read them.
    Best-effort: the record must never stop the e-mail itself."""
    try:
        from uuid import uuid4

        now = datetime.now(timezone.utc)
        outbound = direction == EmailDirection.OUTBOUND.value
        status = (EmailStatus.SENT if delivered else EmailStatus.LOGGED_ONLY) if outbound else EmailStatus.RECEIVED
        await get_email_repository().save(EmailHistoryRecord(
            email_id=f"EMAIL-{uuid4().hex[:10].upper()}", negotiation_id="", deal_id=deal_id, round_no=0,
            direction=EmailDirection(direction), party=party, to_address=to_address, from_address=from_address,
            subject=subject, body=body, status=status, kind=kind,
            delivery_note=None if delivered or not outbound
            else "No outbound mailbox configured; the e-mail was logged, not delivered.",
            sent_at=now if outbound and delivered else None, created_at=now, updated_at=now,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("E-mail history: could not record a %s e-mail for deal %s: %s", kind, deal_id, exc)


def history_time(record: EmailHistoryRecord) -> datetime:
    """When the e-mail actually went or arrived; a draft counts from when it was written."""
    when = record.sent_at or record.created_at
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Module-level singleton — initialised in main.py on_startup
# ---------------------------------------------------------------------------

_email_repo: EmailHistoryRepository = InMemoryEmailHistoryRepository()


def get_email_repository() -> EmailHistoryRepository:
    return _email_repo


async def init_email_repository() -> None:
    global _email_repo
    uri = settings.negotiation_mongo_uri
    if not uri:
        logger.info("NEGOTIATION_MONGO_URI not set — using in-memory email history repository")
        _email_repo = InMemoryEmailHistoryRepository()
        return
    try:
        repo = MongoEmailHistoryRepository(
            uri=uri,
            db_name=settings.negotiation_mongo_db_name,
            collection="email_history",
        )
        await repo._ensure_indexes()
        _email_repo = repo
        logger.info("MongoEmailHistoryRepository initialised (db=%s)", settings.negotiation_mongo_db_name)
    except Exception as exc:
        logger.error("Mongo email history init failed — falling back to in-memory: %s", exc)
        _email_repo = InMemoryEmailHistoryRepository()
