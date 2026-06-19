"""Business logic for the Shield-Link service (skeleton — not implemented)."""
from __future__ import annotations

from .schemas import (
    ChaFilingRequest,
    FreightBookingRequest,
    QcBookingRequest,
    ThirdPartyOrderResponse,
)


async def book_qc(payload: QcBookingRequest) -> ThirdPartyOrderResponse:
    """Book a 3rd-party inspection and emit ``qc.booked``.

    TODO: call SGS/Intertek connector, persist order, emit event.
    """
    raise NotImplementedError


async def book_freight(payload: FreightBookingRequest) -> ThirdPartyOrderResponse:
    """Book freight and emit ``freight.booked``.

    TODO: fetch spot rates (Cogoport/Freightos), book container, emit event.
    """
    raise NotImplementedError


async def file_cha(payload: ChaFilingRequest) -> ThirdPartyOrderResponse:
    """Submit customs docs via CHA. TODO: ICEGATE EDI integration."""
    raise NotImplementedError
