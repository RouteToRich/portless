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
# clamp.
#
# The PRIMARY acceptance signal is the LLM intent classifier
# (graph.classify_inbound_intent); this keyword matcher is the fallback used
# only when the LLM gateway is unavailable. It is deliberately conservative —
# a false negative just falls back to round continuation, but a false positive
# routes a "confirm agreement?" request to a human (the 2026-07-15 incident,
# where the bare keyword "confirm" in "kindly let me know and confirm" closed
# a live negotiation, is why "confirm" now only counts as a first-person
# statement and why conditional/request phrasing vetoes a match).

_ACCEPT_PATTERN = re.compile(
    r"\b(agreed|agree|accept(?:ed|s)?|confirmed|(?:we|i)\s+(?:hereby\s+)?confirm|"
    r"sounds good|ok(?:ay)?\s+(?:with|to)\s+(?:this|that|the)|"
    r"go(?:ing)?\s+ahead|proceed)\b",
    re.IGNORECASE,
)

_NEGATION_PATTERN = re.compile(
    r"\b(not|no|never|don'?t|do not|can'?t|cannot|won'?t|will not|unable|"
    r"disagree(?:s|d)?|reject(?:s|ed|ing)?|declin(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)

# Conditional or request-to-confirm phrasing: the sender is proposing terms or
# asking US to commit, not committing themselves ("...if the unit price would
# be 1900$. Kindly let me know and confirm"). Any hit vetoes acceptance.
_NON_COMMITTAL_PATTERN = re.compile(
    r"\b(if|unless|provided(?:\s+that)?|subject to|in case|"
    r"please confirm|kindly confirm|let (?:me|us) know|"
    r"would (?:you|be)|could you|can you)\b",
    re.IGNORECASE,
)

_QUOTE_BOUNDARY_PATTERN = re.compile(r"(\bOn\b.{0,120}?\bwrote:)|(>\s)", re.IGNORECASE)


def _new_reply_text(inbound_message: str) -> str:
    """Return the *new* text of a reply, excluding quoted reply-chain history.

    Whitespace is collapsed before matching so a quoted-reply attribution line
    line-wrapped by the mail client (e.g. "On <date>, <name>\\n<email> wrote:")
    is still detected as a boundary; only the text before that boundary (or
    the first '>' blockquote line) is considered "new".
    """
    collapsed = re.sub(r"\s+", " ", inbound_message).strip()
    boundary = _QUOTE_BOUNDARY_PATTERN.search(collapsed)
    return (collapsed[: boundary.start()] if boundary else collapsed).strip()


def is_explicit_acceptance(inbound_message: str | None) -> bool:
    """True when the counterpart's *new* reply text — excluding any quoted
    reply-chain history — reads as an outright, unconditional acceptance of
    the last quoted price/terms, with no negation and no conditional /
    request-to-confirm phrasing present.
    """
    if not inbound_message:
        return False
    new_text = _new_reply_text(inbound_message)
    if not new_text or _NEGATION_PATTERN.search(new_text):
        return False
    if _NON_COMMITTAL_PATTERN.search(new_text):
        return False
    return bool(_ACCEPT_PATTERN.search(new_text))


# ---------------------------------------------------------------------------
# Acceptance price consistency
# ---------------------------------------------------------------------------
# Second, independent check on any detected acceptance (LLM or regex): a true
# acceptance quotes either no price at all or the price already on the table.
# A different USD figure in the new text means the sender is countering, no
# matter how agreeable the wording reads.

_USD_PRICE_PATTERN = re.compile(
    r"(?:\$|\busd\b|\bus\$)\s*([\d,]+(?:\.\d+)?)"
    r"|([\d,]+(?:\.\d+)?)\s*(?:\$|\busd\b|\bdollars\b)",
    re.IGNORECASE,
)


def extract_usd_prices(text: str | None) -> list[float]:
    """Extract USD-denominated amounts (e.g. "$1,900", "1900$", "USD 1900")."""
    if not text:
        return []
    prices: list[float] = []
    for match in _USD_PRICE_PATTERN.finditer(text):
        raw = match.group(1) or match.group(2)
        try:
            prices.append(float(raw.replace(",", "")))
        except (TypeError, ValueError):
            continue
    return prices


def acceptance_price_consistent(
    inbound_message: str | None,
    reference_price: float,
    rel_tolerance: float = 0.01,
) -> bool:
    """True when the reply's new text quotes no USD price, or every quoted
    price matches *reference_price* (the price that would be locked in by
    accepting) within *rel_tolerance*."""
    if not inbound_message:
        return True
    prices = extract_usd_prices(_new_reply_text(inbound_message))
    if not prices:
        return True
    tolerance = max(abs(reference_price) * rel_tolerance, 0.01)
    return all(abs(p - reference_price) <= tolerance for p in prices)
