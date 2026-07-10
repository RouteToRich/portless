"""LangGraph state machine for the Negotiation Agent.

LLM calls go through shared.llm_gateway.LLMClient — automatic fallback,
per-request tracking, and retry handled centrally.

Three-node graph
----------------
  orchestrator_node  →  buyer_agent_node    (loops back to orchestrator)
  orchestrator_node  →  supplier_agent_node (loops back to orchestrator)
  orchestrator_node  →  END  (AGREED | FAILED | HITL_PENDING)
"""
from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from .config import settings
from .guards import (
    check_convergence,
    compute_midpoint_price,
    is_near_margin_floor,
    verify_payout_and_margin,
)
from .llm import get_llm_client
from .prompts import (
    BUYER_AGENT_SYSTEM_PROMPT,
    ORCHESTRATOR_SYSTEM_PROMPT,
    SUPPLIER_AGENT_SYSTEM_PROMPT,
)
from .schemas import (
    AgentRouting,
    NegotiationSession,
    OrchestratorDecision,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class NegotiationGraphState(TypedDict, total=False):
    # Immutable inputs
    session: NegotiationSession
    logistics_cost: float
    is_first_round: bool
    party: str
    inbound_message: str | None
    inbound_price: float | None
    operator_override_price: float | None
    buyer_history: str        # party-scoped transcript of prior buyer emails
    supplier_history: str     # party-scoped transcript of prior supplier emails

    # Mutable per-node outputs
    current_bid: float | None
    current_ask: float | None
    current_margin: float
    orchestrator_decision: OrchestratorDecision | None
    buyer_outbound_message: str
    supplier_outbound_message: str
    buyer_terms: dict[str, Any]
    supplier_terms: dict[str, Any]
    routing: AgentRouting
    hitl_required: bool
    agreed_price: float | None
    reasoning: str
    error: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_llm_json(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        logger.warning("LLM response contained no JSON — using fallback")
        return fallback
    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse error in LLM response (%s) — using fallback", exc)
        return fallback


def _current_bid(state: NegotiationGraphState) -> float:
    if state.get("current_bid") is not None:
        return state["current_bid"]  # type: ignore[return-value]
    return state["session"].buyer_target_price


def _current_ask(state: NegotiationGraphState) -> float:
    if state.get("current_ask") is not None:
        return state["current_ask"]  # type: ignore[return-value]
    return state["session"].supplier_quote_price


async def _chat(system_prompt: str, payload: dict[str, Any], caller: str) -> str:
    """Invoke the shared LLM Gateway and return the raw content string."""
    client = get_llm_client()
    response = await client.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload)},
        ],
        caller=caller,
        temperature=settings.negotiation_llm_temperature,
        max_tokens=settings.negotiation_llm_max_tokens,
    )
    return response.content


# ---------------------------------------------------------------------------
# Node: Orchestrator
# ---------------------------------------------------------------------------

