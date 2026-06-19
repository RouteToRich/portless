"""HTTP routes for the DealMaker service."""
from __future__ import annotations

from fastapi import APIRouter

from . import service
from .schemas import IssuePiRequest, IssuePoRequest, PiResponse, PoResponse

router = APIRouter(prefix="/dealmaker", tags=["dealmaker"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "dealmaker"}


@router.post("/pi", response_model=PiResponse, status_code=201)
async def issue_pi(payload: IssuePiRequest) -> PiResponse:
    return await service.issue_pi(payload)


@router.post("/po", response_model=PoResponse, status_code=201)
async def issue_po(payload: IssuePoRequest) -> PoResponse:
    return await service.issue_po(payload)
