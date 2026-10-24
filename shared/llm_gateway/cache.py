"""LLM response cache — backed by the application-wide ``shared.cache``.

Caches identical chat requests keyed on ``(messages, model, temperature)`` so a
repeated prompt during the same deal cycle returns instantly without re-spending
tokens. Uses the shared MongoDB TTL cache (in-memory fallback when Mongo is
absent), so entries are shared across workers and expire automatically.

Wired into ``LLMClient.chat()`` and gated by ``LLM_CACHE_ENABLED`` (default off).
Keep the TTL short (``LLM_CACHE_TTL_SECONDS``, default 120s) so live prices never
go stale within a negotiation.

Usage::

    from shared.llm_gateway import LLMClient, ResponseCache

    client = LLMClient(cache=ResponseCache(ttl_seconds=120))
    r1 = await client.chat(messages, caller="agent")
    r2 = await client.chat(messages, caller="agent")  # served from cache
    assert r1.content == r2.content
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from typing import TYPE_CHECKING

from shared.cache import cache_clear, cache_get, cache_set

if TYPE_CHECKING:
    from .client import LLMResponse

logger = logging.getLogger(__name__)


class ResponseCache:
    """Async LLM response cache keyed on (messages, model, temperature)."""

    def __init__(self, ttl_seconds: int = 120, namespace: str = "llm") -> None:
        self._ttl = ttl_seconds
        self._namespace = namespace

    # ------------------------------------------------------------------ key
    def make_key(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
    ) -> str:
        """Deterministic key for a (messages, model, temperature) triple.

        Temperature is rounded to 4 dp so 0 and 0.0 never split the cache.
        """
        payload = json.dumps(
            {
                "messages": messages,
                "model": model,
                "temperature": round(float(temperature), 4),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return "llmgw:" + hashlib.sha256(payload.encode()).hexdigest()

    # ------------------------------------------------------------------ get/set
    async def aget(self, key: str) -> "LLMResponse | None":
        """Return the cached response for key, or None on miss/error."""
        doc = await cache_get(self._namespace, key)
        if not doc:
            return None
        from .client import LLMResponse  # lazy — avoids circular import
        try:
            return LLMResponse(**doc)
        except Exception as exc:  # malformed cache doc — treat as miss
            logger.debug("LLM cache decode failed for %s: %s", key, exc)
            return None

    async def aset(self, key: str, response: "LLMResponse") -> None:
        """Store a response under key with the configured TTL (best-effort)."""
        await cache_set(self._namespace, key, dataclasses.asdict(response), self._ttl)

    async def clear(self) -> int:
        """Evict every entry in this cache's namespace."""
        return await cache_clear(self._namespace)
