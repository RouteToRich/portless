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

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from shared.bus import get_publisher
from shared.connectors.escrow import EscrowComConnector, TazapayEscrowConnector
from shared.connectors.payments import ConnectorError, PayoneerConnector, StripeTreasuryConnector
from shared.domain.enums import DealStatus
from shared.domain.events import DomainEvent, EventTopic
from shared.domain.state_machine import can_transition

from .config import settings
from .repository import get_repository
from .schemas import (
    AcceptEscrowRequest,
    CreateEscrowRequest,
    DealEscrowSummary,
    DealLedger,
    DisputeEscrowRequest,
    EscrowFundedWebhookPayload,
    EscrowHoldRecord,
    EscrowHoldRequest,
    EscrowResponse,
    PayoutRecord,
    PayoutRequest,
    PayoutResponse,
    PayoutWebhookPayload,
    RefundRequest,
    ReleaseTrancheRequest,
    TrancheInput,
    TrancheSpec,
)

# Default staged-release schedule (pcts sum to 1.0). See target spec §4.1.
DEFAULT_TRANCHES: list[tuple[str, str, float]] = [
    ("T1", "PO_ISSUED", 0.30),  # production advance
    ("T2", "QC_PASSED", 0.40),  # quality cleared (buyer-opted QC team CRF)
    ("T3", "CLEARED", 0.20),    # exported (BL + customs LEO)
    ("T4", "ACCEPTED", 0.10),   # delivered / accepted (+ PortLess margin)
]

# Tolerance (major currency units) for funding / reconciliation comparisons.
_FUNDING_TOLERANCE = 0.01


def _normalize_funded_amount(provider: str, amount: float | int | None) -> float | None:
    """Normalize a webhook-reported funded amount to **major** units.

    Stripe reports amounts in **minor** units (cents); the neutral-escrow
    providers report **major** units. Returns None when the webhook omits it.
    """
    if amount is None:
        return None
    if provider in ("stripe", "stripe_treasury"):
        return round(amount / 100.0, 2)
    return round(float(amount), 2)

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

