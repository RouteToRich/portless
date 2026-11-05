"""Centralized configuration: service registry URLs + cross-cutting settings.

Every service resolves its peers through this single settings object so that
moving a module to its own host only requires changing an environment variable.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Populate os.environ from the repository-root .env so connector classes that read
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
    email_gateway_url: str = "http://localhost:8012"
    email_processor_url: str = "http://localhost:8013"

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

    # --- Auto-flow orchestrator (dev/test) ---
    # When true, an opportunity-seeded deal auto-continues through sourcing,
    # negotiation, PI, and escrow funding; PO/freight/payout still stop at a
    # HITL approval. See shared/orchestration/auto_flow.py.
    auto_flow_enabled: bool = False

    # --- Opportunity → deal auto-creation (production automation leg) ---
    # When true, every OPPORTUNITY_DETECTED event that clears the thresholds
    # below becomes a deal automatically (status MATCHING → RFQ dispatch →
    # negotiation …) instead of waiting for an operator to click "Start Deal".
    # Simulated-source opportunities (no live Comtrade/Census data) are never
    # converted. One deal per opportunity; an existing non-terminal deal on the
    # same HS code × market also blocks a second one. Off by default.
    auto_deal_from_opportunity_enabled: bool = False
    auto_deal_min_arbitrage_rank: float = 60.0
    auto_deal_min_confidence: float = 50.0
    auto_deal_operator_id: str = "system:auto-deal"
    auto_deal_org_id: str = "portless"
    auto_deal_autonomy_profile: str = "L0"

    # --- Shared MongoDB (single connection string + single database) ---
    # Set MONGO_URI once and every service/module uses it, writing to the SAME
    # database (MONGO_DB_NAME) via its own collection(s) — one Mongo database for
    # the whole app, not one per service. A service-specific <SERVICE>_MONGO_URI /
    # <SERVICE>_MONGO_DB_NAME still overrides these when set. Empty MONGO_URI →
    # services degrade to their in-memory stores.
    mongo_uri: str = ""
    mongo_db_name: str = "portless"

    # --- UI / CORS ---
    cors_allow_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        # Swagger UI (/docs) is served from the combined app itself, so
        # "Try it out" requests carry this origin, not the Vite dev origin.
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    ]
    # The deployed operator console's origin (e.g. https://portless-ui.vercel.app).
    # Its API calls go through the shared gateway (same origin, no CORS), but the
    # approvals SSE stream is opened against this backend DIRECTLY — a serverless
    # gateway cannot hold a stream open — so that one origin must be allowed here.
    frontend_url: str = ""

    @property
    def cors_origins(self) -> list[str]:
        origins = list(self.cors_allow_origins)
        if self.frontend_url.strip():
            origins.append(self.frontend_url.strip().rstrip("/"))
        return origins


settings = Settings()
