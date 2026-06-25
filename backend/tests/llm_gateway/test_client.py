"""Tests for LLMClient — fallback flow, retry logic, tracking integration.

All LLM calls go through a patched _dispatch so no real API traffic is sent.
asyncio.sleep is patched in retry tests so the suite runs in milliseconds.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.llm_gateway.client import (
    LLMClient,
    LLMConfigurationError,
    LLMExhaustedException,
    LLMResponse,
    _to_langchain_messages,
)
from shared.llm_gateway.config import LLMGatewaySettings
from shared.llm_gateway.fallback import FallbackChain, ModelEndpoint
from shared.llm_gateway.tracker import RequestTracker

from .conftest import make_groq_status_error, make_httpx_status_error, ok_response


# ─────────────────────────────────── helpers

def _client(chain: FallbackChain, tracker: RequestTracker,
            max_retries: int = 0) -> LLMClient:
    settings = LLMGatewaySettings(
        max_retries_per_model=max_retries,
        retry_base_delay_seconds=0.0,
        tracking_enabled=False,
    )
    return LLMClient(fallback_chain=chain, tracker=tracker, settings=settings)


MESSAGES = [{"role": "user", "content": "hello"}]


# ─────────────────────────────────── _to_langchain_messages

def test_to_langchain_messages_user():
    from langchain_core.messages import HumanMessage
    msgs = _to_langchain_messages([{"role": "user", "content": "hi"}])
    assert len(msgs) == 1
    assert isinstance(msgs[0], HumanMessage)
    assert msgs[0].content == "hi"


def test_to_langchain_messages_system_and_user():
    from langchain_core.messages import HumanMessage, SystemMessage
    msgs = _to_langchain_messages([
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "What is 2+2?"},
    ])
    assert isinstance(msgs[0], SystemMessage)
    assert isinstance(msgs[1], HumanMessage)


def test_to_langchain_messages_unknown_role_defaults_to_human():
    from langchain_core.messages import HumanMessage
    msgs = _to_langchain_messages([{"role": "unknown", "content": "hi"}])
    assert isinstance(msgs[0], HumanMessage)


# ─────────────────────────────────── LLMResponse


def test_llm_response_total_tokens():
    r = LLMResponse("hi", "m", prompt_tokens=10, completion_tokens=5,
                    finish_reason="stop", raw={})
    assert r.total_tokens == 15


# ─────────────────────────────────── success path


async def test_chat_success_returns_response(single_chain, mem_tracker):
    client = _client(single_chain, mem_tracker)
    expected = ok_response(content="negotiation response")

    with patch.object(client, "_dispatch", new=AsyncMock(return_value=expected)):
        resp = await client.chat(MESSAGES, caller="negotiation_agent")

    assert resp.content == "negotiation response"
    assert resp.total_tokens == 15


async def test_chat_success_records_caller(single_chain, mem_tracker):
    client = _client(single_chain, mem_tracker)
    with patch.object(client, "_dispatch", new=AsyncMock(return_value=ok_response())):
        await client.chat(MESSAGES, caller="trade_min")

    records = mem_tracker.get_records()
    assert records[0].caller == "trade_min"


async def test_chat_uses_first_endpoint_on_success(two_chain, mem_tracker):
    client = _client(two_chain, mem_tracker)
    with patch.object(client, "_dispatch", new=AsyncMock(return_value=ok_response())):
        await client.chat(MESSAGES)

    records = mem_tracker.get_records()
    assert records[0].model == "model-a"
    assert records[0].status == "success"


async def test_chat_passes_caller_temperature_max_tokens(single_chain, mem_tracker):
    client = _client(single_chain, mem_tracker)
    captured = {}

    async def _spy(endpoint, messages, temperature, max_tokens, **kwargs):
        captured.update(temperature=temperature, max_tokens=max_tokens)
        return ok_response()

    with patch.object(client, "_dispatch", new=_spy):
        await client.chat(MESSAGES, temperature=0.3, max_tokens=512)

    assert captured["temperature"] == 0.3
    assert captured["max_tokens"] == 512


# ─────────────────────────────────── fallback path


async def test_first_model_fails_falls_to_second(two_chain, mem_tracker):
    client = _client(two_chain, mem_tracker)
    calls: list[str] = []

    async def _dispatch(endpoint, messages, temperature, max_tokens, **kwargs):
        calls.append(endpoint.model)
        if endpoint.model == "model-a":
            raise httpx.ReadTimeout("timeout")
        return ok_response(model="model-b")

    with patch.object(client, "_dispatch", new=_dispatch):
        resp = await client.chat(MESSAGES)

    assert resp.model == "model-b"
    assert calls == ["model-a", "model-b"]


async def test_second_model_tracked_as_fallback(two_chain, mem_tracker):
    client = _client(two_chain, mem_tracker)

    async def _dispatch(endpoint, messages, temperature, max_tokens, **kwargs):
        if endpoint.model == "model-a":
            raise httpx.ReadTimeout("timeout")
        return ok_response(model="model-b")

    with patch.object(client, "_dispatch", new=_dispatch):
        await client.chat(MESSAGES)

    records = mem_tracker.get_records()
    error_rec = next(r for r in records if r.status == "error")
    fallback_rec = next(r for r in records if r.status == "fallback")
    assert error_rec.model == "model-a"
    assert fallback_rec.model == "model-b"
    assert fallback_rec.fallback_model == "model-b"


async def test_all_three_models_tried(three_chain, mem_tracker):
    client = _client(three_chain, mem_tracker)
    calls: list[str] = []

    async def _dispatch(endpoint, messages, temperature, max_tokens, **kwargs):
        calls.append(endpoint.model)
        if endpoint.model != "model-c":
            raise httpx.ReadTimeout("timeout")
        return ok_response(model="model-c")

    with patch.object(client, "_dispatch", new=_dispatch):
        resp = await client.chat(MESSAGES)

    assert resp.model == "model-c"
    assert calls == ["model-a", "model-b", "model-c"]


async def test_all_models_fail_raises_exhausted(three_chain, mem_tracker):
    client = _client(three_chain, mem_tracker)
    with patch.object(client, "_dispatch",
                      new=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))):
        with pytest.raises(LLMExhaustedException, match="3 model"):
            await client.chat(MESSAGES)


async def test_all_models_failure_tracks_each_as_error(three_chain, mem_tracker):
    client = _client(three_chain, mem_tracker)
    with patch.object(client, "_dispatch",
                      new=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))):
        with pytest.raises(LLMExhaustedException):
            await client.chat(MESSAGES)

    records = mem_tracker.get_records()
    assert len(records) == 3
    assert all(r.status == "error" for r in records)


async def test_error_message_captured_in_record(single_chain, mem_tracker):
    client = _client(single_chain, mem_tracker)
    with patch.object(client, "_dispatch",
                      new=AsyncMock(side_effect=httpx.ReadTimeout("read timeout occurred"))):
        with pytest.raises(LLMExhaustedException):
            await client.chat(MESSAGES)

    record = mem_tracker.get_records()[0]
    assert "read timeout occurred" in record.error_message or "ReadTimeout" in record.error_message


# ─────────────────────────────────── retry logic


async def test_transient_error_retried_before_fallback(two_chain, mem_tracker):
    """With max_retries=2, a transient error is retried twice before advancing."""
    client = _client(two_chain, mem_tracker, max_retries=2)
    call_count = 0

    async def _dispatch(endpoint, messages, temperature, max_tokens, **kwargs):
        nonlocal call_count
        if endpoint.model == "model-a":
            call_count += 1
            raise httpx.ReadTimeout("rate limited")
        return ok_response(model="model-b")

    with patch("shared.llm_gateway.client.asyncio.sleep", new=AsyncMock()):
        with patch.object(client, "_dispatch", new=_dispatch):
            await client.chat(MESSAGES)

    # 1 initial + 2 retries = 3 calls on model-a, then success on model-b
    assert call_count == 3


async def test_fatal_error_skips_retries(two_chain, mem_tracker):
    """A fatal error (e.g. bad request) must not trigger any retries."""
    import groq
    client = _client(two_chain, mem_tracker, max_retries=2)
    call_count = 0

    async def _dispatch(endpoint, messages, temperature, max_tokens, **kwargs):
        nonlocal call_count
        if endpoint.model == "model-a":
            call_count += 1
            raise make_groq_status_error(groq.BadRequestError, 400)
        return ok_response(model="model-b")

    with patch("shared.llm_gateway.client.asyncio.sleep", new=AsyncMock()):
        with patch.object(client, "_dispatch", new=_dispatch):
            await client.chat(MESSAGES)

    assert call_count == 1  # exactly one attempt, no retries


async def test_retry_count_tracked_in_record(single_chain, mem_tracker):
    """record.retry_count reflects actual attempts made on that model."""
    client = _client(single_chain, mem_tracker, max_retries=2)
    call_num = 0

    async def _dispatch(endpoint, messages, temperature, max_tokens, **kwargs):
        nonlocal call_num
        call_num += 1
        if call_num < 3:
            raise httpx.ReadTimeout("timeout")
        return ok_response()

    with patch("shared.llm_gateway.client.asyncio.sleep", new=AsyncMock()):
        with patch.object(client, "_dispatch", new=_dispatch):
            await client.chat(MESSAGES)

    record = mem_tracker.get_records()[0]
    assert record.retry_count == 2  # attempts 0, 1 failed; attempt 2 succeeded


async def test_backoff_sleep_called_on_transient_error(single_chain, mem_tracker):
    """asyncio.sleep must be called between retries."""
    client = _client(single_chain, mem_tracker, max_retries=2)

    with patch("shared.llm_gateway.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with patch.object(client, "_dispatch",
                          new=AsyncMock(side_effect=httpx.ReadTimeout("t"))):
            with pytest.raises(LLMExhaustedException):
                await client.chat(MESSAGES)

    # 2 retries → 2 sleep calls
    assert mock_sleep.call_count == 2


async def test_backoff_delay_doubles(single_chain, mem_tracker):
    """Delays follow exponential backoff: base, base×2."""
    settings = LLMGatewaySettings(
        max_retries_per_model=2,
        retry_base_delay_seconds=1.0,
        tracking_enabled=False,
    )
    client = LLMClient(fallback_chain=single_chain, tracker=mem_tracker, settings=settings)
    delays: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        delays.append(secs)

    with patch("shared.llm_gateway.client.asyncio.sleep", new=_fake_sleep):
        with patch.object(client, "_dispatch",
                          new=AsyncMock(side_effect=httpx.ReadTimeout("t"))):
            with pytest.raises(LLMExhaustedException):
                await client.chat(MESSAGES)

    assert delays == [1.0, 2.0]


# ─────────────────────────────────── empty chain / config errors


async def test_empty_chain_raises_config_error(mem_tracker):
    empty = FallbackChain.custom([])
    client = _client(empty, mem_tracker)
    with pytest.raises(LLMConfigurationError, match="empty"):
        await client.chat(MESSAGES)


async def test_http_connector_missing_base_url_raises(mem_tracker):
    ep = ModelEndpoint(model="custom", api_key="key", connector="http", base_url="")
    chain = FallbackChain.custom([ep])
    client = _client(chain, mem_tracker)
    with pytest.raises(LLMExhaustedException):
        await client.chat(MESSAGES)

    records = mem_tracker.get_records()
    assert "base_url" in records[0].error_message


async def test_unknown_connector_raises_config_error(mem_tracker):
    ep = ModelEndpoint(model="custom", api_key="key", connector="unknown")
    chain = FallbackChain.custom([ep])
    client = _client(chain, mem_tracker)
    with pytest.raises(LLMExhaustedException):
        await client.chat(MESSAGES)

    records = mem_tracker.get_records()
    assert records[0].status == "error"


# ─────────────────────────────────── tracking integration


async def test_latency_recorded(single_chain, mem_tracker):
    client = _client(single_chain, mem_tracker)
    with patch.object(client, "_dispatch", new=AsyncMock(return_value=ok_response())):
        await client.chat(MESSAGES)

    record = mem_tracker.get_records()[0]
    assert record.latency_ms > 0


async def test_token_counts_recorded(single_chain, mem_tracker):
    client = _client(single_chain, mem_tracker)
    resp = ok_response(prompt_tokens=120, completion_tokens=40)
    with patch.object(client, "_dispatch", new=AsyncMock(return_value=resp)):
        await client.chat(MESSAGES)

    record = mem_tracker.get_records()[0]
    assert record.prompt_tokens == 120
    assert record.completion_tokens == 40
    assert record.total_tokens == 160


async def test_tracker_stats_after_mixed_results(two_chain, mem_tracker):
    client = _client(two_chain, mem_tracker)
    # Run once: first model fails, second succeeds → 1 error + 1 fallback
    async def _dispatch(endpoint, messages, temperature, max_tokens, **kwargs):
        if endpoint.model == "model-a":
            raise httpx.ReadTimeout("t")
        return ok_response()

    with patch.object(client, "_dispatch", new=_dispatch):
        await client.chat(MESSAGES)

    stats = mem_tracker.get_stats()
    assert stats["total_requests"] == 2
    assert stats["errors"] == 1
    assert stats["fallbacks"] == 1
    assert stats["successes"] == 0
