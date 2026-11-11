"""Escrow provider sandbox app — ``make escrow-sandbox`` (port 8015) or in process.

In process (``ESCROW_SANDBOX_PROVIDERS`` set, no ``ESCROW_SANDBOX_URL``) the
escrow connectors reach this app through ``httpx.ASGITransport``: no network,
state kept for the life of the process. Not a real provider.
"""
from __future__ import annotations

from fastapi import FastAPI

from . import escrow_com, tazapay


def create_app() -> FastAPI:
    application = FastAPI(
        title="PortLess escrow provider sandbox",
        description="Tazapay API v3 escrow + Escrow.com API 2017-09-01 milestone transactions. Not a real provider.",
    )
    application.include_router(tazapay.router, tags=["tazapay"])
    application.include_router(escrow_com.router, tags=["escrow.com"])

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"service": "escrow-sandbox", "status": "ok"}

    return application


app = create_app()


def reset() -> None:
    tazapay.STATE.reset()
    escrow_com.STATE.reset()


async def control(path: str, body: dict | None = None) -> dict:
    """Call a sandbox control endpoint (the buyer paying, the provider deciding),
    in process or on the running sandbox at ``ESCROW_SANDBOX_URL``."""
    import os

    import httpx

    url = os.getenv("ESCROW_SANDBOX_URL", "").strip().rstrip("/")
    client = (httpx.AsyncClient(base_url=url, timeout=20.0) if url else
              httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://escrow-sandbox.local", timeout=20.0))
    async with client:
        resp = await client.post(path, json=body or {})
    resp.raise_for_status()
    return resp.json()


async def buyer_pays(provider: str, ext_ref: str, amount: float | None = None) -> dict:
    """The buyer funds the escrow on the provider (all of it, or *amount*)."""
    if provider == "tazapay":
        return await control(f"/tazapay/_sandbox/escrow/{ext_ref}/pay", {} if amount is None else {"amount": amount})
    return await control(f"/escrow-com/_sandbox/transaction/{ext_ref}/pay")
