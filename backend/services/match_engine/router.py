"""HTTP routes for the Match Engine service."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from . import service
from .schemas import ScoreRequest, ScoreResponse

router = APIRouter(prefix="/match-engine", tags=["match-engine"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "match-engine"}


@router.post("/score", response_model=ScoreResponse)
async def score(payload: ScoreRequest) -> ScoreResponse:
    return await service.score_match(payload)


@router.get("/scores", response_model=list[ScoreResponse])
async def list_scores() -> list[ScoreResponse]:
    return await service.list_scores()


@router.get("/scores/{deal_id}", response_model=ScoreResponse)
async def get_score(deal_id: str) -> ScoreResponse:
    result = await service.get_score(deal_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No match score found for deal {deal_id}",
        )
    return result
