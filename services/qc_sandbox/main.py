"""QC provider sandbox app — ``make qc-sandbox`` (port 8014) or in process.

In process (``QC_SANDBOX_PROVIDERS`` set, no ``QC_SANDBOX_URL``) the QC
connectors reach this app through ``httpx.ASGITransport``: no network, state
kept for the life of the process. With ``QC_SANDBOX_URL`` they call a running
sandbox over HTTP instead.
"""
from __future__ import annotations

from fastapi import FastAPI

from . import portal, qima


def create_app() -> FastAPI:
    application = FastAPI(
        title="PortLess QC provider sandbox",
        description="QIMA Public API contract + SGS SMART / BV OneSource Connect portal simulation. Not a real provider.",
    )
    application.include_router(qima.router, tags=["qima"])
    application.include_router(portal.router, tags=["portal"])

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"service": "qc-sandbox", "status": "ok"}

    return application


app = create_app()


def reset() -> None:
    qima.STATE.reset()
    portal.STATE.reset()
