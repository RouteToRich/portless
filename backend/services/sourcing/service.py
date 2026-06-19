"""Business logic for the Sourcing service (skeleton — not implemented)."""
from __future__ import annotations

from .schemas import QuoteIntakeRequest, QuoteResponse, RfqRequest, RfqResponse


async def dispatch_rfq(payload: RfqRequest) -> RfqResponse:
    """Dispatch RFQs to suppliers and emit ``quote.requested``.

    TODO: call IndiaMART/TradeIndia/Twilio connectors, persist, emit event.
    """
    raise NotImplementedError


async def receive_quote(payload: QuoteIntakeRequest) -> QuoteResponse:
    """Ingest a supplier quote and emit ``quote.received`` (hands off to Negotiator).

    TODO: validate, persist, emit event.
    """
    raise NotImplementedError
