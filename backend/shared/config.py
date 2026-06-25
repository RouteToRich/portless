"""Centralized configuration: service registry URLs + cross-cutting settings.

Every service resolves its peers through this single settings object so that
moving a module to its own host only requires changing an environment variable.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Populate os.environ from backend/.env so connector classes that read
# os.environ[KEY] directly work in local dev.  In production (Docker/k8s)
# env vars are injected by the container runtime — load_dotenv is a no-op
# because override=False leaves real env vars untouched.
load_dotenv(Path(__file__).parent.parent / ".env", override=False)


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

    # --- Regulatory verification connector credentials ---
    api_setu_key: str = ""
    api_setu_client_id: str = ""
    allianz_trade_client_id: str = ""
    allianz_trade_client_secret: str = ""
    allianz_trade_webhook_secret: str = ""

    # --- Trade-intelligence connector credentials ---
    # Set in .env — see .env.example for registration instructions.
    comtrade_subscription_key: str = ""
    volza_api_key: str = ""
    alibaba_app_key: str = ""
    alibaba_app_secret: str = ""
    macmap_api_key: str = ""
    trademo_api_key: str = ""
    importgenius_api_key: str = ""
    indiamart_api_key: str = ""
    indiamart_mobile: str = ""
    tradeindia_api_key: str = ""

    # --- Cross-cutting ---
    http_timeout_seconds: float = 10.0
    log_level: str = "INFO"

    # --- UI / CORS ---
    cors_allow_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


settings = Settings()
