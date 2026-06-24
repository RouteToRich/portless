"""LLM Gateway configuration."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMGatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    # --- JWT authentication ---
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "portless"
    jwt_audience: str = "portless-llm-gateway"

    # --- Primary model ---
    primary_model: str = "claude-sonnet-4-20250514"
    primary_api_key: str = ""
    primary_base_url: str = "https://api.anthropic.com/v1"

    # --- Secondary fallback model ---
    secondary_model: str = "gpt-4o"
    secondary_api_key: str = ""
    secondary_base_url: str = "https://api.openai.com/v1"

    # --- Tertiary fallback model ---
    tertiary_model: str = "gpt-4o-mini"
    tertiary_api_key: str = ""
    tertiary_base_url: str = "https://api.openai.com/v1"

    # --- Request settings ---
    request_timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_delay_seconds: float = 1.0

    # --- Tracking ---
    tracking_enabled: bool = True
    tracking_database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/portless"


llm_gateway_settings = LLMGatewaySettings()
