"""LLM Client: the main entry point for making LLM calls with fallback and tracking."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TypedDict

import httpx
from langgraph.graph import StateGraph, END

from .config import llm_gateway_settings
from .fallback import FallbackChain, ModelEndpoint
from .tracker import RequestRecord, RequestTracker

logger = logging.getLogger(__name__)


class LLMState(TypedDict):
    """Internal state for the LangGraph-based LLM Gateway request flow."""
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


class LLMClient:
    """Resilient LLM client with automatic model fallback and request tracking.

    Usage:
        client = LLMClient()
        response = await client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            caller="negotiation_agent",
        )
    """

    def __init__(
        self,
        fallback_chain: FallbackChain | None = None,
        tracker: RequestTracker | None = None,
    ) -> None:
        self._chain = fallback_chain or FallbackChain.from_settings()
        self._tracker = tracker or RequestTracker()
        self._graph = self._build_graph()

    @property
    def tracker(self) -> RequestTracker:
        return self._tracker

    async def chat(
        self,
        messages: list[dict[str, str]],
        caller: str = "unknown",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat completion request with automatic fallback on failure using LangGraph.

        Tries each model in the fallback chain. On success returns the response;
        on total failure raises LLMExhaustedException.
        """
        endpoints = self._chain.get_ordered_endpoints()
        if not endpoints:
            raise LLMConfigurationError("No model endpoints configured")

        initial_state: LLMState = {
            "messages": messages,
            "caller": caller,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_kwargs": kwargs,
            "endpoints": list(endpoints),
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
            f"All models in fallback chain exhausted. Last error: {final_state['last_error']}"
        )

    def _build_graph(self) -> Any:
        builder = StateGraph(LLMState)
        builder.add_node("init_request", self._init_request_node)
        builder.add_node("call_endpoint", self._call_endpoint_node)
        builder.add_node("handle_outcome", self._handle_outcome_node)

        builder.set_entry_point("init_request")
        builder.add_edge("init_request", "call_endpoint")
        builder.add_edge("call_endpoint", "handle_outcome")

        def route_next(state: LLMState) -> str:
            if state["response"] is not None:
                return END
            if state["current_idx"] < len(state["endpoints"]):
                return "init_request"
            return END

        builder.add_conditional_edges(
            "handle_outcome",
            route_next,
            {"init_request": "init_request", END: END}
        )
        return builder.compile()

    async def _init_request_node(self, state: LLMState) -> dict[str, Any]:
        endpoint = state["endpoints"][state["current_idx"]]
        record = self._tracker.start_request(caller=state["caller"], model=endpoint.model)
        return {"record": record, "start_time": time.perf_counter()}

    async def _call_endpoint_node(self, state: LLMState) -> dict[str, Any]:
        endpoint = state["endpoints"][state["current_idx"]]
        try:
            response = await self._call_endpoint(
                endpoint=endpoint,
                messages=state["messages"],
                temperature=state["temperature"],
                max_tokens=state["max_tokens"],
                **state["extra_kwargs"],
            )
            return {"response": response, "last_error": None}
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            return {"last_error": e, "response": None}

    async def _handle_outcome_node(self, state: LLMState) -> dict[str, Any]:
        endpoint = state["endpoints"][state["current_idx"]]
        latency = (time.perf_counter() - state["start_time"]) * 1000

        if state["response"] is not None:
            primary_model = state["endpoints"][0].model
            self._tracker.complete_request(
                state["record"],  # type: ignore[arg-type]
                prompt_tokens=state["response"].prompt_tokens,
                completion_tokens=state["response"].completion_tokens,
                latency_ms=latency,
                status="fallback" if endpoint.model != primary_model else "success",
                fallback_model=endpoint.model if endpoint.model != primary_model else "",
            )
            return {}
        else:
            self._tracker.complete_request(
                state["record"],  # type: ignore[arg-type]
                latency_ms=latency,
                status="error",
                error_message=str(state["last_error"]),
            )
            logger.warning(
                "Model %s failed (%s), trying next in chain...",
                endpoint.model,
                type(state["last_error"]).__name__,
            )
            await asyncio.sleep(llm_gateway_settings.retry_delay_seconds)
            return {"current_idx": state["current_idx"] + 1}

    async def _call_endpoint(
        self,
        endpoint: ModelEndpoint,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> LLMResponse:
        """Make a single chat completion call to an OpenAI-compatible endpoint."""
        base_url = endpoint.base_url.rstrip("/")
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        async with httpx.AsyncClient(
            timeout=llm_gateway_settings.request_timeout_seconds
        ) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
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


class LLMResponse:
    """Structured response from an LLM call."""

    __slots__ = ("content", "model", "prompt_tokens", "completion_tokens", "finish_reason", "raw")

    def __init__(
        self,
        content: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        finish_reason: str,
        raw: dict[str, Any],
    ) -> None:
        self.content = content
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.finish_reason = finish_reason
        self.raw = raw

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMExhaustedException(Exception):
    """All models in the fallback chain failed."""


class LLMConfigurationError(Exception):
    """LLM gateway is misconfigured (no endpoints available)."""
