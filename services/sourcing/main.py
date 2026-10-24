"""Standalone FastAPI app for the Sourcing service.

Lifespan
--------
startup  — initialise MongoDB repository (falls back to in-memory if URI absent)
shutdown — close MongoDB connection

Reuse from combined app
-----------------------
``app.py`` calls ``on_startup()`` / ``on_shutdown()`` directly.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .repository import close_repository, init_repository
from .router import router

logger = logging.getLogger(__name__)


async def on_startup() -> None:
    await init_repository()


async def on_shutdown() -> None:
    await close_repository()
    logger.info("Sourcing service shut down")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await on_startup()
    # Publish-only event bus (NATS when creds present; never raises).
    from shared.bus import close_event_bus, init_event_bus
    await init_event_bus(consume=False)
    yield
    await close_event_bus()
    await on_shutdown()


def create_app() -> FastAPI:
    return FastAPI(
        title="PortLess · Sourcing Service",
        version="1.0.0",
        lifespan=lifespan,
    )


app = create_app()
app.include_router(router)
