"""Request/response models for the Trade-Min service (FR-1, LLD 4.5)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

OpportunityStatus = Literal["ACTIVE", "EXPIRED", "CONVERTED"]
DataSource = Literal["comtrade", "volza", "macmap", "simulated"]


class ScanRequest(BaseModel):
    hs_codes: list[str] | None = None
    """HS codes to scan; uses configured defaults when omitted."""

    target_markets: list[str] | None = None
    """ISO-3166 alpha-2 destination markets; uses configured defaults when omitted."""

    sources: list[DataSource] | None = None
    """Which data connectors to query; defaults to all available."""

    min_arbitrage_rank: float = Field(default=0.0, ge=0.0, le=100.0)
    """Filter threshold — opportunities below this rank are excluded."""


class OpportunityResponse(BaseModel):
    opportunity_id: str
    hs_code: str
    market: str
    demand_index: float
    """Demand-surge strength in this market for this HS code (0–100)."""
    price_gap_pct: float
    """Percentage spread between India FOB price and destination CIF price."""
    arbitrage_rank: float
    """Composite rank (0–100): geometric mean of demand_index and normalised price_gap."""
    source: DataSource
    currency: str = "USD"
    status: OpportunityStatus = "ACTIVE"
    scanned_at: datetime
