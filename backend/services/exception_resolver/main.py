"""Standalone FastAPI app for the Exception-Resolver service."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .repository import close_repository, init_repository
from .router import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_repository()
    logger.info("ExceptionResolver: repository initialised")
    # Publish-only event bus (NATS when creds present; never raises).
    from shared.bus import close_event_bus, init_event_bus
    await init_event_bus(consume=False)
    yield
    await close_event_bus()
    await close_repository()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Exception-Resolver Agent",
        version="1.0.0",
        description=(
            "LLM-powered agent that classifies deal exceptions, builds a remediation plan, "
            "and either auto-fixes or escalates to the HITL approval service."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
