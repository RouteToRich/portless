"""HTTP routes for the DealMaker service."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from shared.auth.dependencies import AuthUser, require_portless
from shared.connectors.google_drive import GoogleDriveError

from . import service
from .schemas import (
    AcceptPiRequest,
    DealDocumentsResponse,
    ExtendPiRequest,
    IssuePiRequest,
    IssuePoRequest,
    PiResponse,
    PoResponse,
)
from .service import (
    DealNotFoundError,
    InvalidDealState,
    MarginGuardError,
    MarginUnknownError,
    PiExpiredError,
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
    except (PiStateError, PiExpiredError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (DealNotFoundError, InvalidDealState) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/pi/{pi_id}/extend", response_model=PiResponse)
async def extend_pi(
    pi_id: str, payload: ExtendPiRequest, user: AuthUser = Depends(require_portless),
) -> PiResponse:
    """Give an unaccepted PI a new validity (staff only, reason required, audited)."""
    try:
        return await service.extend_pi(pi_id, payload, user.email or user.user_id)
    except PiNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PiStateError, DealNotFoundError) as exc:
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
    except (MarginGuardError, MarginUnknownError) as exc:
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


def _file_response(content: bytes, mime: str, filename: str, download: bool) -> Response:
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type=mime,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/pi/{pi_id}/file")
async def get_pi_file(pi_id: str, download: bool = False) -> Response:
    """Stream the rendered PI document (from Google Drive or local disk).

    Served through the backend's own Drive credentials so the UI's View/
    Download buttons work for any authenticated operator. ``?download=true``
    switches Content-Disposition from inline to attachment."""
    try:
        content, mime, filename = await service.get_pi_file(pi_id)
    except PiNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GoogleDriveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _file_response(content, mime, filename, download)


@router.get("/po/{po_id}/file")
async def get_po_file(po_id: str, download: bool = False) -> Response:
    """Stream the rendered PO document (from Google Drive or local disk)."""
    try:
        content, mime, filename = await service.get_po_file(po_id)
    except PoNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GoogleDriveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _file_response(content, mime, filename, download)
