"""Standalone FastAPI app for the Match Engine service."""
from __future__ import annotations

from fastapi import FastAPI

from .router import router


def create_app() -> FastAPI:
    app = FastAPI(title="PortLess · Match Engine Service", version="0.1.0")
    app.include_router(router)
    return app


app = create_app()
