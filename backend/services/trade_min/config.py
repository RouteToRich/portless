"""Configuration for the Trade-Min opportunity scanner (FR-1, LLD 4.5)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TradeMinConfig:
    # Default HS codes to scan when the caller does not provide a list.
    # Examples from the master spec workflow (cardamom-blend, wheat, glycerol).
    default_hs_codes: tuple[str, ...] = ("090422", "100190", "290543")

    # Default destination markets (ISO-3166 alpha-2).
    default_markets: tuple[str, ...] = ("ES", "DE", "US", "AE", "GB")

    # Minimum arbitrage rank to include an opportunity in scan results (0–100).
    min_arbitrage_rank: float = 20.0

    # Upper bound on opportunities returned per single scan.
    max_per_scan: int = 50

    # Demand index at which a market is considered a "demand surge" (0–100).
    demand_surge_threshold: float = 40.0

    # Maximum price-gap percentage used as the 100-score saturation point.
    price_gap_saturation_pct: float = 40.0


DEFAULT_CONFIG = TradeMinConfig()
