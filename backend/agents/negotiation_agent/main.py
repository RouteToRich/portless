"""Standalone FastAPI app for the Negotiation Agent."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .router import router

logger = logging.getLogger(__name__)


async def on_startup() -> None:
    from .email_history import init_email_repository
    from .repository import init_repository

    await init_repository()
    await init_email_repository()

    # Seed demo data when the in-memory fallback is active (no MongoDB or unreachable)
    from .repository import InMemoryNegotiationRepository, get_repository
    if isinstance(get_repository(), InMemoryNegotiationRepository):
        from ._mock_seed import seed_mock_negotiation_data
        await seed_mock_negotiation_data()

    logger.info("NegotiationAgent startup complete")


async def on_shutdown() -> None:
    logger.info("NegotiationAgent shutdown complete")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await on_startup()
    yield
    await on_shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Negotiation Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