async def orchestrator_node(state: NegotiationGraphState) -> NegotiationGraphState:
    session: NegotiationSession = state["session"]
    logistics = state.get("logistics_cost", 0.0)
    bid = _current_bid(state)
    ask = _current_ask(state)
    policy = session.policy

    # Deterministic guards — evaluated before any LLM call
    converged = check_convergence(session, bid, ask, logistics)
    margin_ok, margin_pct = verify_payout_and_margin(session, bid, ask, logistics)
    near_floor = is_near_margin_floor(session, margin_pct, settings.hitl_margin_warning_pct)

    if converged:
        agreed = compute_midpoint_price(bid, ask)
        return {
            **state,
            "routing": "NEGOTIATION_AGREED",
            "agreed_price": agreed,
            "current_margin": margin_pct,
            "hitl_required": True,
            "reasoning": (
                f"Convergence achieved: bid={bid} ask={ask} "
                f"gap={abs(ask - bid):.4f} margin={margin_pct:.2f}%"
            ),
        }

    if session.round_no >= policy.max_rounds:
        return {
            **state,
            "routing": "NEGOTIATION_FAILED",
            "current_margin": margin_pct,
            "hitl_required": True,
            "reasoning": f"Max rounds ({policy.max_rounds}) exhausted without convergence",
        }

    # Not terminal: an agent will draft a counter this pass. The near-floor and
    # first-outbound conditions no longer short-circuit *before* a message is
    # drafted — they flag the resulting draft for mandatory HITL review instead.
    # (Every outbound email is L0-gated in _post_graph_notify regardless, so an
    # operator must still sign off before any message is actually sent.)
    force_hitl = bool(near_floor) or bool(
        state.get("is_first_round") and settings.hitl_on_first_outbound
    )
    hitl_reason = ""
    if near_floor:
        hitl_reason = (
            f"Margin {margin_pct:.2f}% near floor {policy.margin_floor_pct}% — draft gated for HITL. "
        )
    elif force_hitl:
        hitl_reason = "First outbound — draft gated for HITL. "

    # Delegate routing decision to LLM Gateway
    payload = {
        "trade_match_id": session.trade_match_id,
        "buyer_target_price": session.buyer_target_price,
        "supplier_quote_price": session.supplier_quote_price,
        "current_bid": bid,
        "current_ask": ask,
        "round_no": session.round_no,
        "max_rounds": policy.max_rounds,
        "margin_floor_pct": policy.margin_floor_pct,
        "target_margin_pct": policy.target_margin_pct,
        "cost_envelope": session.cost_envelope.model_dump() if session.cost_envelope else {},
        "logistics_cost_usd": logistics,
        "current_estimated_margin_pct": margin_pct,
        "rag_context": session.rag_context,
        "party": state.get("party", "init"),
        "is_first_round": state.get("is_first_round", False),
        "hitl_on_first_outbound": settings.hitl_on_first_outbound,
    }

    try:
        raw = await _chat(ORCHESTRATOR_SYSTEM_PROMPT, payload, "negotiation_agent.orchestrator")
        decision_dict = _parse_llm_json(raw, {
            "action_routing": "CALL_BUYER_AGENT",
            "current_estimated_margin_pct": margin_pct,
            "margin_at_current": margin_pct,
            "hitl_required": False,
            "reasoning": "LLM parse fallback",
            "buyer_round": 0,
            "supplier_round": 0,
            "trade_match_id": session.trade_match_id,
        })
        decision = OrchestratorDecision(**decision_dict)
    except Exception as exc:
        logger.error("Orchestrator LLM call failed: %s — falling back to CALL_BUYER_AGENT", exc)
        decision = OrchestratorDecision(
            trade_match_id=session.trade_match_id,
            current_estimated_margin_pct=margin_pct,
            action_routing="CALL_BUYER_AGENT",
            reasoning=f"LLM gateway error fallback: {exc}",
            margin_at_current=margin_pct,
            hitl_required=False,
        )

    # Safety clamp: on a non-terminal pass we must route to an agent so a message
    # is drafted. If the LLM returned a terminal/HITL routing, fall back to a
    # deterministic buyer/supplier choice based on the last inbound party.
    routing = decision.action_routing
    if routing not in ("CALL_BUYER_AGENT", "CALL_SUPPLIER_AGENT"):
        routing = "CALL_SUPPLIER_AGENT" if state.get("party") == "buyer" else "CALL_BUYER_AGENT"

    return {
        **state,
        "orchestrator_decision": decision,
        "routing": routing,
        "current_margin": margin_pct,
        "hitl_required": decision.hitl_required or force_hitl,
        "reasoning": hitl_reason + (decision.reasoning or ""),
    }


# ---------------------------------------------------------------------------
# Node: Buyer-Facing Agent
# ---------------------------------------------------------------------------

async def buyer_agent_node(state: NegotiationGraphState) -> NegotiationGraphState:
    session: NegotiationSession = state["session"]
    bid = _current_bid(state)
    ask = _current_ask(state)
    logistics = state.get("logistics_cost", 0.0)
    env_total = session.cost_envelope.total() if session.cost_envelope else 0.0
    floor_pct = session.policy.margin_floor_pct
    # Minimum CIF price the buyer must pay for PortLess to clear its margin floor.
    # It is derived from the supplier ask + costs, but the LLM never receives the
    # raw ask, the midpoint, logistics, or the cost envelope — so the supplier's
    # actual price cannot be recovered from this bound. (The node keeps `ask` only
    # for the deterministic LLM-failure fallback below, never in the payload.)
    denom = max(1.0 - floor_pct / 100.0, 0.01)
    floor_price = round((ask + logistics + env_total) / denom, 2)
    midpoint = compute_midpoint_price(bid, ask)  # local only — fallback, not sent

    payload = {
        "buyer_id": session.buyer_id,
        "current_bid": bid,
        "floor_price": floor_price,
        "round_no": session.round_no,
        "max_rounds": session.policy.max_rounds,
        "target_margin_pct": session.policy.target_margin_pct,
        "margin_floor_pct": floor_pct,
        "concession_budget_remaining": round(bid - floor_price, 4),
        "rag_context": session.rag_context,
        "inbound_buyer_message": state.get("inbound_message") or "",
        "conversation_history": state.get("buyer_history", ""),
    }

    try:
        raw = await _chat(BUYER_AGENT_SYSTEM_PROMPT, payload, "negotiation_agent.buyer_agent")
        result = _parse_llm_json(raw, {
            "counter_price_usd": midpoint,
            "outbound_message": f"Thank you for your offer. We propose USD {midpoint:.2f}/unit CIF.",
            "negotiated_terms": {"unit_price": midpoint, "currency": "USD"},
            "reasoning": "LLM parse fallback",
        })
    except Exception as exc:
        logger.error("BuyerAgent LLM call failed: %s", exc)
        result = {
            "counter_price_usd": midpoint,
            "outbound_message": f"We propose USD {midpoint:.2f}/unit CIF as our revised offer.",
            "negotiated_terms": {"unit_price": midpoint, "currency": "USD"},
            "reasoning": f"LLM gateway error: {exc}",
        }

    new_bid = float(result.get("counter_price_usd", midpoint))
    return {
        **state,
        "current_bid": new_bid,
        "buyer_outbound_message": result.get("outbound_message", ""),
        "buyer_terms": result.get("negotiated_terms", {}),
        "reasoning": result.get("reasoning", ""),
    }


