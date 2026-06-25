"""Negotiation Agent service layer.

Orchestrates:
  1. start_negotiation() — initialise session, load RAG, run first graph tick
  2. run_round()         — ingest inbound counter-offer, run one graph pass
  3. get_negotiation()   — read session state
  4. list_rounds()       — retrieve round history
  5. monitor_inbox()     — placeholder (see email_dispatcher.InboxMonitor)
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from .config import settings
from .email_dispatcher import EmailDispatcher, EmailMessage, InboxMonitor
from .graph import NegotiationGraphState, build_negotiation_graph
from .guards import compute_net_margin
from .rag import load_rag_context
from .repository import NegotiationRepository, get_repository
from .schemas import (
    NegotiationPolicy,
    NegotiationResponse,
    NegotiationRound,
    NegotiationSession,
    NegotiationStatus,
    RoundRequest,
    StartNegotiationRequest,
)

logger = logging.getLogger(__name__)

_dispatcher = EmailDispatcher()
_inbox_monitor = InboxMonitor()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _safe_call(coro: object) -> object:
    try:
        return await coro  # type: ignore[arg-type]
    except Exception as exc:
        return exc


def _session_to_response(session: NegotiationSession, reasoning: str = "", **extra: object) -> NegotiationResponse:
    return NegotiationResponse(
        negotiation_id=session.negotiation_id,
        deal_id=session.deal_id,
        status=session.status,
        round_no=session.round_no,
        current_bid=session.current_bid,
        current_ask=session.current_ask,
        agreed_price=session.agreed_price,
        margin_at_current=session.margin_at_current,
        hitl_required=session.hitl_required,
        reasoning=reasoning or None,
        **extra,
    )


def _graph_state_to_status(routing: str) -> NegotiationStatus:
    mapping: dict[str, NegotiationStatus] = {
        "NEGOTIATION_AGREED": "AGREED",
        "NEGOTIATION_FAILED": "FAILED",
        "HITL_APPROVAL": "HITL_PENDING",
        "CALL_BUYER_AGENT": "ROUND",
        "CALL_SUPPLIER_AGENT": "ROUND",
    }
    return mapping.get(routing, "ROUND")


async def _post_graph_notify(
    session: NegotiationSession,
    final_state: NegotiationGraphState,
    repo: NegotiationRepository,
) -> None:
    """Emit domain events and dispatch outbound messages after graph completes."""
    buyer_msg = final_state.get("buyer_outbound_message", "")
    supplier_msg = final_state.get("supplier_outbound_message", "")

    tasks = []
    if buyer_msg:
        tasks.append(_dispatcher.send(EmailMessage(
            to_address=f"{session.buyer_id}@portless.placeholder",
            subject=f"PortLess Trade Update — Round {session.round_no}",
            body=buyer_msg,
            negotiation_id=session.negotiation_id,
            round_no=session.round_no,
        )))
    if supplier_msg:
        tasks.append(_dispatcher.send(EmailMessage(
            to_address=f"{session.seller_id}@portless.placeholder",
            subject=f"PortLess Trade Quote — Round {session.round_no}",
            body=supplier_msg,
            negotiation_id=session.negotiation_id,
            round_no=session.round_no,
        )))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Email dispatch error: %s", r)

    if session.status == "AGREED" and session.agreed_price:
        await _compute_realized_cms(session, repo)


async def _compute_realized_cms(session: NegotiationSession, repo: NegotiationRepository) -> None:
    """Call Match Engine post-agreement to compute Realized CMS."""
    try:
        from services.match_engine.service import score_match  # type: ignore[import]
        cms_result = await score_match(
            opportunity_id=session.trade_match_id,
            buyer_price=session.agreed_price or session.buyer_target_price,
            supplier_price=session.current_ask or session.supplier_quote_price,
        )
        session.realized_cms = getattr(cms_result, "composite_score", None)
        session.updated_at = datetime.now(timezone.utc)
        await repo.save_session(session)
        logger.info(
            "Realized CMS saved for negotiation_id=%s cms=%s",
            session.negotiation_id,
            session.realized_cms,
        )
    except Exception as exc:
        logger.warning("Realized CMS computation failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_negotiation(payload: StartNegotiationRequest) -> NegotiationResponse:
    repo = get_repository()

    existing = await repo.get_session_by_deal(payload.deal_id)
    if existing and existing.status not in ("FAILED", "ESCALATED"):
        logger.info("Reusing existing negotiation session_id=%s for deal_id=%s",
                    existing.negotiation_id, payload.deal_id)
        return _session_to_response(existing)

    negotiation_id = str(uuid.uuid4())
    rag_context = await load_rag_context(payload.product_code)

    now = datetime.now(timezone.utc)
    session = NegotiationSession(
        negotiation_id=negotiation_id,
        deal_id=payload.deal_id,
        trade_match_id=payload.trade_match_id,
        buyer_id=payload.buyer_id,
        seller_id=payload.seller_id,
        product_code=payload.product_code,
        buyer_country_code=payload.buyer_country_code,
        buyer_target_price=payload.buyer_target_price,
        supplier_quote_price=payload.supplier_quote_price,
        composite_match_score=payload.composite_match_score,
        policy=payload.policy,
        cost_envelope=payload.cost_envelope,
        batna=payload.batna,
        rag_context=rag_context,
        status="STARTED",
        round_no=0,
        current_bid=payload.buyer_target_price,
        current_ask=payload.supplier_quote_price,
        created_at=now,
        updated_at=now,
    )
    await repo.save_session(session)

    initial_state: NegotiationGraphState = {
        "session": session,
        "logistics_cost": 0.0,
        "is_first_round": True,
        "party": "init",
        "inbound_message": None,
        "inbound_price": None,
        "operator_override_price": None,
        "current_bid": session.buyer_target_price,
        "current_ask": session.supplier_quote_price,
        "current_margin": 0.0,
        "orchestrator_decision": None,
        "buyer_outbound_message": "",
        "supplier_outbound_message": "",
        "buyer_terms": {},
        "supplier_terms": {},
        "routing": "CALL_BUYER_AGENT",
        "hitl_required": False,
        "agreed_price": None,
        "reasoning": "",
        "error": None,
    }

    graph = build_negotiation_graph()
    try:
        final_state: NegotiationGraphState = await graph.ainvoke(initial_state)
    except Exception as exc:
        logger.error("Graph execution failed on start_negotiation: %s", exc)
        final_state = {**initial_state, "routing": "HITL_APPROVAL", "error": str(exc), "hitl_required": True}

    session.status = _graph_state_to_status(final_state.get("routing", "HITL_APPROVAL"))
    session.round_no = session.round_no + 1
    session.current_bid = final_state.get("current_bid", session.current_bid)
    session.current_ask = final_state.get("current_ask", session.current_ask)
    session.agreed_price = final_state.get("agreed_price")
    session.margin_at_current = final_state.get("current_margin")
    session.hitl_required = final_state.get("hitl_required", False)
    session.updated_at = datetime.now(timezone.utc)
    await repo.save_session(session)

    round_rec = NegotiationRound(
        round_id=str(uuid.uuid4()),
        negotiation_id=negotiation_id,
        deal_id=payload.deal_id,
        round_no=session.round_no,
        party="init",
        channel="portal",
        inbound_price=None,
        inbound_message=None,
        outbound_message=final_state.get("buyer_outbound_message", ""),
        negotiated_terms=final_state.get("buyer_terms", {}),
        orchestrator_routing=final_state.get("routing"),
        orchestrator_reasoning=final_state.get("reasoning", ""),
        margin_at_round=final_state.get("current_margin"),
        hitl_required=session.hitl_required,
        escalation_flag=False,
        llm_reasoning=final_state.get("reasoning", ""),
        created_at=datetime.now(timezone.utc),
    )
    await repo.save_round(round_rec)

    await _post_graph_notify(session, final_state, repo)

    return NegotiationResponse(
        negotiation_id=negotiation_id,
        deal_id=session.deal_id,
        status=session.status,
        round_no=session.round_no,
        current_bid=session.current_bid,
        current_ask=session.current_ask,
        agreed_price=session.agreed_price,
        margin_at_current=session.margin_at_current,
        hitl_required=session.hitl_required,
        reasoning=final_state.get("reasoning"),
        outbound_buyer_message=final_state.get("buyer_outbound_message") or None,
        outbound_supplier_message=final_state.get("supplier_outbound_message") or None,
    )


async def run_round(negotiation_id: str, request: RoundRequest) -> NegotiationResponse:
    repo = get_repository()
    session = await repo.get_session(negotiation_id)
    if session is None:
        raise ValueError(f"Negotiation session not found: {negotiation_id}")
    if session.status in ("AGREED", "FAILED", "ESCALATED"):
        raise ValueError(f"Negotiation {negotiation_id} is terminal (status={session.status})")

    effective_price = request.operator_override_price or request.counter_price
    if request.party == "buyer":
        session.current_bid = effective_price
    else:
        session.current_ask = effective_price

    state: NegotiationGraphState = {
        "session": session,
        "logistics_cost": 0.0,
        "is_first_round": False,
        "party": request.party,
        "inbound_message": request.inbound_message,
        "inbound_price": effective_price,
        "operator_override_price": request.operator_override_price,
        "current_bid": session.current_bid,
        "current_ask": session.current_ask,
        "current_margin": session.margin_at_current or 0.0,
        "orchestrator_decision": None,
        "buyer_outbound_message": "",
        "supplier_outbound_message": "",
        "buyer_terms": {},
        "supplier_terms": {},
        "routing": "CALL_BUYER_AGENT",
        "hitl_required": False,
        "agreed_price": None,
        "reasoning": "",
        "error": None,
    }

    graph = build_negotiation_graph()
    try:
        final_state: NegotiationGraphState = await graph.ainvoke(state)
    except Exception as exc:
        logger.error("Graph execution failed on run_round: %s", exc)
        final_state = {**state, "routing": "HITL_APPROVAL", "error": str(exc), "hitl_required": True}

    session.status = _graph_state_to_status(final_state.get("routing", "HITL_APPROVAL"))
    session.round_no += 1
    session.current_bid = final_state.get("current_bid", session.current_bid)
    session.current_ask = final_state.get("current_ask", session.current_ask)
    session.agreed_price = final_state.get("agreed_price")
    session.margin_at_current = final_state.get("current_margin")
    session.hitl_required = final_state.get("hitl_required", False)
    session.updated_at = datetime.now(timezone.utc)
    await repo.save_session(session)

    round_rec = NegotiationRound(
        round_id=str(uuid.uuid4()),
        negotiation_id=negotiation_id,
        deal_id=session.deal_id,
        round_no=session.round_no,
        party=request.party,
        channel=request.channel,
        inbound_price=effective_price,
        inbound_message=request.inbound_message,
        outbound_message=(
            final_state.get("buyer_outbound_message", "")
            if request.party == "supplier"
            else final_state.get("supplier_outbound_message", "")
        ),
        negotiated_terms=final_state.get("buyer_terms", {}) or final_state.get("supplier_terms", {}),
        orchestrator_routing=final_state.get("routing"),
        orchestrator_reasoning=final_state.get("reasoning", ""),
        margin_at_round=final_state.get("current_margin"),
        hitl_required=session.hitl_required,
        escalation_flag=session.status == "ESCALATED",
        llm_reasoning=final_state.get("reasoning", ""),
        created_at=datetime.now(timezone.utc),
    )
    await repo.save_round(round_rec)

    await _post_graph_notify(session, final_state, repo)

    return NegotiationResponse(
        negotiation_id=negotiation_id,
        deal_id=session.deal_id,
        status=session.status,
        round_no=session.round_no,
        current_bid=session.current_bid,
        current_ask=session.current_ask,
        agreed_price=session.agreed_price,
        margin_at_current=session.margin_at_current,
        hitl_required=session.hitl_required,
        reasoning=final_state.get("reasoning"),
        outbound_buyer_message=final_state.get("buyer_outbound_message") or None,
        outbound_supplier_message=final_state.get("supplier_outbound_message") or None,
    )


async def get_negotiation(negotiation_id: str) -> NegotiationSession:
    repo = get_repository()
    session = await repo.get_session(negotiation_id)
    if session is None:
        raise ValueError(f"Negotiation session not found: {negotiation_id}")
    return session


async def list_negotiations(deal_id: str | None = None) -> list[NegotiationSession]:
    return await get_repository().list_sessions(deal_id=deal_id)


async def list_rounds(negotiation_id: str) -> list[NegotiationRound]:
    return await get_repository().list_rounds(negotiation_id)


async def monitor_inbox() -> None:
    """Start the IMAP inbox monitor (placeholder — see InboxMonitor for TODO)."""
    await _inbox_monitor.start()
