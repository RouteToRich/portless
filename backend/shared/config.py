"""Centralized configuration: service registry URLs + cross-cutting settings.

Every service resolves its peers through this single settings object so that
moving a module to its own host only requires changing an environment variable.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Service registry (inter-service REST base URLs) ---
    api_gateway_url: str = "http://localhost:8000"
    deals_url: str = "http://localhost:8001"
    trade_min_url: str = "http://localhost:8002"
    match_engine_url: str = "http://localhost:8003"
    sourcing_url: str = "http://localhost:8004"
    negotiation_agent_url: str = "http://localhost:8005"
    dealmaker_url: str = "http://localhost:8006"
    shield_link_url: str = "http://localhost:8007"
    docugen_url: str = "http://localhost:8008"
    escrow_guard_url: str = "http://localhost:8009"
    hitl_approval_url: str = "http://localhost:8010"
    exception_resolver_url: str = "http://localhost:8011"

    # --- Trade-intelligence connector credentials ---
    # Set in .env — see .env.example for registration instructions.
    comtrade_subscription_key: str = ""
    volza_api_key: str = ""
    alibaba_app_key: str = ""
    alibaba_app_secret: str = ""

    # --- Cross-cutting ---
    http_timeout_seconds: float = 10.0
    log_level: str = "INFO"

    # --- UI / CORS ---
    cors_allow_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


settings = Settings()
