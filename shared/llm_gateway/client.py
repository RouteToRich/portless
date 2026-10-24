"""LLM Client: every chat completion in PortLess, sent to the shared LLM Gateway.

PortLess holds no model provider key and imports no provider SDK. ``chat()``
posts to the RouteToRich LLM Gateway through the API gateway's ``/api/llm``
route (``remote.py``), exactly as makemerich-backend does — the gateway owns
model routing, the fallback chain, retries, provider keys and cost metering.

What stays here:
  * the request tracker — one local record per call (caller, model the gateway
    served, tokens, latency) in Mongo ``llm_requests`` or the JSONL file;
  * the response cache (opt-in, shared MongoDB TTL cache);
  * PortLess's error contract: a failed gateway call raises
    ``LLMExhaustedException`` (callers degrade: template negotiation drafts,
    the exception resolver escalates), a missing URL / credential raises
    ``LLMConfigurationError``.

No request is silently dropped: every call that leaves the process lands in
the tracker regardless of outcome.
"""
from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config import LLM_MAX_TOKENS, LLM_TEMPERATURE, LLMGatewaySettings, llm_gateway_settings
from .tracker import RequestRecord, RequestTracker

if TYPE_CHECKING:
    from .cache import ResponseCache  # lazy at runtime — avoids a circular import

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────── response

@dataclass
class LLMResponse:
    """Structured response returned by ``LLMClient.chat``."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    raw: dict[str, Any]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ─────────────────────────────────────────────── main client

class LLMClient:
    """Gateway-backed LLM client with per-request tracking.

    Usage::

        client = LLMClient()
        response = await client.chat(
            messages=[{"role": "user", "content": "Negotiate 5% discount."}],
            caller="negotiation_agent",
        )
        print(response.content, response.total_tokens)
        print(client.tracker.get_stats())
    """

    def __init__(
        self,
        tracker: RequestTracker | None = None,
        settings: LLMGatewaySettings | None = None,
        cache: ResponseCache | None = None,
    ) -> None:
        self._settings = settings if settings is not None else llm_gateway_settings

        if tracker is not None:
            # Explicit tracker passed — use it as-is (sync or async)
            self._tracker: Any = tracker
        elif self._settings.mongo_tracker_enabled and self._settings.mongo_uri:
            # Auto-select MongoDB tracker (Motor client is lazy-connect — no I/O here)
            from .db_tracker import MongoTracker
            self._tracker = MongoTracker.from_settings(self._settings)
        else:
            # Fallback to JSONL file tracker for dev / no-mongo environments
            self._tracker = RequestTracker(
                enabled=self._settings.tracking_enabled,
                tracking_file=self._settings.tracking_file_path,
            )

        # ── response cache (shared MongoDB TTL cache; opt-in) ──────────────
        if cache is not None:
            self._cache: Any = cache
        elif self._settings.cache_enabled:
            from .cache import ResponseCache  # lazy — avoids circular import
            self._cache = ResponseCache(ttl_seconds=self._settings.cache_ttl_seconds)
        else:
            self._cache = None

    @property
    def tracker(self) -> Any:
        """Return the underlying tracker (sync RequestTracker or async MongoTracker)."""
        return self._tracker

    # ─────────────────────── async-transparent tracker dispatch ─────────────
    # LLMClient supports both sync (RequestTracker) and async (MongoTracker)
    # trackers.  These helpers check inspect.iscoroutinefunction at call time
    # so no upfront isinstance() check is needed and both work identically.

    async def _tracker_start(self, caller: str, model: str) -> RequestRecord:
        if inspect.iscoroutinefunction(self._tracker.start_request):
            return await self._tracker.start_request(caller=caller, model=model)
        return self._tracker.start_request(caller=caller, model=model)

    async def _tracker_complete(self, record: RequestRecord, **kwargs: Any) -> None:
        try:
            if inspect.iscoroutinefunction(self._tracker.complete_request):
                await self._tracker.complete_request(record, **kwargs)
            else:
                self._tracker.complete_request(record, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Tracker.complete_request raised unexpectedly — "
                "LLM response is unaffected but this record may be lost. %s: %s",
                type(exc).__name__, exc,
            )

    # ---------------------------------------------------------------- public

    async def chat(
        self,
        messages: list[dict[str, str]],
        caller: str = "unknown",
        temperature: float | None = None,
        max_tokens: int | None = None,
        category: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat request through the shared LLM Gateway.

        ``category`` pins the gateway's routing category (unset → the gateway
        classifies the prompt itself). Extra kwargs (``top_p``,
        ``response_format`` …) are forwarded to the provider via the gateway's
        ``extra`` field.

        Raises:
            LLMExhaustedException: the gateway call failed (every model in the
                gateway's chain failed, throttled, credential refused, network).
            LLMConfigurationError: no gateway URL / credential configured.
        """
        temperature = temperature if temperature is not None else LLM_TEMPERATURE
        max_tokens = max_tokens if max_tokens is not None else LLM_MAX_TOKENS

        # Response cache — return a cached result for an identical request.
        cache_key: str | None = None
        if self._cache is not None:
            cache_key = self._cache.make_key(messages, f"llm-gateway:{category or ''}", temperature)
            cached = await self._cache.aget(cache_key)
            if cached is not None:
                logger.debug("LLM cache hit for caller=%s", caller)
                return cached

        record = await self._tracker_start(caller=caller, model="llm-gateway")
        start = time.perf_counter()
        try:
            response = await self._send(messages, caller, temperature, max_tokens, category, **kwargs)
        except LLMConfigurationError:
            await self._tracker_complete(record, status="error", error_message="gateway not configured")
            raise
        except Exception as exc:
            await self._tracker_complete(
                record,
                latency_ms=(time.perf_counter() - start) * 1000,
                status="error",
                error_message=str(exc),
            )
            raise LLMExhaustedException(f"LLM Gateway call failed: {exc}") from exc

        record.model = response.model
        await self._tracker_complete(
            record,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=(time.perf_counter() - start) * 1000,
            status="success",
        )
        if cache_key is not None:
            await self._cache.aset(cache_key, response)
        return response

    # ----------------------------------------------------------- dispatch

    async def _send(
        self,
        messages: list[dict[str, str]],
        caller: str,
        temperature: float,
        max_tokens: int,
        category: str | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """One ``POST /v1/llm`` to the gateway (see remote.py)."""
        from .remote import GatewayNotConfiguredError, get_remote_client

        try:
            remote = get_remote_client(self._settings)
        except GatewayNotConfiguredError as exc:
            raise LLMConfigurationError(str(exc)) from exc

        prefix = self._settings.gateway_caller_prefix
        gateway_caller = f"{prefix}.{caller}" if prefix else caller
        r = await remote.chat(
            messages,
            caller=gateway_caller,
            temperature=temperature,
            max_tokens=max_tokens,
            category=category,
            **kwargs,
        )
        logger.info(
            "LLM via gateway: caller=%s category=%s model=%s tokens=%s finish=%s",
            gateway_caller, r.category, r.model, r.total_tokens, r.finish_reason,
        )
        return LLMResponse(
            content=r.content,
            model=r.model,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            finish_reason=r.finish_reason,
            raw=r.raw,
        )


# ─────────────────────────────────────────────── exceptions

class LLMExhaustedException(Exception):
    """Raised when the gateway call failed — no model could answer."""


class LLMConfigurationError(Exception):
    """Raised when the gateway is not configured (no URL or credential)."""
