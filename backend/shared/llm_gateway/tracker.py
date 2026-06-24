"""Request tracking: logs every LLM call with latency, tokens, model, and status."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class RequestRecord:
    """A single tracked LLM request/response cycle."""

    request_id: str = field(default_factory=lambda: str(uuid4()))
    caller: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    status: str = "pending"  # pending | success | error | fallback
    error_message: str = ""
    fallback_model: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


class RequestTracker:
    """In-memory request tracker with optional persistent flush.

    Stores request records for observability. Can be extended with a
    database flush for long-term analytics.
    """

    def __init__(self, enabled: bool | None = None) -> None:
        from .config import llm_gateway_settings

        self._enabled = enabled if enabled is not None else llm_gateway_settings.tracking_enabled
        self._records: list[RequestRecord] = []

    def start_request(self, caller: str, model: str, **metadata: Any) -> RequestRecord:
        """Create and register a new request record."""
        record = RequestRecord(caller=caller, model=model, metadata=metadata)
        if self._enabled:
            self._records.append(record)
        return record

    def complete_request(
        self,
        record: RequestRecord,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status: str = "success",
        error_message: str = "",
        fallback_model: str = "",
    ) -> None:
        """Mark a request as completed with usage stats."""
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        record.total_tokens = prompt_tokens + completion_tokens
        record.latency_ms = latency_ms
        record.status = status
        record.error_message = error_message
        record.fallback_model = fallback_model

        if status == "error":
            logger.warning(
                "LLM request failed: id=%s model=%s error=%s",
                record.request_id,
                record.model,
                error_message,
            )
        else:
            logger.info(
                "LLM request: id=%s model=%s tokens=%d latency=%.0fms status=%s",
                record.request_id,
                record.model,
                record.total_tokens,
                latency_ms,
                status,
            )

    def get_records(self, limit: int = 100) -> list[RequestRecord]:
        """Return recent request records."""
        return self._records[-limit:]

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate statistics."""
        if not self._records:
            return {"total_requests": 0}

        total = len(self._records)
        successes = sum(1 for r in self._records if r.status == "success")
        errors = sum(1 for r in self._records if r.status == "error")
        fallbacks = sum(1 for r in self._records if r.status == "fallback")
        total_tokens = sum(r.total_tokens for r in self._records)
        avg_latency = (
            sum(r.latency_ms for r in self._records if r.latency_ms > 0)
            / max(successes + fallbacks, 1)
        )

        return {
            "total_requests": total,
            "successes": successes,
            "errors": errors,
            "fallbacks": fallbacks,
            "total_tokens": total_tokens,
            "avg_latency_ms": round(avg_latency, 1),
        }
