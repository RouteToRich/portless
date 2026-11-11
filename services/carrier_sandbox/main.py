"""Carrier sandbox app — ``make carrier-sandbox`` (port 8016) or in process via ASGI. Not a real carrier."""
from __future__ import annotations

from fastapi import FastAPI

from . import maersk


def create_app() -> FastAPI:
    application = FastAPI(title="PortLess carrier sandbox",
                          description="Maersk Ocean Booking / Track & Trace / eBL / VGM simulation. Not a real carrier.")
    application.include_router(maersk.router, tags=["maersk"])

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"service": "carrier-sandbox", "status": "ok"}

    return application


app = create_app()


def reset() -> None:
    maersk.STATE.reset()


async def control(path: str, body: dict | None = None) -> dict:
    """Call a sandbox control (Maersk confirming, the vessel moving, the B/L issued)."""
    import os

    import httpx

    url = os.getenv("CARRIER_SANDBOX_URL", "").strip().rstrip("/")
    client = (httpx.AsyncClient(base_url=url, timeout=20.0) if url else
              httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://carrier-sandbox.local",
                                timeout=20.0))
    async with client:
        resp = await client.post(path, json=body or {})
    resp.raise_for_status()
    return resp.json()
