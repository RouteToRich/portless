"""Standalone FastAPI app for the Match Engine service."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .router import router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Publish-only event bus (NATS when creds present; never raises).
    from shared.bus import close_event_bus, init_event_bus
    await init_event_bus(consume=False)
    yield
    await close_event_bus()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Match Engine Service",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
