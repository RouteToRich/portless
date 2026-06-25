"""Escrow-Guard business logic.

Hold lifecycle
--------------
  create_hold()                 ESCROW_PENDING → (side-effect) creates PaymentIntent
  handle_escrow_funded_webhook() webhook confirms funds → deal → ESCROW_FUNDED, emits escrow.funded
  release_hold()                internal helper — capture PI when deal is ready to pay

Payout lifecycle
----------------
  request_payout()              deal CLEARED → initiates supplier disbursement → emits payout.requested
  handle_payout_webhook()       provider confirms completion → emits payout.completed → deal → PAID

Guards
------
  • Idempotency key on every create to prevent double-holds / double-payouts
  • Deal state validation before every state-changing operation
  • HMAC webhook verification (Stripe Stripe-Signature, Payoneer x-payoneer-signature)
  • Graceful stub: when API keys are absent the connectors return realistic stubs
    so the deal pipeline can progress in dev/CI

Exceptions
----------
  DealNotFoundError    404
  InvalidDealState     422
  HoldNotFoundError    404
  PayoutNotFoundError  404
  ConnectorCallError   502
  WebhookVerifyError   401
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from shared.bus import get_publisher
from shared.connectors.payments import ConnectorError, PayoneerConnector, StripeTreasuryConnector
from shared.domain.enums import DealStatus
from shared.domain.events import DomainEvent, EventTopic
from shared.domain.state_machine import can_transition

from .config import settings
from .repository import get_repository
from .schemas import (
    DealEscrowSummary,
    EscrowFundedWebhookPayload,
    EscrowHoldRecord,
    EscrowHoldRequest,
    EscrowResponse,
    PayoutRecord,
    PayoutRequest,
    PayoutResponse,
    PayoutWebhookPayload,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DealNotFoundError(Exception):
    def __init__(self, deal_id: str) -> None:
        super().__init__(f"Deal {deal_id!r} not found")
        self.deal_id = deal_id


class InvalidDealState(Exception):
    def __init__(self, deal_id: str, current: str, required: str) -> None:
        super().__init__(f"Deal {deal_id!r} is {current!r}; required {required!r}")


class HoldNotFoundError(Exception):
    def __init__(self, escrow_id: str) -> None:
        super().__init__(f"Escrow hold {escrow_id!r} not found")


class PayoutNotFoundError(Exception):
    def __init__(self, payout_id: str) -> None:
        super().__init__(f"Payout {payout_id!r} not found")


class ConnectorCallError(Exception):
    """Provider returned a non-retryable error."""


class WebhookVerifyError(Exception):
    """Webhook signature verification failed."""


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

def _new_escrow_id() -> str:
    return f"ESC-{uuid4().hex[:10].upper()}"


def _new_payout_id() -> str:
    return f"PAY-{uuid4().hex[:10].upper()}"


# ---------------------------------------------------------------------------
# Deal bridge
# ---------------------------------------------------------------------------

async def _fetch_deal(deal_id: str) -> Any:
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        return await get_deal(deal_id)
    except Exception as exc:
        if "not found" in str(exc).lower():
            raise DealNotFoundError(deal_id) from exc
        raise


async def _transition_deal(deal_id: str, target: str, reason: str) -> None:
    try:
        from services.deals.service import update_status  # type: ignore[import]
        from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
        await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus(target), reason=reason))
    except Exception as exc:
        logger.error("EscrowGuard: deal transition %s→%s failed: %s", deal_id, target, exc)
        raise


# ---------------------------------------------------------------------------
# Event helper
# ---------------------------------------------------------------------------

async def _emit(topic: EventTopic, deal_id: str, payload: dict) -> None:
    try:
        await get_publisher().publish(DomainEvent(topic=topic, deal_id=deal_id, payload=payload))
    except Exception as exc:
        logger.warning("EscrowGuard: emit %s failed: %s", topic, exc)


# ---------------------------------------------------------------------------
# Connector factory
# ---------------------------------------------------------------------------

def _payment_connector(provider: str) -> StripeTreasuryConnector | PayoneerConnector:
    if provider in ("stripe", "stripe_treasury"):
        return StripeTreasuryConnector()
    if provider == "payoneer":
        return PayoneerConnector()
    raise ValueError(f"Unknown payment provider: {provider!r}")


# ---------------------------------------------------------------------------
# Create hold
# ---------------------------------------------------------------------------

async def create_hold(req: EscrowHoldRequest) -> EscrowResponse:
    """Create a buyer-side escrow hold.

    Pre-condition: deal must be in ESCROW_PENDING state.
    Post: creates PaymentIntent; deal remains ESCROW_PENDING until funded webhook arrives.
    Idempotent on ``idempotency_key``.
    """
    repo = get_repository()

    if req.idempotency_key:
        existing = await repo.get_hold_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return EscrowResponse.from_record(existing)

    # Check for existing non-failed hold (one hold per deal)
    existing_by_deal = await repo.get_hold_by_deal(req.deal_id)
    if existing_by_deal and existing_by_deal.status not in ("FAILED",):
        return EscrowResponse.from_record(existing_by_deal)

    deal = await _fetch_deal(req.deal_id)
    deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    if deal_status != "ESCROW_PENDING":
        raise InvalidDealState(req.deal_id, deal_status, "ESCROW_PENDING")

    now = datetime.now(timezone.utc)
    escrow_id = _new_escrow_id()
    provider = req.provider or settings.default_payment_provider

    # PENDING record before connector call
    record = EscrowHoldRecord(
        escrow_id=escrow_id,
        deal_id=req.deal_id,
        provider=provider,
        amount=req.amount,
        currency=req.currency.upper(),
        status="PENDING",
        idempotency_key=req.idempotency_key,
        created_at=now,
        updated_at=now,
    )
    await repo.save_hold(record)

    connector = _payment_connector(provider)
    amount_minor = int(req.amount * 100)  # cents/paise
    try:
        result = await connector.book({
            "amount": amount_minor,
            "currency": req.currency.lower(),
            "metadata": {
                "deal_id": req.deal_id,
                "escrow_id": escrow_id,
            },
        })
    except ConnectorError as exc:
        failed = record.model_copy(update={"status": "FAILED", "updated_at": datetime.now(timezone.utc)})
        await repo.save_hold(failed)
        raise ConnectorCallError(f"Escrow hold failed for deal {req.deal_id}: {exc}") from exc

    updated = record.model_copy(update={
        "ext_ref": result.get("ext_ref"),
        "client_secret": result.get("client_secret"),
        "provider_payload": result,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save_hold(updated)

    logger.info("EscrowGuard: hold created %s for deal %s (ext_ref=%s)", escrow_id, req.deal_id, result.get("ext_ref"))
    return EscrowResponse.from_record(updated)


# ---------------------------------------------------------------------------
# Escrow funded webhook
# ---------------------------------------------------------------------------

async def handle_escrow_funded_webhook(
    payload: EscrowFundedWebhookPayload,
    raw_headers: dict[str, str],
    raw_body: bytes,
) -> dict[str, str]:
    """Process escrow-funded webhook from payment provider.

    Verifies signature, transitions deal → ESCROW_FUNDED, emits escrow.funded.
    Idempotent: if deal is already ESCROW_FUNDED, returns success.
    """
    repo = get_repository()

    hold = await repo.get_hold_by_ext_ref(payload.ext_ref)
    if hold is None:
        raise HoldNotFoundError(payload.ext_ref)

    # Verify webhook signature
    connector = _payment_connector(hold.provider)
    try:
        valid = await connector.webhook_verify(raw_headers, raw_body)
    except Exception:
        valid = False
    if not valid:
        raise WebhookVerifyError(f"Escrow webhook signature invalid for ext_ref={payload.ext_ref}")

    # Idempotent — already funded
    if hold.status == "FUNDED":
        return {"status": "ok", "escrow_id": hold.escrow_id, "deal_id": hold.deal_id}

    now = datetime.now(timezone.utc)
    updated = hold.model_copy(update={
        "status": "FUNDED",
        "funded_at": now,
        "updated_at": now,
        "provider_payload": {**hold.provider_payload, "funded_event": payload.model_dump()},
    })
    await repo.save_hold(updated)

    try:
        await _transition_deal(hold.deal_id, "ESCROW_FUNDED", f"Escrow funded — ref {payload.ext_ref}")
    except Exception as exc:
        logger.error("EscrowGuard: ESCROW_FUNDED transition failed for deal %s: %s", hold.deal_id, exc)

    await _emit(EventTopic.ESCROW_FUNDED, hold.deal_id, {
        "escrow_id": hold.escrow_id,
        "ext_ref": payload.ext_ref,
        "amount": hold.amount,
        "currency": hold.currency,
    })

    logger.info("EscrowGuard: hold %s funded for deal %s", hold.escrow_id, hold.deal_id)
    return {"status": "ok", "escrow_id": hold.escrow_id, "deal_id": hold.deal_id}


# ---------------------------------------------------------------------------
# Request payout
# ---------------------------------------------------------------------------

async def request_payout(req: PayoutRequest) -> PayoutResponse:
    """Initiate a supplier payout.

    Pre-condition: deal must be in CLEARED state (cargo has arrived).
    Guard: deal must have a FUNDED escrow hold (funds are in escrow).
    Idempotent on ``idempotency_key`` — exactly-once payout guarantee.
    """
    repo = get_repository()

    # Idempotency
    existing = await repo.get_payout_by_idempotency_key(req.idempotency_key)
    if existing is not None:
        return PayoutResponse.from_record(existing)

    deal = await _fetch_deal(req.deal_id)
    deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    if deal_status != "CLEARED":
        raise InvalidDealState(req.deal_id, deal_status, "CLEARED")

    # Verify funded escrow exists
    hold = await repo.get_hold_by_deal(req.deal_id)
    if hold is None or hold.status != "FUNDED":
        raise InvalidDealState(
            req.deal_id,
            f"escrow={hold.status if hold else 'none'}",
            "escrow=FUNDED",
        )

    now = datetime.now(timezone.utc)
    payout_id = _new_payout_id()

    payout_record = PayoutRecord(
        payout_id=payout_id,
        deal_id=req.deal_id,
        payee_id=req.payee_id,
        amount=req.amount,
        currency=req.currency.upper(),
        status="REQUESTED",
        description=req.description or f"Payout for deal {req.deal_id}",
        idempotency_key=req.idempotency_key,
        created_at=now,
        updated_at=now,
    )
    await repo.save_payout(payout_record)

    connector = PayoneerConnector()
    try:
        result = await connector.book({
            "payee_id": req.payee_id,
            "amount": req.amount,
            "currency": req.currency.upper(),
            "description": payout_record.description,
            "idempotency_key": req.idempotency_key,
        })
    except ConnectorError as exc:
        failed = payout_record.model_copy(update={"status": "FAILED", "updated_at": datetime.now(timezone.utc)})
        await repo.save_payout(failed)
        raise ConnectorCallError(f"Payout failed for deal {req.deal_id}: {exc}") from exc

    updated = payout_record.model_copy(update={
        "status": "PROCESSING",
        "ext_ref": result.get("ext_ref"),
        "provider_payload": result,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save_payout(updated)

    # Mark hold as RELEASED
    released_hold = hold.model_copy(update={
        "status": "RELEASED",
        "released_at": now,
        "updated_at": now,
    })
    await repo.save_hold(released_hold)

    await _emit(EventTopic.PAYOUT_REQUESTED, req.deal_id, {
        "payout_id": payout_id,
        "payee_id": req.payee_id,
        "amount": req.amount,
        "currency": req.currency,
        "ext_ref": result.get("ext_ref"),
        "stub": result.get("stub", False),
    })

    logger.info("EscrowGuard: payout %s requested for deal %s (ext_ref=%s)", payout_id, req.deal_id, result.get("ext_ref"))
    return PayoutResponse.from_record(updated)


# ---------------------------------------------------------------------------
# Payout webhook
# ---------------------------------------------------------------------------

async def handle_payout_webhook(
    payload: PayoutWebhookPayload,
    raw_headers: dict[str, str],
    raw_body: bytes,
) -> dict[str, str]:
    """Process Payoneer payout status webhook.

    COMPLETED → deal → PAID; emits payout.completed.
    FAILED    → emits deal.exception; deal remains CLEARED for retry.
    """
    repo = get_repository()

    payout = await repo.get_payout_by_ext_ref(payload.ext_ref)
    if payout is None:
        raise PayoutNotFoundError(payload.ext_ref)

    connector = PayoneerConnector()
    try:
        valid = await connector.webhook_verify(raw_headers, raw_body)
    except Exception:
        valid = False
    if not valid:
        raise WebhookVerifyError(f"Payout webhook signature invalid for ext_ref={payload.ext_ref}")

    # Idempotent
    if payout.status == "COMPLETED" and payload.status == "COMPLETED":
        return {"status": "ok", "payout_id": payout.payout_id, "deal_id": payout.deal_id}

    now = datetime.now(timezone.utc)

    if payload.status == "COMPLETED":
        updated = payout.model_copy(update={
            "status": "COMPLETED",
            "completed_at": now,
            "updated_at": now,
        })
        await repo.save_payout(updated)
        try:
            await _transition_deal(payout.deal_id, "PAID", f"Payout completed — ref {payload.ext_ref}")
        except Exception as exc:
            logger.error("EscrowGuard: PAID transition failed for deal %s: %s", payout.deal_id, exc)
        await _emit(EventTopic.PAYOUT_COMPLETED, payout.deal_id, {
            "payout_id": payout.payout_id,
            "ext_ref": payload.ext_ref,
            "amount": payout.amount,
            "currency": payout.currency,
        })
    elif payload.status == "FAILED":
        updated = payout.model_copy(update={
            "status": "FAILED",
            "updated_at": now,
        })
        await repo.save_payout(updated)
        await _emit(EventTopic.DEAL_EXCEPTION, payout.deal_id, {
            "source": "escrow_guard",
            "payout_id": payout.payout_id,
            "ext_ref": payload.ext_ref,
            "notes": payload.notes,
            "action_required": "retry_payout_or_manual_intervention",
        })
    else:
        # PROCESSING update
        updated = payout.model_copy(update={
            "status": "PROCESSING",
            "updated_at": now,
        })
        await repo.save_payout(updated)

    logger.info("EscrowGuard: payout %s → %s for deal %s", payout.payout_id, payload.status, payout.deal_id)
    return {"status": "ok", "payout_id": payout.payout_id, "deal_id": payout.deal_id}


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

async def get_hold(escrow_id: str) -> EscrowResponse:
    repo = get_repository()
    hold = await repo.get_hold(escrow_id)
    if hold is None:
        raise HoldNotFoundError(escrow_id)
    return EscrowResponse.from_record(hold)


async def get_hold_by_deal(deal_id: str) -> EscrowResponse:
    repo = get_repository()
    hold = await repo.get_hold_by_deal(deal_id)
    if hold is None:
        raise HoldNotFoundError(f"deal:{deal_id}")
    return EscrowResponse.from_record(hold)


async def get_payout(payout_id: str) -> PayoutResponse:
    repo = get_repository()
    payout = await repo.get_payout(payout_id)
    if payout is None:
        raise PayoutNotFoundError(payout_id)
    return PayoutResponse.from_record(payout)


async def get_deal_summary(deal_id: str) -> DealEscrowSummary:
    repo = get_repository()
    hold = await repo.get_hold_by_deal(deal_id)
    payouts = await repo.list_payouts_by_deal(deal_id)
    return DealEscrowSummary(
        deal_id=deal_id,
        hold=EscrowResponse.from_record(hold) if hold else None,
        payouts=[PayoutResponse.from_record(p) for p in payouts],
    )
