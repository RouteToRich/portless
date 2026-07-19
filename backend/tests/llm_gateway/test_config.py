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
    # These reflect the live shared/llm_gateway/.env, not the class defaults.
    # 2026-07-18: fallback_1 moved off llama-3.3-70b-versatile (Groq deprecates
    # it 2026-08-16) to openai/gpt-oss-120b, Groq's own recommended replacement
    # and a different model family from fallback_2's Qwen. fallback_3 moved off
    # openai/gpt-oss-safeguard-20b (a content-moderation classifier, not a chat
    # model — can't draft negotiation text) to its general-purpose sibling
    # gpt-oss-20b. Update here whenever the .env chain is deliberately re-tuned.
    # All three must be real text-generation chat models (never a *-guard
    # classifier).
    assert llm_gateway_settings.fallback_1_model == "openai/gpt-oss-120b"
    assert llm_gateway_settings.fallback_2_model == "qwen/qwen3.6-27b"
    assert llm_gateway_settings.fallback_3_model == "openai/gpt-oss-20b"


def test_all_fallback_connectors_are_groq():
    assert llm_gateway_settings.fallback_1_connector == "groq"
    assert llm_gateway_settings.fallback_2_connector == "groq"
    assert llm_gateway_settings.fallback_3_connector == "groq"


def test_auth_disabled_by_default():
    assert llm_gateway_settings.auth_enabled is False


def test_primary_configured_as_openai_gpt4o_mini():
    # 2026-07-18: primary activated — OpenAI gpt-4o-mini is now tried before the
    # Groq fallback chain (see test_fallback.test_primary_from_live_env_is_openai).
    assert llm_gateway_settings.primary_model == "gpt-4o-mini"
    assert llm_gateway_settings.primary_connector == "openai"
    assert llm_gateway_settings.primary_api_key.startswith("sk-proj-")


def test_retry_defaults():
    # shared/llm_gateway/.env deliberately overrides this to 1 (down from the
    # class default of 2) — Groq's API is unreachable on this dev network, so
    # a shorter retry budget bounds a blocked call to ~6s instead of ~12s.
    assert llm_gateway_settings.max_retries_per_model == 1
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
