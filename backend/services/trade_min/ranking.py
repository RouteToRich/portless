"""Deterministic opportunity-ranking math for the Trade-Min module (FR-1, LLD 4.5).

All functions are pure — no I/O, no global state — so they are reproducible
and fully unit-testable regardless of whether the Comtrade/Volza connectors
are live.

Simulated demand data is derived from a deterministic SHA-256 hash of the
(hs_code, market) pair so the same inputs always produce the same scores.
"""
from __future__ import annotations

import hashlib

from .config import DEFAULT_CONFIG, TradeMinConfig


def _deterministic_score(seed: str) -> float:
    """Map an arbitrary string to a stable 0.0–100.0 score via SHA-256."""
    digest = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    return round((digest % 10_001) / 100.0, 1)


def simulated_demand_index(hs_code: str, market: str) -> float:
    """Deterministic demand-surge index (0–100) for an HS code in a destination market.

    Proxies what UN Comtrade / Volza would return: a measure of import-volume
    growth trend for this commodity in this country.
    """
    return _deterministic_score(f"{hs_code}:demand:{market}")


def simulated_price_gap_pct(hs_code: str, market: str) -> float:
    """Deterministic price-gap percentage (0–50 %) between India FOB and market CIF.

    A higher gap means stronger arbitrage potential.
    """
    raw = _deterministic_score(f"{hs_code}:pricegap:{market}")
    return round(raw * 0.5, 1)  # scale 0–100 down to 0–50 %


def price_gap_score(
    price_gap_pct: float,
    config: TradeMinConfig = DEFAULT_CONFIG,
) -> float:
    """Normalise price-gap % to a 0–100 score, saturating at ``price_gap_saturation_pct``."""
    if price_gap_pct <= 0:
        return 0.0
    return min(price_gap_pct / config.price_gap_saturation_pct * 100.0, 100.0)


def arbitrage_rank(
    demand_index: float,
    price_gap_pct: float,
    config: TradeMinConfig = DEFAULT_CONFIG,
) -> float:
    """Composite arbitrage rank (0–100): geometric mean of demand index and normalised
    price-gap score.  Both sub-signals must be positive to produce a non-zero rank.
    """
    pg_score = price_gap_score(price_gap_pct, config)
    if demand_index <= 0 or pg_score <= 0:
        return 0.0
    return round((demand_index * pg_score) ** 0.5, 2)
