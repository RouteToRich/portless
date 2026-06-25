"""HTTP routes for the DealMaker service."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import service
from .schemas import (
    AcceptPiRequest,
    DealDocumentsResponse,
    IssuePiRequest,
    IssuePoRequest,
    PiResponse,
    PoResponse,
)
from .service import (
    DealNotFoundError,
    InvalidDealState,
    MarginGuardError,
    PiNotFoundError,
    PiStateError,
    PoNotFoundError,
)

router = APIRouter(prefix="/dealmaker", tags=["dealmaker"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "dealmaker"}


# ---------------------------------------------------------------------------
# PI endpoints
# ---------------------------------------------------------------------------

@router.post("/pi", response_model=PiResponse, status_code=201)
async def issue_pi(payload: IssuePiRequest) -> PiResponse:
    """Issue a Proforma Invoice to the buyer (deal must be NEGOTIATION_AGREED)."""
    try:
        return await service.issue_pi(payload)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDealState as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/pi/{pi_id}/accept", response_model=PiResponse)
async def accept_pi(pi_id: str, payload: AcceptPiRequest) -> PiResponse:
    """Record buyer acceptance of a Proforma Invoice (PI must be SENT)."""
    try:
        return await service.accept_pi(pi_id, payload)
    except PiNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PiStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (DealNotFoundError, InvalidDealState) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/pi/{pi_id}", response_model=PiResponse)
async def get_pi(pi_id: str) -> PiResponse:
    """Fetch a Proforma Invoice by ID."""
    try:
        return await service.get_pi(pi_id)
    except PiNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# PO endpoints
# ---------------------------------------------------------------------------

@router.post("/po", response_model=PoResponse, status_code=201)
async def issue_po(payload: IssuePoRequest) -> PoResponse:
    """Issue a Purchase Order to the supplier (deal must be ESCROW_FUNDED)."""
    try:
        return await service.issue_po(payload)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDealState as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarginGuardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/po/{po_id}", response_model=PoResponse)
async def get_po(po_id: str) -> PoResponse:
    """Fetch a Purchase Order by ID."""
    try:
        return await service.get_po(po_id)
    except PoNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Deal-scoped aggregate view
# ---------------------------------------------------------------------------

@router.get("/deals/{deal_id}/documents", response_model=DealDocumentsResponse)
async def get_deal_documents(deal_id: str) -> DealDocumentsResponse:
    """Return all DealMaker documents (PI + PO) for a given deal."""
    return await service.get_deal_documents(deal_id)