# ---------------------------------------------------------------------------
# Node: Supplier-Facing Agent
# ---------------------------------------------------------------------------

async def supplier_agent_node(state: NegotiationGraphState) -> NegotiationGraphState:
    session: NegotiationSession = state["session"]
    bid = _current_bid(state)
    ask = _current_ask(state)
    logistics = state.get("logistics_cost", 0.0)
    env_total = session.cost_envelope.total() if session.cost_envelope else 0.0
    implied_max = bid * (1 - session.policy.margin_floor_pct / 100) - logistics - env_total

    payload = {
        "seller_id": session.seller_id,
        "current_ask": ask,
        "implied_max_supplier_price_usd": round(implied_max, 4),
        "round_no": session.round_no,
        "max_rounds": session.policy.max_rounds,
        "target_margin_pct": session.policy.target_margin_pct,
        "margin_floor_pct": session.policy.margin_floor_pct,
        "rag_context": session.rag_context,
        "inbound_supplier_message": state.get("inbound_message") or "",
        "conversation_history": state.get("supplier_history", ""),
        "product_code": session.product_code,
        "concession_budget_remaining": round(implied_max - ask, 4),
    }

    try:
        raw = await _chat(SUPPLIER_AGENT_SYSTEM_PROMPT, payload, "negotiation_agent.supplier_agent")
        result = _parse_llm_json(raw, {
            "counter_price_usd": implied_max,
            "outbound_message": f"We can offer USD {implied_max:.2f}/unit Ex-Works.",
            "negotiated_terms": {"unit_price": implied_max, "currency": "USD"},
            "reasoning": "LLM parse fallback",
        })
    except Exception as exc:
        logger.error("SupplierAgent LLM call failed: %s", exc)
        result = {
            "counter_price_usd": implied_max,
            "outbound_message": f"Our revised Ex-Works price: USD {implied_max:.2f}/unit.",
            "negotiated_terms": {"unit_price": implied_max, "currency": "USD"},
            "reasoning": f"LLM gateway error: {exc}",
        }

    new_ask = float(result.get("counter_price_usd", implied_max))
    return {
        **state,
        "current_ask": new_ask,
        "supplier_outbound_message": result.get("outbound_message", ""),
        "supplier_terms": result.get("negotiated_terms", {}),
        "reasoning": result.get("reasoning", ""),
    }


# ---------------------------------------------------------------------------
# Routing function
# ---------------------------------------------------------------------------

def route_after_orchestrator(state: NegotiationGraphState) -> str:
    routing = state.get("routing", "CALL_BUYER_AGENT")
    mapping = {
        "CALL_BUYER_AGENT": "buyer_agent",
        "CALL_SUPPLIER_AGENT": "supplier_agent",
        "HITL_APPROVAL": "__end__",
        "NEGOTIATION_AGREED": "__end__",
        "NEGOTIATION_FAILED": "__end__",
    }
    return mapping.get(routing, "__end__")


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_negotiation_graph() -> Any:
    """Compile the LangGraph StateGraph for one negotiation pass."""
    try:
        from langgraph.graph import END, StateGraph  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "langgraph is required. Run: pip install langgraph"
        ) from exc

    graph = StateGraph(NegotiationGraphState)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("buyer_agent", buyer_agent_node)
    graph.add_node("supplier_agent", supplier_agent_node)
    graph.set_entry_point("orchestrator")
    graph.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "buyer_agent": "buyer_agent",
            "supplier_agent": "supplier_agent",
            "__end__": END,
        },
    )
    # One inbound → one outbound per graph pass: after an agent drafts its message
    # the pass ends (it does not loop back to the orchestrator for another internal
    # round). Convergence is re-evaluated on the next run_round() call, once a real
    # counter-offer has arrived.
    graph.add_edge("buyer_agent", END)
    graph.add_edge("supplier_agent", END)
    return graph.compile()
