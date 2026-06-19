"""Business logic for the Exception-Resolver service (skeleton — not implemented)."""
from __future__ import annotations

from .schemas import ExceptionResponse, RaiseExceptionRequest


async def raise_exception(payload: RaiseExceptionRequest) -> ExceptionResponse:
    """Classify a ``deal.exception`` and build a remediation plan.

    TODO: classify, decide via autonomy gate (auto-fix saga vs approval.requested),
    persist, emit remediation events.
    """
    raise NotImplementedError


async def get_exception(exception_id: str) -> ExceptionResponse:
    """Return current exception state. TODO: read from persistence."""
    raise NotImplementedError