def _payment_connector(provider: str):
    """Resolve any money-rail connector by provider name.

    Covers the legacy Stripe/Payoneer rails **and** the neutral staged-escrow
    providers (Tazapay, Escrow.com). All implement ``book`` + ``webhook_verify``;
    the escrow ones additionally implement ``release`` / ``refund``.
    """
    if provider in ("stripe", "stripe_treasury"):
        return StripeTreasuryConnector()
    if provider == "payoneer":
        return PayoneerConnector()
    if provider == "tazapay":
        return TazapayEscrowConnector()
    if provider == "escrow_com":
        return EscrowComConnector()
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
    funded_amount = _normalize_funded_amount(hold.provider, payload.amount)
    update: dict[str, Any] = {
        "status": "FUNDED",
        "funded_at": now,
        "updated_at": now,
        "provider_payload": {**hold.provider_payload, "funded_event": payload.model_dump()},
    }
    if funded_amount is not None:
        update["funded_amount"] = funded_amount
    updated = hold.model_copy(update=update)
    await repo.save_hold(updated)

    try:
        await _transition_deal(hold.deal_id, "ESCROW_FUNDED", f"Escrow funded — ref {payload.ext_ref}")
    except Exception as exc:
        logger.error("EscrowGuard: ESCROW_FUNDED transition failed for deal %s: %s", hold.deal_id, exc)

    # Reconcile the received amount against the requested principal.
    if funded_amount is not None and abs(funded_amount - hold.amount) > _FUNDING_TOLERANCE:
        logger.warning(
            "EscrowGuard: funding mismatch for deal %s — expected %.2f, received %.2f %s",
            hold.deal_id, hold.amount, funded_amount, hold.currency,
        )
        await _emit(EventTopic.DEAL_EXCEPTION, hold.deal_id, {
            "source": "escrow_guard",
            "escrow_id": hold.escrow_id,
            "reason": "funding_amount_mismatch",
            "expected": hold.amount,
            "received": funded_amount,
            "currency": hold.currency,
            "action_required": "review_funding_amount",
        })

    await _emit(EventTopic.ESCROW_FUNDED, hold.deal_id, {
        "escrow_id": hold.escrow_id,
        "ext_ref": payload.ext_ref,
        "amount": hold.amount,
        "funded_amount": funded_amount,
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
        await _patch_deal_realized_margin(payout.deal_id)
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
# Target model: neutral, staged-release escrow (Tazapay / Escrow.com)
# ---------------------------------------------------------------------------

def _build_tranches(amount: float, schedule: list[TrancheInput] | None) -> list[TrancheSpec]:
    src = schedule or [
        TrancheInput(tranche_id=t, trigger=trg, pct=pct) for (t, trg, pct) in DEFAULT_TRANCHES
    ]
    return [
        TrancheSpec(
            tranche_id=t.tranche_id,
            trigger=t.trigger,
            pct=t.pct,
            amount=round(amount * t.pct, 2),
        )
        for t in src
    ]


async def create_escrow(req: CreateEscrowRequest) -> EscrowResponse:
    """Open a neutral, staged-release escrow with the buyer's chosen provider.

    Pre-condition: deal in ``PI_ACCEPTED`` (opens escrow → ``ESCROW_PENDING``) or
    already ``ESCROW_PENDING``. Idempotent on ``idempotency_key`` and one active
    escrow per deal. The funds sit in the provider's segregated account — PortLess
    never holds the principal (see target spec §1, §5).
    """
    repo = get_repository()

    if req.idempotency_key:
        existing = await repo.get_hold_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return EscrowResponse.from_record(existing)

    existing_by_deal = await repo.get_hold_by_deal(req.deal_id)
    if existing_by_deal and existing_by_deal.status not in ("FAILED",):
        return EscrowResponse.from_record(existing_by_deal)

    deal = await _fetch_deal(req.deal_id)
    deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    if deal_status not in ("PI_ACCEPTED", "ESCROW_PENDING"):
        raise InvalidDealState(req.deal_id, deal_status, "PI_ACCEPTED")

    now = datetime.now(timezone.utc)
    escrow_id = _new_escrow_id()
    tranches = _build_tranches(req.amount, req.tranche_schedule)

    # PENDING record before the connector call (crash-safe audit trace).
    record = EscrowHoldRecord(
        escrow_id=escrow_id,
        deal_id=req.deal_id,
        provider=req.provider,
        amount=req.amount,
        currency=req.currency.upper(),
        status="PENDING",
        idempotency_key=req.idempotency_key,
        payment_method=req.payment_method,
        qc_provider=req.qc_provider,
        tranches=tranches,
        created_at=now,
        updated_at=now,
    )
    await repo.save_hold(record)

    connector = _payment_connector(req.provider)
    try:
        result = await connector.book({
            "amount": req.amount,
            "currency": req.currency.upper(),
            "reference": req.deal_id,
            "description": f"PortLess escrow for deal {req.deal_id}",
        })
    except ConnectorError as exc:
        failed = record.model_copy(update={"status": "FAILED", "updated_at": datetime.now(timezone.utc)})
        await repo.save_hold(failed)
        raise ConnectorCallError(f"Escrow open failed for deal {req.deal_id}: {exc}") from exc

    updated = record.model_copy(update={
        "ext_ref": result.get("ext_ref"),
        "checkout_url": result.get("checkout_url"),
        "provider_payload": result,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save_hold(updated)

    if deal_status == "PI_ACCEPTED":
        try:
            await _transition_deal(req.deal_id, "ESCROW_PENDING", f"Escrow opened with {req.provider}")
        except Exception as exc:
            logger.error("EscrowGuard: ESCROW_PENDING transition failed for deal %s: %s", req.deal_id, exc)

    logger.info(
        "EscrowGuard: escrow %s opened for deal %s via %s (ext_ref=%s)",
        escrow_id, req.deal_id, req.provider, updated.ext_ref,
    )
    return EscrowResponse.from_record(updated)


async def release_tranche(escrow_id: str, req: ReleaseTrancheRequest) -> EscrowResponse:
    """Release one tranche to PortLess after its independent proof is authenticated.

    Pre-condition: escrow ``FUNDED``. Idempotent per tranche. The caller attaches
    the authenticated proof refs (CRF hash, shipping-bill no., BL) — the release
    is authorized by that proof, not by PortLess's word (target spec §5.4).
    """
    repo = get_repository()
    hold = await repo.get_hold(escrow_id)
    if hold is None:
        raise HoldNotFoundError(escrow_id)
    if hold.status not in ("FUNDED", "RELEASED"):
        raise InvalidDealState(hold.deal_id, f"escrow={hold.status}", "escrow=FUNDED")
    if hold.disputed:
        raise InvalidDealState(hold.deal_id, "disputed", "not_disputed")

    idx = next((i for i, t in enumerate(hold.tranches) if t.tranche_id == req.tranche_id), None)
    if idx is None:
        raise HoldNotFoundError(f"{escrow_id}:tranche:{req.tranche_id}")
    tranche = hold.tranches[idx]
    if tranche.status == "RELEASED":  # idempotent
        return EscrowResponse.from_record(hold)

    # Release authority: the final (ACCEPTED-trigger) tranche is NOT releasable by
    # a PortLess proof call — it needs buyer acceptance or an inspection-window
    # lapse (see accept_escrow / check_inspection_windows). This prevents PortLess
    # from self-releasing the last slice.
    if tranche.trigger == "ACCEPTED":
        raise InvalidDealState(
            hold.deal_id, "final tranche (ACCEPTED)", "buyer_accept_or_inspection_window"
        )

    amount = tranche.amount if tranche.amount is not None else round(hold.amount * tranche.pct, 2)

    # Reconciliation guard: never release more than the held principal.
    if round(hold.released_total + amount + hold.refunded_total, 2) > hold.amount + _FUNDING_TOLERANCE:
        raise InvalidDealState(
            hold.deal_id,
            f"release would over-disburse (released {hold.released_total} + {amount} + refunded {hold.refunded_total})",
            f"<= principal {hold.amount}",
        )

    connector = _payment_connector(hold.provider)
    try:
        result = await connector.release({  # type: ignore[union-attr]
            "ext_ref": hold.ext_ref,
            "amount": amount,
            "tranche_id": tranche.tranche_id,
            "proof_refs": req.proof_refs,
            "note": f"Release {tranche.tranche_id} ({tranche.trigger}) for deal {hold.deal_id}",
        })
    except ConnectorError as exc:
        raise ConnectorCallError(
            f"Tranche release failed for {escrow_id}/{req.tranche_id}: {exc}"
        ) from exc

    now = datetime.now(timezone.utc)
    new_tranches = list(hold.tranches)
    new_tranches[idx] = tranche.model_copy(update={
        "status": "RELEASED",
        "proof_refs": req.proof_refs or tranche.proof_refs,
        "ext_ref": result.get("ext_ref") or result.get("tranche_id"),
        "released_at": now,
    })
    released_total = round(sum(t.amount or 0.0 for t in new_tranches if t.status == "RELEASED"), 2)
    all_released = all(t.status == "RELEASED" for t in new_tranches)

    # Open the buyer-acceptance window once the only remaining tranches are the
    # final ACCEPTED-trigger ones (i.e. all proof-gated tranches are released).
    inspection_deadline = hold.inspection_deadline
    remaining_pending = [t for t in new_tranches if t.status == "PENDING"]
    if remaining_pending and all(t.trigger == "ACCEPTED" for t in remaining_pending) and inspection_deadline is None:
        inspection_deadline = now + timedelta(hours=settings.inspection_window_hours)

    updated = hold.model_copy(update={
        "tranches": new_tranches,
        "released_total": released_total,
        "status": "RELEASED" if all_released else hold.status,
        "released_at": now if all_released else hold.released_at,
        "inspection_deadline": inspection_deadline,
        "updated_at": now,
    })
    await repo.save_hold(updated)

    await _emit(EventTopic.PAYOUT_REQUESTED, hold.deal_id, {
        "escrow_id": escrow_id,
        "tranche_id": tranche.tranche_id,
        "trigger": tranche.trigger,
        "amount": amount,
        "released_total": released_total,
        "provider": hold.provider,
        "proof_refs": req.proof_refs,
    })
    logger.info(
        "EscrowGuard: released tranche %s (%.2f %s) for deal %s (escrow=%s, total=%.2f)",
        tranche.tranche_id, amount, hold.currency, hold.deal_id, escrow_id, released_total,
    )
    return EscrowResponse.from_record(updated)


async def refund(escrow_id: str, req: RefundRequest) -> EscrowResponse:
    """Refund the **undisbursed** balance to the buyer (dispute / failure path).

    Already-released tranches cannot be clawed back — only ``amount -
    released_total`` is refundable (target spec §6.4).
    """
    repo = get_repository()
    hold = await repo.get_hold(escrow_id)
    if hold is None:
        raise HoldNotFoundError(escrow_id)
    if hold.status == "REFUNDED":  # idempotent
        return EscrowResponse.from_record(hold)
    if hold.status not in ("FUNDED", "RELEASED"):
        raise InvalidDealState(hold.deal_id, f"escrow={hold.status}", "escrow=FUNDED")

    undisbursed = round(hold.amount - hold.released_total, 2)
    connector = _payment_connector(hold.provider)
    try:
        result = await connector.refund({  # type: ignore[union-attr]
            "ext_ref": hold.ext_ref,
            "amount": undisbursed,
            "reason": req.reason,
        })
    except ConnectorError as exc:
        raise ConnectorCallError(f"Escrow refund failed for {escrow_id}: {exc}") from exc

    now = datetime.now(timezone.utc)
    updated = hold.model_copy(update={
        "status": "REFUNDED",
        "refunded_total": undisbursed,
        "updated_at": now,
        "provider_payload": {**hold.provider_payload, "refund": result},
    })
    await repo.save_hold(updated)

    await _emit(EventTopic.DEAL_EXCEPTION, hold.deal_id, {
        "source": "escrow_guard",
        "escrow_id": escrow_id,
        "refunded_amount": undisbursed,
        "reason": req.reason,
        "action_required": "escrow_refunded_to_buyer",
    })
    logger.info(
        "EscrowGuard: refunded %.2f (undisbursed) for deal %s (escrow=%s)",
        undisbursed, hold.deal_id, escrow_id,
    )
    return EscrowResponse.from_record(updated)


# ---------------------------------------------------------------------------
# Final-tranche release authority: buyer acceptance + inspection window
# ---------------------------------------------------------------------------

async def _release_final_tranches(hold: EscrowHoldRecord, note: str) -> tuple[list[TrancheSpec], float, bool]:
    """Release every PENDING ``ACCEPTED``-trigger tranche. Returns
    (new_tranches, released_total, all_released). Raises ConnectorCallError on
    provider failure."""
    connector = _payment_connector(hold.provider)
    now = datetime.now(timezone.utc)
    new_tranches = list(hold.tranches)
    for i, t in enumerate(new_tranches):
        if t.trigger == "ACCEPTED" and t.status == "PENDING":
            amount = t.amount if t.amount is not None else round(hold.amount * t.pct, 2)
            try:
                result = await connector.release({  # type: ignore[union-attr]
                    "ext_ref": hold.ext_ref,
                    "amount": amount,
                    "tranche_id": t.tranche_id,
                    "note": note,
                })
            except ConnectorError as exc:
                raise ConnectorCallError(
                    f"Final release failed for {hold.escrow_id}/{t.tranche_id}: {exc}"
                ) from exc
            new_tranches[i] = t.model_copy(update={
                "status": "RELEASED",
                "ext_ref": result.get("ext_ref") or result.get("tranche_id"),
                "released_at": now,
            })
    released_total = round(sum((t.amount or 0.0) for t in new_tranches if t.status == "RELEASED"), 2)
    all_released = all(t.status == "RELEASED" for t in new_tranches)
    return new_tranches, released_total, all_released


async def accept_escrow(escrow_id: str, req: AcceptEscrowRequest) -> EscrowResponse:
    """Buyer acceptance → releases the final ``ACCEPTED``-trigger tranche(s).

    This is the authority for the last slice — PortLess cannot release it via
    ``release_tranche``. Escrow must be FUNDED and not under dispute. Idempotent
    once accepted.
    """
    repo = get_repository()
    hold = await repo.get_hold(escrow_id)
    if hold is None:
        raise HoldNotFoundError(escrow_id)
    if hold.disputed:
        raise InvalidDealState(hold.deal_id, "disputed", "not_disputed")
    if hold.status not in ("FUNDED", "RELEASED"):
        raise InvalidDealState(hold.deal_id, f"escrow={hold.status}", "escrow=FUNDED")

    pending_final = [t for t in hold.tranches if t.trigger == "ACCEPTED" and t.status == "PENDING"]
    if not pending_final and hold.buyer_accepted_at is not None:  # idempotent
        return EscrowResponse.from_record(hold)

    now = datetime.now(timezone.utc)
    new_tranches, released_total, all_released = await _release_final_tranches(
        hold, f"Buyer acceptance ({req.accepted_by or 'buyer'}) for deal {hold.deal_id}"
    )
    updated = hold.model_copy(update={
        "tranches": new_tranches,
        "released_total": released_total,
        "status": "RELEASED" if all_released else hold.status,
        "released_at": now if all_released else hold.released_at,
        "buyer_accepted_at": now,
        "updated_at": now,
    })
    await repo.save_hold(updated)

    await _emit(EventTopic.PAYOUT_REQUESTED, hold.deal_id, {
        "escrow_id": escrow_id,
        "event": "buyer_accepted",
        "released_total": released_total,
        "accepted_by": req.accepted_by,
    })
    logger.info("EscrowGuard: buyer accepted escrow %s (deal %s); released_total=%.2f",
                escrow_id, hold.deal_id, released_total)
    return EscrowResponse.from_record(updated)


async def dispute_escrow(escrow_id: str, req: DisputeEscrowRequest) -> EscrowResponse:
    """Buyer raises a dispute within the window — blocks any auto/manual release.

    Resolution then proceeds via ``refund`` (undisbursed → buyer) or, once
    resolved in PortLess's favour, ``accept_escrow`` after the flag is cleared.
    """
    repo = get_repository()
    hold = await repo.get_hold(escrow_id)
    if hold is None:
        raise HoldNotFoundError(escrow_id)
    if hold.status not in ("FUNDED", "RELEASED"):
        raise InvalidDealState(hold.deal_id, f"escrow={hold.status}", "escrow=FUNDED")

    now = datetime.now(timezone.utc)
    updated = hold.model_copy(update={"disputed": True, "disputed_at": now, "updated_at": now})
    await repo.save_hold(updated)

    await _emit(EventTopic.DEAL_EXCEPTION, hold.deal_id, {
        "source": "escrow_guard",
        "escrow_id": escrow_id,
        "reason": req.reason,
        "raised_by": req.raised_by,
        "action_required": "resolve_dispute_then_refund_or_release",
    })
    logger.info("EscrowGuard: dispute raised on escrow %s (deal %s): %s",
                escrow_id, hold.deal_id, req.reason)
    return EscrowResponse.from_record(updated)


async def check_inspection_windows(now: datetime | None = None) -> int:
    """Auto-release final tranches whose inspection window has lapsed with no
    dispute (silence = consent). Returns the number of escrows auto-released.
    Safe to call repeatedly (idempotent per escrow)."""
    repo = get_repository()
    now = now or datetime.now(timezone.utc)
    released = 0
    for hold in await repo.list_holds():
        if hold.status != "FUNDED" or hold.disputed or not hold.inspection_deadline:
            continue
        deadline = hold.inspection_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline > now:
            continue
        if not any(t.trigger == "ACCEPTED" and t.status == "PENDING" for t in hold.tranches):
            continue
        try:
            new_tranches, released_total, all_released = await _release_final_tranches(
                hold, f"Auto-release: inspection window lapsed for deal {hold.deal_id}"
            )
        except ConnectorCallError as exc:
            logger.warning("EscrowGuard: window auto-release failed for %s: %s", hold.escrow_id, exc)
            continue
        updated = hold.model_copy(update={
            "tranches": new_tranches,
            "released_total": released_total,
            "status": "RELEASED" if all_released else hold.status,
            "released_at": now if all_released else hold.released_at,
            "buyer_accepted_at": now,
            "updated_at": now,
        })
        await repo.save_hold(updated)
        await _emit(EventTopic.PAYOUT_REQUESTED, hold.deal_id, {
            "escrow_id": hold.escrow_id,
            "event": "auto_release_window_lapsed",
            "released_total": released_total,
        })
        logger.info("EscrowGuard: auto-released final tranche for %s (deal %s) — window lapsed",
                    hold.escrow_id, hold.deal_id)
        released += 1
    return released


async def run_inspection_watcher() -> None:
    """Background loop: each tick (a) auto-releases final tranches whose inspection
    window lapsed, and (b) sweeps every hold for ledger-reconciliation breaches.

    Started from the service lifespan; cancelled on shutdown.
    """
    interval = settings.inspection_watcher_interval_seconds
    logger.info("EscrowGuard: escrow watcher started (every %ss)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await check_inspection_windows()
            await check_reconciliation()
        except asyncio.CancelledError:
            logger.info("EscrowGuard: escrow watcher stopped")
            break
        except Exception as exc:  # never let the watcher die on a transient error
            logger.warning("EscrowGuard: watcher tick failed: %s", exc)


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


def _ledger_issues(hold: EscrowHoldRecord) -> tuple[float, list[str]]:
    """Return (outstanding, issues) for a hold's money ledger. ``issues`` is empty
    when the hold reconciles. Shared by ``get_deal_ledger`` and the sweep."""
    outstanding = round(hold.amount - hold.released_total - hold.refunded_total, 2)
    issues: list[str] = []
    if hold.released_total + hold.refunded_total > hold.amount + _FUNDING_TOLERANCE:
        issues.append(
            f"over-disbursed: released {hold.released_total} + refunded {hold.refunded_total} "
            f"> principal {hold.amount}"
        )
    if outstanding < -_FUNDING_TOLERANCE:
        issues.append(f"negative outstanding balance: {outstanding}")
    if hold.funded_amount is not None and abs(hold.funded_amount - hold.amount) > _FUNDING_TOLERANCE:
        issues.append(f"funded {hold.funded_amount} != principal {hold.amount}")
    tranche_sum = round(sum((t.amount or 0.0) for t in hold.tranches if t.status == "RELEASED"), 2)
    if abs(tranche_sum - hold.released_total) > _FUNDING_TOLERANCE:
        issues.append(f"released tranche sum {tranche_sum} != released_total {hold.released_total}")
    return outstanding, issues


async def _patch_deal_realized_margin(deal_id: str) -> None:
    """Write realized_margin_usd onto the deal when a supplier payout completes.

    Realized margin = what the buyer funded into escrow − everything paid out
    to the supplier. Fills the Deal Board's "Realized Margin" metric card,
    which otherwise stays empty even on PAID deals. Best-effort: a failure
    here must never affect the payout webhook's own processing."""
    try:
        repo = get_repository()
        hold = await repo.get_hold_by_deal(deal_id)
        if hold is None:
            return
        payouts = await repo.list_payouts_by_deal(deal_id)
        payouts_total = round(sum(p.amount for p in payouts if p.status == "COMPLETED"), 2)
        funded = hold.funded_amount if hold.funded_amount is not None else hold.amount
        realized = round(funded - payouts_total, 2)

        from services.deals.schemas import UpdateFinancialsRequest  # type: ignore[import]
        from services.deals.service import update_financials  # type: ignore[import]

        await update_financials(deal_id, UpdateFinancialsRequest(realized_margin_usd=realized))
        logger.info("EscrowGuard: realized_margin_usd=%.2f patched onto deal %s", realized, deal_id)
    except Exception as exc:
        logger.warning(
            "EscrowGuard: could not patch realized margin for deal %s: %s", deal_id, exc
        )


async def get_deal_ledger(deal_id: str) -> DealLedger:
    """Money ledger for one trade + a reconciliation check.

    Verifies: released + refunded do not exceed the principal, the outstanding
    balance is non-negative, released tranche amounts sum to ``released_total``,
    and (if known) the funded amount matches the principal. Any breach lands in
    ``issues`` and flips ``reconciled`` to False.
    """
    repo = get_repository()
    hold = await repo.get_hold_by_deal(deal_id)
    payouts = await repo.list_payouts_by_deal(deal_id)
    payouts_total = round(sum(p.amount for p in payouts if p.status == "COMPLETED"), 2)

    if hold is None:
        return DealLedger(
            deal_id=deal_id, currency="", payouts_total=payouts_total, reconciled=True,
        )

    outstanding, issues = _ledger_issues(hold)
    return DealLedger(
        deal_id=deal_id,
        currency=hold.currency,
        provider=hold.provider,
        principal=hold.amount,
        funded_amount=hold.funded_amount,
        released_total=hold.released_total,
        refunded_total=hold.refunded_total,
        outstanding=outstanding,
        payouts_total=payouts_total,
        tranches=hold.tranches,
        reconciled=(len(issues) == 0),
        issues=issues,
    )


async def check_reconciliation() -> int:
    """Sweep every hold and emit a `deal.exception` **once** for any that fails its
    ledger invariant; reset the flag when a hold reconciles again. Returns the
    number newly flagged this pass. Safe to call repeatedly (idempotent)."""
    repo = get_repository()
    now = datetime.now(timezone.utc)
    flagged = 0
    for hold in await repo.list_holds():
        _, issues = _ledger_issues(hold)
        if issues and not hold.reconciliation_flagged:
            await repo.save_hold(hold.model_copy(update={
                "reconciliation_flagged": True, "updated_at": now,
            }))
            await _emit(EventTopic.DEAL_EXCEPTION, hold.deal_id, {
                "source": "escrow_guard",
                "escrow_id": hold.escrow_id,
                "reason": "ledger_unreconciled",
                "issues": issues,
                "action_required": "review_escrow_ledger",
            })
            logger.warning(
                "EscrowGuard: unreconciled ledger for deal %s (escrow=%s): %s",
                hold.deal_id, hold.escrow_id, issues,
            )
            flagged += 1
        elif not issues and hold.reconciliation_flagged:
            await repo.save_hold(hold.model_copy(update={
                "reconciliation_flagged": False, "updated_at": now,
            }))
    return flagged


async def list_escrow_providers() -> list[dict[str, Any]]:
    """Supported neutral-escrow providers + the pay-in methods each offers.

    Powers the buyer's choice on the PI (DealMaker §3.1). ``default`` marks the
    configured fallback (``escrow_default_provider``).
    """
    out: list[dict[str, Any]] = []
    for name in ("tazapay", "escrow_com"):
        conn = _payment_connector(name)
        methods = await conn.list_payment_methods()  # type: ignore[union-attr]
        out.append({
            "provider": name,
            "payment_methods": methods,
            "default": name == settings.escrow_default_provider,
        })
    return out
