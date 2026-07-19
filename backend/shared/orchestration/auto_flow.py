"""Auto-continuation orchestrator — drives an opportunity-seeded deal through
the real pipeline after "Start Deal", using the actual service-layer
functions (no HTTP, no mocking of business logic — only trade-min's
opportunity data is ever mocked, via TRADE_MIN_MOCK_MODE).

Enabled via AUTO_FLOW_ENABLED (shared/config.py); off by default.

Design
------
One long-lived background task per deal, started once from
``services.deals.service.create_deal`` when a deal reaches MATCHING via an
opportunity. It calls each step's real service function directly (in-process
import, matching the codebase's existing lazy-import convention) and reads
whatever IDs it needs straight off that call's response, since several steps
(PI -> escrow, QC/freight -> their webhooks) need an id/ext_ref that isn't
cheaply rediscoverable from deal_id alone afterward.

Two pre-existing pipeline gaps (present before this file existed — see
scripts/e2e_mock_flow.py's module docstring) are worked around the same way
an operator would in the UI, and are called out inline below:
  1. buyer_target_price_usd is never populated by opportunity->deal seeding,
     because none of the currently-active trade-min connectors (Comtrade,
     Census, Alibaba RFQ, GDELT, EU TED) populate BuyerSignal.unit_price_usd
     — only the retired Volza/ImportGenius connectors did.
  2. Nothing drives PI_ACCEPTED -> ESCROW_PENDING.

Negotiation
-----------
Only ``start_negotiation`` is called directly — it seeds the session with
real buyer/seller contact addresses (see ``_MOCK_BUYER_EMAIL`` /
``_MOCK_SELLER_EMAIL``) and drafts round 0. If that doesn't converge
immediately, this module does **not** self-drive further rounds with
synthetic counter-offers; it polls for the session to reach a terminal state
while the real back-and-forth happens over actual email (IMAP poll ->
email_processor -> run_round, using the genuine inbound reply + full
conversation history) or an operator's manual "Run Round" in the
Negotiation Inbox.

HITL gates
----------
PO issuance, freight booking, and payout are never called directly — each
creates a HITL approval (at the deal's own autonomy_profile, usually L0) and
the flow polls for an operator to resolve it via the Approvals Inbox, same as
a real human-in-the-loop deployment.

Stub webhooks
-------------
Escrow-funded, QC-pass, and freight-cleared are normally reported by real
third-party webhooks (Stripe/SGS/Cogoport). Those connectors are stubbed (no
API keys configured), so nothing will ever call them — this module simulates
each a few seconds after its booking call, using the payload shapes already
proven working in scripts/e2e_mock_flow.py.

Caveat: this is an in-memory asyncio task, not a durable job — a server
restart (including a --reload pickup of an unrelated code change) silently
kills any in-flight run.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# Real inboxes for round-trip email testing of mock-opportunity negotiations —
# the PortLess side sends from/polls GMAIL_SENDER_ADDRESS (see .env); these
# are the counterparty addresses so replies can be tested for real instead of
# going to unreachable f"{id}@portless.placeholder" addresses.
_MOCK_SELLER_EMAIL = "sundarapandiank22@gmail.com"
_MOCK_BUYER_EMAIL = "karpoorapaulpandi@gmail.com"

_QUANTITY_MT = 25.0
_WEBHOOK_SIM_DELAY_SECONDS = 3.0
_APPROVAL_POLL_INTERVAL_SECONDS = 10.0
_APPROVAL_POLL_TIMEOUT_SECONDS = 24 * 3600
_PAYOUT_LOOKUP_TIMEOUT_SECONDS = 60.0


def enabled() -> bool:
    from shared.config import settings
    return settings.auto_flow_enabled


def start(deal_id: str) -> None:
    """Fire-and-forget entry point — schedules the background task."""
    if not enabled():
        return
    asyncio.create_task(_run_safely(deal_id))


_RESUMABLE_STATUSES = (
    "MATCHING", "NEGOTIATING", "NEGOTIATION_AGREED", "PI_SENT", "PI_ACCEPTED",
    "ESCROW_PENDING", "ESCROW_FUNDED", "PO_ISSUED", "SUPPLIER_DISPATCH_READY",
    "QC_PASSED", "IN_TRANSIT", "CLEARED",
)
"""Every non-terminal status _run() can leave a deal parked at if its bare
asyncio task dies mid-flight (server restart). PAID/CANCELLED/REFUNDED/
NEGOTIATION_FAILED/QC_FAILED are excluded — those are either terminal or
already routed to the Exception Resolver, not "in flight"."""


async def resume_in_flight() -> None:
    """Re-arm the auto-flow task for every opportunity-seeded deal still
    sitting mid-pipeline at process startup.

    This module's task is a bare asyncio task (see module docstring) — it
    dies with the process. Without this, a restart mid-flow silently orphans
    the deal at whatever status it last reached, even though every other
    service keeps working fine via Mongo + the event bus. Called once from
    app.py's lifespan, after the deals and negotiation-agent repositories are
    initialised.

    Covers every non-terminal status _run() passes through (see
    _RESUMABLE_STATUSES), not just MATCHING/NEGOTIATING — _run() itself
    resumes from the deal's current status (see its own docstring) rather
    than always replaying from the top, so re-arming a deal already past
    negotiation is safe.
    """
    if not enabled():
        return
    from services.deals.repository import get_repository

    repo = get_repository()
    resumed = 0
    for status_str in _RESUMABLE_STATUSES:
        try:
            from shared.domain.enums import DealStatus
            deals = await repo.list_active(limit=500, status=DealStatus(status_str))
        except Exception:
            logger.exception("AutoFlow: resume scan failed for status=%s", status_str)
            continue
        for deal in deals:
            if not deal.opportunity_id:
                continue  # not an auto-flow deal — matches _run()'s own precondition
            start(deal.deal_id)
            resumed += 1
    if resumed:
        logger.info("AutoFlow: resumed %d in-flight deal(s) after restart", resumed)


async def _run_safely(deal_id: str) -> None:
    try:
        await _run(deal_id)
    except Exception:
        logger.exception("AutoFlow: deal %s pipeline aborted with an unhandled error", deal_id)


_STAGE_ORDER = (
    "MATCHING", "NEGOTIATING", "NEGOTIATION_AGREED", "PI_SENT", "PI_ACCEPTED",
    "ESCROW_PENDING", "ESCROW_FUNDED", "PO_ISSUED", "SUPPLIER_DISPATCH_READY",
    "QC_PASSED", "IN_TRANSIT", "CLEARED", "PAID",
)
"""Linear pipeline order _run() drives a deal through. Used only to compare
"has this deal already reached status X" when resuming — not a replacement
for shared.domain.state_machine, which remains the authority on which
transitions are actually legal."""


def _stage_index(status_value: str) -> int:
    """Position of *status_value* in _STAGE_ORDER, or -1 if it precedes
    MATCHING (e.g. DRAFT) — nothing to resume from there."""
    try:
        return _STAGE_ORDER.index(status_value)
    except ValueError:
        return -1


async def _resolve_agreed_price(deal_id: str) -> float | None:
    """The agreed unit price for a deal already past NEGOTIATION_AGREED,
    read back from the PI record rather than re-running negotiation."""
    from services.dealmaker.repository import get_repository as pi_repo
    pi = await pi_repo().get_pi_by_deal(deal_id)
    return pi.agreed_price if pi is not None else None


async def _run(deal_id: str) -> None:
    """Drive deal_id through the pipeline, resuming from wherever it currently
    sits rather than always replaying from the top.

    Needed because this is a bare asyncio task (see module docstring): a
    server restart kills it mid-flight, and resume_in_flight() re-arms it for
    every non-terminal status the deal could be parked at — not just
    MATCHING/NEGOTIATING. Blindly replaying _submit_quote (mints a fresh RFQ)
    or _create_gate (creates a HITL approval) on a deal already past those
    steps would create real duplicate side effects, so each block below only
    runs when the deal's current stage is at or before the step it performs —
    _stage_index comparisons, not a second state machine.
    """
    from services.deals.service import get_deal
    from shared.domain.enums import DealStatus

    deal = await get_deal(deal_id)
    if not deal.opportunity_id:
        return
    stage = _stage_index(deal.status.value)
    if stage < 0:
        logger.info(
            "AutoFlow: deal %s at %s is not a resumable status — stopping",
            deal_id, deal.status.value,
        )
        return

    def at_or_before(target: str) -> bool:
        return stage <= _STAGE_ORDER.index(target)

    if at_or_before("NEGOTIATING"):
        winner, buyer_price, supplier_price = await _ensure_match_results(deal)
        if winner is None:
            logger.info("AutoFlow: deal %s has no viable SOURCE candidate — stopping", deal_id)
            return
        if at_or_before("MATCHING"):
            quote = await _submit_quote(deal_id, deal.hs_code, winner, supplier_price)
            if quote is None:
                return
            await _nudge_to_negotiating(deal_id)

        agreed_price = await _run_negotiation(deal, winner, buyer_price, supplier_price)
        if agreed_price is None:
            logger.info("AutoFlow: deal %s negotiation did not converge — stopping", deal_id)
            return
    else:
        # Resuming past negotiation — re-derive winner/agreed_price from
        # durable state instead of re-running sourcing/negotiation, both of
        # which would create fresh RFQs/quotes/sessions on every restart.
        winner, _, supplier_price = await _ensure_match_results(deal)
        if winner is None:
            logger.error(
                "AutoFlow: deal %s is past negotiation but has no stored match "
                "result — cannot resume", deal_id,
            )
            return
        agreed_price = await _resolve_agreed_price(deal_id)
        if agreed_price is None:
            logger.error(
                "AutoFlow: deal %s is at %s but has no PI on record — cannot resume",
                deal_id, deal.status.value,
            )
            return

    if at_or_before("NEGOTIATION_AGREED"):
        pi = await _issue_and_accept_pi(deal_id, deal.market, agreed_price)
        if pi is None:
            return

    if at_or_before("PI_ACCEPTED"):
        hold = await _fund_escrow(deal_id, agreed_price)
        if hold is None:
            return

    if at_or_before("ESCROW_FUNDED"):
        await _create_gate(
            deal_id, "ISSUE_PO",
            summary=f"Auto-flow: issue PO for {deal.hs_code} at ${supplier_price}/unit",
            financial_impact_usd=round(supplier_price * _QUANTITY_MT, 2),
            autonomy_level=_autonomy(deal),
            callback_payload={
                "deal_id": deal_id, "agreed_price": supplier_price, "quantity_mt": _QUANTITY_MT,
                "incoterm": "FOB", "port_of_loading": "Chennai, IN",
                "delivery_deadline": (date.today() + timedelta(days=21)).isoformat(),
                "payment_terms": "30% advance + 70% on BL",
            },
            idempotency_key=f"auto-po-gate-{deal_id}",
        )
        if not await _wait_for_status(deal_id, DealStatus.PO_ISSUED):
            return

    if at_or_before("SUPPLIER_DISPATCH_READY"):
        # Includes SUPPLIER_DISPATCH_READY itself, not just PO_ISSUED:
        # book_qc() advances the deal to SUPPLIER_DISPATCH_READY synchronously,
        # before the simulated PASS webhook fires — a restart during that gap
        # leaves the deal parked there with qc_result still unset. Both
        # _book_and_simulate_qc (result-aware) and _generate_documents
        # (idempotency_key-aware) are safe to re-enter from either status.
        if await _book_and_simulate_qc(deal_id) is None:
            return
        await _generate_documents(deal_id)

        await _create_gate(
            deal_id, "BOOK_FREIGHT",
            summary=f"Auto-flow: book freight for deal {deal_id}",
            financial_impact_usd=None,
            autonomy_level=_autonomy(deal),
            callback_payload={"deal_id": deal_id, "provider": "cogoport", "incoterm": "FOB"},
            idempotency_key=f"auto-freight-gate-{deal_id}",
        )
        if not await _wait_for_status(deal_id, DealStatus.IN_TRANSIT):
            return

    if at_or_before("IN_TRANSIT"):
        await _file_customs(deal_id)
        if not await _simulate_freight_cleared(deal_id):
            return
        if not await _wait_for_status(deal_id, DealStatus.CLEARED):
            return

    if at_or_before("CLEARED"):
        await _create_gate(
            deal_id, "RELEASE_SUPPLIER_PAYOUT",
            summary=f"Auto-flow: release supplier payout for deal {deal_id}",
            financial_impact_usd=round(supplier_price * _QUANTITY_MT, 2),
            autonomy_level=_autonomy(deal),
            callback_payload={
                "deal_id": deal_id, "payee_id": f"SUPPLIER-{winner.supplier_id}",
                "amount": round(supplier_price * _QUANTITY_MT, 2),
                "idempotency_key": f"auto-payout-{deal_id}",
            },
            idempotency_key=f"auto-payout-gate-{deal_id}",
        )
        if not await _simulate_payout_completed(deal_id):
            return
        if await _wait_for_status(deal_id, DealStatus.PAID):
            logger.info("AutoFlow: deal %s reached PAID", deal_id)


def _autonomy(deal) -> str:
    v = deal.autonomy_profile
    return getattr(v, "value", v)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

async def _ensure_match_results(deal):
    winner = next((m for m in deal.match_results if m.decision == "SOURCE"), None)
    if winner is not None and deal.buyer_target_price_usd is not None:
        return winner, deal.buyer_target_price_usd, winner.fob_price_usd

    # Gap #1 (see module docstring) — derive a buyer price from the cheapest
    # seller's FOB price and complete the scoring create_deal had to skip.
    from services.deals.schemas import MatchResult, UpdateFinancialsRequest
    from services.deals.service import update_financials
    from services.match_engine.schemas import ScoreRequest, SupplierVerification
    from services.match_engine.service import score_match
    from services.trade_min.service import get_match_seed

    try:
        seed = await get_match_seed(deal.opportunity_id)
    except Exception:
        logger.exception("AutoFlow: deal %s match-seed lookup failed", deal.deal_id)
        return None, None, None
    if seed is None or not seed.top_sellers:
        return None, None, None

    sellers = [s for s in seed.top_sellers if s.get("fob_price_usd")]
    if not sellers:
        return None, None, None
    cheapest = min(sellers, key=lambda s: s["fob_price_usd"])
    buyer_price = round(cheapest["fob_price_usd"] * 1.25, 2)

    best: tuple[dict, "MatchResult"] | None = None
    for s in sellers[:3]:
        try:
            resp = await score_match(ScoreRequest(
                deal_id=deal.deal_id, supplier_id=s["supplier_id"],
                buyer_target_price=buyer_price, supplier_quote_price=float(s["fob_price_usd"]),
                currency="USD",
                verification=SupplierVerification(gst_verified=True, iec_active=True, dgft_denied=False),
                supplier_to_port_km=float(s.get("supplier_to_port_km") or 300.0),
                business_age_years=0.0, udyam_registered=False, annual_shipment_count=0,
            ))
        except Exception:
            continue
        # Accept the best-scoring candidate even at decision=DISCARD: the
        # reliability component defaults to 0 for any mocked/thin seller
        # (no business-age/Udyam/shipment history), which alone caps the
        # composite below the 75 SOURCE threshold regardless of price — the
        # same conservative-default issue _nudge_to_negotiating already
        # works around for the next transition, applied here consistently.
        if best is not None and resp.composite_score <= best[1].composite_score:
            continue
        mr = MatchResult(
            match_id=resp.match_id or f"MATCH-{uuid.uuid4().hex[:12].upper()}",
            supplier_id=resp.supplier_id, company_name=s.get("company_name", ""),
            composite_score=resp.composite_score, gatekeeper_passed=resp.gatekeeper_passed,
            arbitrage_score=resp.arbitrage_score, proximity_score=resp.proximity_score,
            reliability_score=resp.reliability_score, decision=resp.decision,
            fob_price_usd=float(s["fob_price_usd"]), supplier_to_port_km=s.get("supplier_to_port_km"),
        )
        best = (s, mr)

    if best is None:
        return None, None, None

    _, winner = best
    await update_financials(deal.deal_id, UpdateFinancialsRequest(
        buyer_target_price_usd=buyer_price,
        winning_supplier_id=winner.supplier_id,
        winning_cms=winner.composite_score,
    ))
    return winner, buyer_price, winner.fob_price_usd


async def _submit_quote(deal_id: str, hs_code: str, winner, supplier_price: float):
    from services.sourcing.schemas import QuoteIntakeRequest, RfqRequest, SupplierRef
    from services.sourcing.service import dispatch_rfq, receive_quote
    try:
        rfq = await dispatch_rfq(RfqRequest(
            deal_id=deal_id, hs_code=hs_code,
            suppliers=[SupplierRef(supplier_id=winner.supplier_id, company_name=winner.company_name)],
        ))
        return await receive_quote(QuoteIntakeRequest(
            rfq_id=rfq.rfq_id, deal_id=deal_id, supplier_id=winner.supplier_id,
            company_name=winner.company_name, unit_price_usd=supplier_price,
            quantity_mt=_QUANTITY_MT, incoterm="FOB",
        ))
    except Exception:
        logger.exception("AutoFlow: deal %s quote submission failed", deal_id)
        return None


async def _nudge_to_negotiating(deal_id: str) -> None:
    from services.deals.schemas import UpdateStatusRequest
    from services.deals.service import get_deal, update_status
    from shared.domain.enums import DealStatus

    deal = await get_deal(deal_id)
    if deal.status != DealStatus.MATCHING:
        return
    # Gap consequence: re-scored CMS often lands below the 75 SOURCE threshold
    # for thin/mocked opportunities — nudge forward the way an operator
    # confirming a supplier quote in the UI would.
    try:
        await update_status(deal_id, UpdateStatusRequest(
            target_status=DealStatus.NEGOTIATING,
            reason="AutoFlow: operator-equivalent confirmation of supplier quote",
        ))
    except Exception:
        logger.exception("AutoFlow: deal %s could not advance to NEGOTIATING", deal_id)


async def _run_negotiation(deal, winner, buyer_price: float, supplier_price: float) -> float | None:
    from agents.negotiation_agent.schemas import NegotiationPolicy, StartNegotiationRequest
    from agents.negotiation_agent.service import start_negotiation

    gap = abs(buyer_price - supplier_price)
    policy = NegotiationPolicy(concession_step=max(round(gap * 0.08, 2), 1.0), max_rounds=8)
    try:
        session = await start_negotiation(StartNegotiationRequest(
            deal_id=deal.deal_id, trade_match_id=winner.match_id,
            buyer_id=f"BUYER-{deal.market}-AUTO", seller_id=winner.supplier_id,
            product_code=deal.hs_code, buyer_country_code=deal.market,
            buyer_target_price=buyer_price, supplier_quote_price=supplier_price,
            buyer_email=_MOCK_BUYER_EMAIL, seller_email=_MOCK_SELLER_EMAIL,
            policy=policy,
            # Quantity is known to the auto-flow; the QC inspector is
            # deliberately NOT pre-selected — the first buyer email must list
            # all supported agencies and ask the buyer to opt (their reply
            # satisfies the agreement checklist; until then the agent asks).
            quantity_mt=_QUANTITY_MT,
        ))
    except Exception:
        logger.exception("AutoFlow: deal %s negotiation could not start", deal.deal_id)
        return None

    if session.status == "AGREED":
        return session.agreed_price
    if session.status in ("FAILED", "ESCALATED"):
        return None

    # Auto-flow does not drive further rounds itself — real replies now come
    # from the actual counterparty inboxes (IMAP poll -> email_processor ->
    # run_round, using the real inbound message + full history) or from an
    # operator clicking "Run Round" in the Negotiation Inbox. Just wait for
    # the session to reach a terminal state.
    return await _wait_for_negotiation_result(session.negotiation_id)


async def _wait_for_negotiation_result(negotiation_id: str) -> float | None:
    from agents.negotiation_agent.service import get_negotiation

    elapsed = 0.0
    while elapsed < _APPROVAL_POLL_TIMEOUT_SECONDS:
        try:
            session = await get_negotiation(negotiation_id)
        except Exception:
            logger.exception("AutoFlow: could not poll negotiation %s", negotiation_id)
            return None
        if session.status == "AGREED":
            return session.agreed_price
        if session.status in ("FAILED", "ESCALATED"):
            return None
        await asyncio.sleep(_APPROVAL_POLL_INTERVAL_SECONDS)
        elapsed += _APPROVAL_POLL_INTERVAL_SECONDS
    logger.warning(
        "AutoFlow: negotiation %s timed out waiting for a real reply (no email/operator "
        "action within 24h)", negotiation_id,
    )
    return None


async def _issue_and_accept_pi(deal_id: str, market: str, agreed_price: float):
    from services.dealmaker.schemas import AcceptPiRequest, IssuePiRequest
    from services.dealmaker.service import accept_pi, issue_pi
    try:
        pi = await issue_pi(IssuePiRequest(
            deal_id=deal_id, agreed_price=agreed_price, quantity_mt=_QUANTITY_MT,
            port_of_destination=f"Destination Port, {market}",
            payment_terms="30% advance + 70% on BL",
        ))
        return await accept_pi(pi.pi_id, AcceptPiRequest(accepted_by="buyer@auto-flow.local"))
    except Exception:
        logger.exception("AutoFlow: deal %s PI issue/accept failed", deal_id)
        return None


async def _fund_escrow(deal_id: str, agreed_price: float):
    from services.deals.schemas import UpdateStatusRequest
    from services.deals.service import get_deal, update_status
    from services.escrow_guard.schemas import EscrowFundedWebhookPayload, EscrowHoldRequest
    from services.escrow_guard.service import create_hold, get_deal_summary, handle_escrow_funded_webhook
    from shared.domain.enums import DealStatus
    try:
        # Resuming after a restart: a hold from the prior run may already
        # exist (and may already be FUNDED) — reuse it instead of creating a
        # second one. idempotency_key is deal-scoped and deterministic (not
        # uuid4()) for the same reason: create_hold's own idempotency check
        # can only dedupe a retry if the key repeats across resumes.
        summary = await get_deal_summary(deal_id)
        hold = summary.hold
        if hold is None:
            deal = await get_deal(deal_id)
            if deal.status not in (DealStatus.ESCROW_PENDING, DealStatus.ESCROW_FUNDED):
                # Gap #2 (see module docstring) — nothing drives this transition.
                await update_status(deal_id, UpdateStatusRequest(
                    target_status=DealStatus.ESCROW_PENDING,
                    reason="AutoFlow: buyer-equivalent escrow initiation",
                ))
            hold = await create_hold(EscrowHoldRequest(
                deal_id=deal_id, amount=round(agreed_price * _QUANTITY_MT, 2),
                idempotency_key=f"auto-escrow-{deal_id}",
            ))
        if hold.status != "FUNDED":
            await asyncio.sleep(_WEBHOOK_SIM_DELAY_SECONDS)
            await handle_escrow_funded_webhook(
                EscrowFundedWebhookPayload(ext_ref=hold.ext_ref, status="FUNDED"),
                raw_headers={}, raw_body=b"{}",
            )
        return hold
    except Exception:
        logger.exception("AutoFlow: deal %s escrow funding failed", deal_id)
        return None


async def _book_and_simulate_qc(deal_id: str):
    """Book (or find) the QC inspection and ensure the simulated PASS webhook
    has actually fired.

    Resuming after a restart can land here two different ways: no QC order
    yet (book fresh), or an order that was already booked but whose simulated
    webhook never fired because the process died during the
    _WEBHOOK_SIM_DELAY_SECONDS sleep — book_qc() itself already advances the
    deal to SUPPLIER_DISPATCH_READY synchronously, before the webhook, so
    that half-completed state is real and must be finished here rather than
    left to stall forever. qc_result is None exactly in that gap; re-calling
    handle_qc_result on an order that already has a result is a harmless
    idempotent no-op (see handle_qc_result's own transition-failure handling).
    """
    from services.shield_link.schemas import QcBookingRequest, QcWebhookPayload
    from services.shield_link.service import book_qc, handle_qc_result, list_deal_orders
    try:
        # book_qc has no idempotency_key, so re-calling it unconditionally on
        # resume would book (and bill) a second inspection — only book if no
        # QC order exists for this deal yet.
        existing = await list_deal_orders(deal_id)
        tpo = next(
            (o for o in existing.orders if o.order_type.upper() == "QC" and o.ext_ref), None
        )
        if tpo is None:
            tpo = await book_qc(QcBookingRequest(deal_id=deal_id))
        if tpo.qc_result is None:
            await asyncio.sleep(_WEBHOOK_SIM_DELAY_SECONDS)
            await handle_qc_result(
                tpo.ext_ref, QcWebhookPayload(ext_ref=tpo.ext_ref, result="PASS"),
                raw_headers={}, raw_body=b"{}",
            )
        return tpo
    except Exception:
        logger.exception("AutoFlow: deal %s QC booking failed", deal_id)
        return None


async def _generate_documents(deal_id: str) -> None:
    from services.docugen.schemas import GenerateDocumentRequest
    from services.docugen.service import generate_document
    from shared.domain.enums import DocumentType

    for dt in (
        DocumentType.COMMERCIAL_INVOICE, DocumentType.PACKING_LIST,
        DocumentType.CERTIFICATE_OF_ORIGIN, DocumentType.SDF, DocumentType.SHIPPING_BILL,
    ):
        try:
            # Deterministic per (deal, doc_type) key: resuming after a restart
            # must return the already-generated document instead of rendering
            # (and re-uploading to Drive) a duplicate — see generate_document's
            # own idempotency_key check.
            await generate_document(GenerateDocumentRequest(
                deal_id=deal_id, doc_type=dt,
                idempotency_key=f"auto-doc-{deal_id}-{dt.value}",
            ))
        except Exception:
            logger.warning("AutoFlow: deal %s document %s generation failed", deal_id, dt.value)


async def _file_customs(deal_id: str) -> None:
    from services.docugen.schemas import CustomsFilingRequest
    from services.docugen.service import file_customs
    try:
        await file_customs(CustomsFilingRequest(deal_id=deal_id))
    except Exception:
        logger.warning("AutoFlow: deal %s customs filing failed", deal_id)


async def _simulate_freight_cleared(deal_id: str) -> bool:
    from services.shield_link.schemas import FreightMilestonePayload
    from services.shield_link.service import handle_freight_milestone, list_deal_orders
    try:
        orders = await list_deal_orders(deal_id)
        freight = next(
            (o for o in orders.orders if o.order_type.upper() == "FREIGHT" and o.ext_ref), None
        )
        if freight is None:
            logger.warning("AutoFlow: deal %s freight booking not found after approval", deal_id)
            return False
        await asyncio.sleep(_WEBHOOK_SIM_DELAY_SECONDS)
        await handle_freight_milestone(
            freight.ext_ref, FreightMilestonePayload(ext_ref=freight.ext_ref, milestone="CLEARED"),
            raw_headers={}, raw_body=b"{}",
        )
        return True
    except Exception:
        logger.exception("AutoFlow: deal %s freight-cleared simulation failed", deal_id)
        return False


async def _simulate_payout_completed(deal_id: str) -> bool:
    from services.escrow_guard.schemas import PayoutWebhookPayload
    from services.escrow_guard.service import get_deal_summary, handle_payout_webhook

    payout = None
    elapsed = 0.0
    while elapsed < _PAYOUT_LOOKUP_TIMEOUT_SECONDS:
        try:
            summary = await get_deal_summary(deal_id)
        except Exception:
            summary = None
        if summary and summary.payouts:
            payout = summary.payouts[-1]
            break
        await asyncio.sleep(2.0)
        elapsed += 2.0

    if payout is None:
        logger.warning("AutoFlow: deal %s payout not found after approval", deal_id)
        return False
    try:
        await asyncio.sleep(_WEBHOOK_SIM_DELAY_SECONDS)
        await handle_payout_webhook(
            PayoutWebhookPayload(ext_ref=payout.ext_ref, status="COMPLETED"),
            raw_headers={}, raw_body=b"{}",
        )
        return True
    except Exception:
        logger.exception("AutoFlow: deal %s payout-completed simulation failed", deal_id)
        return False


# ---------------------------------------------------------------------------
# HITL gate helper
# ---------------------------------------------------------------------------

async def _create_gate(
    deal_id: str, action: str, summary: str, financial_impact_usd: float | None,
    autonomy_level: str, callback_payload: dict, idempotency_key: str | None = None,
) -> None:
    """Create a HITL approval gate.

    idempotency_key should be a deterministic, deal+action-scoped string (not
    uuid4()) when the caller might be resuming after a restart — create_approval
    dedupes on it, so a re-triggered gate for a deal already past this step
    returns the existing approval instead of creating a visible duplicate in
    the Approvals Inbox.
    """
    from services.hitl_approval.schemas import CreateApprovalRequest
    from services.hitl_approval.service import create_approval
    try:
        await create_approval(CreateApprovalRequest(
            deal_id=deal_id, action=action, summary=summary,
            financial_impact_usd=financial_impact_usd,
            autonomy_level=autonomy_level, callback_payload=callback_payload,
            idempotency_key=idempotency_key,
        ))
    except Exception:
        logger.exception("AutoFlow: deal %s could not create %s approval", deal_id, action)


async def _wait_for_status(deal_id: str, target) -> bool:
    from services.deals.service import get_deal
    from shared.domain.enums import DealStatus

    stop_states = {
        DealStatus.CANCELLED, DealStatus.REFUNDED, DealStatus.DISPUTE, DealStatus.NEGOTIATION_FAILED,
    }
    elapsed = 0.0
    while elapsed < _APPROVAL_POLL_TIMEOUT_SECONDS:
        deal = await get_deal(deal_id)
        if deal.status == target:
            return True
        if deal.status in stop_states:
            logger.info("AutoFlow: deal %s diverted to %s while waiting for %s", deal_id, deal.status, target)
            return False
        await asyncio.sleep(_APPROVAL_POLL_INTERVAL_SECONDS)
        elapsed += _APPROVAL_POLL_INTERVAL_SECONDS
    logger.warning("AutoFlow: deal %s timed out waiting for %s (no operator action within 24h)", deal_id, target)
    return False
