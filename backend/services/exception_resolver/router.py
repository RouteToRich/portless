"""HTTP routes for the Exception-Resolver service."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from . import service
from .schemas import (
    ExceptionListResponse,
    ExceptionResponse,
    ExceptionStatus,
    RaiseExceptionRequest,
)
from .service import ExceptionNotFoundError

router = APIRouter(prefix="/exception-resolver", tags=["exception-resolver"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "exception-resolver"}


@router.post(
    "/exceptions",
    response_model=ExceptionResponse,
    status_code=201,
    summary="Raise a deal exception for LLM-powered analysis and remediation",
)
async def raise_exception(payload: RaiseExceptionRequest) -> ExceptionResponse:
    """Classify, analyse, and execute a remediation plan for a deal exception.

    The response is returned synchronously after the agent completes. Status
    will be one of: RESOLVED, ESCALATED, or UNRESOLVABLE.
    """
    try:
        return await service.raise_exception(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/exceptions",
    response_model=ExceptionListResponse,
    summary="List exceptions, optionally filtered by deal_id or status",
)
async def list_exceptions(
    deal_id: str | None = Query(None, description="Filter by deal ID"),
    status: ExceptionStatus | None = Query(None, description="Filter by exception status"),
    limit: int = Query(100, ge=1, le=500),
) -> ExceptionListResponse:
    try:
        items = await service.list_exceptions(deal_id=deal_id, status=status, limit=limit)
        return ExceptionListResponse(items=items, total=len(items))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/exceptions/{exception_id}",
    response_model=ExceptionResponse,
    summary="Get a specific exception by ID",
)
async def get_exception(exception_id: str) -> ExceptionResponse:
    try:
        return await service.get_exception(exception_id)
    except ExceptionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
