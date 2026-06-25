"""Standalone FastAPI app for the HITL Approval service."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .repository import close_repository, init_repository
from .router import router
from .service import run_timeout_watcher

logger = logging.getLogger(__name__)

_watcher_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _watcher_task
    await init_repository()
    _watcher_task = asyncio.create_task(run_timeout_watcher())
    logger.info("HitlApproval: timeout watcher started")
    yield
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
    await close_repository()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · HITL Approval Service",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
