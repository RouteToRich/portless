"""Request/response models for the Match Engine service (LLD 4.3)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from shared.domain.models import MatchScore

Decision = Literal["SOURCE", "DISCARD"]


class SupplierVerification(BaseModel):
    """Gatekeeper inputs (resolved upstream via GSTN / DGFT / MCA tools)."""

    gst_verified: bool
    iec_active: bool
    dgft_denied: bool = False


class ScoreWeights(BaseModel):
    """CMS weights; normalized to sum to 1.0 before use (default 0.5/0.3/0.2)."""

    arbitrage: float = 0.5
    proximity: float = 0.3
    reliability: float = 0.2


class ScoreRequest(BaseModel):
    deal_id: str
    supplier_id: str

    # Arbitrage (M): buyer target vs supplier quote.
    buyer_target_price: float = Field(gt=0)
    supplier_quote_price: float = Field(ge=0)
    currency: str = "USD"

    # Gatekeeper (C): GST / IEC / DGFT verification outcome.
    verification: SupplierVerification

    # Proximity (L): supplier warehouse -> nearest exit port, in km.
    supplier_to_port_km: float = Field(ge=0)

    # Reliability (V): business age (MCA), Udyam, shipment frequency.
    business_age_years: float = Field(ge=0, default=0.0)
    udyam_registered: bool = False
    annual_shipment_count: int = Field(ge=0, default=0)

    # Optional weight override (otherwise the configured defaults apply).
    weights: ScoreWeights | None = None


class ScoreResponse(MatchScore):
    supplier_id: str
    currency: str
    threshold: float
    decision: Decision
    weights: ScoreWeights
