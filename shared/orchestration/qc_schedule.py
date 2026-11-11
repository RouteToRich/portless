"""When the pre-shipment inspection happens.

A pre-shipment inspection (PSI) samples the finished lot, so it is booked for the
day the supplier says the goods are ready — 100 % produced and at least
``QC_MIN_PACKED_PCT`` (80) % packed, the usual readiness rule for a final random
inspection under ISO 2859-1 — and never sooner than the agency can attend:
``QC_BOOKING_LEAD_BUSINESS_DAYS`` (2) working days after the booking. The
inspector also supervises the container stuffing and seals it
(``LOADING_SUPERVISION``), so loading is planned right after the inspection.

The supplier is asked for that date with the PO (and again when the PO
acknowledgement does not name it); ``QC_READINESS_DATE_REQUIRED`` (default on)
holds the booking until it is known. ``POST /shield-link/deals/{id}/qc-readiness``
records a date an operator heard by phone.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_MAX_AHEAD_DAYS = 180


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def readiness_required() -> bool:
    return os.getenv("QC_READINESS_DATE_REQUIRED", "true").strip().lower() in ("1", "true", "yes", "on")


def lead_business_days() -> int:
    return max(0, _int("QC_BOOKING_LEAD_BUSINESS_DAYS", 2))


def min_packed_pct() -> int:
    return min(100, max(0, _int("QC_MIN_PACKED_PCT", 80)))


def report_grace_hours() -> float:
    """How long after the inspection date a missing report becomes overdue."""
    return max(0.0, _float("QC_REPORT_GRACE_HOURS", 48.0))


def poll_minutes() -> float:
    """How often the deal watcher asks an API provider (QIMA) for the order status."""
    return max(1.0, _float("QC_POLL_MINUTES", 60.0))


def ship_after_inspection_days() -> int:
    return max(0, _int("QC_SHIP_AFTER_INSPECTION_DAYS", 7))


def today() -> date:
    return datetime.now(timezone.utc).date()


def add_business_days(start: date, days: int) -> date:
    d = start
    left = days
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def earliest_inspection_date(on: date | None = None) -> date:
    return next_business_day(add_business_days(on or today(), lead_business_days()))


def to_date(value: date | datetime | str | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def inspection_date_for(goods_ready: date | str | None, on: date | None = None) -> date:
    """The day to book: the supplier's ready date, moved to the first working
    day the agency can attend."""
    earliest = earliest_inspection_date(on)
    ready = to_date(goods_ready)
    return max(earliest, next_business_day(ready)) if ready else earliest


def ship_date_for(inspection: date) -> date:
    return inspection + timedelta(days=ship_after_inspection_days())


def parse_ready_date(text: str | None, on: date | None = None) -> date | None:
    """The goods-ready date a supplier wrote, read without a model.

    Understands ``2026-12-15``, ``15 December 2026`` / ``15th Dec``,
    ``December 15, 2026`` / ``Dec 15``, ``15/12/2026`` / ``15-12-2026`` (day
    first, the Indian convention) and ``in 10 days`` / ``within 2 weeks``. Only
    a date from today up to six months ahead counts; a year left out means the
    next such date."""
    if not text:
        return None
    base = on or today()
    low = text.lower()
    found: list[date] = []

    def add(y: int | None, m: int, d: int) -> None:
        try:
            cand = date(y or base.year, m, d)
        except ValueError:
            return
        if y is None and cand < base:
            try:
                cand = date(base.year + 1, m, d)
            except ValueError:
                return
        found.append(cand)

    for y, m, d in re.findall(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", low):
        add(int(y), int(m), int(d))
    for d, mon, y in re.findall(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RE}(?:,?\s+(20\d{{2}}))?", low):
        add(int(y) if y else None, _MONTHS[mon[:3]], int(d))
    for mon, d, y in re.findall(rf"\b{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b", low):
        add(int(y) if y else None, _MONTHS[mon[:3]], int(d))
    for d, m, y in re.findall(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b", low):
        add(int(y), int(m), int(d))
    for n, unit in re.findall(r"\b(?:in|within|after)\s+(\d{1,3})\s+(working\s+days?|business\s+days?|days?|weeks?)\b", low):
        k = int(n)
        if unit.startswith(("working", "business")):
            found.append(add_business_days(base, k))
        elif unit.startswith("week"):
            found.append(base + timedelta(weeks=k))
        else:
            found.append(base + timedelta(days=k))
    valid = [d for d in found if base <= d <= base + timedelta(days=_MAX_AHEAD_DAYS)]
    return max(valid) if valid else None
