"""Shared fixtures and helpers for LLM Gateway tests.

All LLM calls are mocked — no real API traffic is generated.
File I/O goes to pytest's tmp_path so tests are hermetic.
"""
from __future__ import annotations

import httpx
import pytest

from shared.llm_gateway.client import LLMResponse
from shared.llm_gateway.config import LLMGatewaySettings
from shared.llm_gateway.fallback import FallbackChain, ModelEndpoint
from shared.llm_gateway.tracker import RequestTracker


# ──────────────────────────────────────────── groq exception helpers

def make_groq_status_error(cls, status_code: int = 429):
    """Construct a real groq SDK status-error (needs an httpx.Response)."""
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(status_code, request=req)
    return cls("test error", response=resp, body=None)


def make_groq_connection_error():
    import groq
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return groq.APIConnectionError(message="connection failed", request=req)


def make_groq_timeout_error():
    import groq
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return groq.APITimeoutError(request=req)


def make_httpx_status_error(status_code: int):
    req = httpx.Request("POST", "https://api.example.com/chat/completions")
    resp = httpx.Response(status_code, request=req)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=req, response=resp)


# ──────────────────────────────────────────── chain / tracker fixtures

def _fast_settings(**overrides) -> LLMGatewaySettings:
    """Settings with retries and delays zeroed out for fast tests."""
    defaults = dict(
        max_retries_per_model=0,
        retry_base_delay_seconds=0.0,
        tracking_enabled=False,
        groq_api_key="test-groq-key",
        fallback_1_model="model-a",
        fallback_1_connector="groq",
        fallback_2_model="model-b",
        fallback_2_connector="groq",
        fallback_3_model="model-c",
        fallback_3_connector="groq",
        primary_model="",
        primary_api_key="",
    )
    defaults.update(overrides)
    return LLMGatewaySettings(**defaults)


@pytest.fixture()
def fast_settings() -> LLMGatewaySettings:
    return _fast_settings()


@pytest.fixture()
def single_endpoint() -> ModelEndpoint:
    return ModelEndpoint(model="model-a", api_key="key", connector="groq", priority=0)


@pytest.fixture()
def two_endpoints() -> list[ModelEndpoint]:
    return [
        ModelEndpoint(model="model-a", api_key="key", connector="groq", priority=0),
        ModelEndpoint(model="model-b", api_key="key", connector="groq", priority=1),
    ]


@pytest.fixture()
def three_endpoints() -> list[ModelEndpoint]:
    return [
        ModelEndpoint(model="model-a", api_key="key", connector="groq", priority=0),
        ModelEndpoint(model="model-b", api_key="key", connector="groq", priority=1),
        ModelEndpoint(model="model-c", api_key="key", connector="groq", priority=2),
    ]


@pytest.fixture()
def single_chain(single_endpoint) -> FallbackChain:
    return FallbackChain.custom([single_endpoint])


@pytest.fixture()
def two_chain(two_endpoints) -> FallbackChain:
    return FallbackChain.custom(two_endpoints)


@pytest.fixture()
def three_chain(three_endpoints) -> FallbackChain:
    return FallbackChain.custom(three_endpoints)


@pytest.fixture()
def mem_tracker() -> RequestTracker:
    """In-memory only tracker (no file I/O)."""
    return RequestTracker(enabled=False)


@pytest.fixture()
def file_tracker(tmp_path) -> RequestTracker:
    """Tracker that writes to a temp file."""
    return RequestTracker(enabled=True, tracking_file=tmp_path / "tracking.txt")


# ──────────────────────────────────────────── response factory

def ok_response(content: str = "ok", model: str = "model-a",
                prompt_tokens: int = 10, completion_tokens: int = 5) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason="stop",
        raw={},
    )
