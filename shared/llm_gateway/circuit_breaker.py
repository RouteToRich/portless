"""Circuit breaker — placeholder, NOT YET IMPLEMENTED.

Hook point in LLMClient._node_call_endpoint():
    if self._circuit_breaker and self._circuit_breaker.is_open(endpoint.model):
        # skip this endpoint entirely — treat as if it failed
        raise CircuitOpenError(model=endpoint.model)

And in _node_handle_outcome():
    if self._circuit_breaker:
        if state["response"] is not None:
            self._circuit_breaker.record_success(endpoint.model)
        else:
            self._circuit_breaker.record_failure(endpoint.model)

Why this is needed:
    Without circuit breaking, a model that is down or degraded will
    consume the full retry budget on every request, adding several
    seconds of latency before the chain advances. The circuit breaker
    causes the gateway to skip the broken model until it recovers.

State machine:
    CLOSED (normal) → failures > threshold → OPEN (skipping model)
    OPEN → after cool_down_seconds → HALF_OPEN (one probe allowed)
    HALF_OPEN + success → CLOSED
    HALF_OPEN + failure → OPEN (reset timer)

Implementation plan:
    1. Track per-model state in a dict[str, _CircuitState].
    2. On OPEN: check elapsed time since open_ts vs cool_down_seconds.
    3. On HALF_OPEN: allow exactly one probe; lock with threading.Lock.
    4. Config fields on LLMGatewaySettings (already added as placeholders):
       LLM_CIRCUIT_BREAKER_ENABLED, LLM_CB_FAILURE_THRESHOLD,
       LLM_CB_COOL_DOWN_SECONDS.
    5. Expose get_state(model) for health dashboards and is_healthy().

Usage once implemented::

    from shared.llm_gateway import LLMClient, CircuitBreaker

    client = LLMClient(circuit_breaker=CircuitBreaker(failure_threshold=3))
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when the circuit is OPEN and the call is rejected immediately."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(
            f"Circuit breaker OPEN for model='{model}' — skipping to next fallback."
        )


@dataclass
class CircuitStatus:
    """Point-in-time status for one model's circuit."""

    model: str
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0


class CircuitBreaker:
    """Per-model circuit breaker with CLOSED → OPEN → HALF_OPEN state machine.

    This is a stub — all methods raise NotImplementedError.
    Replace with a real implementation when ready.

    Expected constructor args (once implemented):
        failure_threshold: int = 5       # consecutive failures before opening
        cool_down_seconds: float = 60.0  # how long to stay OPEN before probing
    """

    def is_open(self, model: str) -> bool:
        """Return True if the circuit is OPEN for this model.

        Call this BEFORE dispatching — return True means skip the model.

        Raises:
            NotImplementedError: until this is implemented.
        """
        # TODO: check per-model state dict
        # TODO: if OPEN and cool_down elapsed → transition to HALF_OPEN
        # TODO: if HALF_OPEN → allow exactly one probe (thread-safe)
        raise NotImplementedError(
            "CircuitBreaker.is_open() is not yet implemented. "
            "See circuit_breaker.py for the implementation plan."
        )

    def record_success(self, model: str) -> None:
        """Record a successful call. Transitions HALF_OPEN → CLOSED.

        Call this in handle_outcome when a response is returned.
        """
        # TODO: if HALF_OPEN → CLOSED, reset failure_count
        # TODO: if CLOSED → increment success_count (optional metric)
        raise NotImplementedError("CircuitBreaker.record_success() is not yet implemented.")

    def record_failure(self, model: str) -> None:
        """Record a failed call. May transition CLOSED → OPEN or HALF_OPEN → OPEN.

        Call this in handle_outcome when last_error is set.
        """
        # TODO: increment failure_count
        # TODO: if count >= threshold → OPEN, record open_ts
        # TODO: if HALF_OPEN → OPEN (reset cool-down timer)
        raise NotImplementedError("CircuitBreaker.record_failure() is not yet implemented.")

    def get_state(self, model: str) -> CircuitStatus:
        """Return the current circuit status for a model (for dashboards)."""
        # TODO: read from per-model state dict
        raise NotImplementedError("CircuitBreaker.get_state() is not yet implemented.")

    def reset(self, model: str | None = None) -> None:
        """Force-close the circuit. Pass model=None to reset all models."""
        # TODO: clear per-model state dict (or single model entry)
        raise NotImplementedError("CircuitBreaker.reset() is not yet implemented.")
