"""LLM Gateway configuration.

Env-var prefix: LLM_  (e.g. LLM_GATEWAY_URL, LLM_GATEWAY_EMAIL)

Env file load order (later file wins):
  1. .env                           — shared root, run from CWD
  2. shared/llm_gateway/.env        — gateway-specific overrides

PortLess holds NO model provider key. Every chat call goes to the
shared RouteToRich LLM Gateway through the API gateway's /api/llm route — the
same integration as makemerich-backend (see remote.py). Model choice, fallback
chain, retries, provider keys and cost metering are the gateway's business; this
module only says where the gateway is and which credential PortLess signs in with.

This module — and ONLY this module — should ever declare LLM gateway settings.
Every other module reaches models exclusively through
shared.llm_gateway.client.LLMClient.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Module directory — used to resolve the module-local .env file path
_HERE = Path(__file__).parent


class LLMGatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        # Load root .env first, then module-local .env (module wins on conflict)
        env_file=(".env", str(_HERE / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ auth
    # Inbound JWT validation for gateway-facing routes (auth.py). Unrelated to
    # the outbound gateway credential below.
    auth_enabled: bool = False
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "portless"
    jwt_audience: str = "portless-llm-gateway"

    # ------------------------------------------- shared LLM Gateway (remote)
    # Unset gateway_url → every chat() raises LLMConfigurationError (callers
    # degrade: template negotiation drafts, resolver escalates). There is no
    # local provider fallback by design: all LLM spend is metered by the gateway.
    gateway_url: str = ""                   # API gateway base, e.g. https://api-gateway-two-nu.vercel.app
    gateway_llm_path: str = "/api/llm"      # the API gateway's route to the LLM Gateway
    gateway_auth_path: str = "/auth/login"  # auth-service sign-in, through the API gateway
    # PortLess calls the LLM from background event handlers with no user
    # request, so it cannot forward a caller's token the way makemerich does —
    # it signs in as its own auth-service service account. Either:
    #   email + password (+ account_id) → signs in, caches the bearer, refreshes
    #                                     before expiry and once more on a 401
    #   token                           → a pre-minted bearer, sent as-is
    gateway_email: str = ""
    gateway_password: str = ""
    gateway_account_id: str = ""            # PortLess's app account in auth-service
    gateway_token: str = ""
    gateway_token_ttl_seconds: int = 3000   # auth-service tokens last 1 h; refresh before
    gateway_timeout_seconds: float = 120.0
    gateway_caller_prefix: str = "portless" # caller tag "portless.<caller>" in gateway stats

    # -------------------------------------------------- generation defaults
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2048

    # ------------------------------------------------------- tracking
    tracking_enabled: bool = True
    # JSONL file — one record per line, append-only.
    # Used as fallback when MongoDB is disabled or unavailable.
    tracking_file_path: str = "data/llm_gateway_tracking.txt"

    # --------------------------------------------------- MongoDB tracker
    # Local per-call record (caller, model served, tokens, latency) next to
    # the gateway's own metering. LLM_MONGO_TRACKER_ENABLED=false → JSONL file.
    mongo_tracker_enabled: bool = True
    mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("LLM_MONGO_URI", "MONGO_URI")
    )
    mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("LLM_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    mongo_collection: str = "llm_requests"
    # Connection pool / timeout configuration (milliseconds)
    mongo_max_pool_size: int = 50
    mongo_connect_timeout_ms: int = 5_000
    mongo_server_selection_timeout_ms: int = 5_000
    mongo_socket_timeout_ms: int = 30_000

    # --------------------------------------- placeholder: rate limiting
    # TODO(rate_limiter): set rate_limit_enabled=true to enforce per-caller RPM/TPM.
    #   Backend options: "memory" (single-process) or "mongo" (multi-worker,
    #   via the shared MongoDB cache).
    rate_limit_enabled: bool = False
    rate_limit_rpm: int = 60           # requests per minute per caller
    rate_limit_tpm: int = 100_000      # tokens per minute per caller
    rate_limit_backend: str = "memory" # "memory" | "mongo"

    # ------------------------------------- placeholder: circuit breaker
    # TODO(circuit_breaker): set circuit_breaker_enabled=true to skip degraded models.
    circuit_breaker_enabled: bool = False
    cb_failure_threshold: int = 5      # consecutive failures before opening
    cb_cool_down_seconds: float = 60.0 # seconds to stay OPEN before probing

    # -------------------------------------- placeholder: cost estimator
    # TODO(cost_estimator): set cost_tracking_enabled=true to log per-call USD cost.
    #   Set budget_per_caller_usd > 0 to hard-cap spend (raises BudgetExceeded).
    cost_tracking_enabled: bool = False
    budget_per_caller_usd: float = 0.0 # 0 = no limit

    # -------------------------------------------- response caching
    # Set cache_enabled=true to skip identical LLM calls. Backed by the shared
    # MongoDB TTL cache (shared.cache) — see llm_gateway/cache.py. Keep the TTL
    # short so live prices never go stale mid-negotiation.
    cache_enabled: bool = False
    cache_ttl_seconds: int = 120

    # ------------------------------------------ placeholder: streaming
    # TODO(streaming): set stream_first_chunk_timeout_seconds when stream_chat() is wired.
    stream_first_chunk_timeout_seconds: float = 10.0

    # ---------------------------------------- placeholder: health check
    # TODO(health): set health_check_enabled=true to run background endpoint probes.
    health_check_enabled: bool = False
    health_check_interval_seconds: float = 30.0
    health_check_timeout_seconds: float = 5.0

    # ---------------------------------------- placeholder: prompt guard
    # TODO(prompt_guard): set prompt_guard_enabled=true to detect injection attempts.
    #   Mode options: "heuristic" (fast, no LLM), "llm_judge" (accurate, +300ms),
    #   "composite" (both).
    prompt_guard_enabled: bool = False
    prompt_guard_max_input_chars: int = 32_000
    prompt_guard_mode: str = "heuristic" # "heuristic" | "llm_judge" | "composite"

    @property
    def gateway_configured(self) -> bool:
        """URL and a credential are both present — calls can leave the process."""
        has_credential = bool(
            self.gateway_token.strip() or (self.gateway_email and self.gateway_password)
        )
        return bool(self.gateway_url.strip()) and has_credential


llm_gateway_settings = LLMGatewaySettings()

# Module-level constants so callers can: from shared.llm_gateway.config import LLM_TEMPERATURE
LLM_TEMPERATURE: float = llm_gateway_settings.llm_temperature
LLM_MAX_TOKENS: int = llm_gateway_settings.llm_max_tokens
