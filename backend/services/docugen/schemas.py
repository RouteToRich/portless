"""Request/response models for the DocuGen service."""
from __future__ import annotations

from pydantic import BaseModel

from shared.domain.enums import DocumentType


class GenerateDocumentRequest(BaseModel):
    deal_id: str
    type: DocumentType


class DocumentResponse(BaseModel):
    doc_id: str
    deal_id: str
    type: DocumentType
    storage_uri: str | None = None
    hash: str | None = None
    signed: bool = False


class CustomsFilingRequest(BaseModel):
    deal_id: str
    shipping_bill_payload: dict | None = None
    rodtep_declaration: bool = True


class CustomsFilingResponse(BaseModel):
    deal_id: str
    shipping_bill_number: str | None = None
    status: str = "FILED"
