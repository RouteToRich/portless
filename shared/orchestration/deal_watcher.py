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
  PO_ISSUED       the supplier never acknowledged the Purchase Order / QC was
                  never booked → supplier reminder every
                  ``DEAL_PO_ACK_REMINDER_AFTER_HOURS`` (max ``DEAL_REMINDER_MAX``)
  SUPPLIER_DISPATCH_READY  the inspection never produced a result → supplier
                  readiness reminder every ``DEAL_QC_READINESS_REMINDER_AFTER_HOURS``
                  (max ``DEAL_REMINDER_MAX``), then ``deal.exception``
  IN_TRANSIT      carrier-tracking poll every ``TRACKING_POLL_HOURS`` (the
                  ``apify_carrier_tracking`` actor; no-op while it is a stub)
  CLEARED         no delivery confirmation ``INSPECTION_WINDOW_FALLBACK_DAYS``
                  after clearance → the buyer's acceptance window is opened
                  anyway (the money chain must not wait forever)
  MATCHING /      buyer outreach follow-ups (``shared/orchestration/outreach``):
  NEGOTIATING     one follow-up after ``OUTREACH_FOLLOWUP_DAYS``, next candidate
                  after ``OUTREACH_NO_REPLY_DAYS`` more
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

_last_tracking_poll: dict[str, datetime] = {}
"""deal_id → last carrier-tracking poll (in-process)."""


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


async def _sweep_supplier_wait(
    deal: Any, status: str, now: datetime, counts: dict[str, int],
    *, every_var: str, every_default: float, kind: str, notify,
) -> None:
    """Supplier-side waits: remind the supplier on a cadence, then raise."""
    since = _aware(deal.updated_at) or now
    every = _hours(every_var, every_default)
    due = int((now - since).total_seconds() // (every * 3600)) if every > 0 else 0
    if 1 <= due <= _reminder_max():
        if await notify(deal, due):
            counts["reminders"] += 1
    elif due > _reminder_max():
        if await _emit_exception(
            deal.deal_id, f"{status}:{kind}",
            f"supplier silent in {status} for {(now - since).days} day(s) after {_reminder_max()} reminder(s)",
            {"status": status, "since": since.isoformat()},
        ):
            counts["exceptions"] += 1


async def _sweep_in_transit(deal: Any, now: datetime, counts: dict[str, int]) -> None:
    """Poll the carrier for the container; the generic stall SLA still applies."""
    every = _hours("TRACKING_POLL_HOURS", 12.0)
    last = _last_tracking_poll.get(deal.deal_id)
    if last is None or now - last >= timedelta(hours=every):
        _last_tracking_poll[deal.deal_id] = now
        try:
            from services.shield_link.service import poll_tracking  # type: ignore[import]
            result = await poll_tracking(deal.deal_id)
            if result.get("status") == "ok":
                counts["tracking_updates"] = counts.get("tracking_updates", 0) + 1
                logger.info("DealWatcher: tracking → %s for deal %s", result.get("milestone"), deal.deal_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DealWatcher: tracking poll failed for %s: %s", deal.deal_id, exc)
    await _sweep_generic(deal, "IN_TRANSIT", now, counts)


async def _sweep_cleared(deal: Any, now: datetime, counts: dict[str, int]) -> None:
    """Delivery never confirmed: open the acceptance window after the fallback
    period so the final tranche and the supplier's balance are not stuck."""
    try:
        from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
        hold = await _eg_repo().get_hold_by_deal(deal.deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DealWatcher: escrow lookup failed for %s: %s", deal.deal_id, exc)
        hold = None
    if hold is None or hold.status not in ("FUNDED", "RELEASED"):
        await _sweep_generic(deal, "CLEARED", now, counts)
        return
    pending_final = [t for t in (hold.tranches or []) if t.trigger == "ACCEPTED" and t.status == "PENDING"]
    if hold.inspection_deadline is None and pending_final:
        since = _aware(deal.updated_at)
        days = _hours("INSPECTION_WINDOW_FALLBACK_DAYS", 21.0)
        if since is not None and now - since >= timedelta(days=days):
            try:
                from services.escrow_guard.service import open_inspection_window  # type: ignore[import]
                await open_inspection_window(
                    hold.escrow_id, trigger="fallback",
                    reason=f"no delivery confirmation {int(days)} days after customs clearance", now=now,
                )
                counts["window_fallbacks"] = counts.get("window_fallbacks", 0) + 1
                logger.warning("DealWatcher: inspection window opened by fallback for deal %s", deal.deal_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("DealWatcher: window fallback failed for %s: %s", deal.deal_id, exc)
        return  # the escrow timeline owns this deal until the window closes
    if hold.inspection_deadline is not None and pending_final:
        return  # window running — the escrow watcher auto-releases on lapse
    await _sweep_generic(deal, "CLEARED", now, counts)


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
    from shared.orchestration import kill_switch
    if await kill_switch.is_paused():
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
            elif status == "PO_ISSUED":
                from shared.orchestration import notifications
                await _sweep_supplier_wait(
                    deal, status, now, counts, every_var="DEAL_PO_ACK_REMINDER_AFTER_HOURS",
                    every_default=48.0, kind="po_unacknowledged", notify=notifications.notify_po_reminder,
                )
            elif status == "SUPPLIER_DISPATCH_READY":
                from shared.orchestration import notifications
                await _sweep_supplier_wait(
                    deal, status, now, counts, every_var="DEAL_QC_READINESS_REMINDER_AFTER_HOURS",
                    every_default=72.0, kind="inspection_overdue",
                    notify=notifications.notify_qc_readiness_reminder,
                )
            elif status == "IN_TRANSIT":
                await _sweep_in_transit(deal, now, counts)
            elif status == "CLEARED":
                await _sweep_cleared(deal, now, counts)
            elif status == "MATCHING" and _outreach_owns(deal):
                continue  # the buyer funnel's own follow-up cadence applies
            else:
                await _sweep_generic(deal, status, now, counts)
        except Exception as exc:  # noqa: BLE001 — one bad deal must not stop the sweep
            logger.error("DealWatcher: sweep failed for deal %s (%s): %s", deal.deal_id, status, exc)

    # Buyer outreach follow-ups (MATCHING / NEGOTIATING deals waiting for a buyer).
    try:
        from shared.orchestration import outreach
        oc = await outreach.sweep_followups(now)
        for key in ("followups", "no_reply", "advanced"):
            if oc.get(key):
                counts[f"outreach_{key}"] = oc[key]
    except Exception as exc:  # noqa: BLE001
        logger.error("DealWatcher: outreach follow-ups failed: %s", exc)

    if any(v for k, v in counts.items() if k != "scanned"):
        logger.info("DealWatcher: %s", counts)
    return counts


def _outreach_owns(deal: Any) -> bool:
    try:
        from shared.orchestration import outreach
        return outreach.waiting_for_buyer(deal)
    except Exception:  # noqa: BLE001
        return False


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
    _last_tracking_poll.clear()
