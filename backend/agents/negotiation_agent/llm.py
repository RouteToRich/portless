"""LLM access for the Negotiation Agent — delegates to shared.llm_gateway.LLMClient.

The gateway provides automatic fallback, per-request tracking, retry with
exponential backoff, and (future) rate limiting / circuit breaking.

Primary model is configured via LLM gateway env vars (prefix LLM_):
  LLM_PRIMARY_CONNECTOR=anthropic
  LLM_PRIMARY_MODEL=claude-sonnet-4-6
  LLM_PRIMARY_API_KEY=<your anthropic key>

If LLM_PRIMARY_API_KEY is not set, the gateway falls back to the Groq chain
(qwen/qwen3-32b → qwen/qwen3.6-27b → openai/gpt-oss-safeguard-20b).
"""
from __future__ import annotations

from shared.llm_gateway import LLMClient

# Module-level singleton — one LangGraph fallback graph shared across all calls
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Return the shared LLMClient instance (lazy-init, thread-safe for asyncio)."""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
