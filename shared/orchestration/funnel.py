"""Funnel analytics: scans → prospects → interested → matched → PI → paid.

For deals created in the last ``days`` (default 90): how many reached each step, the
conversion from the step before, the median days from the deal's start to the step, and the
cost spent (Apify scan cost of the opportunities the prospects came from; every e-mail sent).
``GET /deals/funnel?days=90``. Read-only, never raises — a source that is down shows as 0.

Steps (a deal counts in a step once it got there, whatever happened later):
- **opportunities** — opportunities the radar found in the window (the scans' output);
- **prospects** — deals created in the window;
- **interested** — a buyer said yes (candidate interested / selected) or the deal moved past matching;
- **matched** — NEGOTIATING or later (a buyer × supplier pair was confirmed);
- **pi** — PI_SENT or later;
- **paid** — PAID.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_ORDER = ["DRAFT", "MATCHING", "NEGOTIATING", "NEGOTIATION_AGREED", "PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING",
          "ESCROW_FUNDED", "PO_ISSUED", "SUPPLIER_DISPATCH_READY", "QC_PASSED", "IN_TRANSIT", "CLEARED", "PAID"]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _reached(deal: Any, status: str) -> datetime | None:
    """When the deal first reached ``status`` or any later step of the happy path."""
    later = set(_ORDER[_ORDER.index(status):])
    times = [_aware(h.transitioned_at) for h in getattr(deal, "status_history", None) or []
             if str(getattr(h.status, "value", h.status)) in later]
    times = [t for t in times if t]
    if times:
        return min(times)
    current = str(getattr(getattr(deal, "status", ""), "value", getattr(deal, "status", "")))
    return _aware(getattr(deal, "updated_at", None)) if current in later else None


def _interested_at(deal: Any) -> datetime | None:
    times = [_aware(getattr(c, "replied_at", None)) for c in getattr(deal, "buyer_candidates", None) or []
             if getattr(c, "status", "") in ("interested", "selected")]
    times = [t for t in times if t]
    nego = _reached(deal, "NEGOTIATING")
    return min(times + ([nego] if nego else [])) if (times or nego) else None


def _median_days(starts_ends: list[tuple[datetime, datetime]]) -> float | None:
    days = [(e - s).total_seconds() / 86400 for s, e in starts_ends if s and e and e >= s]
    return round(statistics.median(days), 1) if days else None


async def build(days: int = 90, now: datetime | None = None, *, org_id: str | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    deals: list[Any] = []
    try:
        from services.deals.service import list_deals  # type: ignore[import]
        deals = [d for d in await list_deals(limit=5000, org_id=org_id)
                 if (_aware(getattr(d, "created_at", None)) or now) >= since]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Funnel: deals unavailable: %s", exc)

    opportunities, scan_cost = 0, 0.0
    try:
        from services.trade_min.repository import get_repository as trade_repo  # type: ignore[import]
        for o in await (await trade_repo()).list_active(limit=5000, min_rank=0.0):
            if (_aware(getattr(o, "scanned_at", None)) or now) >= since:
                opportunities += 1
                scan_cost += float(((getattr(o, "scan_cost", None) or {}).get("usd")) or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Funnel: opportunities unavailable: %s", exc)

    emails = 0
    try:
        from agents.negotiation_agent.email_history import get_email_repository  # type: ignore[import]
        erepo = get_email_repository()
        for d in deals:
            emails += sum(1 for e in await erepo.list_by_deal(d.deal_id)
                          if str(getattr(getattr(e, "direction", ""), "value", getattr(e, "direction", ""))).lower() == "outbound")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Funnel: e-mails unavailable: %s", exc)

    steps_def = [("prospects", lambda d: _aware(getattr(d, "created_at", None))),
                 ("interested", _interested_at),
                 ("matched", lambda d: _reached(d, "NEGOTIATING")),
                 ("pi", lambda d: _reached(d, "PI_SENT")),
                 ("paid", lambda d: _reached(d, "PAID"))]
    steps = [{"step": "opportunities", "count": opportunities, "conversion_pct": None, "median_days": None}]
    prev = opportunities
    for name, fn in steps_def:
        reached = [(d, fn(d)) for d in deals]
        hit = [(d, t) for d, t in reached if t]
        count = len(hit)
        steps.append({
            "step": name, "count": count,
            "conversion_pct": round(count / prev * 100, 1) if prev else None,
            "median_days": _median_days([(_aware(getattr(d, "created_at", None)), t) for d, t in hit]) if name != "prospects" else 0.0,
        })
        prev = count
    paid = steps[-1]["count"]
    return {
        "days": days, "since": since.isoformat(), "steps": steps,
        "cost": {"apify_scan_usd": round(scan_cost, 2), "emails_sent": emails,
                 "apify_usd_per_paid_deal": round(scan_cost / paid, 2) if paid else None},
        "biggest_drop": min((s for s in steps[1:] if s["conversion_pct"] is not None),
                            key=lambda s: s["conversion_pct"], default=None),
    }
