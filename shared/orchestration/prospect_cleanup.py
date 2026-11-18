"""Close prospects that never converted.

A prospect (DRAFT / MATCHING) untouched for ``PROSPECT_RETENTION_DAYS`` (60) — no status
change, no reply, no quote, nothing — clutters the desk and keeps reminders alive. The
clean-up drops it the same way staff would (``prospect_actions.drop``: CANCELLED with the
reason, its open prospect gates closed). Parked prospects are left alone while the park runs.

- ``GET /deals/prospects/cleanup`` (staff) lists what would be closed;
- ``POST /deals/prospects/cleanup`` (staff) closes them;
- with ``PROSPECT_CLEANUP_ENABLED=true`` the deal watcher runs it once a day.

Records are kept (cancelled, not deleted): the outcome history and the directory stay intact.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_last_run: dict[str, str] = {}


def retention_days() -> int:
    try:
        return max(7, int(os.getenv("PROSPECT_RETENTION_DAYS", "60")))
    except ValueError:
        return 60


def enabled() -> bool:
    return os.getenv("PROSPECT_CLEANUP_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _last_activity(deal: Any) -> datetime | None:
    times = [_aware(getattr(deal, "updated_at", None)), _aware(getattr(deal, "created_at", None))]
    for c in getattr(deal, "buyer_candidates", None) or []:
        times += [_aware(getattr(c, "replied_at", None)), _aware(getattr(c, "contacted_at", None))]
    for o in getattr(deal, "supplier_offers", None) or []:
        times.append(_aware(getattr(o, "received_at", None)))
    times = [t for t in times if t]
    return max(times) if times else None


async def stale(now: datetime | None = None) -> list[dict[str, Any]]:
    from services.deals.repository import get_repository  # type: ignore[import]
    from shared.orchestration.prospect_actions import PROSPECT_STATUSES, is_parked
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=retention_days())
    out = []
    for d in await get_repository().list_active(limit=5000):
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status not in PROSPECT_STATUSES or is_parked(d, now):
            continue
        last = _last_activity(d)
        if last is not None and last < cut:
            out.append({"deal_id": d.deal_id, "hs_code": d.hs_code, "market": d.market,
                        "last_activity_at": last.isoformat(), "idle_days": (now - last).days})
    return sorted(out, key=lambda r: -r["idle_days"])


async def run(now: datetime | None = None, by: str = "system:cleanup") -> dict[str, Any]:
    from shared.orchestration import prospect_actions
    closed, failed = [], []
    for row in await stale(now):
        try:
            await prospect_actions.drop(row["deal_id"], f"no activity for {row['idle_days']} days — closed by the "
                                        f"prospect clean-up (PROSPECT_RETENTION_DAYS={retention_days()})", by)
            closed.append(row["deal_id"])
        except Exception as exc:  # noqa: BLE001 - one prospect never stops the rest
            logger.warning("ProspectCleanup: %s not closed: %s", row["deal_id"], exc)
            failed.append(row["deal_id"])
    if closed:
        logger.info("ProspectCleanup: closed %d idle prospect(s): %s", len(closed), closed)
    return {"closed": closed, "failed": failed, "retention_days": retention_days()}


async def daily(now: datetime | None = None) -> dict[str, Any] | None:
    """The deal watcher's hook: once per UTC day, only when enabled."""
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    if _last_run.get("day") == day:
        return None
    _last_run["day"] = day
    return await run(now)
