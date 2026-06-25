"""HTTP routes for the Deals service (LLD 4.2)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from . import service
from .schemas import (
    CreateDealRequest,
    DealRecord,
    DealStatus,
    MatchResult,
    UpdateFinancialsRequest,
    UpdateStatusRequest,
)
from .service import DealNotFoundError, InvalidTransitionError

router = APIRouter(prefix="/deals", tags=["deals"])


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "deals"}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=list[DealRecord])
async def list_deals(
    limit: int = Query(default=100, ge=1, le=500),
    status: DealStatus | None = Query(default=None),
    operator_id: str | None = Query(default=None),
) -> list[DealRecord]:
    """List deals sorted by creation time descending.

    Use ``status`` to filter by lifecycle state (e.g. ``MATCHING``, ``NEGOTIATING``).
    Use ``operator_id`` to scope to a single operator's deals.
    """
    return await service.list_deals(
        limit=limit,
        status=status,
        operator_id=operator_id,
    )


@router.post("", response_model=DealRecord, status_code=status.HTTP_201_CREATED)
async def create_deal(payload: CreateDealRequest) -> DealRecord:
    """Create a new deal.

    When ``opportunity_id`` is provided the service fetches the Trade-Min
    MatchEngineSeed, scores all seller candidates via Match Engine, and
    transitions to ``MATCHING`` if a SOURCE decision is found.
    """
    return await service.create_deal(payload)


@router.get("/{deal_id}", response_model=DealRecord)
async def get_deal(deal_id: str) -> DealRecord:
    """Fetch a single deal by ID."""
    try:
        return await service.get_deal(deal_id)
    except DealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

@router.patch("/{deal_id}/status", response_model=DealRecord)
async def update_status(deal_id: str, payload: UpdateStatusRequest) -> DealRecord:
    """Transition a deal to a new lifecycle state.

    Topology is enforced by the state machine in ``shared.domain.state_machine``.
    Returns ``422`` when the transition is not permitted.
    """
    try:
        return await service.update_status(deal_id, payload)
    except DealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Financial patch (Sourcing / Negotiation phase updates)
# ---------------------------------------------------------------------------

@router.patch("/{deal_id}/financials", response_model=DealRecord)
async def update_financials(
    deal_id: str, payload: UpdateFinancialsRequest
) -> DealRecord:
    """Patch financial fields after Sourcing or Negotiation phases update them.

    Used by the Negotiation Agent when a price is agreed, and by the Sourcing
    service when regulatory verification results change the composite score.
    """
    try:
        return await service.update_financials(deal_id, payload)
    except DealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Match Engine results sub-resource
# ---------------------------------------------------------------------------

@router.get("/{deal_id}/match-scores", response_model=list[MatchResult])
async def get_match_scores(deal_id: str) -> list[MatchResult]:
    """Return all Match Engine scores computed for a deal's seller candidates.

    The list is sorted by composite score descending so the winning candidate
    appears first.  ``decision`` is either ``SOURCE`` (viable) or ``DISCARD``.
    """
    try:
        deal = await service.get_deal(deal_id)
    except DealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    return sorted(
        deal.match_results,
        key=lambda mr: mr.composite_score,
        reverse=True,
    )
