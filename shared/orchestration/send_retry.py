"""Automatic retry of outreach and RFQ e-mails that failed in transport.

A send that fails in the mail transport ("Connection lost", SMTP refused) is retried on a
back-off schedule — ``SEND_RETRY_MINUTES`` (default ``5,30,120``) counted from the last
failure — before it becomes a ``deal.exception`` (``OUTREACH:send_failed`` /
``RFQ:send_failed``) for the operator. An e-mail that was only *logged* (no outbound mailbox)
is not retried: nothing will change until the mailbox is set up.

- The deal watcher calls ``sweep()`` on every pass (open, unparked prospects only).
- ``retry_now(deal_id)`` — the prospect's "Retry sends" button
  (``POST /deals/{id}/retry-sends``) — re-sends every failed e-mail of the deal at once,
  whatever the schedule says, as long as it has tries left.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

PROSPECT_STATUSES = ("DRAFT", "MATCHING")
_DEFAULT = [5.0, 30.0, 120.0]


def schedule_minutes() -> list[float]:
    raw = os.getenv("SEND_RETRY_MINUTES", "5,30,120")
    try:
        steps = [float(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        return list(_DEFAULT)
    return [s for s in steps if s >= 0] or list(_DEFAULT)


def exhausted(attempts: int) -> bool:
    """``attempts`` sends tried (the first send included) — no tries left."""
    return attempts > len(schedule_minutes())


def due(attempts: int, failed_at: datetime | None, now: datetime) -> bool:
    if attempts < 1 or exhausted(attempts):
        return False
    if failed_at is None:
        return True
    at = failed_at if failed_at.tzinfo else failed_at.replace(tzinfo=timezone.utc)
    return now >= at + timedelta(minutes=schedule_minutes()[attempts - 1])


async def _retry_deal(deal: Any, now: datetime, force: bool) -> dict[str, int]:
    from services.sourcing.service import retry_failed_email_sends  # type: ignore[import]
    from shared.orchestration import outreach
    counts: dict[str, int] = {}
    try:
        for k, v in (await outreach.retry_failed_sends(deal, now=now, force=force)).items():
            counts[f"outreach_{k}"] = v
    except Exception as exc:  # noqa: BLE001
        logger.error("SendRetry: outreach of %s failed: %s", deal.deal_id, exc)
    try:
        for k, v in (await retry_failed_email_sends(deal.deal_id, now=now, force=force)).items():
            counts[f"rfq_{k}"] = v
    except Exception as exc:  # noqa: BLE001
        logger.error("SendRetry: RFQs of %s failed: %s", deal.deal_id, exc)
    return counts


async def sweep(now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    totals: dict[str, int] = {}
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("SendRetry: could not list deals: %s", exc)
        return totals
    from shared.orchestration.prospect_actions import is_parked
    for deal in deals:
        status = str(getattr(getattr(deal, "status", ""), "value", getattr(deal, "status", "")) or "")
        if status not in PROSPECT_STATUSES or is_parked(deal, now):
            continue
        for k, v in (await _retry_deal(deal, now, force=False)).items():
            totals[k] = totals.get(k, 0) + v
    return {k: v for k, v in totals.items() if v}


async def retry_now(deal_id: str) -> dict[str, int]:
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    return await _retry_deal(deal, datetime.now(timezone.utc), force=True)
