"""Tests for LLMClient — tracking and error contract around the gateway call.

``LLMClient._send`` (the one POST to the gateway) is patched; the HTTP layer
itself is covered in test_remote.py.
"""
from __future__ import annotations

import pytest

from shared.llm_gateway.client import (
    LLMClient,
    LLMConfigurationError,
    LLMExhaustedException,
    LLMResponse,
)
from shared.llm_gateway.config import LLMGatewaySettings
from shared.llm_gateway.remote import GatewayError

from .conftest import ok_response


def _settings(**overrides) -> LLMGatewaySettings:
    defaults = {"gateway_url": "", "tracking_enabled": False, "cache_enabled": False}
    defaults.update(overrides)
    return LLMGatewaySettings(**defaults)


def _client(mem_tracker, monkeypatch, send) -> LLMClient:
    monkeypatch.setattr(LLMClient, "_send", send)
    return LLMClient(tracker=mem_tracker, settings=_settings())


def test_llm_response_total_tokens():
    assert ok_response(prompt_tokens=7, completion_tokens=3).total_tokens == 10


async def test_success_tracked_with_served_model(mem_tracker, monkeypatch):
    async def send(self, messages, caller, temperature, max_tokens, category, **kw):
        return ok_response(model="groq/served", prompt_tokens=4, completion_tokens=2)

    client = _client(mem_tracker, monkeypatch, send)
    resp = await client.chat([{"role": "user", "content": "hi"}], caller="negotiation_agent")

    assert isinstance(resp, LLMResponse)
    (record,) = mem_tracker.get_records()
    assert record.caller == "negotiation_agent"
    assert record.model == "groq/served"
    assert record.status == "success"
    assert record.total_tokens == 6


async def test_defaults_and_overrides_reach_send(mem_tracker, monkeypatch):
    seen = {}

    async def send(self, messages, caller, temperature, max_tokens, category, **kw):
        seen.update(temperature=temperature, max_tokens=max_tokens, category=category, kw=kw)
        return ok_response()

    client = _client(mem_tracker, monkeypatch, send)
    await client.chat([{"role": "user", "content": "x"}], temperature=0.1, max_tokens=99,
                      category="reasoning", top_p=0.5)
    assert seen == {"temperature": 0.1, "max_tokens": 99, "category": "reasoning",
                    "kw": {"top_p": 0.5}}


async def test_gateway_error_becomes_exhausted_and_is_tracked(mem_tracker, monkeypatch):
    async def send(self, *a, **kw):
        raise GatewayError(503, "all_models_failed", "every model failed")

    client = _client(mem_tracker, monkeypatch, send)
    with pytest.raises(LLMExhaustedException, match="503"):
        await client.chat([{"role": "user", "content": "x"}])
    (record,) = mem_tracker.get_records()
    assert record.status == "error"
    assert "all_models_failed" in record.error_message


async def test_unconfigured_gateway_raises_configuration_error(mem_tracker):
    client = LLMClient(tracker=mem_tracker, settings=_settings())
    with pytest.raises(LLMConfigurationError, match="LLM_GATEWAY_URL"):
        await client.chat([{"role": "user", "content": "x"}])
