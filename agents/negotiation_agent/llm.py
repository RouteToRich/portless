"""LLM access for the Negotiation Agent — delegates to shared.llm_gateway.LLMClient.

Every call goes to the shared RouteToRich LLM Gateway (LLM_GATEWAY_URL +
service-account credential, see shared/llm_gateway/remote.py); the gateway
picks the model, runs the fallback chain and holds the provider keys. This
module holds no model or key config.
"""
from __future__ import annotations

from shared.llm_gateway import LLMClient

# Module-level singleton — one client (tracker + cache) shared across all calls
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Return the shared LLMClient instance (lazy-init, thread-safe for asyncio)."""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
