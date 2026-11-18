"""Sanctions "review" queue: counterparties whose screening could not decide.

The screening actor answers ``review`` for a possible list match or when a sanctions list
was unavailable. Such a company stays ``unverified`` — never failed, never verified — until a
person looks at it. Staff list them here (``GET /trade-min/screening/review``) and clear or
fail each one with a reason (``POST /trade-min/{buyers|suppliers}/{id}/screening-review``):

- ``clear`` → sanctions ``clear``; the record is verified once its e-mail is deliverable too.
- ``fail``  → sanctions ``block``; the record is ``failed``: never contacted, never a deal party.

Every decision is kept on the record (``screening_reviews``: who, when, why, what the
screening said, the certificate looked at) and logged. A later re-screen writes a new
result; if it is ``review`` again it comes back to this queue.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord, ScreeningReview
from services.trade_min.validation import decide

logger = logging.getLogger(__name__)

Decision = Literal["clear", "fail"]


class NotInReview(ValueError):
    """The record's screening is not (or no longer) ``review``."""


def in_review(rec: CounterpartyRecord) -> bool:
    return rec.validation is not None and rec.validation.sanctions == "review"


async def queue(role: str | None = None, limit: int = 500) -> list[CounterpartyRecord]:
    """Every directory company whose sanctions screening is ``review``, oldest check first."""
    store = await directory.get_directory()
    roles = [role] if role else ["buyer", "supplier"]
    rows: list[CounterpartyRecord] = []
    for r in roles:
        rows += [rec for rec in await store.list(r, limit=5000) if in_review(rec)]
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def checked(rec: CounterpartyRecord) -> datetime:
        at = rec.validation.checked_at if rec.validation else None
        if at is None:
            return epoch
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)

    rows.sort(key=checked)
    return rows[:limit]


async def resolve(record_id: str, decision: Decision, reason: str, by: str) -> CounterpartyRecord | None:
    """Apply a staff decision to a ``review`` record. None when the record does not exist;
    ``NotInReview`` when its screening is not ``review``."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required")
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None:
        return None
    if not in_review(rec):
        raise NotInReview(f"screening of {rec.name!r} is {rec.validation.sanctions if rec.validation else 'not run'}, not review")
    v = rec.validation
    assert v is not None
    review = ScreeningReview(
        decision=decision, reason=reason[:500], by=by, at=datetime.now(timezone.utc),
        previous_sanctions=v.sanctions, previous_reason=v.sanctions_reason, evidence_url=v.evidence_url,
    )
    updated_v = decide(
        v.model_copy(update={
            "sanctions": "clear" if decision == "clear" else "block",
            "sanctions_reason": f"staff {('cleared' if decision == 'clear' else 'failed')}: {review.reason} (by {by})",
        }),
        role=rec.role, country=rec.country, has_phone=bool(rec.phone),
    )
    reviews = [*rec.screening_reviews, review]
    saved = await store.set_contact_preference(record_id, {
        "validation": updated_v.model_dump(mode="json"),
        "screening_reviews": [r.model_dump(mode="json") for r in reviews],
    })
    from shared import audit_log
    await audit_log.record(f"screening.{decision}", by, record_id,
                           {"company": rec.name, "reason": review.reason, "previous": review.previous_sanctions})
    logger.warning("ScreeningReview: %s %s (%s) %s by %s — %s", rec.role, rec.name, record_id,
                   "CLEARED" if decision == "clear" else "FAILED", by, review.reason)
    return saved
