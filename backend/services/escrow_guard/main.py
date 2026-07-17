"""Standalone FastAPI app for the Escrow-Guard service."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .repository import close_repository, init_repository
from .router import router
from .service import run_inspection_watcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_repository()
    # Publish-only event bus (NATS when creds present; never raises).
    from shared.bus import close_event_bus, init_event_bus
    await init_event_bus(consume=False)
    watcher = asyncio.create_task(run_inspection_watcher())
    try:
        yield
    finally:
        watcher.cancel()
        await close_event_bus()
        await close_repository()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Escrow-Guard Service",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
