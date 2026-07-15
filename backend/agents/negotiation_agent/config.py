"""Negotiation Agent configuration (§4.3.1)."""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class NegotiationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB persistence ────────────────────────────────────────────────
    negotiation_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("NEGOTIATION_MONGO_URI", "MONGO_URI")
    )
    negotiation_mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("NEGOTIATION_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    negotiation_sessions_collection: str = "sessions"
    negotiation_rounds_collection: str = "rounds"

    # ── LLM (via shared.llm_gateway.LLMClient) ────────────────────────────
    # Model selection and API key are configured in the shared LLM Gateway:
    #   LLM_PRIMARY_CONNECTOR=anthropic
    #   LLM_PRIMARY_MODEL=claude-sonnet-4-6
    #   LLM_PRIMARY_API_KEY=<your anthropic key>
    # These two fields are passed as per-call overrides to LLMClient.chat().
    negotiation_llm_temperature: float = 0.3
    negotiation_llm_max_tokens: int = 4096
    """4096, not 2048: the Groq fallback models (qwen3-*) are reasoning models
    that emit a <think> block before the JSON answer. At 2048 the supplier
    agent's draft was intermittently truncated mid-JSON and silently fell back
    to a hardcoded one-line template. Overridable via NEGOTIATION_LLM_MAX_TOKENS."""

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

    # ── RAG ────────────────────────────────────────────────────────────────
    rag_enabled: bool = Field(default=True, validation_alias="NEGOTIATION_RAG_ENABLED")
    """Set NEGOTIATION_RAG_ENABLED=false to skip the shared RAG pipeline
    entirely and always use the static playbook stub. Escape hatch for
    environments where the RAG pipeline's local reranker (cross-encoder/
    ms-marco-MiniLM-L-6-v2) blocks on a HuggingFace Hub connectivity check
    that never returns — a genuinely synchronous call, so rag_timeout_seconds
    below cannot preempt it (asyncio.wait_for can't cancel non-yielding code)
    and it freezes the whole single-process asyncio server, not just this
    request."""

    rag_timeout_seconds: float = 15.0
    """Wall-clock budget for load_rag_context(). The underlying pipeline's own
    exception handling only covers calls that raise — a call that hangs (e.g.
    a vector-store or embedding-model call with no timeout of its own) would
    otherwise block negotiation start/round indefinitely. Exceeding this
    budget is treated the same as any other RAG failure: fall back to the
    static playbook stub."""

    # ── Market Intelligence (live web search) ──────────────────────────────
    market_search_enabled: bool = Field(default=False, validation_alias="NEGOTIATION_MARKET_SEARCH_ENABLED")
    """Set NEGOTIATION_MARKET_SEARCH_ENABLED=true to fetch live web-search market
    pricing/demand color (Tavily) before each buyer/supplier email draft. Off by
    default — mirrors shared/rag's own RAG_WEB_SEARCH_ENABLED default for the
    identical underlying mechanism, since this is an additional open-internet
    retrieval leg with a prompt-injection surface, not core infrastructure like
    RAG. No-op even when true if TAVILY_API_KEY is unset (fails open, no network
    call). Results are screened by the same prompt-injection filter the RAG
    pipeline uses before reaching any drafting prompt."""

    fallback_usd_inr_rate: float = Field(
        default=96.0, validation_alias="NEGOTIATION_FALLBACK_USD_INR_RATE"
    )
    """Used when a live rate can't be fetched (market_search_enabled=False,
    no TAVILY_API_KEY, or the fetch/parse fails) - the supplier agent still
    needs some rate to avoid guessing one itself. Periodically-updated
    static value, not a live feed; last checked 2026-07 (~95.9-96.2 range)."""


settings = NegotiationSettings()
