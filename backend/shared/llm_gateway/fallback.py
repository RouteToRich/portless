"""Model fallback chain: try models in priority order until one succeeds."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from .config import llm_gateway_settings

logger = logging.getLogger(__name__)


@dataclass
class ModelEndpoint:
    """A single LLM endpoint configuration."""

    model: str
    api_key: str
    base_url: str
    priority: int = 0  # lower = higher priority


@dataclass
class FallbackChain:
    """Ordered chain of model endpoints to try on failure.

    If the primary model fails (timeout, rate-limit, server error),
    the chain advances to the next model automatically.
    """

    endpoints: list[ModelEndpoint] = field(default_factory=list)

    @classmethod
    def from_settings(cls) -> FallbackChain:
        """Build a fallback chain from environment configuration."""
        endpoints: list[ModelEndpoint] = []

        if llm_gateway_settings.primary_api_key:
            endpoints.append(
                ModelEndpoint(
                    model=llm_gateway_settings.primary_model,
                    api_key=llm_gateway_settings.primary_api_key,
                    base_url=llm_gateway_settings.primary_base_url,
                    priority=0,
                )
            )

        if llm_gateway_settings.secondary_api_key:
            endpoints.append(
                ModelEndpoint(
                    model=llm_gateway_settings.secondary_model,
                    api_key=llm_gateway_settings.secondary_api_key,
                    base_url=llm_gateway_settings.secondary_base_url,
                    priority=1,
                )
            )

        if llm_gateway_settings.tertiary_api_key:
            endpoints.append(
                ModelEndpoint(
                    model=llm_gateway_settings.tertiary_model,
                    api_key=llm_gateway_settings.tertiary_api_key,
                    base_url=llm_gateway_settings.tertiary_base_url,
                    priority=2,
                )
            )

        endpoints.sort(key=lambda e: e.priority)
        return cls(endpoints=endpoints)

    def get_ordered_endpoints(self) -> Sequence[ModelEndpoint]:
        """Return endpoints in fallback order."""
        return self.endpoints

    def get_primary(self) -> ModelEndpoint | None:
        """Return the highest-priority endpoint, or None if chain is empty."""
        return self.endpoints[0] if self.endpoints else None
