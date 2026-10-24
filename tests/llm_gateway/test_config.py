"""Tests for LLMGatewaySettings — gateway-only configuration (no provider keys)."""
from __future__ import annotations

from shared.llm_gateway.config import (
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    LLMGatewaySettings,
    llm_gateway_settings,
)


def test_no_provider_key_settings_exist():
    # PortLess holds no model provider credential — the gateway owns them.
    fields = set(LLMGatewaySettings.model_fields)
    for removed in ("groq_api_key", "primary_api_key", "primary_model",
                    "fallback_1_model"):
        assert removed not in fields


def test_auth_disabled_by_default():
    assert llm_gateway_settings.auth_enabled is False


def test_gateway_defaults():
    s = LLMGatewaySettings(gateway_url="")
    assert s.gateway_llm_path == "/api/llm"
    assert s.gateway_auth_path == "/auth/login"
    assert s.gateway_caller_prefix == "portless"
    assert s.gateway_configured is False


def test_gateway_configured_needs_url_and_credential():
    url = "https://gw.test"
    assert LLMGatewaySettings(gateway_url=url, gateway_token="t").gateway_configured
    assert LLMGatewaySettings(
        gateway_url=url, gateway_token="", gateway_email="a@b.c", gateway_password="p"
    ).gateway_configured
    assert not LLMGatewaySettings(
        gateway_url=url, gateway_token="", gateway_email="a@b.c", gateway_password=""
    ).gateway_configured
    assert not LLMGatewaySettings(gateway_url="", gateway_token="t").gateway_configured


def test_tracking_enabled_by_default():
    assert llm_gateway_settings.tracking_enabled is True


def test_tracking_file_path_default():
    assert "llm_gateway_tracking" in llm_gateway_settings.tracking_file_path


def test_llm_temperature_constant_matches_settings():
    assert LLM_TEMPERATURE == llm_gateway_settings.llm_temperature


def test_llm_max_tokens_constant_matches_settings():
    assert LLM_MAX_TOKENS == llm_gateway_settings.llm_max_tokens


def test_temperature_reasonable_range():
    s = LLMGatewaySettings(llm_temperature=0.5)
    assert 0.0 <= s.llm_temperature <= 1.0
