"""HTTP routes for the Deals service (LLD 4.2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from shared.auth.dependencies import AuthUser, get_current_user, require_org_scope

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


async def _org_deal(deal_id: str, user: AuthUser) -> DealRecord:
    """Fetch a deal and enforce that it belongs to the caller's organization
    (else 404). Portless platform staff (org_filter is None) bypass this."""
    org_filter = require_org_scope(user)
    try:
        deal = await service.get_deal(deal_id)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if org_filter is not None and deal.org_id != org_filter:
        # Hide existence of other organizations' deals.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Deal {deal_id!r} not found")
    return deal


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
    user: AuthUser = Depends(get_current_user),
) -> list[DealRecord]:
    """List deals visible to the authenticated user, newest first.

    Scoped to every deal in the caller's organization (multi-tenancy) —
    Portless platform staff see deals across every organization. Use
    ``status`` to filter by lifecycle state (e.g. ``MATCHING``).
    """
    return await service.list_deals(
        limit=limit,
        status=status,
        org_id=require_org_scope(user),
    )


@router.post("", response_model=DealRecord, status_code=status.HTTP_201_CREATED)
async def create_deal(
    payload: CreateDealRequest,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Create a new deal owned by the authenticated user.

    The deal's ``operator_id`` and ``org_id`` are forced to the caller's
    identity/organization so it is tied to their account regardless of the
    request body.
    """
    payload.operator_id = user.user_id
    org_id = user.org_id if user.is_portless else require_org_scope(user)
    return await service.create_deal(payload, org_id=org_id)


@router.get("/{deal_id}", response_model=DealRecord)
async def get_deal(
    deal_id: str,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Fetch a single deal in the caller's organization (404 if not theirs)."""
    return await _org_deal(deal_id, user)


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

@router.patch("/{deal_id}/status", response_model=DealRecord)
async def update_status(
    deal_id: str,
    payload: UpdateStatusRequest,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Transition one of the caller's deals to a new lifecycle state.

    Topology is enforced by the state machine in ``shared.domain.state_machine``.
    Returns ``422`` when the transition is not permitted, ``404`` if not theirs.
    """
    await _org_deal(deal_id, user)  # organization ownership gate
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
async def get_match_scores(
    deal_id: str,
    user: AuthUser = Depends(get_current_user),
) -> list[MatchResult]:
    """Return Match Engine scores for one of the caller's organization's deals.

    The list is sorted by composite score descending so the winning candidate
    appears first.  ``decision`` is either ``SOURCE`` (viable) or ``DISCARD``.
    """
    deal = await _org_deal(deal_id, user)

    return sorted(
        deal.match_results,
        key=lambda mr: mr.composite_score,
        reverse=True,
    )
