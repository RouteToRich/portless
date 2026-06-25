"""Tests for LLM Gateway configuration."""
from __future__ import annotations

from shared.llm_gateway.config import (
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    LLMGatewaySettings,
    llm_gateway_settings,
)


def test_groq_api_key_has_correct_prefix():
    assert llm_gateway_settings.groq_api_key.startswith("gsk_")


def test_three_fallback_models_configured():
    assert llm_gateway_settings.fallback_1_model == "qwen/qwen3-32b"
    assert llm_gateway_settings.fallback_2_model == "qwen/qwen3.6-27b"
    assert llm_gateway_settings.fallback_3_model == "openai/gpt-oss-safeguard-20b"


def test_all_fallback_connectors_are_groq():
    assert llm_gateway_settings.fallback_1_connector == "groq"
    assert llm_gateway_settings.fallback_2_connector == "groq"
    assert llm_gateway_settings.fallback_3_connector == "groq"


def test_auth_disabled_by_default():
    assert llm_gateway_settings.auth_enabled is False


def test_primary_api_key_empty_by_default():
    # primary is opt-in; Groq fallbacks handle all traffic until a primary is set
    assert llm_gateway_settings.primary_api_key == ""


def test_retry_defaults():
    assert llm_gateway_settings.max_retries_per_model == 2
    assert llm_gateway_settings.retry_base_delay_seconds == 1.0


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


def test_custom_settings_override():
    s = LLMGatewaySettings(
        max_retries_per_model=5,
        retry_base_delay_seconds=2.0,
        groq_api_key="gsk_test",
    )
    assert s.max_retries_per_model == 5
    assert s.retry_base_delay_seconds == 2.0
    assert s.groq_api_key == "gsk_test"
