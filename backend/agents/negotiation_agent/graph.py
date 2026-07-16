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
    acceptance_price_consistent,
    check_convergence,
    compute_midpoint_price,
    is_explicit_acceptance,
    is_near_margin_floor,
    verify_payout_and_margin,
)
from .llm import get_llm_client
from .prompts import (
    BUYER_AGENT_SYSTEM_PROMPT,
    INTENT_CLASSIFIER_SYSTEM_PROMPT,
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
    buyer_market_context: str      # live web-search market color for buyer-side drafting (may be empty)
    supplier_market_context: str   # live web-search market color for supplier-side drafting (may be empty)
    usd_inr_rate: float            # USD/INR rate for supplier-side INR pricing (live fetch or fallback)

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


_UNPARSEABLE: dict[str, Any] = {"__unparseable__": True}


async def _chat_json(
    system_prompt: str,
    payload: dict[str, Any],
    caller: str,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Chat and parse a strict-JSON reply, retrying once if it can't be parsed.

    The Groq fallback models (qwen3-*) are reasoning models that emit a <think>
    block before the JSON answer and can occasionally overrun the token budget
    mid-answer, yielding a reply with no closing brace. These calls are
    stochastic (temperature > 0), so a single retry recovers the large majority
    of such truncations before we resort to ``fallback`` (a deterministic,
    template-based draft). On a transport error the fallback's ``reasoning`` is
    annotated with the error for the round record."""
    last_error: str | None = None
    for attempt in range(2):
        try:
            raw = await _chat(system_prompt, payload, caller)
        except Exception as exc:  # noqa: BLE001 — any gateway error → retry then fallback
            last_error = str(exc)
            logger.error("%s LLM call failed (attempt %d/2): %s", caller, attempt + 1, exc)
            continue
        parsed = _parse_llm_json(raw, _UNPARSEABLE)
        if parsed is not _UNPARSEABLE:
            return parsed
        logger.warning("%s: unparseable LLM JSON (attempt %d/2)", caller, attempt + 1)
    result = dict(fallback)
    if last_error:
        result["reasoning"] = f"LLM gateway error: {last_error}"
    return result


# ---------------------------------------------------------------------------
# Inbound intent classification
# ---------------------------------------------------------------------------

_INTENT_UNAVAILABLE = "UNAVAILABLE"
_VALID_INTENTS = {"ACCEPT", "COUNTER_OFFER", "REJECT", "QUESTION", "OTHER"}


async def classify_inbound_intent(
    party: str,
    inbound_message: str,
    reference_price: float,
    conversation_history: str = "",
) -> dict[str, Any]:
    """Classify the counterpart's reply via the LLM Gateway.

    Returns ``{"intent": ..., "mentioned_price_usd": float | None, "reasoning": ...}``.
    ``intent`` is one of _VALID_INTENTS, or ``_INTENT_UNAVAILABLE`` when the
    gateway itself failed (transport error / unparseable after retry) — the
    caller then falls back to the deterministic keyword guard. A reply the LLM
    *did* classify but with an unknown label is coerced to "OTHER" (not an
    acceptance), never to the fallback path.
    """
    payload = {
        "party": party,
        "inbound_message": inbound_message,
        "reference_price_usd": reference_price,
        "conversation_history": conversation_history,
    }
    result = await _chat_json(
        INTENT_CLASSIFIER_SYSTEM_PROMPT, payload, "negotiation_agent.intent_classifier",
        fallback={"intent": _INTENT_UNAVAILABLE, "mentioned_price_usd": None,
                  "reasoning": "intent classifier unavailable"},
    )
    intent = str(result.get("intent", "")).strip().upper()
    if intent != _INTENT_UNAVAILABLE and intent not in _VALID_INTENTS:
        intent = "OTHER"
    mentioned: float | None
    try:
        raw_price = result.get("mentioned_price_usd")
        mentioned = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        mentioned = None
    return {
        "intent": intent,
        "mentioned_price_usd": mentioned,
        "reasoning": str(result.get("reasoning", "")),
    }


async def _detect_explicit_acceptance(
    state: NegotiationGraphState,
    party: str,
    reference_price: float,
) -> tuple[bool, str]:
    """Decide whether the inbound reply is an outright acceptance.

    Layered: LLM intent classification (primary, language understanding) →
    keyword guard (fallback only when the gateway is down) → deterministic
    price-consistency check (always). Every path is conservative: a false
    negative just continues the round.
    """
    inbound = state.get("inbound_message") or ""
    history_key = "buyer_history" if party == "buyer" else "supplier_history"
    result = await classify_inbound_intent(
        party, inbound, reference_price, state.get(history_key, "")
    )
    intent = result["intent"]
    if intent == _INTENT_UNAVAILABLE:
        accepted = is_explicit_acceptance(inbound)
        detail = "keyword-guard fallback (intent classifier unavailable)"
    else:
        accepted = intent == "ACCEPT"
        detail = f"LLM intent={intent}" + (
            f" ({result['reasoning']})" if result["reasoning"] else ""
        )
    if not accepted:
        return False, detail

    # Deterministic cross-checks — an "acceptance" naming any other price is a counter.
    mentioned = result["mentioned_price_usd"]
    tolerance = max(abs(reference_price) * 0.01, 0.01)
    if mentioned is not None and abs(mentioned - reference_price) > tolerance:
        return False, (
            f"{detail} — vetoed: message price {mentioned} differs from "
            f"reference {reference_price}"
        )
    if not acceptance_price_consistent(inbound, reference_price):
        return False, (
            f"{detail} — vetoed: USD price in message differs from "
            f"reference {reference_price}"
        )
    return True, detail


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

    # A counterpart that outright accepts the last quoted price (rather than
    # countering) never satisfies check_convergence()'s bid/ask gap test when
    # the other side's price is fixed (e.g. a pre-sourced supplier quote) —
    # that gap is the margin, not a spread expected to close. Acceptance is
    # detected by the LLM intent classifier (keyword guard only as gateway-down
    # fallback) plus a deterministic price-consistency check. Only evaluated
    # when margin is already healthy; an accept at a sub-floor price still
    # falls through to the normal LLM routing / near-floor HITL gate below.
    party = state.get("party")
    explicit_accept = False
    accept_detail = ""
    if (
        not converged
        and margin_ok
        and party in ("buyer", "supplier")
        and state.get("inbound_message")
    ):
        reference_price = bid if party == "buyer" else ask
        explicit_accept, accept_detail = await _detect_explicit_acceptance(
            state, party, reference_price
        )

    if converged or explicit_accept:
        if converged:
            agreed = compute_midpoint_price(bid, ask)
            reasoning = (
                f"Convergence achieved: bid={bid} ask={ask} "
                f"gap={abs(ask - bid):.4f} margin={margin_pct:.2f}%"
            )
        else:
            agreed = bid if party == "buyer" else ask
            reasoning = (
                f"Explicit acceptance detected from {party} ({accept_detail}) — "
                f"agreed_price={agreed} margin={margin_pct:.2f}% "
                f"(>= floor {policy.margin_floor_pct}%) — pending operator confirmation"
            )
        return {
            **state,
            "routing": "NEGOTIATION_AGREED",
            "agreed_price": agreed,
            "current_margin": margin_pct,
            "hitl_required": True,
            "reasoning": reasoning,
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
        "market_context": state.get("buyer_market_context", ""),
    }

    result = await _chat_json(
        BUYER_AGENT_SYSTEM_PROMPT, payload, "negotiation_agent.buyer_agent",
        fallback={
            "counter_price_usd": midpoint,
            "outbound_message": f"Thank you for your offer. We propose USD {midpoint:.2f}/unit CIF.",
            "negotiated_terms": {"unit_price": midpoint, "currency": "USD"},
            "reasoning": "LLM parse fallback",
        },
    )

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
    # Computed here, not by the LLM: a bare "communicate in INR" instruction
    # with no rate supplied previously sent the model into a token-burning
    # spiral trying to guess a conversion (see market_intel.get_usd_inr_rate).
    usd_inr_rate = state.get("usd_inr_rate") or settings.fallback_usd_inr_rate
    # Every price is supplied in BOTH currencies, each explicitly suffixed
    # _usd / _inr. `ask` (the supplier's quote) is a USD figure — leaving it
    # unlabeled next to INR fields previously made the model assume it was INR,
    # see a "huge gap" against the INR ceiling, and burn its whole token budget
    # trying to reconcile the mismatch. Nothing here is left for the LLM to
    # convert or infer.
    implied_max_inr = round(implied_max * usd_inr_rate, 2)
    ask_inr = round(ask * usd_inr_rate, 2)
    concession_usd = round(implied_max - ask, 4)

    payload = {
        "seller_id": session.seller_id,
        "current_ask_usd": round(ask, 4),
        "current_ask_inr": ask_inr,
        "implied_max_supplier_price_usd": round(implied_max, 4),
        "implied_max_supplier_price_inr": implied_max_inr,
        "usd_inr_rate": usd_inr_rate,
        "round_no": session.round_no,
        "max_rounds": session.policy.max_rounds,
        "target_margin_pct": session.policy.target_margin_pct,
        "margin_floor_pct": session.policy.margin_floor_pct,
        "rag_context": session.rag_context,
        "inbound_supplier_message": state.get("inbound_message") or "",
        "conversation_history": state.get("supplier_history", ""),
        "market_context": state.get("supplier_market_context", ""),
        "product_code": session.product_code,
        "concession_budget_remaining_usd": concession_usd,
        "concession_budget_remaining_inr": round(concession_usd * usd_inr_rate, 2),
    }

    result = await _chat_json(
        SUPPLIER_AGENT_SYSTEM_PROMPT, payload, "negotiation_agent.supplier_agent",
        fallback={
            "counter_price_usd": implied_max,
            "outbound_message": f"We can offer USD {implied_max:.2f}/unit Ex-Works.",
            "negotiated_terms": {"unit_price": implied_max, "currency": "USD"},
            "reasoning": "LLM parse fallback",
        },
    )

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
