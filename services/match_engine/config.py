"""Scoring configuration for the Match Engine (LLD 4.3).

All tunables live here so the Composite Match Score stays a pure, deterministic
function of its inputs. Defaults follow the master specification
(weights 0.5 / 0.3 / 0.2, decision threshold CMS >= 75).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoringConfig:
    # --- Top-level CMS weights (must sum to 1.0; default 0.5 / 0.3 / 0.2) ---
    w_arbitrage: float = 0.5
    w_proximity: float = 0.3
    w_reliability: float = 0.2

    # --- Decision threshold: CMS >= threshold -> SOURCE, else DISCARD ---
    cms_threshold: float = 75.0

    # --- Arbitrage: buyer/supplier spread % at which the score saturates to 100 ---
    arbitrage_saturation_pct: float = 25.0

    # --- Proximity: warehouse -> nearest exit port distance at which score hits 0 ---
    max_port_distance_km: float = 600.0

    # --- Reliability sub-weights (must sum to 1.0) ---
    rel_w_age: float = 0.4
    rel_w_udyam: float = 0.2
    rel_w_frequency: float = 0.4

    # --- Reliability saturations ---
    business_age_full_years: float = 10.0
    annual_shipment_full: float = 120.0


DEFAULT_CONFIG = ScoringConfig()
