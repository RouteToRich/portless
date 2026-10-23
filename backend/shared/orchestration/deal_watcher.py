"""Deal SLA watcher — keeps a deal from silently dying while it waits on the
counterparty.

The event-driven continuations move a deal forward the moment something
happens. This sweep handles the opposite case — *nothing* happens:

  PI_SENT         the buyer never accepted the Proforma Invoice
                  → reminder emails (templated, policy-gated) every
                    ``DEAL_PI_REMINDER_AFTER_HOURS``, at most ``DEAL_REMINDER_MAX``
                  → once the PI's own ``expires_at`` passes: ``deal.exception``
                    (stage ``PI_SENT:expired``) so the Exception Resolver /
                    operator decides (re-issue, re-negotiate, cancel)
  ESCROW_PENDING  the buyer never funded the escrow
                  → same reminder cadence (``DEAL_ESCROW_REMINDER_AFTER_HOURS``)
                  → after the last reminder window: ``deal.exception``
                    (stage ``ESCROW_PENDING:unfunded``)
  any other       no status change for ``DEAL_STALL_SLA_HOURS`` (default 7 days)
  non-terminal    → ``deal.exception`` (stage ``<STATUS>:stalled``), once per
                    (deal, status)

Reminders are idempotent through the notification email id (kind + deal +
reminder number); exceptions are de-duplicated in-process per (deal, kind) —
after a restart a still-stuck deal may be reported once more, which is the
safe direction. Negotiation-stage stalls are NOT handled here: the
negotiation agent's own stale watcher owns NEGOTIATING (follow-ups, 30-day
fail). Enabled by default; ``DEAL_WATCHER_ENABLED=false`` turns it off.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.domain.events import DomainEvent, EventTopic

logger = logging.getLogger(__name__)

_TERMINAL = {"PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED"}
_NEGOTIATION_OWNED = {"NEGOTIATING"}  # negotiation agent's stale watcher owns these
_SKIP = _TERMINAL | _NEGOTIATION_OWNED | {"DRAFT"}

_raised: set[tuple[str, str]] = set()
"""(deal_id, kind) exceptions already emitted by this process."""


def _flag(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def enabled() -> bool:
    return _flag("DEAL_WATCHER_ENABLED", "true").lower() not in ("0", "false", "no", "off")


def _hours(name: str, default: float) -> float:
    try:
        return float(_flag(name, str(default)))
    except ValueError:
        return default


def interval_seconds() -> float:
    return _hours("DEAL_WATCHER_INTERVAL_SECONDS", 600.0)


def _reminder_max() -> int:
    try:
        return int(_flag("DEAL_REMINDER_MAX", "2"))
    except ValueError:
        return 2


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _emit_exception(deal_id: str, kind: str, detail: str, extra: dict[str, Any]) -> bool:
    key = (deal_id, kind)
    if key in _raised:
        return False
    try:
        from shared.bus import get_publisher
        await get_publisher().publish(DomainEvent(
            topic=EventTopic.DEAL_EXCEPTION,
            deal_id=deal_id,
            payload={
                "source": "deal_watcher",
                "stage": kind,
                "error": detail,
                "action_required": "counterparty_silent_or_deal_stalled",
                **extra,
            },
        ))
        _raised.add(key)
        logger.warning("DealWatcher: %s for deal %s — %s", kind, deal_id, detail)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("DealWatcher: could not emit %s for deal %s: %s", kind, deal_id, exc)
        return False


async def _sweep_pi_sent(deal: Any, now: datetime, counts: dict[str, int]) -> None:
    from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
    from shared.orchestration import notifications

    pi = await _dm_repo().get_pi_by_deal(deal.deal_id)
    if pi is None:
        await _emit_exception(
            deal.deal_id, "PI_SENT:missing_pi",
            "deal is PI_SENT but no Proforma Invoice record exists", {},
        )
        counts["exceptions"] += 1
        return
    expires_at = _aware(getattr(pi, "expires_at", None))
    if expires_at is not None and expires_at <= now:
        if await _emit_exception(
            deal.deal_id, "PI_SENT:expired",
            f"PI {pi.pi_id} expired on {expires_at.date()} without buyer acceptance",
            {"pi_id": pi.pi_id, "expires_at": expires_at.isoformat()},
        ):
            counts["exceptions"] += 1
        return
    issued_at = _aware(getattr(pi, "issued_at", None)) or _aware(deal.updated_at) or now
    every = _hours("DEAL_PI_REMINDER_AFTER_HOURS", 48.0)
    due = int((now - issued_at).total_seconds() // (every * 3600)) if every > 0 else 0
    if 1 <= due <= _reminder_max():
        if await notifications.notify_pi_reminder(deal, pi, due):
            counts["reminders"] += 1


async def _sweep_escrow_pending(deal: Any, now: datetime, counts: dict[str, int]) -> None:
    from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
    from shared.orchestration import notifications

    hold = await _eg_repo().get_hold_by_deal(deal.deal_id)
    if hold is None:
        await _emit_exception(
            deal.deal_id, "ESCROW_PENDING:missing_escrow",
            "deal is ESCROW_PENDING but no escrow record exists", {},
        )
        counts["exceptions"] += 1
        return
    opened_at = _aware(getattr(hold, "created_at", None)) or _aware(deal.updated_at) or now
    every = _hours("DEAL_ESCROW_REMINDER_AFTER_HOURS", 48.0)
    due = int((now - opened_at).total_seconds() // (every * 3600)) if every > 0 else 0
    if 1 <= due <= _reminder_max():
        if await notifications.notify_escrow_reminder(deal, hold, due):
            counts["reminders"] += 1
    elif due > _reminder_max():
        if await _emit_exception(
            deal.deal_id, "ESCROW_PENDING:unfunded",
            f"escrow {hold.escrow_id} ({hold.amount} {hold.currency}) unfunded after "
            f"{_reminder_max()} reminder(s)",
            {"escrow_id": hold.escrow_id, "amount": hold.amount},
        ):
            counts["exceptions"] += 1


async def _sweep_generic(deal: Any, status: str, now: datetime, counts: dict[str, int]) -> None:
    since = _aware(deal.updated_at)
    if since is None:
        return
    sla = _hours("DEAL_STALL_SLA_HOURS", 168.0)
    if now - since >= timedelta(hours=sla):
        if await _emit_exception(
            deal.deal_id, f"{status}:stalled",
            f"deal unchanged in {status} for {(now - since).days} day(s) (SLA {sla:.0f}h)",
            {"status": status, "since": since.isoformat()},
        ):
            counts["exceptions"] += 1


async def sweep_stalled_deals(now: datetime | None = None) -> dict[str, int]:
    """One pass over every non-terminal deal. Returns counters for logging/tests."""
    counts = {"scanned": 0, "reminders": 0, "exceptions": 0}
    if not enabled():
        return counts
    now = now or datetime.now(timezone.utc)
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("DealWatcher: could not list deals: %s", exc)
        return counts

    for deal in deals:
        status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
        if status in _SKIP:
            continue
        counts["scanned"] += 1
        try:
            if status == "PI_SENT":
                await _sweep_pi_sent(deal, now, counts)
            elif status == "ESCROW_PENDING":
                await _sweep_escrow_pending(deal, now, counts)
            else:
                await _sweep_generic(deal, status, now, counts)
        except Exception as exc:  # noqa: BLE001 — one bad deal must not stop the sweep
            logger.error("DealWatcher: sweep failed for deal %s (%s): %s", deal.deal_id, status, exc)
    if counts["reminders"] or counts["exceptions"]:
        logger.info("DealWatcher: %s", counts)
    return counts


async def run_deal_watcher() -> None:
    """Background loop started from the app lifespan; cancelled on shutdown."""
    if not enabled():
        logger.info("DealWatcher: disabled (DEAL_WATCHER_ENABLED=false)")
        return
    logger.info("DealWatcher: started (every %.0fs)", interval_seconds())
    while True:
        try:
            await asyncio.sleep(interval_seconds())
            await sweep_stalled_deals()
        except asyncio.CancelledError:
            logger.info("DealWatcher: stopped")
            return
        except Exception as exc:  # noqa: BLE001 — never let the watcher die
            logger.warning("DealWatcher: tick failed: %s", exc)


def reset_for_tests() -> None:
    _raised.clear()
