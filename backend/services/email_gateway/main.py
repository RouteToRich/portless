"""Standalone FastAPI app for the Email Gateway service.

Transport only: runs the inbound IMAP pollers (forwarding each fetched email to
the Email Processor) and serves the outbound send API. Runs on port 8012 in
microservice mode (see Makefile).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .imap_poller import ImapPoller, create_buyer_poller, create_supplier_poller
from .router import router

logger = logging.getLogger(__name__)

_buyer_poller: ImapPoller | None = None
_supplier_poller: ImapPoller | None = None


def _forward_callback():
    """Return the callback the IMAP pollers forward each fetched email to.

    Default (in-process): call the Email Processor's process_inbound directly —
    efficient in the combined monolith. When FORWARD_VIA_HTTP=true, POST each
    email to the Processor's receiving API so the two services can run apart.
    """
    if settings.forward_via_http:
        from shared.config import settings as shared_settings
        from services.email_processor.client import EmailProcessorClient

        logger.info("EmailGateway: pollers forward via HTTP to %s", shared_settings.email_processor_url)
        return EmailProcessorClient().submit_inbound

    from services.email_processor import service as processor
    return processor.process_inbound


async def on_startup() -> None:
    global _buyer_poller, _supplier_poller
    callback = _forward_callback()
    _buyer_poller = create_buyer_poller(callback)
    _supplier_poller = create_supplier_poller(callback)
    await _buyer_poller.start()
    await _supplier_poller.start()
    logger.info("EmailGateway startup complete (IMAP pollers active + SMTP send ready)")


async def on_shutdown() -> None:
    global _buyer_poller, _supplier_poller
    if _buyer_poller:
        await _buyer_poller.stop()
    if _supplier_poller:
        await _supplier_poller.stop()
    logger.info("EmailGateway shutdown complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await on_startup()
    yield
    await on_shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Email Gateway Service",
        version="1.0.0",
        description=(
            "Email transport (single IMAP + SMTP config). Monitors and fetches "
            "inbound mail over IMAP (automatic) and forwards it to the Email "
            "Processor; sends outbound mail over SMTP via POST /email-gateway/send."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
