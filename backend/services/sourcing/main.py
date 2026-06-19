"""Standalone FastAPI app for the Sourcing service."""
from __future__ import annotations

from fastapi import FastAPI

from .router import router


def create_app() -> FastAPI:
    app = FastAPI(title="PortLess · Sourcing Service", version="0.1.0")
    app.include_router(router)
    return app


app = create_app()
