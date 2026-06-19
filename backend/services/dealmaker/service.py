"""Business logic for the DealMaker service (skeleton — not implemented)."""
from __future__ import annotations

from .schemas import IssuePiRequest, IssuePoRequest, PiResponse, PoResponse


async def issue_pi(payload: IssuePiRequest) -> PiResponse:
    """Generate the Proforma Invoice and emit ``pi.sent``.

    TODO: render PI document, persist, emit event.
    """
    raise NotImplementedError


async def issue_po(payload: IssuePoRequest) -> PoResponse:
    """Issue the Purchase Order and emit ``po.issued``.

    TODO: enforce PO guard (net_margin >= floor AND credit-cleared/prepaid),
    render PO, persist, emit event.
    """
    raise NotImplementedError
