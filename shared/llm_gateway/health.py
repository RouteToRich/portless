"""Endpoint health monitor — placeholder, NOT YET IMPLEMENTED.

Hook point in LLMClient._node_call_endpoint():
    if self._health_monitor and not self._health_monitor.is_healthy(endpoint.model):
        # skip without spending tokens on a probe
        raise EndpointUnhealthyError(model=endpoint.model)

Why this is needed:
    The fallback chain advances on failure, but each advance costs one
    retry budget worth of latency (up to timeout_seconds per attempt).
    A background health monitor proactively marks endpoints as degraded
    so the gateway skips them without waiting for a timeout.

    It also surfaces endpoint health on a /health FastAPI route so
    the ops team can see which models are degraded at a glance.

Implementation plan:
    1. Background async task (asyncio.create_task) polls each endpoint
       every health_interval_seconds with a cheap "ping" call.
    2. Ping: call the endpoint with a 1-token prompt; mark healthy/unhealthy
       based on success or exception.
    3. Thread-safe: asyncio.Lock per model for probe results.
    4. Expose:
       is_healthy(model) → bool
       get_status() → dict[str, EndpointHealth]
    5. FastAPI integration:
       @app.get("/health") → returns gateway + per-model health.
    6. Config fields on LLMGatewaySettings (already added as placeholders):
       LLM_HEALTH_CHECK_ENABLED, LLM_HEALTH_CHECK_INTERVAL_SECONDS,
       LLM_HEALTH_CHECK_TIMEOUT_SECONDS.

Usage once implemented::

    from shared.llm_gateway import LLMClient
    from shared.llm_gateway.health import HealthMonitor

    monitor = HealthMonitor(check_interval_seconds=30)
    client = LLMClient(health_monitor=monitor)
    await monitor.start_background_check(chain=client._chain)

    # FastAPI route
    @app.get("/health")
    async def health():
        return monitor.get_status()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class EndpointUnhealthyError(Exception):
    """Raised when a call is skipped because the endpoint is known-unhealthy."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(
            f"Endpoint '{model}' is marked unhealthy by HealthMonitor — skipping."
        )


@dataclass
class EndpointHealth:
    """Current health snapshot for one model endpoint."""

    model: str
    status: HealthStatus = HealthStatus.UNKNOWN
    last_check_ts: float = 0.0        # epoch seconds of last probe
    consecutive_failures: int = 0
    last_error: str = ""


class HealthMonitor:
    """Background health monitor for LLM endpoints.

    This is a stub — all methods raise NotImplementedError.
    Replace with a real implementation when ready.

    Expected constructor args (once implemented):
        check_interval_seconds: float = 30.0
        check_timeout_seconds: float = 5.0
        failure_threshold: int = 2   # how many fails to mark degraded
    """

    async def start_background_check(self, chain: Any) -> None:
        """Launch a background asyncio task that probes each endpoint periodically.

        Call this once after creating LLMClient, e.g. from an app lifespan handler.
        The task runs until the process exits or stop() is called.

        Raises:
            NotImplementedError: until this is implemented.
        """
        # TODO: for each endpoint in chain.get_ordered_endpoints():
        #       asyncio.create_task(_probe_loop(endpoint))
        raise NotImplementedError(
            "HealthMonitor.start_background_check() is not yet implemented. "
            "See health.py for the implementation plan."
        )

    def is_healthy(self, model: str) -> bool:
        """Return False only when the model is confirmed DEGRADED.

        Returns True for UNKNOWN (first run) to avoid false-positive skips.
        """
        # TODO: read from per-model status dict
        # TODO: return status != DEGRADED
        raise NotImplementedError("HealthMonitor.is_healthy() is not yet implemented.")

    def get_status(self) -> dict[str, EndpointHealth]:
        """Return {model: EndpointHealth} for all monitored endpoints.

        Used by FastAPI /health endpoint.
        """
        # TODO: return a copy of the per-model health dict
        raise NotImplementedError("HealthMonitor.get_status() is not yet implemented.")

    async def stop(self) -> None:
        """Cancel the background probe tasks gracefully."""
        # TODO: cancel all running asyncio tasks; await them
        raise NotImplementedError("HealthMonitor.stop() is not yet implemented.")
