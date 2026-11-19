"""What each scan did per product × market, including why corridors were dropped.

Before this, "no live demand data" / "net gap below the floor" / "rank below the
minimum" only reached the log and the directory record of each company. The scan
now collects one line per corridor and keeps the last reports (Mongo
``trade_scan_reports`` next to the opportunities, else in memory) for
``GET /trade-min/scan/report``.
"""
from __future__ import annotations

import contextvars
import logging
from datetime import datetime, timezone
from uuid import uuid4

from .schemas import ScanCorridorResult, ScanReport

logger = logging.getLogger(__name__)

_COLLECTION = "trade_scan_reports"
_KEEP = 20
_memory: list[ScanReport] = []
_current: contextvars.ContextVar[ScanReport | None] = contextvars.ContextVar("trade_min_scan_report", default=None)


def reset_for_tests() -> None:
    _memory.clear()


def start() -> contextvars.Token:
    report = ScanReport(scan_id=f"SCAN-{uuid4().hex[:10].upper()}", started_at=datetime.now(timezone.utc))
    return _current.set(report)


def current() -> ScanReport | None:
    return _current.get()


def record(hs_code: str, market: str, result: str, reason: str, opportunity_id: str | None = None) -> None:
    """One corridor's outcome on the running scan (no-op outside a scan)."""
    report = _current.get()
    if report is None:
        return
    report.corridors.append(ScanCorridorResult(
        hs_code=hs_code, market=market, result=result,  # type: ignore[arg-type]
        reason=reason, opportunity_id=opportunity_id,
    ))


def add_diagnostics(rows: list[dict] | None) -> None:
    """The company search's per-query diagnostics on the running scan (no-op outside one)."""
    report = _current.get()
    if report is None or not rows:
        return
    report.search_diagnostics.extend(rows)


async def _collection():
    try:
        from .repository import MongoOpportunityRepository, get_repository
        repo = await get_repository()
        if isinstance(repo, MongoOpportunityRepository):
            return repo._col.database[_COLLECTION]  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Scan report: no Mongo collection: %s", exc)
    return None


async def finish(token: contextvars.Token) -> ScanReport | None:
    report = _current.get()
    _current.reset(token)
    if report is None:
        return None
    report.finished_at = datetime.now(timezone.utc)
    report.opportunities = sum(1 for c in report.corridors if c.result == "opportunity")
    report.dropped = sum(1 for c in report.corridors if c.result == "dropped")
    report.errors = sum(1 for c in report.corridors if c.result == "error")
    _memory.insert(0, report)
    del _memory[_KEEP:]
    col = await _collection()
    if col is not None:
        try:
            doc = report.model_dump(mode="json")
            doc["_id"] = report.scan_id
            await col.replace_one({"_id": report.scan_id}, doc, upsert=True)
        except Exception as exc:  # noqa: BLE001 - a report never fails the scan
            logger.warning("Scan report %s not saved: %s", report.scan_id, exc)
    return report


async def latest() -> ScanReport | None:
    col = await _collection()
    if col is not None:
        try:
            doc = await col.find_one({}, sort=[("started_at", -1)])
            if doc:
                doc.pop("_id", None)
                return ScanReport.model_validate(doc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Scan report: read failed: %s", exc)
    return _memory[0] if _memory else None
