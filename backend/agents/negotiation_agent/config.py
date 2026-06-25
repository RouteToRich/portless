"""Negotiation Agent configuration (§4.3.1)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class NegotiationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB persistence ────────────────────────────────────────────────
    negotiation_mongo_uri: str = ""
    negotiation_mongo_db_name: str = "negotiation"
    negotiation_sessions_collection: str = "sessions"
    negotiation_rounds_collection: str = "rounds"

    # ── LLM (via shared.llm_gateway.LLMClient) ────────────────────────────
    # Model selection and API key are configured in the shared LLM Gateway:
    #   LLM_PRIMARY_CONNECTOR=anthropic
    #   LLM_PRIMARY_MODEL=claude-sonnet-4-6
    #   LLM_PRIMARY_API_KEY=<your anthropic key>
    # These two fields are passed as per-call overrides to LLMClient.chat().
    negotiation_llm_temperature: float = 0.3
    negotiation_llm_max_tokens: int = 2048

    # ── Email outbound (Gmail SMTP via aiosmtplib) ────────────────────────
    # Gmail App Password is required — NOT the regular account password.
    # Create one at: https://myaccount.google.com/apppasswords
    # Leave GMAIL_SENDER_ADDRESS empty to run in log-only mode (dev/CI).
    gmail_sender_address: str = ""
    gmail_app_password: str = ""
    smtp_from_name: str = "PortLess Trade Agent"

    # ── IMAP inbox monitoring (placeholder) ───────────────────────────────
    # Monitor the configured inbox for buyer/supplier replies and route them
    # to the active negotiation session via run_round().
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True
    imap_poll_interval_seconds: int = 60
    imap_inbox_folder: str = "INBOX"

    # ── Redis (optional, for session locks) ───────────────────────────────
    # Falls back to in-memory dict when REDIS_URL is empty.
    redis_url: str = ""

    # ── Business defaults ─────────────────────────────────────────────────
    default_max_rounds: int = 5
    default_margin_floor_pct: float = 8.0
    default_concession_step_usd: float = 0.10
    hitl_margin_warning_pct: float = 2.0
    """Trigger HITL when margin is within this many pct points of the floor."""
    hitl_on_first_outbound: bool = True
    """Require operator sign-off before the very first outbound message."""


settings = NegotiationSettings()
