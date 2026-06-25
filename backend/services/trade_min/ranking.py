"""Deterministic opportunity-ranking math for the Trade-Min module (FR-1, LLD 4.5).

All functions are pure — no I/O, no global state — reproducible and fully
unit-testable regardless of whether connectors are live.

Simulated demand data uses a deterministic SHA-256 hash of the (hs_code, market)
pair so the same inputs always produce the same scores.
"""
from __future__ import annotations

import hashlib
import math

from .config import DEFAULT_CONFIG, TradeMinConfig


def _deterministic_score(seed: str) -> float:
    """Map an arbitrary string to a stable 0.0–100.0 score via SHA-256."""
    digest = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    return round((digest % 10_001) / 100.0, 1)


def simulated_demand_index(hs_code: str, market: str) -> float:
    return _deterministic_score(f"{hs_code}:demand:{market}")


def simulated_price_gap_pct(hs_code: str, market: str) -> float:
    raw = _deterministic_score(f"{hs_code}:pricegap:{market}")
    return round(raw * 0.5, 1)  # scale 0–100 → 0–50 %


def price_gap_score(
    price_gap_pct: float,
    config: TradeMinConfig = DEFAULT_CONFIG,
) -> float:
    """Normalise a price-gap % to a 0–100 score, saturating at ``price_gap_saturation_pct``."""
    if price_gap_pct <= 0:
        return 0.0
    return min(price_gap_pct / config.price_gap_saturation_pct * 100.0, 100.0)


def arbitrage_rank(
    demand_index: float,
    net_price_gap_pct: float,
    config: TradeMinConfig = DEFAULT_CONFIG,
) -> float:
    """Composite arbitrage rank (0–100): geometric mean of demand index and
    normalised net-price-gap score.

    Uses ``net_price_gap_pct`` (gross gap minus tariff/landed-cost add-on) so
    the score already reflects real take-home margin after duties.
    """
    pg_score = price_gap_score(net_price_gap_pct, config)
    if demand_index <= 0 or pg_score <= 0:
        return 0.0
    return round((demand_index * pg_score) ** 0.5, 2)


def reorder_probability(
    days_since_last_import: float,
    avg_days_between_imports: float,
) -> float:
    """Sigmoid probability that a buyer is due to reorder.

    Maps the ratio (days_elapsed / avg_interval) to a 0–1 probability via a
    logistic curve centred at 1.0 (i.e. exactly one interval elapsed).
    Probability ≥ 0.70 → upgrade urgency tier 1 → 2 (reorder window open).
    """
    if avg_days_between_imports <= 0:
        return 0.0
    x = days_since_last_import / avg_days_between_imports
    return round(1.0 / (1.0 + math.exp(-5.0 * (x - 1.0))), 4)
