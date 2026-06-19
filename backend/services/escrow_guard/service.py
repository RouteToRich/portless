"""Business logic for the Escrow-Guard service (skeleton — not implemented).

Payout requires QC_PASSED + dual-doc match pass; idempotency_key dedupes
commands so there is never a double payout.
"""
from __future__ import annotations

from fastapi import Request

from .schemas import (
    EscrowHoldRequest,
    EscrowResponse,
    PayoutRequest,
    PayoutResponse,
)


async def create_hold(payload: EscrowHoldRequest) -> EscrowResponse:
    """Hold buyer funds in multi-currency escrow.

    TODO: call Stripe Treasury/Payoneer connector, persist, return ext_ref.
    """
    raise NotImplementedError


async def request_payout(payload: PayoutRequest) -> PayoutResponse:
    """Release a milestone payout and emit ``payout.requested``.

    TODO: enforce QC_PASSED + dual-doc guard, dedupe on idempotency_key,
    write to ledger, emit event.
    """
    raise NotImplementedError


async def handle_escrow_funded_webhook(request: Request) -> dict[str, str]:
    """Ingest the provider's escrow-funded webhook and emit ``escrow.funded``.

    TODO: verify signature, dedupe, emit event.
    """
    raise NotImplementedError
