"""Standalone FastAPI app for the Deals service (run as its own microservice)."""
from __future__ import annotations

from fastapi import FastAPI

from .router import router


def create_app() -> FastAPI:
    app = FastAPI(title="PortLess · Deals Service", version="0.1.0")
    app.include_router(router)
    return app


app = create_app()
