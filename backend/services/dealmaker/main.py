"""Standalone FastAPI app for the DealMaker service.

Run:
    uvicorn services.dealmaker.main:app --reload --port 8085

In the combined monolith (app.py) the lifespan hooks are registered directly
via ``on_startup`` / ``on_shutdown`` and this module is not imported.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .repository import close_repository, init_repository
from .router import router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_repository()
    try:
        yield
    finally:
        await close_repository()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · DealMaker Service",
        version="0.1.0",
        description=(
            "Generates and tracks trade finance documents: "
            "Proforma Invoice (PI) and Purchase Order (PO)."
        ),
        lifespan=_lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
