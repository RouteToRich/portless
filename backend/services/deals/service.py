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

``update_status`` enforces the deal state machine from
``shared.domain.state_machine``.  Business guards (margin floor, dual-doc
match) are NOT called yet because the guard stubs raise ``NotImplementedError``
— they will be wired once Sourcing resolves the financial data.

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

async def create_deal(payload: CreateDealRequest) -> DealRecord:
    """Create a new deal, optionally orchestrating Match Engine scoring.

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

    deal = DealRecord(
        deal_id=deal_id,
        operator_id=payload.operator_id,
        hs_code=payload.hs_code,
        market=payload.market,
        status=initial_status,
        autonomy_profile=payload.autonomy_profile,
        opportunity_id=payload.opportunity_id,
        match_results=match_results,
        winning_supplier_id=winning_supplier_id,
        winning_cms=winning_cms,
        buyer_target_price_usd=buyer_target_price,
        margin_floor_pct=payload.margin_floor_pct,
        parties=payload.parties,
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
) -> list[DealRecord]:
    """List deals, sorted by creation time descending."""
    repo = get_repository()
    return await repo.list_active(limit=limit, status=status, operator_id=operator_id)


async def update_status(deal_id: str, payload: UpdateStatusRequest) -> DealRecord:
    """Transition a deal's status through the state machine.

    Topology check (``can_transition``) is always enforced.

    Business guards (``margin_floor_ok``, ``dual_doc_match_ok``) are NOT called
    yet — the guard stubs raise ``NotImplementedError`` until Sourcing resolves
    the financial inputs.  Add guard calls here once the stubs are implemented.
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
