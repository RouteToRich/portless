"""Request/response models for the Shield-Link service.

Shield-Link orchestrates three physical-inspection and logistics operations:

  QC booking        PO_ISSUED → SUPPLIER_DISPATCH_READY
  QC result webhook SUPPLIER_DISPATCH_READY → QC_PASSED | QC_FAILED
  Freight booking   QC_PASSED → IN_TRANSIT
  Freight milestone IN_TRANSIT → CLEARED (when cargo arrives)
  CHA filing        CHA docs submitted for customs clearance
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Third-party order record (persisted)
# ---------------------------------------------------------------------------

class ThirdPartyOrderRecord(BaseModel):
    """One external provider order — QC booking, freight booking, or CHA filing."""

    tpo_id: str
    deal_id: str
    order_type: Literal["QC", "FREIGHT", "CHA"]
    provider: str
    status: Literal["PENDING", "BOOKED", "IN_PROGRESS", "COMPLETED", "FAILED", "CANCELLED"]
    ext_ref: str | None = None
    crf_hash: str | None = None

    # Logistics details
    pickup_ready_at: str | None = None
    origin_port: str | None = None
    destination_port: str | None = None
    incoterm: str | None = None
    container_number: str | None = None
    vessel: str | None = None
    voyage: str | None = None
    etd: str | None = None
    eta: str | None = None

    # QC result (populated by webhook)
    qc_result: Literal["PASS", "FAIL", "PENDING"] | None = None
    qc_report_url: str | None = None
    qc_completed_at: datetime | None = None

    # Audit
    created_at: datetime
    updated_at: datetime
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class QcBookingRequest(BaseModel):
    deal_id: str
    provider: str = "sgs"
    pickup_ready_at: str | None = None
    """ISO date string — when the goods will be ready for inspection."""
    location: str | None = None
    """Supplier warehouse address or GPS coords."""
    hs_code: str | None = None
    quantity_mt: float | None = None
    idempotency_key: str | None = None


class FreightBookingRequest(BaseModel):
    deal_id: str
    provider: str = "cogoport"
    incoterm: str = "FOB"
    origin_port: str | None = None
    destination_port: str | None = None
    container_type: str = "40HC"
    quantity_mt: float | None = None
    hs_code: str | None = None
    idempotency_key: str | None = None


class ChaFilingRequest(BaseModel):
    deal_id: str
    provider: str = "icegate"
    documents: list[str] = Field(default_factory=list)
    """List of document IDs (DocuGen doc_ids) to include in the filing."""
    rodtep_declaration: bool = True
    idempotency_key: str | None = None


class QcWebhookPayload(BaseModel):
    """Inbound webhook from QC provider with inspection result."""
    ext_ref: str
    result: Literal["PASS", "FAIL"]
    report_url: str | None = None
    crf_hash: str | None = None
    completed_at: str | None = None
    notes: str | None = None


class FreightMilestonePayload(BaseModel):
    """Inbound webhook from freight provider with shipment milestone."""
    ext_ref: str
    milestone: Literal["DEPARTED", "IN_TRANSIT", "ARRIVED", "CLEARED"]
    location: str | None = None
    timestamp: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class ThirdPartyOrderResponse(BaseModel):
    tpo_id: str
    deal_id: str
    order_type: str
    provider: str
    status: str
    ext_ref: str | None = None
    origin_port: str | None = None
    destination_port: str | None = None
    eta: str | None = None
    qc_result: str | None = None
    created_at: datetime

    @classmethod
    def from_record(cls, r: ThirdPartyOrderRecord) -> "ThirdPartyOrderResponse":
        return cls(
            tpo_id=r.tpo_id,
            deal_id=r.deal_id,
            order_type=r.order_type,
            provider=r.provider,
            status=r.status,
            ext_ref=r.ext_ref,
            origin_port=r.origin_port,
            destination_port=r.destination_port,
            eta=r.eta,
            qc_result=r.qc_result,
            created_at=r.created_at,
        )


class DealOrdersResponse(BaseModel):
    deal_id: str
    orders: list[ThirdPartyOrderResponse]
