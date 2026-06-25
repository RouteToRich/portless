"""LLM Client: resilient chat completion with automatic per-model retry and
model-level fallback, backed by request tracking.

Fallback flow
─────────────
For each endpoint in the chain (primary → groq-1 → groq-2 → groq-3):

  1. Call the endpoint.
  2. If transient error and retries_remaining > 0:
       wait (base_delay × 2^attempt), increment attempt, go to 1.
  3. If fatal error OR retries exhausted:
       log, persist error record, advance to next endpoint.
  4. If success: persist record, return response.
  5. If all endpoints exhausted: raise LLMExhaustedException.

No request is silently dropped.  Every attempt that reaches complete_request()
lands in the tracking file regardless of outcome.

Connector dispatch
──────────────────
  connector="groq"       → ChatGroq from langchain_groq
  connector="openai"     → ChatOpenAI from langchain_openai
  connector="anthropic"  → ChatAnthropic from langchain_anthropic
  connector="http"       → raw httpx POST to base_url/chat/completions
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, TypedDict

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from .config import LLM_MAX_TOKENS, LLM_TEMPERATURE, LLMGatewaySettings, llm_gateway_settings
from .fallback import FallbackChain, ModelEndpoint, classify_error
from .tracker import RequestRecord, RequestTracker

# Placeholder imports — None until each feature is implemented.
# TODO(rate_limiter):   from .rate_limiter import RateLimiter, RateLimitExceeded
# TODO(circuit_breaker): from .circuit_breaker import CircuitBreaker, CircuitOpenError
# TODO(cost_estimator): from .cost_estimator import CostEstimator, BudgetExceeded
# TODO(cache):          from .cache import ResponseCache
# TODO(health):         from .health import HealthMonitor, EndpointUnhealthyError
# TODO(prompt_guard):   from .prompt_guard import PromptGuard, PromptBlocked

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────── response

@dataclass
class LLMResponse:
    """Structured response returned from any connector."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    raw: dict[str, Any]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ─────────────────────────────────────────────── LangGraph state

class LLMState(TypedDict):
    messages: list[dict[str, str]]
    caller: str
    temperature: float
    max_tokens: int
    extra_kwargs: dict[str, Any]
    endpoints: list[ModelEndpoint]
    current_idx: int
    response: LLMResponse | None
    last_error: Exception | None
    record: RequestRecord | None
    start_time: float


# ─────────────────────────────────────────────── message helpers

_ROLE_TO_LC: dict[str, type] = {
    "user": HumanMessage,
    "human": HumanMessage,
    "assistant": AIMessage,
    "system": SystemMessage,
}


def _to_langchain_messages(messages: list[dict[str, str]]) -> list:
    result = []
    for m in messages:
        cls = _ROLE_TO_LC.get(m.get("role", "user"), HumanMessage)
        result.append(cls(content=m.get("content", "")))
    return result


# ─────────────────────────────────────────────── connector factory

def _build_langchain_client(endpoint: ModelEndpoint) -> BaseChatModel:
    """Return a bare LangChain chat model for the given connector.

    Temperature and max_tokens are NOT set here — callers apply them via
    .bind() so each invocation can override them independently.
    """
    if endpoint.connector == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=endpoint.model, api_key=endpoint.api_key)

    if endpoint.connector == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=endpoint.model, api_key=endpoint.api_key)

    if endpoint.connector == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=endpoint.model, api_key=endpoint.api_key)

    raise LLMConfigurationError(
        f"Unknown connector '{endpoint.connector}' for model '{endpoint.model}'. "
        "Supported: groq, openai, anthropic, http."
    )


# ─────────────────────────────────────────────── main client

