"""Standalone FastAPI app for the DocuGen service.

Run:
    uvicorn services.docugen.main:app --reload --port 8086

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
        title="PortLess · DocuGen Service",
        version="0.1.0",
        description=(
            "Generates export and customs documents: "
            "Commercial Invoice, Packing List, Certificate of Origin, SDF, Shipping Bill."
        ),
        lifespan=_lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
