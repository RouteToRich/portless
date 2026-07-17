"""Tests for FallbackChain assembly and classify_error."""
from __future__ import annotations

import httpx
import pytest

from shared.llm_gateway.config import LLMGatewaySettings
from shared.llm_gateway.fallback import (
    FallbackChain,
    ModelEndpoint,
    classify_error,
    get_retry_delay_seconds,
)

from .conftest import (
    make_groq_connection_error,
    make_groq_status_error,
    make_groq_timeout_error,
    make_httpx_status_error,
)


# ─────────────────────────────────── FallbackChain.from_settings


def test_default_chain_has_three_groq_endpoints():
    chain = FallbackChain.from_settings()
    endpoints = list(chain.get_ordered_endpoints())
    assert len(endpoints) == 3


def test_default_chain_all_groq_connector():
    chain = FallbackChain.from_settings()
    for ep in chain.get_ordered_endpoints():
        assert ep.connector == "groq"


def test_default_chain_model_names():
    # Reflects the live .env chain (see test_config.test_three_fallback_models_configured).
    chain = FallbackChain.from_settings()
    models = [ep.model for ep in chain.get_ordered_endpoints()]
    assert "llama-3.3-70b-versatile" in models
    assert "qwen/qwen3.6-27b" in models
    assert "openai/gpt-oss-safeguard-20b" in models


def test_default_chain_all_use_groq_api_key():
    from shared.llm_gateway.config import llm_gateway_settings
    chain = FallbackChain.from_settings()
    for ep in chain.get_ordered_endpoints():
        assert ep.api_key == llm_gateway_settings.groq_api_key


def test_primary_prepended_when_api_key_set(monkeypatch):
    settings = LLMGatewaySettings(
        primary_model="claude-sonnet-4-6",
        primary_api_key="sk-ant-test",
        primary_connector="anthropic",
        groq_api_key="gsk_test",
    )
    monkeypatch.setattr(
        "shared.llm_gateway.fallback.llm_gateway_settings", settings
    )
    chain = FallbackChain.from_settings()
    endpoints = list(chain.get_ordered_endpoints())
    assert len(endpoints) == 4
    assert endpoints[0].model == "claude-sonnet-4-6"
    assert endpoints[0].connector == "anthropic"


def test_primary_not_added_when_api_key_empty(monkeypatch):
    settings = LLMGatewaySettings(
        primary_model="claude-sonnet-4-6",
        primary_api_key="",           # no key → must not be added
        groq_api_key="gsk_test",
    )
    monkeypatch.setattr(
        "shared.llm_gateway.fallback.llm_gateway_settings", settings
    )
    chain = FallbackChain.from_settings()
    models = [ep.model for ep in chain.get_ordered_endpoints()]
    assert "claude-sonnet-4-6" not in models


def test_primary_not_added_when_model_name_empty(monkeypatch):
    settings = LLMGatewaySettings(
        primary_model="",             # key set but model name missing → not added
        primary_api_key="sk-ant-test",
        groq_api_key="gsk_test",
    )
    monkeypatch.setattr(
        "shared.llm_gateway.fallback.llm_gateway_settings", settings
    )
    chain = FallbackChain.from_settings()
    assert len(chain) == 3  # only the three Groq fallbacks


def test_empty_chain_when_all_keys_missing(monkeypatch):
    settings = LLMGatewaySettings(
        primary_model="", primary_api_key="",
        groq_api_key="",
        fallback_1_model="qwen/qwen3-32b", fallback_1_api_key="",
        fallback_2_model="qwen/qwen3.6-27b", fallback_2_api_key="",
        fallback_3_model="openai/gpt-oss-safeguard-20b", fallback_3_api_key="",
    )
    monkeypatch.setattr(
        "shared.llm_gateway.fallback.llm_gateway_settings", settings
    )
    chain = FallbackChain.from_settings()
    assert len(chain) == 0


# ─────────────────────────────────── FallbackChain.custom


def test_custom_chain_sorted_by_priority():
    chain = FallbackChain.custom([
        ModelEndpoint(model="model-z", api_key="k", priority=2),
        ModelEndpoint(model="model-a", api_key="k", priority=0),
        ModelEndpoint(model="model-m", api_key="k", priority=1),
    ])
    models = [ep.model for ep in chain.get_ordered_endpoints()]
    assert models == ["model-a", "model-m", "model-z"]


def test_custom_chain_empty():
    chain = FallbackChain.custom([])
    assert len(chain) == 0
    assert chain.get_primary() is None


def test_get_primary_returns_first():
    chain = FallbackChain.custom([
        ModelEndpoint(model="first", api_key="k", priority=0),
        ModelEndpoint(model="second", api_key="k", priority=1),
    ])
    assert chain.get_primary().model == "first"


def test_len():
    chain = FallbackChain.custom([
        ModelEndpoint(model="a", api_key="k"),
        ModelEndpoint(model="b", api_key="k"),
    ])
    assert len(chain) == 2


# ─────────────────────────────────── classify_error — groq


