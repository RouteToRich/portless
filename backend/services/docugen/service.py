"""Business logic for the DocuGen service (skeleton — not implemented)."""
from __future__ import annotations

from .schemas import (
    CustomsFilingRequest,
    CustomsFilingResponse,
    DocumentResponse,
    GenerateDocumentRequest,
)


async def generate_document(payload: GenerateDocumentRequest) -> DocumentResponse:
    """Generate a trade/customs document (PI, CI, PL, CoO, SDF, Shipping Bill).

    TODO: render from templates, hash, store in object store.
    """
    raise NotImplementedError


async def file_customs(payload: CustomsFilingRequest) -> CustomsFilingResponse:
    """File the Shipping Bill via ICEGATE/CHA and emit ``customs.filed``.

    TODO: stamp RoDTEP declaration, submit, capture LEO, emit event.
    """
    raise NotImplementedError
