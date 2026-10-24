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
    # Model selection and provider keys live in the shared LLM Gateway
    # (LLM_GATEWAY_URL); PortLess holds none. These two fields are passed as
    # per-call overrides to LLMClient.chat().
    negotiation_llm_temperature: float = 0.3
    negotiation_llm_max_tokens: int = 4096
    """4096, not 2048: the gateway's chain includes reasoning models (qwen3-*)
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

    # ── Stale-negotiation watcher ─────────────────────────────────────────
    # Background sweep (started from the app lifespan) that finds negotiation
    # sessions stuck in a non-terminal status (STARTED / ROUND / HITL_PENDING)
    # and unblocks or terminates them, so a stalled deal never sits invisibly
    # in NEGOTIATING forever. Two cases, two thresholds:
    #   • round 0 never sent (round_no == 0): the first-outbound SEND_NEGOTIATION_EMAIL
    #     L0 approval was never actioned → re-notify it so it resurfaces in the
    #     Approvals Inbox (we never auto-send the first email without a human).
    #   • post-round-0 gone quiet (round_no >= 1): after the cooling period with
    #     no activity (updated_at age) → mark the session FAILED, which advances
    #     the deal to NEGOTIATION_FAILED and hands off to the Exception Resolver.
    stale_watcher_enabled: bool = Field(
        default=True, validation_alias="NEGOTIATION_STALE_WATCHER_ENABLED"
    )
    stale_watcher_interval_seconds: float = Field(
        default=300.0, validation_alias="NEGOTIATION_STALE_WATCHER_INTERVAL_SECONDS"
    )
    """How often the sweep runs (default 5 min)."""
    stale_round0_after_seconds: float = Field(
        default=900.0, validation_alias="NEGOTIATION_STALE_ROUND0_AFTER_SECONDS"
    )
    """A round-0 session (first email never sent) is re-notified once its
    updated_at is older than this (default 15 min) — long enough that a human
    genuinely had a chance to action the approval, short enough that a truly
    forgotten one resurfaces the same day."""
    stale_post_round0_after_seconds: float = Field(
        default=30 * 24 * 3600.0, validation_alias="NEGOTIATION_STALE_POST_ROUND0_AFTER_SECONDS"
    )
    """A session past round 0 that has gone quiet this long (default 30 days —
    the cooling period) is marked FAILED so the deal leaves NEGOTIATING."""

    # ── No-reply follow-ups ───────────────────────────────────────────────
    # Runs inside the same stale-watcher sweep. When PortLess's last outbound
    # to a party was actually SENT and that party has not replied within the
    # window, a courteous follow-up is DRAFTED and routed through the normal
    # SEND_NEGOTIATION_EMAIL L0 approval — never auto-sent. An operator REJECT
    # of a follow-up suppresses further follow-ups for that same unanswered
    # message (the chain resets when the party finally replies).
    followup_enabled: bool = Field(
        default=True, validation_alias="NEGOTIATION_FOLLOWUP_ENABLED"
    )
    followup_after_seconds: float = Field(
        default=2 * 24 * 3600.0, validation_alias="NEGOTIATION_FOLLOWUP_AFTER_SECONDS"
    )
    """How long an outbound may sit unanswered before a follow-up is drafted
    (default 2 days)."""
    followup_max_per_party: int = Field(
        default=2, validation_alias="NEGOTIATION_FOLLOWUP_MAX_PER_PARTY"
    )
    """Maximum consecutive follow-ups per unanswered message before going
    quiet and letting the 30-day stale fail take over."""

    # ── Business defaults ─────────────────────────────────────────────────
    default_max_rounds: int = 5
    default_margin_floor_pct: float = 8.0
    default_concession_step_usd: float = 0.10
    hitl_margin_warning_pct: float = 2.0
    """Trigger HITL when margin is within this many pct points of the floor."""
    hitl_on_first_outbound: bool = True
    """Require operator sign-off before the very first outbound message."""

    # ── Market Intelligence (live web search) ──────────────────────────────
    market_search_enabled: bool = Field(default=False, validation_alias="NEGOTIATION_MARKET_SEARCH_ENABLED")
    """Set NEGOTIATION_MARKET_SEARCH_ENABLED=true to fetch live web-search market
    pricing/demand color (Tavily) before each buyer/supplier email draft. Off by
    default. No-op even when true if TAVILY_API_KEY is unset (fails open, no
    network call). Search results matching known prompt-injection patterns are
    discarded before reaching any drafting prompt."""

    # ── Inbound price plausibility band ────────────────────────────────────
    price_band_factor: float = Field(
        default=3.0, validation_alias="NEGOTIATION_PRICE_BAND_FACTOR"
    )
    """An inbound counter-price outside [corridor_min/factor, corridor_max×factor]
    is treated as an extraction error: it never becomes the working bid/ask and
    the round is flagged for HITL. Operator override prices bypass the guard."""

    # ── Agreement completeness checklist ───────────────────────────────────
    agreement_checklist_enabled: bool = Field(
        default=True, validation_alias="NEGOTIATION_AGREEMENT_CHECKLIST_ENABLED"
    )
    """Before NEGOTIATION_AGREED is declared, verify every required piece of
    information has been received (guards.missing_agreement_info: buyer's QC
    inspector choice, confirmed quantity). If anything is missing the agent
    drafts an email requesting it instead of closing. Operator price override
    bypasses the checklist. Set false to restore close-on-price-alone."""

    # ── Freight (logistics) cost in pricing ────────────────────────────────
    freight_cost_enabled: bool = Field(
        default=True, validation_alias="NEGOTIATION_FREIGHT_COST_ENABLED"
    )
    """Quote the lane's ocean freight via Shield-Link at negotiation start and
    include the per-unit cost in the margin formula (buyer prices are CIF,
    supplier Ex-Works — freight belongs inside the margin). Set
    NEGOTIATION_FREIGHT_COST_ENABLED=false to restore the legacy behavior of
    logistics_cost=0, e.g. for demos whose seeded buyer/supplier price gap
    can't absorb a realistic freight cost."""

    fallback_usd_inr_rate: float = Field(
        default=96.0, validation_alias="NEGOTIATION_FALLBACK_USD_INR_RATE"
    )
    """Used when a live rate can't be fetched (market_search_enabled=False,
    no TAVILY_API_KEY, or the fetch/parse fails) - the supplier agent still
    needs some rate to avoid guessing one itself. Periodically-updated
    static value, not a live feed; last checked 2026-07 (~95.9-96.2 range)."""


settings = NegotiationSettings()
