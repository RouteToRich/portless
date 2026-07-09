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

    # ── Email transport (separate services) ───────────────────────────────
    # All email I/O lives in services/email_gateway/ (single IMAP + SMTP config):
    # it fetches inbound mail over IMAP and sends outbound over SMTP. Inbound
    # processing (match to negotiation, extract price, persist) lives in
    # services/email_processor/. The negotiation flow calls the Email Gateway to
    # deliver approved drafts.

    # ── Session locks ─────────────────────────────────────────────────────
    # Uses an in-memory dict (single-instance dev). For multi-replica
    # deployments use the shared MongoDB cache (shared.cache) as the lock store.

    # ── Business defaults ─────────────────────────────────────────────────
    default_max_rounds: int = 5
    default_margin_floor_pct: float = 8.0
    default_concession_step_usd: float = 0.10
    hitl_margin_warning_pct: float = 2.0
    """Trigger HITL when margin is within this many pct points of the floor."""
    hitl_on_first_outbound: bool = True
    """Require operator sign-off before the very first outbound message."""


settings = NegotiationSettings()
