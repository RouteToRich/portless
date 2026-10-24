"""Rate limiter — placeholder, NOT YET IMPLEMENTED.

Hook point in LLMClient.chat():
    self._rate_limiter.check(caller=caller, model=endpoint.model)
    # raises RateLimitExceeded → caller gets a 429 immediately

Why this is needed:
    Without rate limiting, a single misbehaving agent (e.g. a runaway
    Trade-Min loop) can saturate the Groq quota for ALL agents.

Implementation plan:
    1. Choose algorithm: token-bucket (bursty OK) or sliding-window (strict).
    2. Choose backend:
       - "memory"  — threading.Lock + deque, single-process only (good for dev).
       - "mongo"   — atomic $inc on a TTL doc in the shared MongoDB cache,
                     shared across workers (production).
    3. Limits to enforce:
       - Per-caller RPM (requests per minute).
       - Per-caller TPM (tokens per minute) — needs estimated token count upfront.
       - Per-model global QPM (queries per minute) — stay within Groq quota.
    4. Expose current usage via get_usage(caller) for observability dashboards.
    5. Config fields on LLMGatewaySettings to wire (already added as placeholders):
       LLM_RATE_LIMIT_ENABLED, LLM_RATE_LIMIT_RPM, LLM_RATE_LIMIT_TPM,
       LLM_RATE_LIMIT_BACKEND.

Usage once implemented::

    from shared.llm_gateway import LLMClient, RateLimiter

    client = LLMClient(rate_limiter=RateLimiter())
    # Raises RateLimitExceeded if caller exceeds configured limits.
    response = await client.chat(messages, caller="negotiation_agent")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when a caller has exceeded their allowed request rate."""

    def __init__(self, caller: str, model: str, limit_type: str) -> None:
        self.caller = caller
        self.model = model
        self.limit_type = limit_type  # "rpm" | "tpm" | "global_qpm"
        super().__init__(
            f"Rate limit exceeded for caller='{caller}' on model='{model}' "
            f"(limit_type={limit_type})"
        )


@dataclass
class RateLimitUsage:
    """Snapshot of current usage for one caller."""

    caller: str
    requests_this_minute: int = 0
    tokens_this_minute: int = 0
    rpm_limit: int = 0
    tpm_limit: int = 0


class RateLimiter:
    """Per-caller, per-model rate limiter.

    This is a stub — all methods raise NotImplementedError.
    Replace with a real implementation when ready.

    Expected constructor args (once implemented):
        backend: str = "memory"          # "memory" | "mongo"
        rpm: int = 60                    # requests per minute per caller
        tpm: int = 100_000               # tokens per minute per caller
        global_qpm_per_model: int = 300  # model-level global cap
    """

    def check(self, caller: str, model: str, estimated_tokens: int = 0) -> None:
        """Check whether the caller is within rate limits.

        Call this BEFORE dispatching to the LLM.

        Args:
            caller: agent identifier (e.g. "negotiation_agent").
            model: model name being called (e.g. "qwen/qwen3-32b").
            estimated_tokens: rough token estimate for TPM enforcement.

        Raises:
            RateLimitExceeded: if any limit is breached.
            NotImplementedError: until this is implemented.
        """
        # TODO: implement token-bucket or sliding-window algorithm
        # TODO: persist window state in a memory dict or the shared MongoDB cache
        # TODO: enforce rpm, tpm, and global_qpm_per_model
        raise NotImplementedError(
            "RateLimiter.check() is not yet implemented. "
            "See rate_limiter.py for the implementation plan."
        )

    def record_tokens_used(self, caller: str, model: str, tokens_used: int) -> None:
        """Update TPM counters after a successful call.

        Call this AFTER a successful response in handle_outcome_node.
        """
        # TODO: increment token counter for the current window
        raise NotImplementedError("RateLimiter.record_tokens_used() is not yet implemented.")

    def get_usage(self, caller: str) -> RateLimitUsage:
        """Return the current usage snapshot for a caller (for dashboards)."""
        # TODO: read from the rate-limit window store
        raise NotImplementedError("RateLimiter.get_usage() is not yet implemented.")

    def reset(self, caller: str | None = None) -> None:
        """Reset counters. Pass caller=None to reset all."""
        # TODO: clear the window store for the given caller (or all)
        raise NotImplementedError("RateLimiter.reset() is not yet implemented.")
