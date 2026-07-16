"""Negotiation Agent service layer.

Orchestrates:
  1. start_negotiation() — initialise session, load RAG, run first graph tick
  2. run_round()         — ingest inbound counter-offer, run one graph pass
  3. get_negotiation()   — read session state
  4. list_rounds()       — retrieve round history
  5. list_email_history()  — email audit trail for a negotiation
  6. send_approved_email() — called by HITL callback when operator approves;
                             delegates SMTP transport to the Email Sender service
  7. reject_email()        — called by HITL callback when operator rejects
  8. finalize_agreement()  — called by the CONFIRM_NEGOTIATION_AGREEMENT HITL
                             callback on APPROVE; the ONLY path to status AGREED
  9. reject_agreement()    — same callback on REJECT; reopens the negotiation

Email transport (IMAP fetch + SMTP send) lives in services/email_gateway/;
inbound processing lives in services/email_processor/. This flow calls the
Email Gateway to deliver approved drafts.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from shared.bus.publisher import get_publisher
from shared.domain.events import DomainEvent, EventTopic

from .config import settings
from .email_history import (
    EmailDirection,
    EmailHistoryRecord,
    EmailStatus,
    get_email_repository,
)
from .graph import NegotiationGraphState, build_negotiation_graph
from .guards import compute_net_margin
from .market_intel import fetch_both, get_usd_inr_rate
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

_SMTP_FROM = "portless-trade@portless.io"


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


def _graph_state_to_status(routing: str, hitl_required: bool = False) -> NegotiationStatus:
    if routing == "NEGOTIATION_AGREED":
        # A detected agreement is provisional: the session parks at HITL_PENDING
        # behind a CONFIRM_NEGOTIATION_AGREEMENT approval, and only the operator's
        # APPROVE (via finalize_agreement) moves it to AGREED. Neither the LLM nor
        # the deterministic guards ever close a deal on their own.
        return "HITL_PENDING"
    if routing == "NEGOTIATION_FAILED":
        return "FAILED"
    if routing == "HITL_APPROVAL":
        return "HITL_PENDING"
    # An agent drafted an outbound message this pass; it is L0-gated for approval.
    # Reflect HITL_PENDING when the draft was flagged (near-floor / first-outbound),
    # otherwise ROUND (active round, awaiting the next reply).
    return "HITL_PENDING" if hitl_required else "ROUND"


# ---------------------------------------------------------------------------
# Conversation history — party-scoped transcript fed to the drafting agents
# ---------------------------------------------------------------------------

_HISTORY_MAX_MESSAGES = 10
_HISTORY_BODY_CHARS = 400


def _format_history(records: list[EmailHistoryRecord], party: str) -> str:
    """Build a compact, party-scoped transcript of *actually communicated* emails.

    Only messages the counterpart truly saw are included — outbound that reached
    ``SENT`` / ``MODIFIED_SENT`` and inbound that is ``RECEIVED``. Unsent drafts
    (``DRAFT`` / ``PENDING_APPROVAL`` / ``REJECTED`` / ``FAILED``) are excluded so
    the agent never "continues" from a message that was never sent. Scoping by
    ``party`` keeps the buyer thread out of the supplier agent's context and
    vice-versa, so neither side's price can leak across.
    """
    relevant = [
        r for r in records
        if r.party == party and (
            (r.direction == EmailDirection.INBOUND and r.status == EmailStatus.RECEIVED)
            or (
                r.direction == EmailDirection.OUTBOUND
                and r.status in (EmailStatus.SENT, EmailStatus.MODIFIED_SENT)
            )
        )
    ]
    relevant.sort(key=lambda r: r.created_at)
    relevant = relevant[-_HISTORY_MAX_MESSAGES:]

    lines: list[str] = []
    for r in relevant:
        who = f"PortLess → {party}" if r.direction == EmailDirection.OUTBOUND else f"{party} → PortLess"
        body = " ".join((r.body or "").split())
        if len(body) > _HISTORY_BODY_CHARS:
            body = body[:_HISTORY_BODY_CHARS] + "…"
        lines.append(f"[round {r.round_no}] {who}: {body}")
    return "\n".join(lines)


async def _build_conversation_history(negotiation_id: str) -> dict[str, str]:
    """Return party-scoped conversation transcripts for the drafting agents.

    Sourced from the ``email_history`` audit log so each agent can continue its
    thread coherently (reference prior offers, avoid re-conceding). Best-effort —
    returns empty strings on any error so drafting never breaks.
    """
    try:
        records = await get_email_repository().list_by_negotiation(negotiation_id)
    except Exception as exc:
        logger.warning("Could not load conversation history for %s: %s", negotiation_id, exc)
        return {"buyer": "", "supplier": ""}
    return {
        "buyer": _format_history(records, "buyer"),
        "supplier": _format_history(records, "supplier"),
    }


async def _create_and_gate_email(
    session: NegotiationSession,
    party: str,
    to_address: str,
    subject: str,
    body: str,
) -> str:
    """Persist an email draft and create an L0 HITL approval gate.

    The email is NOT sent here.  The HITL callback (send_approved_email /
    reject_email) handles the actual dispatch once an operator decides.

    Returns the generated email_id.
    """
    now = datetime.now(timezone.utc)
    email_id = f"EMAIL-{uuid.uuid4().hex[:10].upper()}"

    record = EmailHistoryRecord(
        email_id=email_id,
        negotiation_id=session.negotiation_id,
        deal_id=session.deal_id,
        round_no=session.round_no,
        direction=EmailDirection.OUTBOUND,
        party=party,
        to_address=to_address,
        from_address=_SMTP_FROM,
        subject=subject,
        body=body,
        status=EmailStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    await get_email_repository().save(record)

    try:
        from services.hitl_approval.service import create_approval  # type: ignore[import]
        from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]

        preview = body[:150] + ("..." if len(body) > 150 else "")
        approval_req = CreateApprovalRequest(
            deal_id=session.deal_id,
            action="SEND_NEGOTIATION_EMAIL",
            summary=(
                f"Round {session.round_no} draft email to {party} — {preview}"
            ),
            risk_flags=["email_content_review"],
            autonomy_level="L0",
            callback_payload={
                "email_id": email_id,
                "negotiation_id": session.negotiation_id,
                "party": party,
            },
            idempotency_key=email_id,
        )
        approval = await create_approval(approval_req)

        updated = record.model_copy(update={
            "approval_id": approval.approval_id,
            "status": EmailStatus.PENDING_APPROVAL,
            "updated_at": datetime.now(timezone.utc),
        })
        await get_email_repository().save(updated)

        logger.info(
            "Email draft %s gated via HITL approval %s (party=%s round=%d)",
            email_id, approval.approval_id, party, session.round_no,
        )
    except Exception as exc:
        logger.error("Failed to create HITL approval for email %s: %s", email_id, exc)

    return email_id


async def _create_agreement_gate(
    session: NegotiationSession,
    final_state: NegotiationGraphState,
) -> None:
    """Create the L0 HITL approval that gates a detected agreement.

    The graph's NEGOTIATION_AGREED verdict (convergence or detected acceptance)
    is NOT final — the session sits at HITL_PENDING until an operator resolves
    this approval. APPROVE → finalize_agreement(); REJECT → reject_agreement()
    (soft reject: the negotiation reopens, no deal exception).

    Idempotent per round via the idempotency key, so a re-run of the same
    round never creates a duplicate gate. Best-effort like the email gate —
    a failure here leaves the session at HITL_PENDING for manual follow-up.
    """
    try:
        from services.hitl_approval.service import create_approval  # type: ignore[import]
        from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]

        margin = session.margin_at_current
        margin_txt = f"{margin:.2f}%" if margin is not None else "n/a"
        approval_req = CreateApprovalRequest(
            deal_id=session.deal_id,
            action="CONFIRM_NEGOTIATION_AGREEMENT",
            summary=(
                f"Negotiation {session.negotiation_id} reads as agreed at "
                f"USD {session.agreed_price:.2f} (margin {margin_txt}). "
                f"{final_state.get('reasoning', '')[:200]}"
            ),
            risk_flags=["deal_closing_confirmation"],
            autonomy_level="L0",
            callback_payload={
                "negotiation_id": session.negotiation_id,
                "agreed_price": session.agreed_price,
            },
            idempotency_key=f"{session.negotiation_id}:agreement:{session.round_no}",
        )
        approval = await create_approval(approval_req)
        logger.info(
            "Agreement for negotiation %s gated via HITL approval %s (price=%s)",
            session.negotiation_id, approval.approval_id, session.agreed_price,
        )
    except Exception as exc:
        logger.error(
            "Failed to create agreement-confirmation approval for negotiation %s: %s",
            session.negotiation_id, exc,
        )


async def _post_graph_notify(
    session: NegotiationSession,
    final_state: NegotiationGraphState,
    repo: NegotiationRepository,
) -> None:
    """Create HITL-gated email drafts and emit domain events after graph completes.

    Emails are NOT dispatched directly — each draft is stored and routed
    through a SEND_NEGOTIATION_EMAIL HITL approval.  The operator can then
    approve (send as-is), modify+approve, or reject each draft.
    """
    buyer_msg = final_state.get("buyer_outbound_message", "")
    supplier_msg = final_state.get("supplier_outbound_message", "")

    # First outbound is an introduction, not a negotiation update — "Round 1"
    # in a cold email reads as if the recipient missed prior messages. Reply
    # threading is unaffected: inbound mapping uses headers + the [NEG:{uuid}]
    # token the Email Gateway appends, never the human-readable subject.
    first_contact = session.round_no <= 1
    buyer_subject = (
        f"Trade Proposal — HS {session.product_code} Supply Partnership"
        if first_contact
        else f"PortLess Trade Update — Round {session.round_no}"
    )
    supplier_subject = (
        f"Sourcing Inquiry — HS {session.product_code}"
        if first_contact
        else f"PortLess Trade Quote — Round {session.round_no}"
    )

    tasks = []
    if buyer_msg:
        tasks.append(_create_and_gate_email(
            session,
            "buyer",
            session.buyer_email or f"{session.buyer_id}@portless.placeholder",
            buyer_subject,
            buyer_msg,
        ))
    if supplier_msg:
        tasks.append(_create_and_gate_email(
            session,
            "supplier",
            session.seller_email or f"{session.seller_id}@portless.placeholder",
            supplier_subject,
            supplier_msg,
        ))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Email gating error: %s", r)

    # A detected agreement is gated behind operator confirmation — the CMS
    # computation and deal hand-off happen in finalize_agreement() (the
    # CONFIRM_NEGOTIATION_AGREEMENT approve callback), never here.
    if final_state.get("routing") == "NEGOTIATION_AGREED" and session.agreed_price:
        await _create_agreement_gate(session, final_state)

    # Hand off to the deal pipeline on a terminal failure.
    if session.status == "FAILED":
        await _sync_deal_status(session)

    await get_publisher().publish(DomainEvent(
        topic=EventTopic.NEGOTIATION_ROUND,
        deal_id=session.deal_id,
        payload={
            "negotiation_id": session.negotiation_id,
            "round_no": session.round_no,
            "status": session.status,
            "current_bid": session.current_bid,
            "current_ask": session.current_ask,
            "margin_at_current": session.margin_at_current,
            "routing": final_state.get("routing"),
            "hitl_required": session.hitl_required,
        },
    ))


async def _compute_realized_cms(session: NegotiationSession, repo: NegotiationRepository) -> None:
    """Call Match Engine post-agreement to compute Realized CMS.

    Scores the agreed price (as buyer target) against the supplier's final ask with
    pass-through verification — the supplier was already verified during Sourcing
    before the deal reached negotiation. Best-effort: a failure never blocks the
    agreement.
    """
    try:
        from services.match_engine.service import score_match  # type: ignore[import]
        from services.match_engine.schemas import (  # type: ignore[import]
            ScoreRequest,
            SupplierVerification,
        )

        req = ScoreRequest(
            deal_id=session.deal_id,
            supplier_id=session.seller_id,
            buyer_target_price=session.agreed_price or session.buyer_target_price,
            supplier_quote_price=session.current_ask or session.supplier_quote_price,
            currency="USD",
            verification=SupplierVerification(
                gst_verified=True, iec_active=True, dgft_denied=False
            ),
            supplier_to_port_km=300.0,
            business_age_years=0.0,
            udyam_registered=False,
            annual_shipment_count=0,
        )
        resp = await score_match(req)
        session.realized_cms = getattr(resp, "composite_score", None)
        session.updated_at = datetime.now(timezone.utc)
        await repo.save_session(session)
        logger.info(
            "Realized CMS saved for negotiation_id=%s cms=%s",
            session.negotiation_id,
            session.realized_cms,
        )
    except Exception as exc:
        logger.warning("Realized CMS computation failed (non-fatal): %s", exc)


async def _sync_deal_status(session: NegotiationSession) -> None:
    """Advance the parent deal when the negotiation reaches a terminal outcome.

    This is the hand-off to the rest of the pipeline:
      AGREED → deal ``NEGOTIATION_AGREED``  (unblocks DealMaker PI issuance)
      FAILED → deal ``NEGOTIATION_FAILED``  (routes to Exception Resolver / re-match)

    The deal-service transition emits the corresponding domain event, so this is
    the single point where the negotiation outcome propagates to other services.
    Best-effort and idempotent: if the deal is already at the target status, or the
    transition isn't currently valid, it is logged and swallowed — a failed hand-off
    never crashes the round (it surfaces as the deal simply staying in NEGOTIATING).
    """
    target = {"AGREED": "NEGOTIATION_AGREED", "FAILED": "NEGOTIATION_FAILED"}.get(session.status)
    if target is None:
        return
    try:
        from services.deals.service import get_deal, update_status  # type: ignore[import]
        from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
        from shared.domain.enums import DealStatus

        deal = await get_deal(session.deal_id)
        current = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
        if current == target:
            return  # already advanced — idempotent no-op

        reason = (
            f"Negotiation {session.negotiation_id} agreed at {session.agreed_price}"
            if session.status == "AGREED"
            else f"Negotiation {session.negotiation_id} failed after {session.round_no} round(s)"
        )
        await update_status(
            session.deal_id,
            UpdateStatusRequest(target_status=DealStatus(target), reason=reason),
        )
        logger.info(
            "Deal %s → %s (from negotiation %s)",
            session.deal_id, target, session.negotiation_id,
        )
    except Exception as exc:
        logger.warning(
            "Could not sync deal %s to %s from negotiation %s: %s",
            session.deal_id, target, session.negotiation_id, exc,
        )


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
    rag_context, market_context, usd_inr_rate = await asyncio.gather(
        load_rag_context(payload.product_code),
        fetch_both(payload.product_code, payload.buyer_country_code),
        get_usd_inr_rate(),
    )

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
        buyer_email=payload.buyer_email,
        seller_email=payload.seller_email,
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
        "buyer_history": "",
        "supplier_history": "",
        "buyer_market_context": market_context["buyer"],
        "supplier_market_context": market_context["supplier"],
        "usd_inr_rate": usd_inr_rate,
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

    session.status = _graph_state_to_status(
        final_state.get("routing", "HITL_APPROVAL"),
        final_state.get("hitl_required", False),
    )
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

    history, market_context, usd_inr_rate = await asyncio.gather(
        _build_conversation_history(negotiation_id),
        fetch_both(session.product_code, session.buyer_country_code),
        get_usd_inr_rate(),
    )

    state: NegotiationGraphState = {
        "session": session,
        "logistics_cost": 0.0,
        "is_first_round": False,
        "party": request.party,
        "inbound_message": request.inbound_message,
        "inbound_price": effective_price,
        "operator_override_price": request.operator_override_price,
        "buyer_history": history["buyer"],
        "supplier_history": history["supplier"],
        "buyer_market_context": market_context["buyer"],
        "supplier_market_context": market_context["supplier"],
        "usd_inr_rate": usd_inr_rate,
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

    session.status = _graph_state_to_status(
        final_state.get("routing", "HITL_APPROVAL"),
        final_state.get("hitl_required", False),
    )
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


# ---------------------------------------------------------------------------
# Email history helpers — called by HITL callbacks and router
# ---------------------------------------------------------------------------

async def send_approved_email(email_id: str, modified_body: str | None = None) -> None:
    """Send an operator-approved email draft.

    Called by the HITL SEND_NEGOTIATION_EMAIL callback on APPROVE.
    If *modified_body* is provided the operator has edited the draft; the
    original body is preserved in ``EmailHistoryRecord.original_body``.
    """
    repo = get_email_repository()
    record = await repo.get(email_id)
    if not record:
        logger.error("send_approved_email: email %s not found", email_id)
        return

    body_to_send = modified_body or record.body

    try:
        # SMTP transport is a separate service (the Email Gateway; single
        # responsibility). In the monolith this is an in-process call; in
        # microservice mode swap for EmailGatewayClient().send_email(...) over HTTP.
        from services.email_gateway.sender import send_email  # type: ignore[import]
        from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]

        await send_email(SendEmailRequest(
            to_address=record.to_address,
            subject=record.subject,
            body=body_to_send,
            negotiation_id=record.negotiation_id,
            round_no=record.round_no,
            email_id=record.email_id,
        ))
        now = datetime.now(timezone.utc)
        new_status = EmailStatus.MODIFIED_SENT if modified_body else EmailStatus.SENT
        updates: dict = {"status": new_status, "sent_at": now, "updated_at": now}
        if modified_body:
            updates["original_body"] = record.body
            updates["body"] = modified_body
        await repo.save(record.model_copy(update=updates))
        logger.info("Email %s dispatched (status=%s)", email_id, new_status)
    except Exception as exc:
        logger.error("Email %s send failed: %s", email_id, exc)
        await repo.save(record.model_copy(update={
            "status": EmailStatus.FAILED,
            "updated_at": datetime.now(timezone.utc),
        }))


async def reject_email(email_id: str) -> None:
    """Mark an email draft as rejected (operator declined to send it).

    Called by the HITL SEND_NEGOTIATION_EMAIL callback on REJECT.
    """
    repo = get_email_repository()
    record = await repo.get(email_id)
    if not record:
        logger.error("reject_email: email %s not found", email_id)
        return

    await repo.save(record.model_copy(update={
        "status": EmailStatus.REJECTED,
        "updated_at": datetime.now(timezone.utc),
    }))
    logger.info("Email %s rejected by operator", email_id)


async def finalize_agreement(
    negotiation_id: str, agreed_price: float | None = None
) -> None:
    """Operator confirmed a detected agreement — close the negotiation for real.

    Called by the HITL CONFIRM_NEGOTIATION_AGREEMENT callback on APPROVE. This
    is the single place a negotiation transitions to AGREED: it computes the
    realized CMS and advances the parent deal to NEGOTIATION_AGREED. Idempotent.
    """
    repo = get_repository()
    session = await repo.get_session(negotiation_id)
    if session is None:
        logger.error("finalize_agreement: negotiation %s not found", negotiation_id)
        return
    if session.status == "AGREED":
        return  # already finalized — idempotent no-op

    session.status = "AGREED"
    if agreed_price is not None:
        session.agreed_price = agreed_price
    session.hitl_required = False
    session.updated_at = datetime.now(timezone.utc)
    await repo.save_session(session)

    if session.agreed_price:
        await _compute_realized_cms(session, repo)
    await _sync_deal_status(session)
    await _provision_counterparty_orgs(session)

    await get_publisher().publish(DomainEvent(
        topic=EventTopic.NEGOTIATION_AGREED,
        deal_id=session.deal_id,
        payload={
            "negotiation_id": session.negotiation_id,
            "agreed_price": session.agreed_price,
            "round_no": session.round_no,
            "realized_cms": session.realized_cms,
        },
    ))
    logger.info(
        "Negotiation %s finalized as AGREED at %s (operator-confirmed)",
        negotiation_id, session.agreed_price,
    )


async def _provision_counterparty_orgs(session: NegotiationSession) -> None:
    """Auto-provision buyer + seller organizations once the deal is agreed.

    Finds-or-creates an Organization for each side (named after the deal's
    party legal names, falling back to the session's buyer/seller ids),
    registers the negotiation contact email on it, and links both org ids
    onto the deal — from then on, users assigned to those organizations see
    this deal in their own portal (deals list/get scope matches owner org OR
    buyer org OR seller org). Best-effort: a failure here never rolls back
    the agreement."""
    try:
        from services.auth.service import find_or_create_organization_for_deal  # type: ignore[import]
        from services.deals.service import get_deal, set_counterparty_orgs  # type: ignore[import]

        buyer_name = session.buyer_id
        seller_name = session.seller_id
        try:
            deal = await get_deal(session.deal_id)
            for p in getattr(deal, "parties", None) or []:
                raw_role = getattr(p, "role", "")
                role = (raw_role.value if hasattr(raw_role, "value") else str(raw_role)).lower()
                if role == "buyer" and p.legal_name:
                    buyer_name = p.legal_name
                elif role == "supplier" and p.legal_name:
                    seller_name = p.legal_name
        except Exception:  # noqa: BLE001 — party names are a nice-to-have
            pass

        buyer_org = await find_or_create_organization_for_deal(
            buyer_name, session.buyer_email
        )
        seller_org = await find_or_create_organization_for_deal(
            seller_name, session.seller_email
        )
        await set_counterparty_orgs(
            session.deal_id,
            buyer_org_id=buyer_org.org_id,
            seller_org_id=seller_org.org_id,
        )
        logger.info(
            "Negotiation %s: counterparty orgs provisioned (buyer=%s %r, seller=%s %r)",
            session.negotiation_id, buyer_org.org_id, buyer_org.name,
            seller_org.org_id, seller_org.name,
        )
    except Exception as exc:
        logger.error(
            "Negotiation %s: counterparty org provisioning failed (deal %s): %s",
            session.negotiation_id, session.deal_id, exc,
        )


async def reject_agreement(negotiation_id: str) -> None:
    """Operator declined a detected agreement — reopen the negotiation.

    Called by the HITL CONFIRM_NEGOTIATION_AGREEMENT callback on REJECT (soft
    reject). The session returns to ROUND with the provisional agreed_price
    cleared, so the negotiation simply continues on the next inbound reply.
    """
    repo = get_repository()
    session = await repo.get_session(negotiation_id)
    if session is None:
        logger.error("reject_agreement: negotiation %s not found", negotiation_id)
        return
    if session.status == "AGREED":
        logger.warning(
            "reject_agreement: negotiation %s already finalized — ignoring", negotiation_id
        )
        return

    session.status = "ROUND"
    session.agreed_price = None
    session.hitl_required = False
    session.updated_at = datetime.now(timezone.utc)
    await repo.save_session(session)
    logger.info("Negotiation %s agreement declined by operator — reopened", negotiation_id)


async def list_email_history(negotiation_id: str) -> list[EmailHistoryRecord]:
    """Return all email records for the given negotiation, oldest first."""
    return await get_email_repository().list_by_negotiation(negotiation_id)


async def get_email_threads(
    deal_id: str | None = None,
) -> list[object]:
    """Return all negotiation sessions with their full email threads attached.

    Used by GET /negotiation-agent/email-threads to power the Negotiation
    Inbox page in the UI.  Sessions are sorted most-recently-updated first.
    Email queries run in parallel via asyncio.gather for performance.
    """
    from .schemas import NegotiationEmailThread  # local import avoids circulars

    sessions = await get_repository().list_sessions(deal_id=deal_id)
    if not sessions:
        return []

    email_lists = await asyncio.gather(
        *[get_email_repository().list_by_negotiation(s.negotiation_id) for s in sessions]
    )

    threads = []
    for session, emails in zip(sessions, email_lists):
        sorted_emails = sorted(emails, key=lambda e: e.created_at)
        pending = sum(
            1 for e in sorted_emails
            if e.status == EmailStatus.PENDING_APPROVAL
        )
        thread_data = session.model_dump()
        thread_data["emails"] = [e.model_dump() for e in sorted_emails]
        thread_data["pending_approval_count"] = pending
        threads.append(NegotiationEmailThread(**thread_data))

    threads.sort(key=lambda t: t.updated_at, reverse=True)
    return threads


async def record_inbound_email(
    negotiation_id: str,
    deal_id: str,
    round_no: int,
    party: str,
    from_address: str,
    subject: str,
    body: str,
) -> EmailHistoryRecord:
    """Persist an inbound email received from a buyer or supplier.

    Called by InboxMonitor when it parses a reply.  The record is terminal
    (status=RECEIVED) — no HITL gate is created for inbound messages.
    """
    now = datetime.now(timezone.utc)
    email_id = f"EMAIL-{uuid.uuid4().hex[:10].upper()}"
    record = EmailHistoryRecord(
        email_id=email_id,
        negotiation_id=negotiation_id,
        deal_id=deal_id,
        round_no=round_no,
        direction=EmailDirection.INBOUND,
        party=party,
        to_address=_SMTP_FROM,
        from_address=from_address,
        subject=subject,
        body=body,
        status=EmailStatus.RECEIVED,
        sent_at=now,
        created_at=now,
        updated_at=now,
    )
    await get_email_repository().save(record)
    logger.info("Inbound email %s recorded (party=%s round=%d)", email_id, party, round_no)
    return record
