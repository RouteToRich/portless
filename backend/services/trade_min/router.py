"""HTTP routes for the Trade-Min service (FR-1, LLD 4.5, §10)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from . import service
from .schemas import EnrichedOpportunity, MatchEngineSeed, ScanRequest

router = APIRouter(prefix="/trade-min", tags=["trade-min"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "trade-min"}


@router.post("/scan", response_model=list[EnrichedOpportunity])
async def scan(payload: ScanRequest) -> list[EnrichedOpportunity]:
    """Trigger a 5-phase opportunity discovery scan.

    Runs all configured connectors concurrently and returns enriched
    opportunities sorted by urgency tier then arbitrage rank.
    """
    return await service.scan_opportunities(payload)


@router.get("/opportunities", response_model=list[EnrichedOpportunity])
async def list_opportunities(
    limit: int = Query(default=100, ge=1, le=500),
    min_rank: float = Query(default=0.0, ge=0.0, le=100.0),
    urgency_tier: int | None = Query(default=None, ge=1, le=4),
) -> list[EnrichedOpportunity]:
    """List active enriched opportunities.

    Results are sorted by urgency tier (4 = emergency first) then arbitrage rank.
    Use ``urgency_tier`` to filter for a specific urgency level only.
    """
    return await service.list_opportunities(
        limit=limit,
        min_rank=min_rank,
        urgency_tier=urgency_tier,
    )


@router.get("/opportunities/{opportunity_id}", response_model=EnrichedOpportunity)
async def get_opportunity(opportunity_id: str) -> EnrichedOpportunity:
    """Fetch a single enriched opportunity by ID."""
    result = await service.get_opportunity(opportunity_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Opportunity {opportunity_id!r} not found",
        )
    return result


@router.get(
    "/opportunities/{opportunity_id}/match-seed",
    response_model=MatchEngineSeed,
    summary="Get Match Engine seed values for an opportunity",
)
async def get_match_seed(opportunity_id: str) -> MatchEngineSeed:
    """Return pre-computed Match Engine input values derived from the opportunity.

    The Deals service / DealMaker agent calls this when creating a deal from an
    opportunity to seed ``ScoreRequest`` without redundant connector calls:

    - ``buyer_target_price_usd``  → ``ScoreRequest.buyer_target_price``
    - ``top_sellers[i].fob_price_usd`` → ``ScoreRequest.supplier_quote_price``
    - ``top_sellers[i].supplier_to_port_km`` → ``ScoreRequest.supplier_to_port_km``

    Fields not pre-seeded here (resolved in the Sourcing phase):
    ``deal_id``, ``verification``, ``business_age_years``,
    ``udyam_registered``, ``annual_shipment_count``.
    """
    seed = await service.get_match_seed(opportunity_id)
    if seed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Opportunity {opportunity_id!r} not found",
        )
    return seed


@router.post(
    "/opportunities/{opportunity_id}/convert",
    status_code=status.HTTP_200_OK,
    summary="Mark an opportunity as CONVERTED",
)
async def mark_converted(opportunity_id: str) -> dict[str, bool]:
    """Mark an opportunity as CONVERTED once a deal has been created from it.

    Called by the Deals service after ``deal.created`` is emitted to prevent
    the opportunity from being surfaced to other deal flows.
    """
    updated = await service.mark_opportunity_converted(opportunity_id)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Opportunity {opportunity_id!r} not found",
        )
    return {"updated": True}
