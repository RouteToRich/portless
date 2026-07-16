"""Business logic for the Deals service (LLD 4.2).

Orchestration
-------------
``create_deal`` is the entry point for a new deal.  When ``opportunity_id``
is supplied the service performs a three-step orchestration entirely within
the same process (combined monolith) or across HTTP (microservice):

  1. Fetch ``MatchEngineSeed`` from Trade-Min
     → ``services.trade_min.service.get_match_seed()``
  2. Score all top-seller candidates via Match Engine (concurrent)
     → ``services.match_engine.service.score_match()``
  3. Pick the highest-scoring SOURCE candidate as the winning supplier
  4. Emit ``deal.created`` and mark the opportunity CONVERTED

``update_status`` enforces the deal state machine topology from
``shared.domain.state_machine``.  The deterministic business guards
(``margin_floor_ok``, ``dual_doc_match_ok``) are implemented there but are
applied by the caller at the point the financial / document inputs exist
(e.g. the margin floor at PO issuance in DealMaker), not inside this
topology-only transition check.

Exceptions
----------
``DealNotFoundError`` — 404 in the router
``InvalidTransitionError`` — 422 in the router
All inter-service calls are wrapped in try/except; failures are logged as
WARNING and the deal continues without that data (graceful degradation).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from shared.bus import get_publisher
from shared.domain.events import DomainEvent, EventTopic
from shared.domain.state_machine import can_transition

from shared.domain.models import Party

from .repository import get_repository, init_repository
from .schemas import (
    CreateDealRequest,
    DealRecord,
    DealStatus,
    MatchResult,
    StatusHistoryEntry,
    UpdateFinancialsRequest,
    UpdateStatusRequest,
)

logger = logging.getLogger(__name__)

# Applied when the caller doesn't specify one — matches NegotiationPolicy.
# margin_floor_pct and the DealMaker PO margin guard, so the Deal Board's
# "Margin Floor" card reflects the floor actually enforced downstream.
_DEFAULT_MARGIN_FLOOR_PCT = 8.0

# ---------------------------------------------------------------------------
# Status → event topic mapping (only statuses that produce domain events)
# ---------------------------------------------------------------------------

_STATUS_TO_EVENT: dict[DealStatus, EventTopic] = {
    DealStatus.NEGOTIATING: EventTopic.NEGOTIATION_STARTED,
    DealStatus.NEGOTIATION_AGREED: EventTopic.NEGOTIATION_AGREED,
    DealStatus.NEGOTIATION_FAILED: EventTopic.NEGOTIATION_FAILED,
    DealStatus.PI_SENT: EventTopic.PI_SENT,
    DealStatus.PI_ACCEPTED: EventTopic.PI_ACCEPTED,
    DealStatus.ESCROW_FUNDED: EventTopic.ESCROW_FUNDED,
    DealStatus.PO_ISSUED: EventTopic.PO_ISSUED,
    DealStatus.QC_PASSED: EventTopic.QC_PASSED,
    DealStatus.QC_FAILED: EventTopic.QC_FAILED,
    DealStatus.CLEARED: EventTopic.CUSTOMS_CLEARED,
    DealStatus.PAID: EventTopic.PAYOUT_COMPLETED,
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DealNotFoundError(Exception):
    def __init__(self, deal_id: str) -> None:
        super().__init__(f"Deal {deal_id!r} not found")
        self.deal_id = deal_id


class InvalidTransitionError(Exception):
    def __init__(self, current: DealStatus, target: DealStatus) -> None:
        super().__init__(
            f"Cannot transition deal from {current.value!r} to {target.value!r}"
        )
        self.current = current
        self.target = target


# ---------------------------------------------------------------------------
# Internal helpers — inter-service calls (direct function calls in monolith)
# ---------------------------------------------------------------------------

async def _fetch_match_seed(opportunity_id: str) -> dict[str, Any] | None:
    """Fetch pre-seeded Match Engine inputs from Trade-Min.

    Uses a direct in-process import so the combined monolith avoids the
    overhead of an HTTP round-trip to itself.  In a microservice deployment
    swap this for ``TradeMinClient().get_match_seed(opportunity_id)``.
    """
    try:
        from services.trade_min.service import get_match_seed  # type: ignore[import]
        seed = await get_match_seed(opportunity_id)
        return seed.model_dump() if seed is not None else None
    except Exception as exc:
        logger.warning(
            "Could not fetch match seed for opportunity %s: %s", opportunity_id, exc
        )
        return None


async def _mark_opportunity_converted(opportunity_id: str) -> None:
    """Mark the Trade-Min opportunity CONVERTED (best-effort, non-blocking)."""
    try:
        from services.trade_min.service import mark_opportunity_converted  # type: ignore[import]
        await mark_opportunity_converted(opportunity_id)
    except Exception as exc:
        logger.warning(
            "Could not mark opportunity %s as CONVERTED: %s", opportunity_id, exc
        )


async def _score_seller(
    deal_id: str,
    seller: dict[str, Any],
    buyer_target_price: float,
) -> MatchResult | None:
    """Score one seller candidate via Match Engine.

    Verification flags (gst_verified, iec_active, dgft_denied) are stubbed as
    passing because the real GSTN / DGFT checks run in the Sourcing phase.
    The CMS gatekeeper multiplier will be 1.0 (pass) until Sourcing resolves
    these values and calls ``update_financials`` with the corrected scores.
    """
    fob = seller.get("fob_price_usd")
    if fob is None:
        return None
    try:
        from services.match_engine.service import score_match  # type: ignore[import]
        from services.match_engine.schemas import ScoreRequest, SupplierVerification  # type: ignore[import]

        req = ScoreRequest(
            deal_id=deal_id,
            supplier_id=seller["supplier_id"],
            buyer_target_price=buyer_target_price,
            supplier_quote_price=float(fob),
            currency="USD",
            verification=SupplierVerification(
                gst_verified=True,
                iec_active=True,
                dgft_denied=False,
            ),
            supplier_to_port_km=float(seller.get("supplier_to_port_km") or 300.0),
            business_age_years=0.0,
            udyam_registered=False,
            annual_shipment_count=0,
        )
        resp = await score_match(req)
        return MatchResult(
            match_id=resp.match_id or f"MATCH-{uuid4().hex[:12].upper()}",
            supplier_id=resp.supplier_id,
            company_name=seller.get("company_name", ""),
            composite_score=resp.composite_score,
            gatekeeper_passed=resp.gatekeeper_passed,
            arbitrage_score=resp.arbitrage_score,
            proximity_score=resp.proximity_score,
            reliability_score=resp.reliability_score,
            decision=resp.decision,
            fob_price_usd=float(fob),
            supplier_to_port_km=seller.get("supplier_to_port_km"),
        )
    except Exception as exc:
        logger.warning(
            "Match Engine scoring failed for seller %s in deal %s: %s",
            seller.get("supplier_id"),
            deal_id,
            exc,
        )
        return None


async def _emit_event(
    topic: EventTopic,
    deal_id: str,
    payload: dict[str, Any],
) -> None:
    """Emit a domain event; swallows errors so the calling flow is never blocked."""
    try:
        await get_publisher().publish(
            DomainEvent(topic=topic, deal_id=deal_id, payload=payload)
        )
    except Exception as exc:
        logger.warning("Failed to emit %s for deal %s: %s", topic.value, deal_id, exc)


def _new_deal_id() -> str:
    return f"DEAL-{uuid4().hex[:12].upper()}"


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------

def _seed_parties_from_opportunity(
    opportunity: Any,
    winning_supplier_id: str | None,
    market: str,
) -> list[Party]:
    """Build initial buyer/supplier Party entries from the opportunity data.

    Deals created from an opportunity previously had an empty ``parties``
    list for their whole lifecycle, leaving the Overview tab blank. The top
    buyer signal and the winning (or cheapest) seller candidate are the best
    identities known at creation time; Sourcing verification can enrich them
    later."""
    parties: list[Party] = []
    try:
        signals = getattr(opportunity, "buyer_signals", None) or []
        if signals:
            top = signals[0]
            parties.append(Party(
                party_id=getattr(top, "company_id", None),
                role="buyer",
                legal_name=top.buyer_name,
                country=top.buyer_country or market,
                risk_score=getattr(top, "risk_score", None),
            ))
        candidates = getattr(opportunity, "seller_candidates", None) or []
        winner = next(
            (c for c in candidates if c.candidate_id == winning_supplier_id),
            candidates[0] if candidates else None,
        )
        if winner is not None:
            parties.append(Party(
                party_id=winner.candidate_id,
                role="supplier",
                legal_name=winner.company_name,
                country="IN",
            ))
    except Exception as exc:  # noqa: BLE001 — party seeding must never block creation
        logger.warning("Could not seed parties from opportunity: %s", exc)
    return parties


async def _fetch_opportunity(opportunity_id: str) -> Any | None:
    """Best-effort fetch of the full EnrichedOpportunity (for party seeding)."""
    try:
        from services.trade_min.service import get_opportunity  # type: ignore[import]
        return await get_opportunity(opportunity_id)
    except Exception as exc:
        logger.warning("Could not fetch opportunity %s: %s", opportunity_id, exc)
        return None


async def create_deal(payload: CreateDealRequest, org_id: str) -> DealRecord:
    """Create a new deal, optionally orchestrating Match Engine scoring.

    ``org_id`` ties the deal to the caller's organization (stamped by the
    router from the authenticated user, never client-supplied).

    When ``opportunity_id`` is provided:
      • Fetches the Trade-Min MatchEngineSeed
      • Scores all top-seller candidates via Match Engine (max 3, concurrent)
      • Sets status = MATCHING if ≥1 SOURCE decision, otherwise DRAFT
      • Marks the opportunity CONVERTED after emitting deal.created

    Always:
      • Persists the deal to MongoDB (or in-memory fallback)
      • Emits ``deal.created``
    """
    now = datetime.now(timezone.utc)
    deal_id = _new_deal_id()

    match_results: list[MatchResult] = []
    winning_supplier_id: str | None = None
    winning_cms: float | None = None
    buyer_target_price: float | None = None
    initial_status = DealStatus.DRAFT

    if payload.opportunity_id:
        seed = await _fetch_match_seed(payload.opportunity_id)
        if seed is not None:
            buyer_target_price = seed.get("buyer_target_price_usd")
            initial_status = DealStatus.MATCHING  # always move to MATCHING once seeded

            if buyer_target_price:
                top_sellers: list[dict[str, Any]] = seed.get("top_sellers", [])[:3]
                score_coros = [
                    _score_seller(deal_id, s, buyer_target_price)
                    for s in top_sellers
                    if s.get("fob_price_usd") is not None
                ]
                raw_results = await asyncio.gather(*score_coros, return_exceptions=True)
                for r in raw_results:
                    if isinstance(r, BaseException):
                        logger.error("Uncaught scoring error for deal %s: %s", deal_id, r)
                    elif r is not None:
                        match_results.append(r)

            # Pick best SOURCE candidate
            source_results = [mr for mr in match_results if mr.decision == "SOURCE"]
            if source_results:
                best = max(source_results, key=lambda mr: mr.composite_score)
                winning_supplier_id = best.supplier_id
                winning_cms = best.composite_score

    # Seed parties from the opportunity when the caller didn't supply any —
    # otherwise the deal's Overview tab has no buyer/supplier for its lifetime.
    parties = payload.parties
    if not parties and payload.opportunity_id:
        opportunity = await _fetch_opportunity(payload.opportunity_id)
        if opportunity is not None:
            parties = _seed_parties_from_opportunity(
                opportunity, winning_supplier_id, payload.market
            )

    deal = DealRecord(
        deal_id=deal_id,
        operator_id=payload.operator_id,
        org_id=org_id,
        hs_code=payload.hs_code,
        market=payload.market,
        status=initial_status,
        autonomy_profile=payload.autonomy_profile,
        opportunity_id=payload.opportunity_id,
        match_results=match_results,
        winning_supplier_id=winning_supplier_id,
        winning_cms=winning_cms,
        buyer_target_price_usd=buyer_target_price,
        margin_floor_pct=(
            payload.margin_floor_pct
            if payload.margin_floor_pct is not None
            else _DEFAULT_MARGIN_FLOOR_PCT
        ),
        parties=parties,
        status_history=[
            StatusHistoryEntry(
                status=initial_status,
                transitioned_at=now,
                reason="Initial deal creation",
            )
        ],
        created_at=now,
        updated_at=now,
    )

    repo = get_repository()
    await repo.save(deal)

    if initial_status == DealStatus.MATCHING:
        try:
            from shared.orchestration.auto_flow import start as start_auto_flow
            start_auto_flow(deal_id)
        except Exception as exc:
            logger.warning("Could not start auto-flow for deal %s: %s", deal_id, exc)

    await _emit_event(
        EventTopic.DEAL_CREATED,
        deal_id=deal_id,
        payload={
            "operator_id": payload.operator_id,
            "hs_code": payload.hs_code,
            "market": payload.market,
            "status": initial_status.value,
            "opportunity_id": payload.opportunity_id,
            "winning_supplier_id": winning_supplier_id,
            "winning_cms": winning_cms,
            "match_results_count": len(match_results),
        },
    )

    # Convert the originating opportunity after the event is emitted (best-effort).
    if payload.opportunity_id:
        await _mark_opportunity_converted(payload.opportunity_id)

    return deal


async def get_deal(deal_id: str) -> DealRecord:
    """Fetch a deal by ID.  Raises ``DealNotFoundError`` when absent."""
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)
    return deal


async def list_deals(
    limit: int = 100,
    status: DealStatus | None = None,
    operator_id: str | None = None,
    org_id: str | None = None,
) -> list[DealRecord]:
    """List deals, sorted by creation time descending.

    ``org_id=None`` means unrestricted (Portless platform staff); callers that
    need per-org isolation must pass the caller's org explicitly.
    """
    repo = get_repository()
    return await repo.list_active(limit=limit, status=status, operator_id=operator_id, org_id=org_id)


async def update_status(deal_id: str, payload: UpdateStatusRequest) -> DealRecord:
    """Transition a deal's status through the state machine.

    Topology check (``can_transition``) is always enforced.

    The deterministic business guards (``margin_floor_ok``, ``dual_doc_match_ok``)
    are implemented in ``shared.domain.state_machine`` but are evaluated by the
    caller that holds the financial / document inputs (e.g. DealMaker enforces the
    margin floor at PO issuance), not inside this topology-only check.
    """
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    if not can_transition(deal.status, payload.target_status):
        raise InvalidTransitionError(deal.status, payload.target_status)

    now = datetime.now(timezone.utc)
    entry = StatusHistoryEntry(
        status=payload.target_status,
        transitioned_at=now,
        reason=payload.reason,
    )

    # Persist via mark_status (atomic update) then reload for response
    updated = await repo.mark_status(deal_id, entry)
    if not updated:
        raise DealNotFoundError(deal_id)

    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    event_topic = _STATUS_TO_EVENT.get(payload.target_status)
    if event_topic:
        await _emit_event(
            event_topic,
            deal_id=deal_id,
            payload={
                "status": payload.target_status.value,
                "reason": payload.reason,
                "winning_supplier_id": deal.winning_supplier_id,
            },
        )

    return deal


async def update_financials(deal_id: str, payload: UpdateFinancialsRequest) -> DealRecord:
    """Patch financial fields after Sourcing / Negotiation phases update them.

    Called by the Negotiation Agent when a price is agreed and by the Sourcing
    service when regulatory verification results change the CMS.
    """
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if payload.est_margin_usd is not None:
        updates["est_margin_usd"] = payload.est_margin_usd
    if payload.realized_margin_usd is not None:
        updates["realized_margin_usd"] = payload.realized_margin_usd
    if payload.buyer_target_price_usd is not None:
        updates["buyer_target_price_usd"] = payload.buyer_target_price_usd
    if payload.winning_supplier_id is not None:
        updates["winning_supplier_id"] = payload.winning_supplier_id
    if payload.winning_cms is not None:
        updates["winning_cms"] = payload.winning_cms

    updated_deal = deal.model_copy(update=updates)
    await repo.save(updated_deal)
    return updated_deal


async def set_counterparty_orgs(
    deal_id: str,
    buyer_org_id: str | None = None,
    seller_org_id: str | None = None,
) -> DealRecord:
    """Link the auto-provisioned buyer/seller organizations to the deal.

    Called by the Negotiation Agent's finalize_agreement() once the deal is
    agreed — from then on, users assigned to either organization see this
    deal in their portal (list/get scoping matches org_id OR buyer_org_id OR
    seller_org_id). Only provided (non-None) sides are updated."""
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if buyer_org_id is not None:
        updates["buyer_org_id"] = buyer_org_id
    if seller_org_id is not None:
        updates["seller_org_id"] = seller_org_id

    updated_deal = deal.model_copy(update=updates)
    await repo.save(updated_deal)
    logger.info(
        "Deal %s linked to counterparty orgs (buyer=%s seller=%s)",
        deal_id, buyer_org_id, seller_org_id,
    )
    return updated_deal
