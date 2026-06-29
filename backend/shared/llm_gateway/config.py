"""LLM Gateway configuration.

Env-var prefix: LLM_  (e.g. LLM_GROQ_API_KEY, LLM_PRIMARY_MODEL)

Env file load order (later file wins):
  1. backend/.env                           — shared root, run from CWD
  2. backend/shared/llm_gateway/.env        — gateway-specific overrides

Fallback chain (always active when groq_api_key is set):
  1. qwen/qwen3-32b        via Groq
  2. qwen/qwen3.6-27b      via Groq
  3. openai/gpt-oss-safeguard-20b  via Groq

Optional primary (tried first if LLM_PRIMARY_API_KEY is set):
  Set LLM_PRIMARY_MODEL, LLM_PRIMARY_API_KEY, LLM_PRIMARY_CONNECTOR.
  E.g. for Claude: LLM_PRIMARY_CONNECTOR=anthropic, LLM_PRIMARY_MODEL=claude-sonnet-4-6

Auth is disabled by default (LLM_AUTH_ENABLED=false). Enable when JWT infra is ready.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Module directory — used to resolve the module-local .env file path
_HERE = Path(__file__).parent

# Groq API key — override with LLM_GROQ_API_KEY env var in production
_DEFAULT_GROQ_API_KEY = "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"


class LLMGatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        # Load root .env first, then module-local .env (module wins on conflict)
        env_file=(".env", str(_HERE / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ auth
    # Set auth_enabled=true and jwt_secret when JWT middleware is wired up.
    auth_enabled: bool = False
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "portless"
    jwt_audience: str = "portless-llm-gateway"

    # --------------------------------------------------- optional primary model
    # Add LLM_PRIMARY_API_KEY to .env to activate a higher-tier primary model.
    # When empty the chain starts directly at the Groq fallbacks below.
    primary_model: str = ""
    primary_api_key: str = ""
    primary_connector: str = "http"  # "groq" | "openai" | "anthropic" | "http"
    primary_base_url: str = ""       # only used when connector="http"

    # ----------------------------------------------- Groq fallback chain
    # Shared across all three fallback slots unless overridden per-slot.
    groq_api_key: str = _DEFAULT_GROQ_API_KEY

    fallback_1_model: str = "qwen/qwen3-32b"
    fallback_1_connector: str = "groq"
    fallback_1_api_key: str = ""  # uses groq_api_key when empty

    fallback_2_model: str = "qwen/qwen3.6-27b"
    fallback_2_connector: str = "groq"
    fallback_2_api_key: str = ""

    fallback_3_model: str = "openai/gpt-oss-safeguard-20b"
    fallback_3_connector: str = "groq"
    fallback_3_api_key: str = ""

    # -------------------------------------------------- request / retry
    request_timeout_seconds: float = 60.0
    max_retries_per_model: int = 2          # attempts per model before advancing chain
    retry_base_delay_seconds: float = 1.0   # doubles each retry (exponential backoff)

    # -------------------------------------------------- generation defaults
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2048

    # ------------------------------------------------------- tracking
    tracking_enabled: bool = True
    # JSONL file — one record per line, append-only.
    # Used as fallback when MongoDB is disabled or unavailable.
    tracking_file_path: str = "data/llm_gateway_tracking.txt"

    # --------------------------------------------------- MongoDB tracker
    # Primary persistence for LLM request tracking.
    # Set LLM_MONGO_TRACKER_ENABLED=false to fall back to JSONL file tracker.
    mongo_tracker_enabled: bool = True
    mongo_uri: str = (
        "mongodb+srv://richminds84_db_user:8M7dOW4srdGAy7aM"
        "@cluster0.taios8r.mongodb.net/?appName=Cluster0"
    )
    mongo_db_name: str = "gateway"
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


llm_gateway_settings = LLMGatewaySettings()

# Module-level constants so callers can: from shared.llm_gateway.config import LLM_TEMPERATURE
LLM_TEMPERATURE: float = llm_gateway_settings.llm_temperature
LLM_MAX_TOKENS: int = llm_gateway_settings.llm_max_tokens
