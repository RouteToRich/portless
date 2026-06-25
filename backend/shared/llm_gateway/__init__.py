"""LLM Gateway — authenticated, resilient LLM access with fallback and tracking.

Quick start::

    from shared.llm_gateway import LLMClient

    client = LLMClient()
    response = await client.chat(
        messages=[{"role": "user", "content": "Negotiate a 5% discount on 20MT turmeric."}],
        caller="negotiation_agent",
    )
    print(response.content)
    print(client.tracker.get_stats())

Fallback chain (default, Groq-backed):
    qwen/qwen3-32b  →  qwen/qwen3.6-27b  →  openai/gpt-oss-safeguard-20b

Add a primary model by setting LLM_PRIMARY_MODEL + LLM_PRIMARY_API_KEY in .env.
Enable JWT auth by setting LLM_AUTH_ENABLED=true + LLM_JWT_SECRET.
Tracking file: data/llm_gateway_tracking.txt (JSONL, append-only).

Future features (placeholders — NOT YET IMPLEMENTED):
    RateLimiter        — per-caller RPM/TPM limiting (rate_limiter.py)
    CircuitBreaker     — per-model circuit breaker (circuit_breaker.py)
    CostEstimator      — per-call cost calculation (cost_estimator.py)
    ResponseCache      — prompt/response caching (cache.py)
    stream_chat()      — streaming async generator (streaming.py)
    DatabaseTracker    — PostgreSQL-backed tracker (db_tracker.py)
    HealthMonitor      — background endpoint health check (health.py)
    PromptGuard        — prompt injection detection (prompt_guard.py)
"""
from .config import LLMGatewaySettings, llm_gateway_settings, LLM_TEMPERATURE, LLM_MAX_TOKENS
from .auth import JWTValidator, InvalidTokenError, TokenClaims, require_valid_token
from .client import LLMClient, LLMResponse, LLMExhaustedException, LLMConfigurationError
from .fallback import FallbackChain, ModelEndpoint, classify_error
from .tracker import RequestTracker, RequestRecord

# ── MongoDB persistence (production tracker) ───────────────────────────────
from .mongo_connection import MongoConnection, get_connection, close_all as close_mongo
from .db_tracker import MongoTracker

# ── placeholder imports (NOT YET IMPLEMENTED — all methods raise NotImplementedError) ──
from .rate_limiter import RateLimiter, RateLimitExceeded, RateLimitUsage
from .circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState, CircuitStatus
from .cost_estimator import CostEstimator, BudgetExceeded
from .cache import ResponseCache
from .streaming import StreamChunk, stream_chat, SSEAdapter
from .health import HealthMonitor, EndpointUnhealthyError, HealthStatus, EndpointHealth
from .prompt_guard import PromptGuard, HeuristicGuard, LLMJudgeGuard, PromptBlocked, GuardResult

__all__ = [
    # config
    "LLMGatewaySettings",
    "llm_gateway_settings",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    # auth
    "JWTValidator",
    "InvalidTokenError",
    "TokenClaims",
    "require_valid_token",
    # client
    "LLMClient",
    "LLMResponse",
    "LLMExhaustedException",
    "LLMConfigurationError",
    # fallback
    "FallbackChain",
    "ModelEndpoint",
    "classify_error",
    # tracker
    "RequestTracker",
    "RequestRecord",
    # ── MongoDB persistence ────────────────────────────────────────────────
    "MongoConnection",
    "MongoTracker",
    "get_connection",
    "close_mongo",
    # ── placeholders (NOT YET IMPLEMENTED) ────────────────────────────────
    # rate_limiter
    "RateLimiter",
    "RateLimitExceeded",
    "RateLimitUsage",
    # circuit_breaker
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "CircuitStatus",
    # cost_estimator
    "CostEstimator",
    "BudgetExceeded",
    # cache
    "ResponseCache",
    # streaming
    "StreamChunk",
    "stream_chat",
    "SSEAdapter",
    # db_tracker
    "DatabaseTracker",
    # health
    "HealthMonitor",
    "EndpointUnhealthyError",
    "HealthStatus",
    "EndpointHealth",
    # prompt_guard
    "PromptGuard",
    "HeuristicGuard",
    "LLMJudgeGuard",
    "PromptBlocked",
    "GuardResult",
]
