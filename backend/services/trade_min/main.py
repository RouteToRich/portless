"""Standalone FastAPI app for the Trade-Min service (FR-1, LLD 4.5, §10).

Lifespan
--------
startup  — initialise MongoDB (falls back to in-memory if URI absent)
           + start APScheduler cron jobs
shutdown — stop scheduler + close MongoDB connection

Scheduled jobs (§10.7)
-----------------------
nightly_scan    — full HS × market scan at 02:00 UTC (configurable)
reorder_window  — 6-hourly reorder-window recheck (configurable)

Reuse from combined app
-----------------------
``app.py`` calls ``on_startup()`` / ``on_shutdown()`` directly so the combined
dev server shares the same scheduler and repository without duplicating logic.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from .config import settings
from .repository import close_repository, init_repository
from .router import router
from .schemas import ScanRequest

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Scheduled job bodies
# ---------------------------------------------------------------------------

async def _run_nightly_scan() -> None:
    from . import service
    logger.info("Scheduler: starting nightly full scan")
    try:
        results = await service.scan_opportunities(ScanRequest())
        logger.info("Scheduler: nightly scan complete — %d opportunities", len(results))
    except Exception as exc:
        logger.exception("Scheduler: nightly scan failed: %s", exc)


async def _run_reorder_window() -> None:
    from . import service
    logger.info("Scheduler: starting reorder window check")
    try:
        results = await service.scan_opportunities(
            ScanRequest(min_arbitrage_rank=10.0)
        )
        logger.info("Scheduler: reorder window complete — %d opportunities", len(results))
    except Exception as exc:
        logger.exception("Scheduler: reorder window check failed: %s", exc)


# ---------------------------------------------------------------------------
# Reusable startup / shutdown (called by both standalone and combined app)
# ---------------------------------------------------------------------------

async def on_startup() -> None:
    """Initialise MongoDB and start the APScheduler."""
    global _scheduler

    await init_repository()  # logs WARNING if falling back to in-memory

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _run_nightly_scan,
        CronTrigger(**_cron_to_kwargs(settings.scheduler_nightly_cron)),
        id="trade_min_nightly",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    _scheduler.add_job(
        _run_reorder_window,
        CronTrigger(**_cron_to_kwargs(settings.scheduler_reorder_cron)),
        id="trade_min_reorder",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    _scheduler.start()
    logger.info(
        "Trade-Min scheduler started (nightly=%s, reorder=%s)",
        settings.scheduler_nightly_cron,
        settings.scheduler_reorder_cron,
    )


async def on_shutdown() -> None:
    """Stop the APScheduler and close the MongoDB connection."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Trade-Min scheduler stopped")
        _scheduler = None
    await close_repository()


# ---------------------------------------------------------------------------
# FastAPI lifespan (standalone deployment)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await on_startup()
    yield
    await on_shutdown()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    return FastAPI(
        title="PortLess · Trade-Min Service",
        version="2.0.0",
        lifespan=lifespan,
    )


app = create_app()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cron_to_kwargs(cron_expr: str) -> dict[str, str]:
    """Parse a standard 5-field cron expression into APScheduler CronTrigger kwargs.

    Format: ``minute hour day month day_of_week``
    Example: ``"0 2 * * *"``  → every day at 02:00 UTC
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-field cron expression, got: {cron_expr!r}")
    minute, hour, day, month, day_of_week = parts
    return {
        "minute":      minute,
        "hour":        hour,
        "day":         day,
        "month":       month,
        "day_of_week": day_of_week,
    }
