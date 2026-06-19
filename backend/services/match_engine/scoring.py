"""Deterministic Composite Match Score math (LLD 4.3).

    CMS = C_supplier * (w1 * M_arbitrage + w2 * L_proximity + w3 * V_reliability)

Every function here is a pure function of its inputs — no I/O, no LLM, no global
state — so the gatekeeping decision is fully reproducible and unit-testable.
"""
from __future__ import annotations

from .config import DEFAULT_CONFIG, ScoringConfig


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp ``value`` into the inclusive ``[low, high]`` range."""
    return max(low, min(high, value))


def gatekeeper_multiplier(gst_verified: bool, iec_active: bool, dgft_denied: bool) -> int:
    """C_supplier ∈ {0, 1}.

    0 if GST verification fails, the IEC is inactive, or the supplier is on the
    DGFT denied list; otherwise 1.
    """
    return 1 if (gst_verified and iec_active and not dgft_denied) else 0


def arbitrage_score(
    buyer_target_price: float,
    supplier_quote_price: float,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> float:
    """M_arbitrage ∈ [0, 100] from the buyer-target vs supplier-quote spread.

    No (or negative) spread scores 0; the spread percentage scales linearly up to
    ``arbitrage_saturation_pct``, at which point the score saturates at 100.
    """
    if buyer_target_price <= 0:
        return 0.0
    spread_pct = (buyer_target_price - supplier_quote_price) / buyer_target_price * 100.0
    if spread_pct <= 0:
        return 0.0
    return clamp(spread_pct / config.arbitrage_saturation_pct * 100.0)


def proximity_score(
    supplier_to_port_km: float,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> float:
    """L_proximity ∈ [0, 100]; closer to the exit port scores higher.

    Linear decay from 100 (at the port) to 0 (at ``max_port_distance_km`` or beyond).
    """
    if supplier_to_port_km <= 0:
        return 100.0
    return clamp((1.0 - supplier_to_port_km / config.max_port_distance_km) * 100.0)


def reliability_score(
    business_age_years: float,
    udyam_registered: bool,
    annual_shipment_count: int,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> float:
    """V_reliability ∈ [0, 100] from business age (MCA), Udyam, shipment frequency."""
    age = clamp(business_age_years / config.business_age_full_years * 100.0)
    udyam = 100.0 if udyam_registered else 0.0
    frequency = clamp(annual_shipment_count / config.annual_shipment_full * 100.0)
    return clamp(
        age * config.rel_w_age
        + udyam * config.rel_w_udyam
        + frequency * config.rel_w_frequency
    )


def normalize_weights(
    w_arbitrage: float,
    w_proximity: float,
    w_reliability: float,
) -> tuple[float, float, float]:
    """Normalize the three CMS weights so they sum to 1.0."""
    if min(w_arbitrage, w_proximity, w_reliability) < 0:
        raise ValueError("weights must be non-negative")
    total = w_arbitrage + w_proximity + w_reliability
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    return (w_arbitrage / total, w_proximity / total, w_reliability / total)


def composite_match_score(
    gatekeeper: int,
    arbitrage: float,
    proximity: float,
    reliability: float,
    weights: tuple[float, float, float],
) -> float:
    """Combine sub-scores into the final CMS ∈ [0, 100]."""
    w_arbitrage, w_proximity, w_reliability = weights
    weighted = (
        w_arbitrage * arbitrage
        + w_proximity * proximity
        + w_reliability * reliability
    )
    return gatekeeper * weighted
