"""Standalone FastAPI app for the Deals service (LLD 4.2).

Lifespan
--------
startup  — initialise MongoDB repository (falls back to in-memory if URI absent)
shutdown — close MongoDB connection

Reuse from combined app
-----------------------
``app.py`` calls ``on_startup()`` / ``on_shutdown()`` directly so the combined
dev server shares the same repository without duplicating logic.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .repository import close_repository, init_repository
from .router import router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reusable startup / shutdown (called by both standalone and combined app)
# ---------------------------------------------------------------------------

async def on_startup() -> None:
    """Initialise the deals repository.  Falls back gracefully if no URI."""
    await init_repository()


async def on_shutdown() -> None:
    """Release the MongoDB client."""
    await close_repository()
    logger.info("Deals service shut down")


# ---------------------------------------------------------------------------
# FastAPI lifespan (standalone deployment)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await on_startup()
    # Publish-only event bus (NATS when creds present; never raises).
    # Continuation consumers run only in the combined monolith (app.py).
    from shared.bus import close_event_bus, init_event_bus
    await init_event_bus(consume=False)
    yield
    await close_event_bus()
    await on_shutdown()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    return FastAPI(
        title="PortLess · Deals Service",
        version="1.0.0",
        lifespan=lifespan,
    )


app = create_app()
app.include_router(router)
