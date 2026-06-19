"""HTTP routes for the Exception-Resolver service."""
from __future__ import annotations

from fastapi import APIRouter

from . import service
from .schemas import ExceptionResponse, RaiseExceptionRequest

router = APIRouter(prefix="/exception-resolver", tags=["exception-resolver"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "exception-resolver"}


@router.post("/exceptions", response_model=ExceptionResponse, status_code=201)
async def raise_exception(payload: RaiseExceptionRequest) -> ExceptionResponse:
    return await service.raise_exception(payload)


@router.get("/exceptions/{exception_id}", response_model=ExceptionResponse)
async def get_exception(exception_id: str) -> ExceptionResponse:
    return await service.get_exception(exception_id)