class LLMClient:
    """Resilient LLM client with automatic fallback and per-request tracking.

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
        fallback_chain: FallbackChain | None = None,
        tracker: RequestTracker | None = None,
        settings: LLMGatewaySettings | None = None,
        # ── future feature hooks (all disabled / None by default) ──────────
        # TODO(rate_limiter):   rate_limiter: RateLimiter | None = None,
        # TODO(circuit_breaker): circuit_breaker: CircuitBreaker | None = None,
        # TODO(cost_estimator): cost_estimator: CostEstimator | None = None,
        # TODO(cache):          cache: ResponseCache | None = None,
        # TODO(health):         health_monitor: HealthMonitor | None = None,
        # TODO(prompt_guard):   prompt_guard: PromptGuard | None = None,
    ) -> None:
        # Use `is None` — FallbackChain has __len__, so an empty chain is falsy
        # and `or` would silently replace it with from_settings().
        self._chain = fallback_chain if fallback_chain is not None else FallbackChain.from_settings()
        self._settings = settings if settings is not None else llm_gateway_settings

        if tracker is not None:
            # Explicit tracker passed — use it as-is (sync or async)
            self._tracker: Any = tracker
        elif self._settings.mongo_tracker_enabled:
            # Auto-select MongoDB tracker (Motor client is lazy-connect — no I/O here)
            from .db_tracker import MongoTracker
            self._tracker = MongoTracker.from_settings(self._settings)
        else:
            # Fallback to JSONL file tracker for dev / no-mongo environments
            self._tracker = RequestTracker(
                enabled=self._settings.tracking_enabled,
                tracking_file=self._settings.tracking_file_path,
            )

        # ── future feature hook storage ────────────────────────────────────
        # TODO(rate_limiter):   self._rate_limiter = rate_limiter
        # TODO(circuit_breaker): self._circuit_breaker = circuit_breaker
        # TODO(cost_estimator): self._cost_estimator = cost_estimator
        # TODO(cache):          self._cache = cache
        # TODO(health):         self._health_monitor = health_monitor
        # TODO(prompt_guard):   self._prompt_guard = prompt_guard
        self._graph = self._build_graph()

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
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat request, trying each model in the fallback chain.

        Raises:
            LLMExhaustedException: all models failed after their retry budgets.
            LLMConfigurationError: the chain has no endpoints.
        """
        endpoints = list(self._chain.get_ordered_endpoints())
        if not endpoints:
            raise LLMConfigurationError(
                "LLM Gateway fallback chain is empty — check configuration."
            )

        # TODO(prompt_guard): check messages before dispatch
        # if self._prompt_guard:
        #     result = self._prompt_guard.check(messages)
        #     if result.blocked:
        #         raise PromptBlocked(reason=result.reason, category=result.category)

        # TODO(cache): return cached response if available
        # if self._cache:
        #     key = self._cache.make_key(messages, endpoints[0].model, temperature or LLM_TEMPERATURE)
        #     cached = self._cache.get(key)
        #     if cached is not None:
        #         return cached

        # TODO(rate_limiter): enforce per-caller rate limits
        # if self._rate_limiter:
        #     self._rate_limiter.check(caller=caller, model=endpoints[0].model)

        # TODO(cost_estimator): budget gate before spending tokens
        # if self._cost_estimator:
        #     self._cost_estimator.check_budget(caller=caller, estimated_cost=0.01)

        initial_state: LLMState = {
            "messages": messages,
            "caller": caller,
            "temperature": temperature if temperature is not None else LLM_TEMPERATURE,
            "max_tokens": max_tokens if max_tokens is not None else LLM_MAX_TOKENS,
            "extra_kwargs": kwargs,
            "endpoints": endpoints,
            "current_idx": 0,
            "response": None,
            "last_error": None,
            "record": None,
            "start_time": 0.0,
        }

        final_state = await self._graph.ainvoke(initial_state)

        if final_state["response"] is not None:
            return final_state["response"]

        raise LLMExhaustedException(
            f"All {len(endpoints)} model(s) in the fallback chain exhausted. "
            f"Last error: {final_state['last_error']}"
        )

    # --------------------------------------------------------- LangGraph nodes

    def _build_graph(self) -> Any:
        builder = StateGraph(LLMState)
        builder.add_node("init_request", self._node_init_request)
        builder.add_node("call_endpoint", self._node_call_endpoint)
        builder.add_node("handle_outcome", self._node_handle_outcome)

        builder.set_entry_point("init_request")
        builder.add_edge("init_request", "call_endpoint")
        builder.add_edge("call_endpoint", "handle_outcome")

        def _route(state: LLMState) -> str:
            if state["response"] is not None:
                return END
            if state["current_idx"] < len(state["endpoints"]):
                return "init_request"
            return END

        builder.add_conditional_edges(
            "handle_outcome",
            _route,
            {"init_request": "init_request", END: END},
        )
        return builder.compile()

    async def _node_init_request(self, state: LLMState) -> dict[str, Any]:
        endpoint = state["endpoints"][state["current_idx"]]
        record = await self._tracker_start(caller=state["caller"], model=endpoint.model)
        return {"record": record, "start_time": time.perf_counter()}

    async def _node_call_endpoint(self, state: LLMState) -> dict[str, Any]:
        """Call the current endpoint with per-model retries.

        Retries on transient errors with exponential backoff.
        Skips retries immediately on fatal errors.
        Updates record.retry_count in-place so the tracker knows how many
        attempts were made when handle_outcome finalises the record.
        """
        endpoint = state["endpoints"][state["current_idx"]]
        record: RequestRecord | None = state["record"]
        max_retries = self._settings.max_retries_per_model
        base_delay = self._settings.retry_base_delay_seconds
        last_exc: Exception | None = None

        # TODO(circuit_breaker): skip endpoint if circuit is open
        # if self._circuit_breaker and self._circuit_breaker.is_open(endpoint.model):
        #     return {"last_error": CircuitOpenError(model=endpoint.model), "response": None}

        # TODO(health): skip endpoint if health monitor marks it degraded
        # if self._health_monitor and not self._health_monitor.is_healthy(endpoint.model):
        #     return {"last_error": EndpointUnhealthyError(model=endpoint.model), "response": None}

        for attempt in range(max_retries + 1):  # 0, 1, …, max_retries
            if record is not None:
                record.retry_count = attempt  # live-update so handle_outcome sees it

            try:
                response = await self._dispatch(
                    endpoint=endpoint,
                    messages=state["messages"],
                    temperature=state["temperature"],
                    max_tokens=state["max_tokens"],
                    **state["extra_kwargs"],
                )
                return {"response": response, "last_error": None}

            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                error_class = classify_error(exc)

                if error_class == "fatal":
                    logger.warning(
                        "Fatal error on %s — skipping retries. %s: %s",
                        endpoint.model,
                        type(exc).__name__,
                        exc,
                    )
                    break  # advance to next model immediately

                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "Transient error on %s (attempt %d/%d) — retrying in %.1fs. %s: %s",
                        endpoint.model,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        "Retries exhausted for %s after %d attempt(s). %s: %s",
                        endpoint.model,
                        max_retries + 1,
                        type(exc).__name__,
                        exc,
                    )

        return {"last_error": last_exc, "response": None}

    async def _node_handle_outcome(self, state: LLMState) -> dict[str, Any]:
        """Persist the result in the tracker and advance the chain index on failure."""
        endpoint = state["endpoints"][state["current_idx"]]
        latency = (time.perf_counter() - state["start_time"]) * 1000
        is_fallback = state["current_idx"] > 0
        record: RequestRecord = state["record"]  # type: ignore[assignment]

        if state["response"] is not None:
            await self._tracker_complete(
                record,
                prompt_tokens=state["response"].prompt_tokens,
                completion_tokens=state["response"].completion_tokens,
                latency_ms=latency,
                status="fallback" if is_fallback else "success",
                fallback_model=endpoint.model if is_fallback else "",
                retry_count=record.retry_count,
            )
            # TODO(circuit_breaker): record success so HALF_OPEN → CLOSED
            # if self._circuit_breaker:
            #     self._circuit_breaker.record_success(endpoint.model)

            # TODO(cost_estimator): compute + accumulate per-call cost
            # if self._cost_estimator:
            #     cost = self._cost_estimator.estimate(
            #         model=endpoint.model,
            #         prompt_tokens=state["response"].prompt_tokens,
            #         completion_tokens=state["response"].completion_tokens,
            #     )
            #     self._cost_estimator.record(caller=state["caller"], cost_usd=cost)
            #     record.metadata["estimated_cost_usd"] = cost

            # TODO(rate_limiter): update TPM window after a successful call
            # if self._rate_limiter:
            #     self._rate_limiter.record_tokens_used(
            #         caller=state["caller"],
            #         model=endpoint.model,
            #         tokens_used=state["response"].total_tokens,
            #     )

            # TODO(cache): store response so identical future calls are instant
            # if self._cache:
            #     key = self._cache.make_key(
            #         state["messages"], endpoint.model, state["temperature"]
            #     )
            #     self._cache.set(key, state["response"])

            return {}  # success → graph routes to END

        # Model failed — track the error, then advance the chain
        await self._tracker_complete(
            record,
            latency_ms=latency,
            status="error",
            error_message=str(state["last_error"]),
            retry_count=record.retry_count,
        )
        # TODO(circuit_breaker): record failure; may open the circuit
        # if self._circuit_breaker:
        #     self._circuit_breaker.record_failure(endpoint.model)
        next_idx = state["current_idx"] + 1
        if next_idx < len(state["endpoints"]):
            logger.info(
                "Advancing fallback chain: %s failed → trying %s",
                endpoint.model,
                state["endpoints"][next_idx].model,
            )
        else:
            logger.error(
                "All models exhausted after %s failed. No more fallbacks.",
                endpoint.model,
            )
        return {"current_idx": next_idx}

    # ----------------------------------------------------------- dispatch

    async def _dispatch(
        self,
        endpoint: ModelEndpoint,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> LLMResponse:
        if endpoint.connector in ("groq", "openai", "anthropic"):
            return await self._call_langchain(endpoint, messages, temperature, max_tokens, **kwargs)
        return await self._call_http(endpoint, messages, temperature, max_tokens, **kwargs)

    async def _call_langchain(
        self,
        endpoint: ModelEndpoint,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> LLMResponse:
        """Invoke a LangChain-backed model (Groq / OpenAI / Anthropic)."""
        lc_client = _build_langchain_client(endpoint)
        lc_messages = _to_langchain_messages(messages)
        bound = lc_client.bind(temperature=temperature, max_tokens=max_tokens, **kwargs)
        ai_msg: AIMessage = await bound.ainvoke(lc_messages)

        usage = getattr(ai_msg, "usage_metadata", None) or {}
        return LLMResponse(
            content=str(ai_msg.content),
            model=endpoint.model,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            finish_reason=getattr(ai_msg, "response_metadata", {}).get("finish_reason", "stop"),
            raw={"usage_metadata": usage},
        )

    async def _call_http(
        self,
        endpoint: ModelEndpoint,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> LLMResponse:
        """POST to an OpenAI-compatible /chat/completions endpoint via httpx."""
        if not endpoint.base_url:
            raise LLMConfigurationError(
                f"base_url is required for connector='http' (model={endpoint.model})"
            )
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        async with httpx.AsyncClient(
            timeout=self._settings.request_timeout_seconds
        ) as http_client:
            resp = await http_client.post(
                f"{endpoint.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {endpoint.api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()

        data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return LLMResponse(
            content=choice["message"]["content"],
            model=data.get("model", endpoint.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", ""),
            raw=data,
        )


# ─────────────────────────────────────────────── exceptions

class LLMExhaustedException(Exception):
    """Raised when every model in the fallback chain has failed."""


class LLMConfigurationError(Exception):
    """Raised when the gateway is misconfigured (no endpoints, bad connector, etc.)."""
