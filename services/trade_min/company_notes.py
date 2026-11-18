"""Staff notes and follow-up reminders on a directory company.

A call, a trade-fair meeting, what the buyer said on WhatsApp: staff write it on the company
(``POST /trade-min/{buyers|suppliers}/{id}/notes``, optional ``remind_at``). Each note is kept
on the record (``CounterpartyRecord.notes``, newest last) and sent to knowledge memory
(``memory.remember_note``) so the drafting agents can use it for that company. A reminder that
is due shows in the daily digest until it is marked done
(``POST …/notes/{note_id}/done``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from services.trade_min import directory
from services.trade_min.schemas import CompanyNote, CounterpartyRecord

logger = logging.getLogger(__name__)

MAX_NOTES = 200


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def add(record_id: str, text: str, by: str, remind_at: datetime | None = None) -> CounterpartyRecord | None:
    text = (text or "").strip()
    if not text:
        raise ValueError("the note is empty")
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None:
        return None
    note = CompanyNote(note_id=f"N-{uuid4().hex[:10].upper()}", text=text[:2000], by=by,
                       at=datetime.now(timezone.utc), remind_at=_aware(remind_at))
    notes = [*rec.notes, note][-MAX_NOTES:]
    saved = await store.set_contact_preference(record_id, {"notes": [n.model_dump(mode="json") for n in notes]})
    try:
        from shared.orchestration import memory
        memory.remember_note(subject_type=rec.role, subject_id=rec.name, text=text, author=by,
                             extra={"counterparty_id": rec.id, "website": rec.website})
    except Exception as exc:  # noqa: BLE001 - the note is kept on the record either way
        logger.debug("CompanyNotes: knowledge memory unavailable: %s", exc)
    return saved


async def done(record_id: str, note_id: str, by: str) -> CounterpartyRecord | None:
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None:
        return None
    if not any(n.note_id == note_id for n in rec.notes):
        raise LookupError(f"note {note_id} not on {record_id}")
    now = datetime.now(timezone.utc)
    notes = [n.model_copy(update={"done_at": now, "done_by": by}) if n.note_id == note_id else n for n in rec.notes]
    return await store.set_contact_preference(record_id, {"notes": [n.model_dump(mode="json") for n in notes]})


async def due(now: datetime | None = None, limit: int = 50) -> list[dict]:
    """Open reminders that are due, oldest first (for the digest)."""
    now = now or datetime.now(timezone.utc)
    store = await directory.get_directory()
    out = []
    for role in ("buyer", "supplier"):
        for rec in await store.list(role, limit=5000):
            for n in rec.notes:
                when = _aware(n.remind_at)
                if when and when <= now and n.done_at is None:
                    out.append({"record_id": rec.id, "role": rec.role, "company": rec.name, "note_id": n.note_id,
                                "text": n.text, "remind_at": when.isoformat(), "by": n.by})
    out.sort(key=lambda r: r["remind_at"])
    return out[:limit]
