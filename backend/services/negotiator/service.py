"""Business logic for the Negotiator service (skeleton — not implemented).

Convergence rule (pure function, outside the LLM): an offer/counter pair is
accepted only if net_margin >= margin_floor AND |ask - bid| <= concession_step.
"""
from __future__ import annotations

from .schemas import NegotiationResponse, RoundRequest, StartNegotiationRequest


async def start_negotiation(payload: StartNegotiationRequest) -> NegotiationResponse:
    """Seed the bargaining loop and emit ``negotiation.started``.

    TODO: persist negotiation state, emit event.
    """
    raise NotImplementedError


async def run_round(negotiation_id: str, payload: RoundRequest) -> NegotiationResponse:
    """Process one counter-offer round and emit ``negotiation.round``.

    TODO: apply concession_step / max_rounds bounds, evaluate the deterministic
    convergence guard, then emit agreed/failed/escalated as appropriate.
    """
    raise NotImplementedError


async def get_negotiation(negotiation_id: str) -> NegotiationResponse:
    """Return current negotiation state. TODO: read from persistence."""
    raise NotImplementedError
