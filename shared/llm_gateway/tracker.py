"""Request tracker: records every LLM call to memory + an append-only JSONL file.

Every completed request (success, fallback, or error) is persisted so no call
goes untracked.  The file format is one JSON object per line, making it trivial
to tail, grep, or load into pandas later.  Swap the file writer for a DB flush
by replacing _append_to_file without changing any other interface.

Thread-safety: a threading.Lock guards both the in-memory list and file writes,
so the tracker is safe under FastAPI's async workers (which run in threads).
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class RequestRecord:
    """One tracked LLM call/response cycle."""

    request_id: str = field(default_factory=lambda: str(uuid4()))
    caller: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    status: str = "pending"   # pending | success | fallback | error
    error_message: str = ""
    fallback_model: str = ""
    retry_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


class RequestTracker:
    """In-memory tracker with append-only JSONL file persistence.

    Usage::

        tracker = RequestTracker()
        record = tracker.start_request(caller="negotiation_agent", model="qwen/qwen3-32b")
        # ... make the LLM call ...
        tracker.complete_request(record, prompt_tokens=800, completion_tokens=200,
                                 latency_ms=1230.5, status="success")
    """

    def __init__(
        self,
        enabled: bool | None = None,
        tracking_file: str | Path | None = None,
    ) -> None:
        from .config import llm_gateway_settings

        self._enabled = enabled if enabled is not None else llm_gateway_settings.tracking_enabled
        self._file_path = Path(tracking_file or llm_gateway_settings.tracking_file_path)
        self._records: list[RequestRecord] = []
        self._lock = threading.Lock()

        if self._enabled:
            try:
                self._file_path.parent.mkdir(parents=True, exist_ok=True)
                # Write a session-start marker so the file is easy to scan
                self._write_raw(
                    json.dumps({
                        "_marker": "session_start",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                )
            except OSError as exc:
                logger.warning(
                    "LLM tracking: cannot initialise file %s — %s. "
                    "Tracking will be in-memory only.",
                    self._file_path,
                    exc,
                )
                self._enabled = False

    # ----------------------------------------------------------------- public

    def start_request(self, caller: str, model: str, **metadata: Any) -> RequestRecord:
        """Create and register a new pending record."""
        record = RequestRecord(caller=caller, model=model, metadata=dict(metadata))
        with self._lock:
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
        retry_count: int = 0,
    ) -> None:
        """Finalise a record with usage stats and persist it.

        This is always called — even for error paths — so every initiated
        request has a corresponding completed entry in the tracking file.
        """
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        record.total_tokens = prompt_tokens + completion_tokens
        record.latency_ms = latency_ms
        record.status = status
        record.error_message = error_message
        record.fallback_model = fallback_model
        record.retry_count = retry_count

        if status == "error":
            logger.warning(
                "LLM request failed | id=%s model=%s retries=%d error=%s",
                record.request_id,
                record.model,
                retry_count,
                error_message,
            )
        else:
            logger.info(
                "LLM request ok | id=%s model=%s tokens=%d latency=%.0fms "
                "status=%s retries=%d fallback=%s",
                record.request_id,
                record.model,
                record.total_tokens,
                latency_ms,
                status,
                retry_count,
                fallback_model or "—",
            )

        if self._enabled:
            self._append_record(record)

    def get_records(self, limit: int = 100) -> list[RequestRecord]:
        with self._lock:
            return list(self._records[-limit:])

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._records)

        if not records:
            return {"total_requests": 0}

        total = len(records)
        successes = sum(1 for r in records if r.status == "success")
        errors = sum(1 for r in records if r.status == "error")
        fallbacks = sum(1 for r in records if r.status == "fallback")
        total_tokens = sum(r.total_tokens for r in records)
        completed = [r for r in records if r.latency_ms > 0]
        avg_latency = (
            sum(r.latency_ms for r in completed) / len(completed)
            if completed
            else 0.0
        )
        total_retries = sum(r.retry_count for r in records)

        return {
            "total_requests": total,
            "successes": successes,
            "errors": errors,
            "fallbacks": fallbacks,
            "success_rate": round(successes / total, 3) if total else 0.0,
            "total_tokens": total_tokens,
            "avg_latency_ms": round(avg_latency, 1),
            "total_retries": total_retries,
        }

    @property
    def tracking_file(self) -> Path:
        return self._file_path

    # --------------------------------------------------------------- private

    def _append_record(self, record: RequestRecord) -> None:
        line = json.dumps(
            {
                "request_id": record.request_id,
                "ts": record.created_at.isoformat(),
                "caller": record.caller,
                "model": record.model,
                "status": record.status,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
                "total_tokens": record.total_tokens,
                "latency_ms": round(record.latency_ms, 2),
                "retry_count": record.retry_count,
                "fallback_model": record.fallback_model,
                "error": record.error_message,
            },
            ensure_ascii=False,
        )
        self._write_raw(line)

    def _write_raw(self, line: str) -> None:
        """Thread-safe append of one line to the tracking file."""
        try:
            with self._lock:
                with open(self._file_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            logger.error(
                "LLM tracking: failed to write to %s — %s",
                self._file_path,
                exc,
            )
