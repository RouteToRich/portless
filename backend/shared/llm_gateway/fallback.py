"""Fallback chain: ordered list of LLM endpoints tried in sequence on failure.

Chain assembly (from_settings):
  slot 0  — primary model     (optional; added only when primary_api_key is set)
  slot 1  — qwen/qwen3-32b   via Groq
  slot 2  — qwen/qwen3.6-27b via Groq
  slot 3  — openai/gpt-oss-safeguard-20b via Groq

Error classification (classify_error):
  "transient"  — rate-limit, timeout, connection drop, 5xx → retry then advance chain
  "fatal"      — bad request, auth error, 4xx ≠ 429 → skip retries, advance chain
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

import httpx

from .config import llm_gateway_settings

logger = logging.getLogger(__name__)


@dataclass
class ModelEndpoint:
    """Single LLM endpoint — provider-agnostic descriptor."""

    model: str
    api_key: str
    connector: str = "http"  # "groq" | "openai" | "anthropic" | "http"
    base_url: str = ""       # required only for connector="http"
    priority: int = 0        # lower = higher priority in chain


@dataclass
class FallbackChain:
    """Ordered sequence of endpoints to try when a model call fails."""

    endpoints: list[ModelEndpoint] = field(default_factory=list)

    # ---------------------------------------------------------------- factory

    @classmethod
    def from_settings(cls) -> FallbackChain:
        """Build the fallback chain from environment configuration.

        Always includes the three Groq fallback models.
        Prepends a primary model endpoint when LLM_PRIMARY_API_KEY is set.
        """
        endpoints: list[ModelEndpoint] = []

        if llm_gateway_settings.primary_api_key and llm_gateway_settings.primary_model:
            endpoints.append(
                ModelEndpoint(
                    model=llm_gateway_settings.primary_model,
                    api_key=llm_gateway_settings.primary_api_key,
                    connector=llm_gateway_settings.primary_connector,
                    base_url=llm_gateway_settings.primary_base_url,
                    priority=0,
                )
            )

        groq_key = llm_gateway_settings.groq_api_key
        groq_fallbacks = [
            (
                llm_gateway_settings.fallback_1_model,
                llm_gateway_settings.fallback_1_connector,
                llm_gateway_settings.fallback_1_api_key or groq_key,
            ),
            (
                llm_gateway_settings.fallback_2_model,
                llm_gateway_settings.fallback_2_connector,
                llm_gateway_settings.fallback_2_api_key or groq_key,
            ),
            (
                llm_gateway_settings.fallback_3_model,
                llm_gateway_settings.fallback_3_connector,
                llm_gateway_settings.fallback_3_api_key or groq_key,
            ),
        ]

        for i, (model, connector, api_key) in enumerate(groq_fallbacks, start=1):
            if model and api_key:
                endpoints.append(
                    ModelEndpoint(
                        model=model,
                        api_key=api_key,
                        connector=connector,
                        base_url="",
                        priority=i,
                    )
                )

        if not endpoints:
            logger.error(
                "LLM Gateway: fallback chain is empty — set LLM_GROQ_API_KEY "
                "or configure a primary model."
            )

        logger.info(
            "LLM Gateway chain: %s",
            " → ".join(f"{e.model}({e.connector})" for e in endpoints),
        )
        return cls(endpoints=endpoints)

    @classmethod
    def custom(cls, endpoints: list[ModelEndpoint]) -> FallbackChain:
        """Build a chain from an explicit list (useful for tests)."""
        return cls(endpoints=sorted(endpoints, key=lambda e: e.priority))

    # ------------------------------------------------------------ accessors

    def get_ordered_endpoints(self) -> Sequence[ModelEndpoint]:
        return self.endpoints

    def get_primary(self) -> ModelEndpoint | None:
        return self.endpoints[0] if self.endpoints else None

    def __len__(self) -> int:
        return len(self.endpoints)


# ---------------------------------------------------------------- error helpers


def classify_error(exc: Exception) -> str:
    """Classify an exception as "transient" (retry) or "fatal" (skip to next model).

    Transient: rate-limits, timeouts, network drops, 5xx — worth retrying.
    Fatal: bad request, wrong auth, model-level refusal — retrying the same
           model won't help; advance to the next one immediately.
    """
    # ---- Groq SDK exceptions (groq package, installed with langchain-groq) ----
    try:
        import groq

        if isinstance(
            exc,
            (
                groq.RateLimitError,
                groq.APIConnectionError,
                groq.InternalServerError,
                groq.APITimeoutError,
            ),
        ):
            return "transient"

        if isinstance(
            exc,
            (
                groq.BadRequestError,
                groq.AuthenticationError,
                groq.PermissionDeniedError,
            ),
        ):
            return "fatal"
    except ImportError:
        pass

    # ---- httpx exceptions (raw HTTP connector) ----
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return "transient"

    if isinstance(exc, httpx.HTTPStatusError):
        return "transient" if exc.response.status_code in (429, 500, 502, 503, 504) else "fatal"

    # ---- Generic fallback: inspect status_code attribute if present ----
    status_code: int | None = getattr(exc, "status_code", None)
    if status_code is None:
        resp = getattr(exc, "response", None)
        status_code = getattr(resp, "status_code", None)

    if status_code is not None:
        if status_code in (429, 500, 502, 503, 504):
            return "transient"
        if status_code in (400, 401, 403):
            return "fatal"

    # Unknown errors: treat as transient — give the model one more chance
    return "transient"
