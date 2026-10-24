"""Thin async HTTP wrapper that every service-specific client extends.

Keeping all REST plumbing here means a microservice split changes only the
``base_url`` a client is constructed with — call sites stay identical.
"""
from __future__ import annotations

from typing import Any

import httpx

from shared.config import settings


class BaseServiceClient:
    """Async REST client scoped to a single downstream service."""

    def __init__(self, base_url: str, *, timeout: float | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout if timeout is not None else settings.http_timeout_seconds

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            if response.content:
                return response.json()
            return None

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, json: Any | None = None, **kwargs: Any) -> Any:
        return await self._request("POST", path, json=json, **kwargs)

    async def patch(self, path: str, json: Any | None = None, **kwargs: Any) -> Any:
        return await self._request("PATCH", path, json=json, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self._request("DELETE", path, **kwargs)
