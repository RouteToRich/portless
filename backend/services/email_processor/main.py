"""Standalone FastAPI app for the Email Processor service.

Receives fetched/pushed emails (from the Email Gateway's IMAP pollers, provider
webhooks, or manual submission) and does the processing: dedupe, persist, match
to a live negotiation, extract the counter-price, and trigger the negotiation
orchestration agent. Owns the inbound_emails repository. Runs on port 8013 in
microservice mode (see Makefile).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .repository import close_repository, init_repository
from .router import router

logger = logging.getLogger(__name__)


async def on_startup() -> None:
    await init_repository()
    logger.info("EmailProcessor startup complete (inbound repository ready)")


async def on_shutdown() -> None:
    await close_repository()
    logger.info("EmailProcessor shutdown complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await on_startup()
    yield
    await on_shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Email Processor Service",
        version="1.0.0",
        description=(
            "Processes inbound emails forwarded by the Email Gateway (IMAP) or "
            "provider webhooks (SendGrid, Mailgun): maps each email to its live "
            "negotiation session, extracts the counter-price, persists an audit "
            "record, and advances the negotiation pipeline via run_round()."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
