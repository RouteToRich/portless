"""HTTP routes for the Sourcing service."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from . import service
from .schemas import (
    QuoteIntakeRequest,
    QuoteRecord,
    RfqRecord,
    RfqRequest,
    RfqResponse,
    VerificationResult,
)
from .service import QuoteNotFoundError, RfqNotFoundError

router = APIRouter(prefix="/sourcing", tags=["sourcing"])


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "sourcing"}


# ---------------------------------------------------------------------------
# RFQ dispatch
# ---------------------------------------------------------------------------

@router.post("/rfq", response_model=RfqResponse, status_code=status.HTTP_201_CREATED)
async def dispatch_rfq(payload: RfqRequest) -> RfqResponse:
    """Dispatch automated RFQs to one or more suppliers.

    Routes each supplier via its preferred connector (IndiaMART / TradeIndia).
    Falls back to a portal-queued dispatch when a connector is not configured.
    Emits ``quote.requested`` after persistence.
    """
    return await service.dispatch_rfq(payload)


@router.get("/rfqs/{rfq_id}", response_model=RfqRecord)
async def get_rfq(rfq_id: str) -> RfqRecord:
    """Fetch an RFQ by ID."""
    try:
        return await service.get_rfq(rfq_id)
    except RfqNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.get("/deals/{deal_id}/rfqs", response_model=list[RfqRecord])
async def list_rfqs(deal_id: str) -> list[RfqRecord]:
    """List all RFQs dispatched for a given deal."""
    return await service.list_rfqs(deal_id)


# ---------------------------------------------------------------------------
# Quote ingestion
# ---------------------------------------------------------------------------

@router.post("/quotes", response_model=QuoteRecord, status_code=status.HTTP_201_CREATED)
async def receive_quote(payload: QuoteIntakeRequest) -> QuoteRecord:
    """Ingest a supplier quote and run the verification + re-scoring pipeline.

    Steps:
      1. Concurrent: verify supplier (GSTN + DGFT + MCA + Udyam) + fetch deal
      2. Re-score via Match Engine with real verification data
      3. Persist quote
      4. Update deal financials and advance to NEGOTIATING on SOURCE decision
      5. Emit ``quote.received``
    """
    return await service.receive_quote(payload)


@router.get("/quotes/{quote_id}", response_model=QuoteRecord)
async def get_quote(quote_id: str) -> QuoteRecord:
    """Fetch a quote by ID."""
    try:
        return await service.get_quote(quote_id)
    except QuoteNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.get("/deals/{deal_id}/quotes", response_model=list[QuoteRecord])
async def list_quotes(
    deal_id: str,
    rfq_id: str | None = Query(default=None),
) -> list[QuoteRecord]:
    """List all quotes received for a deal, optionally filtered by RFQ ID."""
    return await service.list_quotes(deal_id=deal_id, rfq_id=rfq_id)


# ---------------------------------------------------------------------------
# Ad-hoc supplier verification
# ---------------------------------------------------------------------------

@router.post("/verify/{supplier_id}", response_model=VerificationResult)
async def verify_supplier(
    supplier_id: str,
    gstin: str | None = Query(default=None),
    iec: str | None = Query(default=None),
    cin: str | None = Query(default=None),
    udyam_number: str | None = Query(default=None),
) -> VerificationResult:
    """Manually trigger regulatory verification for a supplier.

    Runs GSTN, DGFT IEC, MCA company registration, and Udyam checks
    concurrently.  Useful for pre-screening suppliers before issuing an RFQ.
    All checks are best-effort — missing credentials are skipped.
    """
    return await service.verify_supplier(
        supplier_id=supplier_id,
        gstin=gstin,
        iec=iec,
        cin=cin,
        udyam_number=udyam_number,
    )