def test_groq_rate_limit_is_rate_limit():
    import groq
    exc = make_groq_status_error(groq.RateLimitError, 429)
    assert classify_error(exc) == "rate_limit"


def test_groq_bad_request_413_is_request_too_large():
    # Groq reports its "request too large ... tokens per minute" TPM quota
    # error as a 413 BadRequestError, not a 429 RateLimitError. Distinct from
    # "rate_limit" (429): waiting never helps a 413 since the oversized
    # request doesn't shrink between retries.
    import groq
    exc = make_groq_status_error(groq.BadRequestError, 413)
    assert classify_error(exc) == "request_too_large"


def test_groq_rate_limit_413_is_request_too_large():
    # Some Groq error paths raise RateLimitError (429 by default in the SDK)
    # even for a 413 response — classify_error must trust the actual
    # status_code on the exception, not the exception class alone.
    import groq
    exc = make_groq_status_error(groq.RateLimitError, 413)
    assert classify_error(exc) == "request_too_large"


def test_groq_internal_server_error_is_transient():
    import groq
    exc = make_groq_status_error(groq.InternalServerError, 500)
    assert classify_error(exc) == "transient"


def test_groq_connection_error_is_transient():
    exc = make_groq_connection_error()
    assert classify_error(exc) == "transient"


def test_groq_timeout_is_transient():
    exc = make_groq_timeout_error()
    assert classify_error(exc) == "transient"


def test_groq_authentication_error_is_fatal():
    import groq
    exc = make_groq_status_error(groq.AuthenticationError, 401)
    assert classify_error(exc) == "fatal"


def test_groq_bad_request_is_fatal():
    import groq
    exc = make_groq_status_error(groq.BadRequestError, 400)
    assert classify_error(exc) == "fatal"


def test_groq_permission_denied_is_fatal():
    import groq
    exc = make_groq_status_error(groq.PermissionDeniedError, 403)
    assert classify_error(exc) == "fatal"


# ─────────────────────────────────── classify_error — httpx


def test_httpx_read_timeout_is_transient():
    assert classify_error(httpx.ReadTimeout("timed out")) == "transient"


def test_httpx_connect_error_is_transient():
    assert classify_error(httpx.ConnectError("refused")) == "transient"


def test_httpx_429_is_rate_limit():
    assert classify_error(make_httpx_status_error(429)) == "rate_limit"


def test_httpx_413_is_request_too_large():
    assert classify_error(make_httpx_status_error(413)) == "request_too_large"


def test_httpx_500_is_transient():
    assert classify_error(make_httpx_status_error(500)) == "transient"


def test_httpx_502_is_transient():
    assert classify_error(make_httpx_status_error(502)) == "transient"


def test_httpx_503_is_transient():
    assert classify_error(make_httpx_status_error(503)) == "transient"


def test_httpx_400_is_fatal():
    assert classify_error(make_httpx_status_error(400)) == "fatal"


def test_httpx_401_is_fatal():
    assert classify_error(make_httpx_status_error(401)) == "fatal"


def test_httpx_403_is_fatal():
    assert classify_error(make_httpx_status_error(403)) == "fatal"


# ─────────────────────────────────── classify_error — generic


def test_generic_exception_with_429_status_code_is_rate_limit():
    class ApiError(Exception):
        status_code = 429
    assert classify_error(ApiError()) == "rate_limit"


def test_generic_exception_with_413_status_code_is_request_too_large():
    class ApiError(Exception):
        status_code = 413
    assert classify_error(ApiError()) == "request_too_large"


def test_generic_exception_with_401_status_code_is_fatal():
    class ApiError(Exception):
        status_code = 401
    assert classify_error(ApiError()) == "fatal"


def test_generic_exception_with_response_status_code():
    class FakeResponse:
        status_code = 500

    class ApiError(Exception):
        response = FakeResponse()

    assert classify_error(ApiError()) == "transient"


def test_unknown_exception_defaults_to_transient():
    assert classify_error(ValueError("something unexpected")) == "transient"


def test_runtime_error_defaults_to_transient():
    assert classify_error(RuntimeError("boom")) == "transient"


# ─────────────────────────────────── get_retry_delay_seconds


def test_rate_limit_delay_uses_floor_when_no_retry_after():
    exc = RuntimeError("Request too large ... tokens per minute (TPM): Limit 6000")
    delay = get_retry_delay_seconds(exc, base_delay=1.0, attempt=0)
    assert delay >= 20.0  # default rate_limit_retry_floor_seconds


def test_rate_limit_delay_parses_retry_after_from_message():
    exc = RuntimeError("rate limited, please retry after 5.5 seconds")
    delay = get_retry_delay_seconds(exc, base_delay=1.0, attempt=0)
    assert delay == 5.5


def test_rate_limit_delay_prefers_response_header():
    class FakeHeaders(dict):
        pass

    class FakeResponse:
        headers = FakeHeaders({"retry-after": "12"})

    class ApiError(Exception):
        response = FakeResponse()

    delay = get_retry_delay_seconds(ApiError(), base_delay=1.0, attempt=0)
    assert delay == 12.0
