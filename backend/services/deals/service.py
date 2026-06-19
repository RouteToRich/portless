"""Business logic for the Deals service (skeleton — not implemented)."""
from __future__ import annotations

from .schemas import CreateDealRequest, DealResponse, UpdateStatusRequest


async def list_deals(status: str | None = None) -> list[DealResponse]:
    """List deals, optionally filtered by status. TODO: read from persistence."""
    raise NotImplementedError


async def create_deal(payload: CreateDealRequest) -> DealResponse:
    """Create a new deal in DRAFT and emit ``deal.created``.

    TODO: persist the deal, generate a deal_id, emit the domain event.
    """
    raise NotImplementedError


async def get_deal(deal_id: str) -> DealResponse:
    """Fetch a deal by id. TODO: read from persistence."""
    raise NotImplementedError


async def update_status(deal_id: str, payload: UpdateStatusRequest) -> DealResponse:
    """Transition a deal's status.

    TODO: load current status, validate via ``shared.domain.can_transition``,
    evaluate deterministic guards, persist, and emit the matching event.
    """
    raise NotImplementedError
