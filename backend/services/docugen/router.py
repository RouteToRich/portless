"""HTTP routes for the DocuGen service."""
from __future__ import annotations

from fastapi import APIRouter

from . import service
from .schemas import (
    CustomsFilingRequest,
    CustomsFilingResponse,
    GenerateDocumentRequest,
    DocumentResponse,
)

router = APIRouter(prefix="/docugen", tags=["docugen"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "docugen"}


@router.post("/documents", response_model=DocumentResponse, status_code=201)
async def generate_document(payload: GenerateDocumentRequest) -> DocumentResponse:
    return await service.generate_document(payload)


@router.post("/customs", response_model=CustomsFilingResponse, status_code=201)
async def file_customs(payload: CustomsFilingRequest) -> CustomsFilingResponse:
    return await service.file_customs(payload)
