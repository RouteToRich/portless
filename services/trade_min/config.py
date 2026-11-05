"""Configuration for the Trade-Min opportunity scanner (FR-1, LLD 4.5, §10).

All runtime-tuneable values are env-var driven via pydantic-settings.
Algorithmic constants (saturation points, tier thresholds) live in the
dataclass so they can be unit-tested without any environment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_HOUR = 3600
_DAY = 24 * _HOUR

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class TradeMinSettings(BaseSettings):
    """Runtime settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    trade_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("TRADE_MONGO_URI", "MONGO_URI")
    )
    trade_mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("TRADE_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    trade_mongo_collection: str = "opportunities"

    # ── Raw API response cache ────────────────────────────────────────────
    # Collection (same shared DB) holding cached raw connector responses, with
    # a TTL index. Falls back to in-memory when trade_mongo_uri is absent.
    # Named distinctly from shared.cache's "api_cache" collection since both now
    # live in the same database.
    trade_cache_collection: str = "trade_api_cache"
    # Kill switch — set TRADE_CACHE_ENABLED=0 to always hit the live API.
    trade_cache_enabled: bool = True

    # ── Connector credentials (read-only; connectors read os.environ directly,
    #    but settings exposes them for health-check / redaction logging) ──────
    comtrade_subscription_key: str = ""
    volza_api_key: str = ""
    macmap_api_key: str = ""
    trademo_api_key: str = ""
    importgenius_api_key: str = ""
    alibaba_app_key: str = ""
    alibaba_app_secret: str = ""
    indiamart_api_key: str = ""
    indiamart_mobile: str = ""
    tradeindia_api_key: str = ""

    # ── Scheduling (cron expressions) ─────────────────────────────────────
    scheduler_nightly_cron: str = "0 2 * * *"   # 02:00 UTC daily
    scheduler_reorder_cron: str = "0 */6 * * *"  # every 6 h


@dataclass(frozen=True)
class TradeMinConfig:
    """Algorithmic constants for the five-phase pipeline."""

    # Default HS codes to scan when the caller does not provide a list.
    default_hs_codes: tuple[str, ...] = ("090422", "100190", "290543")

    # Default destination markets (ISO-3166 alpha-2).
    default_markets: tuple[str, ...] = ("ES", "DE", "US", "AE", "GB")

    # Minimum composite arbitrage rank to include in results (0–100).
    min_arbitrage_rank: float = 20.0

    # Upper bound on opportunities returned per single scan.
    max_per_scan: int = 50

    # Demand index at which a market is considered a "demand surge" (0–100).
    demand_surge_threshold: float = 40.0

    # Maximum price-gap % used as the 100-score saturation point.
    price_gap_saturation_pct: float = 40.0

    # ── Phase 1 guardrails ────────────────────────────────────────────────
    # Abort the pipeline for this hs_code×market pair when fewer than this
    # many Phase-1 connectors respond successfully. Set to 1 now that Phase 1
    # runs on free official statistics (UN Comtrade is authoritative and
    # global on its own; US Census augments for the US market).
    phase1_min_connectors: int = 1

    # ── Tariff gate (Phase 3) ─────────────────────────────────────────────
    # net_price_gap_pct < tariff_hard_discard_floor  → hard discard
    # tariff_hard_discard_floor ≤ gap < tariff_marginal_floor → MARGINAL
    # gap ≥ tariff_marginal_floor                              → ACTIVE / viable
    tariff_hard_discard_floor: float = 1.0
    tariff_marginal_floor: float = 5.0

    # ── Confidence score weights ──────────────────────────────────────────
    confidence_phase1_max: int = 50     # 4/4 connectors = 50 pts
    confidence_live_max: int = 25       # rfq + tender + disruption
    confidence_tariff_max: int = 15     # MacMap confirmed viable = 15 pts
    confidence_sellers_max: int = 10    # ≥3 candidates = 10 pts

    # ── Reorder window ────────────────────────────────────────────────────
    reorder_probability_threshold: float = 0.70  # sigmoid ≥ 0.70 → tier 2

    # ── Urgency tier TTLs (hours) ─────────────────────────────────────────
    urgency_ttl_hours: tuple[int, ...] = (0, 168, 72, 48, 24)
    # index 0 unused; tier 1 = 168 h (7 d), tier 2 = 72 h, tier 3 = 48 h, tier 4 = 24 h

    # ── Raw API response cache TTLs (seconds), keyed by connector name ─────
    # Only connectors listed here are cached. Values are slow-changing:
    #   comtrade    — prior-year annual trade data is effectively immutable
    #   census      — monthly US trade data, settled once published
    #   wits_tariff — applied tariffs change rarely (free MacMap replacement)
    # Live signals (alibaba_rfq, gdelt_disruption, eu_ted) are deliberately
    # absent so they are always fetched fresh.
    cache_ttl_seconds: dict[str, int] = field(
        default_factory=lambda: {
            "comtrade":       7 * _DAY,
            "census":         7 * _DAY,
            "wits_tariff":    7 * _DAY,
            "access2markets": 7 * _DAY,   # EU tariffs/NTBs (when configured)
            # PortLess-owned Apify actors — every run costs money, so cache the
            # slow-moving ones; the Alibaba RFQ board is refreshed twice a day.
            "apify_export_markets": 7 * _DAY,
            "apify_us_importers":   3 * _DAY,
            "apify_india_suppliers": 7 * _DAY,
            "apify_alibaba_rfq":    12 * _HOUR,
        }
    )


DEFAULT_CONFIG = TradeMinConfig()
settings = TradeMinSettings()
