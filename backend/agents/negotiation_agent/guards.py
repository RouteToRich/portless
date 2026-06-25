"""Deterministic business guards — never delegated to LLM."""
from __future__ import annotations

from .schemas import CostEnvelope, NegotiationPolicy, NegotiationSession


def compute_net_margin(
    buyer_price: float,
    supplier_price: float,
    logistics_cost: float,
    cost_envelope: CostEnvelope | None,
) -> float:
    """Net Margin = (buyer_price - supplier_price - logistics - cost_envelope_total) / buyer_price * 100"""
    if buyer_price <= 0:
        return 0.0
    env_total = cost_envelope.total() if cost_envelope else 0.0
    net = buyer_price - supplier_price - logistics_cost - env_total
    return (net / buyer_price) * 100.0


def verify_payout_and_margin(
    session: NegotiationSession,
    buyer_price: float,
    supplier_price: float,
    logistics_cost: float = 0.0,
) -> tuple[bool, float]:
    """Return (margin_ok, margin_pct). margin_ok iff margin_pct >= floor."""
    margin = compute_net_margin(
        buyer_price, supplier_price, logistics_cost, session.cost_envelope
    )
    floor = session.policy.margin_floor_pct
    return (margin >= floor, margin)


def check_convergence(
    session: NegotiationSession,
    current_bid: float,
    current_ask: float,
    logistics_cost: float = 0.0,
) -> bool:
    """True when price gap AND margin both satisfy the convergence rule."""
    gap = abs(current_ask - current_bid)
    step = session.policy.concession_step
    if gap > step:
        return False
    margin_ok, _ = verify_payout_and_margin(
        session, current_bid, current_ask, logistics_cost
    )
    return margin_ok


def compute_midpoint_price(bid: float, ask: float) -> float:
    """Midpoint between buyer bid and supplier ask, rounded to 2 dp."""
    return round((bid + ask) / 2, 2)


def is_near_margin_floor(
    session: NegotiationSession,
    margin_pct: float,
    warning_pct: float | None = None,
) -> bool:
    """True when margin is within warning_pct points of the floor."""
    warn = warning_pct if warning_pct is not None else 2.0
    return margin_pct < (session.policy.margin_floor_pct + warn)
