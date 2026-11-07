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
    last_milestone: str | None = None
    last_milestone_at: datetime | None = None
    last_milestone_source: str | None = None
    delivered_at: datetime | None = None

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
    provider: str | None = None
    """QC provider key (sgs/qima/bv/intertek/tuv_sud). When unset the service
    resolves it: buyer-opted provider from the deal's negotiation session,
    else the configured default."""
    pickup_ready_at: str | None = None
    """ISO date string — when the goods will be ready for inspection."""
    location: str | None = None
    """Supplier warehouse address or GPS coords."""
    hs_code: str | None = None
    quantity_mt: float | None = None
    idempotency_key: str | None = None


class QcQuoteRequest(BaseModel):
    """Quote the QC inspection cost for a provider — used pre-negotiation to
    price the QC component into the buyer's CIF offer."""

    provider: str | None = None
    """QC provider key or alias; default provider when unset."""
    deal_id: str | None = None
    location: str | None = None
    hs_code: str | None = None
    quantity_mt: float | None = None


class QcQuoteResponse(BaseModel):
    provider: str
    display_name: str
    available: bool = True
    inspection_date: str | None = None
    fee_usd: float
    qc_cost_per_unit_usd: float | None = None
    """fee_usd / quantity_mt when a quantity was supplied."""
    currency: str = "USD"
    evidence: dict[str, bool] = Field(default_factory=dict)
    stub: bool = False


class QcProviderInfo(BaseModel):
    """Buyer-facing catalog entry for one supported QC provider."""

    provider: str
    display_name: str
    headquarters: str
    acceptance: str
    api_integration: str
    evidence: dict[str, bool]
    available_in_india: bool
    indicative_fee_usd: float
    notes: str = ""


class QcProvidersResponse(BaseModel):
    default_provider: str
    providers: list[QcProviderInfo]


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


class FreightQuoteRequest(BaseModel):
    """Quote the freight cost for a lane — used pre-negotiation so the CIF
    price floor covers ocean freight (buyer prices are CIF, supplier Ex-Works).
    Read-only: no booking is created."""

    provider: str | None = None
    """Freight provider (cogoport/freightos); default provider when unset."""
    deal_id: str | None = None
    origin_port: str | None = None
    destination_port: str | None = None
    container_type: str = "40HC"
    quantity_mt: float | None = None
    hs_code: str | None = None
    incoterm: str = "FOB"


class FreightQuoteResponse(BaseModel):
    provider: str
    origin: str | None = None
    destination: str | None = None
    container_type: str = "40HC"
    rate_usd_per_container: float
    containers: int = 1
    total_freight_usd: float
    logistics_cost_per_unit_usd: float | None = None
    """total_freight_usd / quantity_mt when a quantity was supplied."""
    transit_days: int | None = None
    valid_until: str | None = None
    stub: bool = False


class ChaFilingRequest(BaseModel):
    deal_id: str
    provider: str = "icegate"
    documents: list[str] = Field(default_factory=list)
    """List of document IDs (DocuGen doc_ids) to include in the filing."""
    rodtep_declaration: bool = True
    idempotency_key: str | None = None


class QcWebhookPayload(BaseModel):
    """Inbound webhook from QC provider with inspection result.

    When the inspector also supervised the container stuffing, the loading
    fields carry what went into the container and the seal applied."""
    ext_ref: str
    result: Literal["PASS", "FAIL"]
    report_url: str | None = None
    crf_hash: str | None = None
    completed_at: str | None = None
    notes: str | None = None
    container_numbers: list[str] = Field(default_factory=list)
    seal_numbers: list[str] = Field(default_factory=list)
    loaded_quantity_mt: float | None = None
    loaded_hs_code: str | None = None
    goods_description: str | None = None
    photos: list[str] = Field(default_factory=list)


class LoadingRecordRequest(BaseModel):
    """Supervised loading (stuffing) record: what went into which container
    under which seal — from the inspector's report, entered by an operator or
    posted by the inspection provider."""
    container_numbers: list[str] = Field(min_length=1)
    seal_numbers: list[str] = Field(min_length=1)
    loaded_quantity_mt: float | None = None
    hs_code: str | None = None
    goods_description: str | None = None
    inspector_ref: str | None = None
    photos: list[str] = Field(default_factory=list)
    """Links to stuffing / seal photos."""
    source: str = "operator"


FreightMilestone = Literal["DEPARTED", "IN_TRANSIT", "ARRIVED", "CLEARED", "DELIVERED"]


class FreightMilestonePayload(BaseModel):
    """Inbound webhook from freight provider with shipment milestone."""
    ext_ref: str
    milestone: FreightMilestone
    location: str | None = None
    timestamp: str | None = None
    notes: str | None = None
    seal_numbers: list[str] = Field(default_factory=list)
    """Seal number(s) on the bill of lading — must match the inspector's seal."""
    container_number: str | None = None


class RecordMilestoneRequest(BaseModel):
    """Operator / tracking-poll milestone (no provider signature)."""
    milestone: FreightMilestone
    location: str | None = None
    timestamp: str | None = None
    notes: str | None = None
    source: str = "operator"
    seal_numbers: list[str] = Field(default_factory=list)
    container_number: str | None = None


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
    container_number: str | None = None
    last_milestone: str | None = None
    last_milestone_at: datetime | None = None
    delivered_at: datetime | None = None
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
            container_number=r.container_number,
            last_milestone=r.last_milestone,
            last_milestone_at=r.last_milestone_at,
            delivered_at=r.delivered_at,
            created_at=r.created_at,
        )


class DealOrdersResponse(BaseModel):
    deal_id: str
    orders: list[ThirdPartyOrderResponse]
