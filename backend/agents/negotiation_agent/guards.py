"""Deterministic business guards — never delegated to LLM."""
from __future__ import annotations

import re

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


# ---------------------------------------------------------------------------
# Explicit acceptance detection
# ---------------------------------------------------------------------------
# check_convergence() assumes both sides are actively converging their bid/ask
# toward each other, which never happens when the supplier's price is fixed
# (sourced via RFQ, not renegotiated) — the bid/ask gap there IS the margin,
# and is never supposed to shrink. Without this guard, a counterpart that
# outright accepts the last quoted price (rather than countering) can never
# close the negotiation: check_convergence() stays false forever, and the
# orchestrator LLM's own terminal verdicts are discarded by graph.py's safety
# clamp. This guard is deliberately conservative (word-boundary keyword match,
# no LLM) since it gates a terminal, hard-to-reverse status transition — a
# false negative just falls back to the existing round-continuation behavior,
# but a false positive would prematurely close a live negotiation.

_ACCEPT_PATTERN = re.compile(
    r"\b(agreed|agree|accept(?:ed|s)?|confirm(?:ed|s)?|sounds good|"
    r"ok(?:ay)?\s+(?:with|to)\s+(?:this|that|the)|go(?:ing)?\s+ahead|proceed)\b",
    re.IGNORECASE,
)

_NEGATION_PATTERN = re.compile(
    r"\b(not|no|never|don'?t|do not|can'?t|cannot|won'?t|will not|unable|"
    r"disagree(?:s|d)?|reject(?:s|ed|ing)?|declin(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)

_QUOTE_BOUNDARY_PATTERN = re.compile(r"(\bOn\b.{0,120}?\bwrote:)|(>\s)", re.IGNORECASE)


def is_explicit_acceptance(inbound_message: str | None) -> bool:
    """True when the counterpart's *new* reply text — excluding any quoted
    reply-chain history — reads as an outright acceptance of the last quoted
    price/terms, with no negation present.

    Whitespace is collapsed before matching so a quoted-reply attribution line
    line-wrapped by the mail client (e.g. "On <date>, <name>\\n<email> wrote:")
    is still detected as a boundary; only the text before that boundary (or
    the first '>' blockquote line) is considered "new".
    """
    if not inbound_message:
        return False
    collapsed = re.sub(r"\s+", " ", inbound_message).strip()
    boundary = _QUOTE_BOUNDARY_PATTERN.search(collapsed)
    new_text = (collapsed[: boundary.start()] if boundary else collapsed).strip()
    if not new_text or _NEGATION_PATTERN.search(new_text):
        return False
    return bool(_ACCEPT_PATTERN.search(new_text))
