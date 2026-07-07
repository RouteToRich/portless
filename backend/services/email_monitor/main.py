"""Standalone FastAPI app for the Email Monitor service."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .imap_poller import ImapPoller, create_buyer_poller, create_supplier_poller
from .repository import close_repository, init_repository
from .router import router
from . import service

logger = logging.getLogger(__name__)

_buyer_poller: ImapPoller | None = None
_supplier_poller: ImapPoller | None = None


async def on_startup() -> None:
    global _buyer_poller, _supplier_poller
    await init_repository()
    _buyer_poller = create_buyer_poller(service.process_inbound)
    _supplier_poller = create_supplier_poller(service.process_inbound)
    await _buyer_poller.start()
    await _supplier_poller.start()
    logger.info("EmailMonitor startup complete (buyer + supplier pollers active)")


async def on_shutdown() -> None:
    global _buyer_poller, _supplier_poller
    if _buyer_poller:
        await _buyer_poller.stop()
    if _supplier_poller:
        await _supplier_poller.stop()
    await close_repository()
    logger.info("EmailMonitor shutdown complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await on_startup()
    yield
    await on_shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Email Monitor Service",
        version="1.0.0",
        description=(
            "Ingests inbound emails from buyers and suppliers via IMAP polling "
            "or provider webhooks (SendGrid, Mailgun), maps each email to its "
            "active negotiation session using email threading headers and subject "
            "tokens, extracts the counter-price, and automatically advances the "
            "negotiation pipeline by calling run_round()."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
