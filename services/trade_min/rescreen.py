"""Re-screen counterparties PortLess is working with when their check gets old.

Sanctions lists change. A buyer or supplier verified at scan time keeps that result until a
new scan happens to find it again — which may be never for a company already in a prospect
or deal. This job re-runs the check (sanctions, e-mail, GST / IEC) for every **verified**
directory company that is linked to an **open** prospect or deal and whose result is older
than ``TRADE_MIN_VALIDATION_MAX_AGE_DAYS``.

- Runs daily from the trade-min scheduler (``scheduler_rescreen_cron``, 03:30 UTC) and on
  demand (``POST /trade-min/screening/rescreen``, staff).
- At most ``TRADE_MIN_RESCREEN_MAX_PER_RUN`` companies per run, oldest result first (each
  check is a paid Apify run).
- Without an Apify token nothing runs: a check that could not run must never turn a
  verified company into an unverified one.
- A company that was verified and is now ``failed`` raises ``deal.exception``
  (``SCREENING:rescreen_failed``) on each open prospect / deal it is part of; ``review``
  lands in the screening review queue (screening_review.py).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from services.trade_min import directory, relationships, validation
from services.trade_min.config import settings
from services.trade_min.schemas import CounterpartyRecord

logger = logging.getLogger(__name__)

TERMINAL = {"PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED"}


def max_per_run() -> int:
    try:
        return max(0, int(os.getenv("TRADE_MIN_RESCREEN_MAX_PER_RUN", "20")))
    except ValueError:
        return 20


def _screening_available() -> bool:
    conn = validation._connector("apify_counterparty_check")
    return bool(conn is not None and getattr(conn, "configured", False))


def _linked_deals(rec: CounterpartyRecord, index: dict[str, dict[str, Any]]) -> set[str]:
    e = next((index[f"{rec.role}|{k}"] for k in relationships.record_keys(rec) if f"{rec.role}|{k}" in index), None)
    if e is None:
        return set()
    return set(e["prospects"]) | set(e["deals_active"])


async def due(now: datetime | None = None) -> list[tuple[CounterpartyRecord, set[str]]]:
    """(record, open deal ids) for every verified company linked to an open prospect / deal
    whose check is older than the max age — oldest check first."""
    now = now or datetime.now(timezone.utc)
    deals = [d for d in await relationships._deals() if relationships._status(d) not in TERMINAL]
    if not deals:
        return []
    index = relationships.build_index(deals)
    store = await directory.get_directory()
    out: list[tuple[CounterpartyRecord, set[str]]] = []
    for role in ("buyer", "supplier"):
        for rec in await store.list(role, limit=5000, validation="verified"):
            if validation.is_fresh(rec.validation, now):
                continue
            linked = _linked_deals(rec, index)
            if linked:
                out.append((rec, linked))
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def checked(item: tuple[CounterpartyRecord, set[str]]) -> datetime:
        at = item[0].validation.checked_at if item[0].validation else None
        return epoch if at is None else (at if at.tzinfo else at.replace(tzinfo=timezone.utc))

    out.sort(key=checked)
    return out


async def _raise(deal_id: str, rec: CounterpartyRecord, reasons: list[str]) -> None:
    try:
        from shared.bus import get_publisher
        from shared.domain.events import DomainEvent, EventTopic
        await get_publisher().publish(DomainEvent(
            topic=EventTopic.DEAL_EXCEPTION, deal_id=deal_id,
            payload={
                "source": "trade_min.rescreen", "stage": "SCREENING:rescreen_failed",
                "error": f"{rec.role} {rec.name} was verified but now fails its check: " + "; ".join(reasons),
                "action_required": "review_counterparty_before_any_further_step",
                "counterparty_id": rec.id, "role": rec.role,
            },
        ))
    except Exception as exc:  # noqa: BLE001
        logger.error("Rescreen: exception for deal %s not raised: %s", deal_id, exc)


async def run(now: datetime | None = None) -> dict[str, Any]:
    """Re-check what is due. Never raises; returns what happened."""
    summary: dict[str, Any] = {"due": 0, "checked": 0, "still_verified": 0, "now_review": 0,
                               "now_failed": 0, "skipped": None, "failed_ids": []}
    if not settings.trade_min_validation_enabled:
        summary["skipped"] = "validation is switched off (TRADE_MIN_VALIDATION_ENABLED)"
        return summary
    if not _screening_available():
        summary["skipped"] = "no Apify token — a check that cannot run never downgrades a verified company"
        return summary
    try:
        items = await due(now)
    except Exception as exc:  # noqa: BLE001
        logger.error("Rescreen: could not list what is due: %s", exc)
        summary["skipped"] = f"listing failed: {exc}"
        return summary
    summary["due"] = len(items)
    store = await directory.get_directory()
    for rec, linked in items[:max_per_run()]:
        try:
            checked = await validation.validate_record(rec.model_copy(deep=True))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rescreen: %s %s not checked: %s", rec.role, rec.name, exc)
            continue
        v = checked.validation
        if v is None or v.sanctions == "unchecked":
            logger.warning("Rescreen: %s %s — screening did not answer; the old result is kept", rec.role, rec.name)
            continue
        await store.set_validation(checked)
        summary["checked"] += 1
        if v.status == "verified":
            summary["still_verified"] += 1
        elif v.status == "failed":
            summary["now_failed"] += 1
            summary["failed_ids"].append(rec.id)
            logger.warning("Rescreen: %s %s was verified, now FAILED (%s) — open deals %s",
                           rec.role, rec.name, "; ".join(v.reasons), sorted(linked))
            for deal_id in sorted(linked):
                await _raise(deal_id, checked, v.reasons)
        elif v.sanctions == "review":
            summary["now_review"] += 1
    logger.info("Rescreen: %s", {k: v for k, v in summary.items() if k != "failed_ids"})
    return summary
