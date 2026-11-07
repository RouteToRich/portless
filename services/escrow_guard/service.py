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
    + replay protection on provider ``event_id``
  • Funding shortfall: a provider total below the principal keeps the escrow
    ``PARTIALLY_FUNDED`` and the deal in ESCROW_PENDING (no PO against money
    that is not there)
  • Tranches release strictly in schedule order, never while the deal is in
    DISPUTE / CANCELLED / REFUNDED, never above the principal
  • Supplier payouts never exceed the net funds released from escrow; the deal
    is PAID only once the supplier received the full PO value
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

# Deal statuses in which no money may leave the escrow towards PortLess.
_RELEASE_BLOCKING_DEAL_STATUSES = {"DISPUTE", "CANCELLED", "REFUNDED", "QC_FAILED"}

# Provider fee (% of each release) when the connector cannot quote one.
_DEFAULT_FEE_PCT = {"tazapay": 1.0, "escrow_com": 0.89, "stripe": 2.9, "stripe_treasury": 2.9}


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
        fee_pct=await _provider_fee_pct(provider),
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

    # Replay protection — a provider re-delivering the same event is acknowledged, not re-applied.
    if payload.event_id and payload.event_id in hold.processed_events:
        logger.info("EscrowGuard: funded webhook event %s already applied — ignored", payload.event_id)
        return {"status": "ok", "escrow_id": hold.escrow_id, "deal_id": hold.deal_id, "replayed": True}

    # Idempotent — already funded (or past funding)
    if hold.status in ("FUNDED", "RELEASED", "REFUNDED"):
        return {"status": "ok", "escrow_id": hold.escrow_id, "deal_id": hold.deal_id}

    now = datetime.now(timezone.utc)
    funded_amount = _normalize_funded_amount(hold.provider, payload.amount)
    events = list(hold.processed_events) + ([payload.event_id] if payload.event_id else [])
    base_update: dict[str, Any] = {
        "updated_at": now,
        "processed_events": events,
        "provider_payload": {**hold.provider_payload, "funded_event": payload.model_dump()},
    }

    # Funding shortfall: the provider holds less than the Proforma Invoice total.
    # Money that is not there cannot back a PO — keep the deal waiting, tell the
    # buyer the balance, and raise an exception so the operator sees it.
    if funded_amount is not None and funded_amount < hold.amount - _FUNDING_TOLERANCE:
        shortfall = round(hold.amount - funded_amount, 2)
        updated = hold.model_copy(update={
            **base_update,
            "status": "PARTIALLY_FUNDED",
            "funded_amount": funded_amount,
            "shortfall": shortfall,
        })
        await repo.save_hold(updated)
        logger.warning(
            "EscrowGuard: funding shortfall for deal %s — expected %.2f, received %.2f %s (short %.2f)",
            hold.deal_id, hold.amount, funded_amount, hold.currency, shortfall,
        )
        await _emit(EventTopic.DEAL_EXCEPTION, hold.deal_id, {
            "source": "escrow_guard",
            "escrow_id": hold.escrow_id,
            "reason": "funding_shortfall",
            "category": "PAYMENT_FAILED",
            "expected": hold.amount,
            "received": funded_amount,
            "shortfall": shortfall,
            "currency": hold.currency,
            "action_required": "buyer_must_fund_balance",
        })
        await _emit(EventTopic.ESCROW_FUNDING_SHORTFALL, hold.deal_id, {
            "escrow_id": hold.escrow_id,
            "expected": hold.amount,
            "received": funded_amount,
            "shortfall": shortfall,
            "currency": hold.currency,
            "checkout_url": hold.checkout_url,
        })
        await _notify_funding_shortfall(updated)
        return {"status": "shortfall", "escrow_id": hold.escrow_id, "deal_id": hold.deal_id,
                "shortfall": shortfall}

    update: dict[str, Any] = {
        **base_update,
        "status": "FUNDED",
        "funded_at": now,
        "shortfall": None,
    }
    if funded_amount is not None:
        update["funded_amount"] = funded_amount
    updated = hold.model_copy(update=update)
    await repo.save_hold(updated)

    try:
        await _transition_deal(hold.deal_id, "ESCROW_FUNDED", f"Escrow funded — ref {payload.ext_ref}")
    except Exception as exc:
        logger.error("EscrowGuard: ESCROW_FUNDED transition failed for deal %s: %s", hold.deal_id, exc)

    # Over-funding is not a blocker (the surplus is refundable) but it is a
    # ledger mismatch the operator must see.
    if funded_amount is not None and funded_amount > hold.amount + _FUNDING_TOLERANCE:
        logger.warning(
            "EscrowGuard: over-funding for deal %s — expected %.2f, received %.2f %s",
            hold.deal_id, hold.amount, funded_amount, hold.currency,
        )
        await _emit(EventTopic.DEAL_EXCEPTION, hold.deal_id, {
            "source": "escrow_guard",
            "escrow_id": hold.escrow_id,
            "reason": "funding_amount_mismatch",
            "expected": hold.amount,
            "received": funded_amount,
            "currency": hold.currency,
            "action_required": "review_funding_amount_refund_surplus",
        })

    await _emit(EventTopic.ESCROW_FUNDED, hold.deal_id, {
        "escrow_id": hold.escrow_id,
        "ext_ref": payload.ext_ref,
        "amount": hold.amount,
        "funded_amount": funded_amount,
        "currency": hold.currency,
        "payer_name": payload.payer_name,
        "payer_country": payload.payer_country,
    })

    logger.info("EscrowGuard: hold %s funded for deal %s", hold.escrow_id, hold.deal_id)
    return {"status": "ok", "escrow_id": hold.escrow_id, "deal_id": hold.deal_id}


async def _notify_funding_shortfall(hold: EscrowHoldRecord) -> None:
    """Best-effort buyer notice with the outstanding balance (templated e-mail)."""
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        from shared.orchestration import notifications
        deal = await get_deal(hold.deal_id)
        await notifications.notify_funding_shortfall(deal, hold)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EscrowGuard: shortfall notice for deal %s not sent: %s", hold.deal_id, exc)


async def _provider_fee_pct(provider: str) -> float | None:
    """Fee the provider takes on each release (from its quote, else a documented default)."""
    try:
        connector = _payment_connector(provider)
        quote = await connector.quote({})
        fee = quote.get("fee_pct") if isinstance(quote, dict) else None
        if fee is not None:
            return float(fee)
    except Exception as exc:  # noqa: BLE001
        logger.debug("EscrowGuard: fee quote for %s unavailable: %s", provider, exc)
    return _DEFAULT_FEE_PCT.get(provider)


def _fee_on(hold: EscrowHoldRecord, amount: float) -> float:
    return round(amount * (hold.fee_pct or 0.0) / 100.0, 2)


async def _deal_status(deal_id: str) -> str | None:
    """Best-effort deal status (None when the deal service is unreachable)."""
    try:
        deal = await _fetch_deal(deal_id)
        return deal.status.value if hasattr(deal.status, "value") else str(deal.status)
    except Exception:  # noqa: BLE001
        return None


async def _supplier_owed(deal_id: str) -> float | None:
    """What the supplier is owed in total — the Purchase Order value (best-effort)."""
    try:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        po = await _dm_repo().get_po_by_deal(deal_id)
        if po is not None and po.total_value:
            return round(float(po.total_value), 2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("EscrowGuard: PO lookup failed for %s: %s", deal_id, exc)
    return None


def _net_received(hold: EscrowHoldRecord) -> float:
    """Cash PortLess holds from this escrow: released tranches net of fees, or —
    for a legacy Stripe hold (no tranches) — the funded principal."""
    if hold.tranches:
        return round(hold.released_total - hold.fees_total, 2)
    if hold.status in ("FUNDED", "RELEASED"):
        return round(hold.funded_amount if hold.funded_amount is not None else hold.amount, 2)
    return 0.0


def _payout_totals(payouts: list[PayoutRecord]) -> tuple[float, float]:
    """→ (completed_total, pending_total) over non-failed payouts."""
    completed = round(sum(p.amount for p in payouts if p.status == "COMPLETED"), 2)
    pending = round(sum(p.amount for p in payouts if p.status in ("REQUESTED", "PROCESSING")), 2)
    return completed, pending


async def _settle_if_complete(hold: EscrowHoldRecord, now: datetime) -> EscrowHoldRecord:
    """When every tranche is RELEASED: stamp ``settled_at``, emit ESCROW_SETTLED,
    refresh the deal's realised margin. Idempotent."""
    if hold.settled_at is not None or not hold.tranches:
        return hold
    if not all(t.status == "RELEASED" for t in hold.tranches):
        return hold
    repo = get_repository()
    settled = hold.model_copy(update={"settled_at": now, "updated_at": now})
    await repo.save_hold(settled)
    owed = await _supplier_owed(hold.deal_id)
    completed, pending = _payout_totals(await repo.list_payouts_by_deal(hold.deal_id))
    await _emit(EventTopic.ESCROW_SETTLED, hold.deal_id, {
        "escrow_id": hold.escrow_id,
        "provider": hold.provider,
        "principal": hold.amount,
        "released_total": settled.released_total,
        "fees_total": settled.fees_total,
        "net_to_portless": _net_received(settled),
        "supplier_owed": owed,
        "supplier_paid": completed,
        "supplier_pending": pending,
        "supplier_balance_due": round(owed - completed - pending, 2) if owed is not None else None,
        "currency": hold.currency,
    })
    await _patch_deal_realized_margin(hold.deal_id)
    logger.info("EscrowGuard: escrow %s settled for deal %s (released %.2f, fees %.2f)",
                hold.escrow_id, hold.deal_id, settled.released_total, settled.fees_total)
    return settled


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
    # The main payout needs cleared cargo; the balance payout follows the final
    # tranche and may land after the deal is already PAID-on-paper (CLEARED/PAID).
    allowed = ("CLEARED", "PAID") if req.kind == "supplier_balance" else ("CLEARED",)
    if deal_status not in allowed:
        raise InvalidDealState(req.deal_id, deal_status, "/".join(allowed))

    # Verify funded escrow exists (RELEASED = every tranche already left the
    # escrow towards PortLess — the money is there to pay the supplier from).
    hold = await repo.get_hold_by_deal(req.deal_id)
    if hold is None or hold.status not in ("FUNDED", "RELEASED"):
        raise InvalidDealState(
            req.deal_id,
            f"escrow={hold.status if hold else 'none'}",
            "escrow=FUNDED",
        )
    if hold.disputed:
        raise InvalidDealState(req.deal_id, "escrow disputed", "not_disputed")

    # Never pay out money PortLess has not received from the escrow.
    completed, pending = _payout_totals(await repo.list_payouts_by_deal(req.deal_id))
    available = round(_net_received(hold) - completed - pending, 2)
    if req.amount > available + _FUNDING_TOLERANCE:
        raise InvalidDealState(
            req.deal_id,
            f"payout {req.amount:.2f} exceeds funds released from escrow (available {available:.2f})",
            "amount <= available_for_payout",
        )
    if req.amount <= 0:
        raise InvalidDealState(req.deal_id, "payout amount <= 0", "amount > 0")

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
        kind=req.kind,
        total_owed=req.total_owed,
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

    # Legacy Stripe hold (no tranche ledger): the payout IS the release.
    # A staged escrow keeps its own tranche ledger — the hold status is driven
    # by tranche releases, never by a payout.
    if not hold.tranches:
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
        "kind": req.kind,
        "total_owed": req.total_owed,
        "available_before": available,
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

    # Replay protection
    if payload.event_id and payload.event_id in payout.processed_events:
        return {"status": "ok", "payout_id": payout.payout_id, "deal_id": payout.deal_id, "replayed": True}

    # Idempotent
    if payout.status == "COMPLETED" and payload.status == "COMPLETED":
        return {"status": "ok", "payout_id": payout.payout_id, "deal_id": payout.deal_id}

    now = datetime.now(timezone.utc)
    events = list(payout.processed_events) + ([payload.event_id] if payload.event_id else [])

    if payload.status == "COMPLETED":
        updated = payout.model_copy(update={
            "status": "COMPLETED",
            "completed_at": now,
            "updated_at": now,
            "processed_events": events,
        })
        await repo.save_payout(updated)

        # PAID means the supplier received everything they are owed — not
        # merely that *a* payout went through. Partial payouts (capped by the
        # escrow releases) leave the deal CLEARED until the balance is paid.
        owed = updated.total_owed if updated.total_owed is not None else await _supplier_owed(payout.deal_id)
        completed, pending = _payout_totals(await repo.list_payouts_by_deal(payout.deal_id))
        fully_paid = owed is None or completed >= owed - _FUNDING_TOLERANCE
        balance_due = round(owed - completed, 2) if owed is not None else None
        if fully_paid:
            if await _deal_status(payout.deal_id) != "PAID":
                try:
                    await _transition_deal(payout.deal_id, "PAID", f"Payout completed — ref {payload.ext_ref}")
                except Exception as exc:
                    logger.error("EscrowGuard: PAID transition failed for deal %s: %s", payout.deal_id, exc)
        else:
            logger.info(
                "EscrowGuard: payout %s completed but supplier still owed %.2f of %.2f for deal %s — "
                "deal stays CLEARED until the balance is paid",
                payout.payout_id, balance_due or 0.0, owed or 0.0, payout.deal_id,
            )
        await _emit(EventTopic.PAYOUT_COMPLETED, payout.deal_id, {
            "payout_id": payout.payout_id,
            "ext_ref": payload.ext_ref,
            "amount": payout.amount,
            "currency": payout.currency,
            "kind": updated.kind,
            "fully_paid": fully_paid,
            "completed_total": completed,
            "pending_total": pending,
            "supplier_owed": owed,
            "balance_due": balance_due,
        })
        await _patch_deal_realized_margin(payout.deal_id)
    elif payload.status == "FAILED":
        updated = payout.model_copy(update={
            "status": "FAILED",
            "updated_at": now,
            "processed_events": events,
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
            "processed_events": events,
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
        fee_pct=await _provider_fee_pct(req.provider),
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

    # A deal in dispute / cancelled / refunded keeps its money where it is.
    deal_status = await _deal_status(hold.deal_id)
    if deal_status in _RELEASE_BLOCKING_DEAL_STATUSES:
        raise InvalidDealState(hold.deal_id, deal_status, "deal not in DISPUTE/CANCELLED/REFUNDED/QC_FAILED")

    # Tranches release in schedule order: QC money never leaves before the PO
    # advance did (a rejected/failed earlier gate must be resolved first).
    earlier_pending = [t.tranche_id for t in hold.tranches[:idx] if t.status == "PENDING"]
    if earlier_pending:
        raise InvalidDealState(
            hold.deal_id,
            f"tranche {tranche.tranche_id} requested while {', '.join(earlier_pending)} still PENDING",
            "earlier tranches released first",
        )

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
    # final ACCEPTED-trigger ones (all proof-gated tranches released) AND the
    # trigger is met: legacy "proof" mode opens it here; "delivery" mode only
    # when the goods were already confirmed delivered (otherwise
    # open_inspection_window opens it on the DELIVERED milestone).
    inspection_deadline = hold.inspection_deadline
    remaining_pending = [t for t in new_tranches if t.status == "PENDING"]
    window_opened = False
    trigger_met = _window_trigger() == "proof" or hold.delivery_confirmed_at is not None
    if (remaining_pending and all(t.trigger == "ACCEPTED" for t in remaining_pending)
            and inspection_deadline is None and trigger_met):
        inspection_deadline = now + timedelta(hours=settings.inspection_window_hours)
        window_opened = True

    updated = hold.model_copy(update={
        "tranches": new_tranches,
        "released_total": released_total,
        "fees_total": round(hold.fees_total + _fee_on(hold, amount), 2),
        "status": "RELEASED" if all_released else hold.status,
        "released_at": now if all_released else hold.released_at,
        "inspection_deadline": inspection_deadline,
        "inspection_opened_at": now if window_opened else hold.inspection_opened_at,
        "inspection_trigger": ("delivery" if hold.delivery_confirmed_at else "proof") if window_opened else hold.inspection_trigger,
        "updated_at": now,
    })
    await repo.save_hold(updated)

    next_pending = remaining_pending[0] if remaining_pending else None
    await _emit(EventTopic.ESCROW_TRANCHE_RELEASED, hold.deal_id, {
        "escrow_id": escrow_id,
        "tranche_id": tranche.tranche_id,
        "trigger": tranche.trigger,
        "amount": amount,
        "released_total": released_total,
        "fees_total": updated.fees_total,
        "all_released": all_released,
        "next_pending_tranche": next_pending.tranche_id if next_pending else None,
        "next_pending_trigger": next_pending.trigger if next_pending else None,
        "inspection_window_opened": window_opened,
        "inspection_deadline": inspection_deadline.isoformat() if inspection_deadline else None,
        "provider": hold.provider,
        "proof_refs": req.proof_refs,
        "currency": hold.currency,
    })
    logger.info(
        "EscrowGuard: released tranche %s (%.2f %s) for deal %s (escrow=%s, total=%.2f)",
        tranche.tranche_id, amount, hold.currency, hold.deal_id, escrow_id, released_total,
    )
    updated = await _settle_if_complete(updated, now)
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
    if hold.status not in ("FUNDED", "RELEASED", "PARTIALLY_FUNDED"):
        raise InvalidDealState(hold.deal_id, f"escrow={hold.status}", "escrow=FUNDED")

    received = hold.funded_amount if hold.funded_amount is not None else hold.amount
    undisbursed = round(received - hold.released_total, 2)
    if undisbursed <= _FUNDING_TOLERANCE:
        raise InvalidDealState(
            hold.deal_id,
            f"nothing left to refund (received {received:.2f}, released {hold.released_total:.2f})",
            "undisbursed > 0",
        )
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

    # A refund closes the dispute path: DISPUTE → REFUNDED (terminal). Any other
    # deal status is left alone — the refund is recorded on the hold either way.
    try:
        deal = await _fetch_deal(hold.deal_id)
        deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
        if deal_status == "DISPUTE":
            await _transition_deal(
                hold.deal_id, "REFUNDED",
                f"Escrow {escrow_id} refunded {undisbursed:.2f} {hold.currency} to buyer",
            )
    except Exception as exc:  # noqa: BLE001 — the refund itself already committed
        logger.error("EscrowGuard: REFUNDED transition failed for deal %s: %s", hold.deal_id, exc)

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
# Inspection window trigger (delivery) + dispute clearing
# ---------------------------------------------------------------------------

def _window_trigger() -> str:
    raw = (getattr(settings, "inspection_window_trigger", "delivery") or "delivery").strip().lower()
    return "proof" if raw == "proof" else "delivery"


def _final_amount(hold: EscrowHoldRecord) -> float:
    return round(sum(
        (t.amount if t.amount is not None else hold.amount * t.pct)
        for t in hold.tranches if t.trigger == "ACCEPTED" and t.status == "PENDING"
    ), 2)


async def open_inspection_window(
    escrow_id: str, *, reason: str = "delivery confirmed", trigger: str = "delivery",
    now: datetime | None = None,
) -> EscrowResponse:
    """The goods reached the buyer: start the acceptance window for the final
    tranche(s). Idempotent. If a proof-gated tranche is still PENDING the
    delivery is recorded and the window opens when that tranche releases
    (``release_tranche``)."""
    repo = get_repository()
    hold = await repo.get_hold(escrow_id)
    if hold is None:
        raise HoldNotFoundError(escrow_id)
    now = now or datetime.now(timezone.utc)
    if hold.inspection_deadline is not None:
        return EscrowResponse.from_record(hold)
    if hold.status not in ("FUNDED", "RELEASED"):
        raise InvalidDealState(hold.deal_id, f"escrow={hold.status}", "escrow=FUNDED")
    pending_final = [t for t in hold.tranches if t.trigger == "ACCEPTED" and t.status == "PENDING"]
    if not pending_final:
        return EscrowResponse.from_record(hold)  # nothing left to accept

    earlier_pending = [t.tranche_id for t in hold.tranches if t.trigger != "ACCEPTED" and t.status == "PENDING"]
    if earlier_pending:
        updated = hold.model_copy(update={
            "delivery_confirmed_at": hold.delivery_confirmed_at or now, "updated_at": now,
        })
        await repo.save_hold(updated)
        logger.info(
            "EscrowGuard: delivery recorded for %s (deal %s) — window opens once %s release(s)",
            escrow_id, hold.deal_id, ", ".join(earlier_pending),
        )
        return EscrowResponse.from_record(updated)

    deadline = now + timedelta(hours=settings.inspection_window_hours)
    updated = hold.model_copy(update={
        "delivery_confirmed_at": hold.delivery_confirmed_at or (now if trigger == "delivery" else None),
        "inspection_deadline": deadline,
        "inspection_opened_at": now,
        "inspection_trigger": trigger,
        "updated_at": now,
    })
    await repo.save_hold(updated)
    await _emit(EventTopic.ESCROW_INSPECTION_OPENED, hold.deal_id, {
        "escrow_id": escrow_id,
        "trigger": trigger,
        "reason": reason,
        "deadline": deadline.isoformat(),
        "final_amount": _final_amount(hold),
        "currency": hold.currency,
        "provider": hold.provider,
    })
    logger.info("EscrowGuard: inspection window opened for %s (deal %s, %s) until %s",
                escrow_id, hold.deal_id, trigger, deadline.isoformat())
    return EscrowResponse.from_record(updated)


async def clear_dispute(escrow_id: str, *, reason: str = "resolved") -> EscrowResponse:
    """Operator / playbook lifts a buyer dispute (e.g. a re-inspection was agreed)."""
    repo = get_repository()
    hold = await repo.get_hold(escrow_id)
    if hold is None:
        raise HoldNotFoundError(escrow_id)
    if not hold.disputed:
        return EscrowResponse.from_record(hold)
    now = datetime.now(timezone.utc)
    updated = hold.model_copy(update={
        "disputed": False, "updated_at": now,
        "provider_payload": {**hold.provider_payload, "dispute_cleared": {"at": now.isoformat(), "reason": reason}},
    })
    await repo.save_hold(updated)
    logger.info("EscrowGuard: dispute cleared on %s (deal %s): %s", escrow_id, hold.deal_id, reason)
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
    final_amount = round(released_total - hold.released_total, 2)
    updated = hold.model_copy(update={
        "tranches": new_tranches,
        "released_total": released_total,
        "fees_total": round(hold.fees_total + _fee_on(hold, final_amount), 2),
        "status": "RELEASED" if all_released else hold.status,
        "released_at": now if all_released else hold.released_at,
        "buyer_accepted_at": now,
        "updated_at": now,
    })
    await repo.save_hold(updated)

    await _emit(EventTopic.ESCROW_TRANCHE_RELEASED, hold.deal_id, {
        "escrow_id": escrow_id,
        "tranche_id": "FINAL",
        "trigger": "ACCEPTED",
        "event": "buyer_accepted",
        "amount": final_amount,
        "released_total": released_total,
        "fees_total": updated.fees_total,
        "all_released": all_released,
        "accepted_by": req.accepted_by,
        "provider": hold.provider,
        "currency": hold.currency,
    })
    logger.info("EscrowGuard: buyer accepted escrow %s (deal %s); released_total=%.2f",
                escrow_id, hold.deal_id, released_total)
    updated = await _settle_if_complete(updated, now)
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
        final_amount = round(released_total - hold.released_total, 2)
        updated = hold.model_copy(update={
            "tranches": new_tranches,
            "released_total": released_total,
            "fees_total": round(hold.fees_total + _fee_on(hold, final_amount), 2),
            "status": "RELEASED" if all_released else hold.status,
            "released_at": now if all_released else hold.released_at,
            "buyer_accepted_at": now,
            "updated_at": now,
        })
        await repo.save_hold(updated)
        await _emit(EventTopic.ESCROW_TRANCHE_RELEASED, hold.deal_id, {
            "escrow_id": hold.escrow_id,
            "tranche_id": "FINAL",
            "trigger": "ACCEPTED",
            "event": "auto_release_window_lapsed",
            "amount": final_amount,
            "released_total": released_total,
            "fees_total": updated.fees_total,
            "all_released": all_released,
            "provider": hold.provider,
            "currency": hold.currency,
        })
        logger.info("EscrowGuard: auto-released final tranche for %s (deal %s) — window lapsed",
                    hold.escrow_id, hold.deal_id)
        await _settle_if_complete(updated, now)
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
    if hold.fees_total < -_FUNDING_TOLERANCE or hold.fees_total > hold.released_total + _FUNDING_TOLERANCE:
        issues.append(f"fees_total {hold.fees_total} inconsistent with released_total {hold.released_total}")
    return outstanding, issues


def _payout_issues(hold: EscrowHoldRecord, payouts: list[PayoutRecord]) -> list[str]:
    """Supplier payouts may never exceed what PortLess received from the escrow."""
    completed, pending = _payout_totals(payouts)
    received = _net_received(hold)
    if completed + pending > received + _FUNDING_TOLERANCE:
        return [f"payouts {completed + pending:.2f} exceed net funds received from escrow {received:.2f}"]
    return []


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
        payouts_total, _ = _payout_totals(payouts)
        # Realised margin = cash actually received from the escrow (released
        # tranches net of provider fees; the funded principal for a legacy
        # Stripe hold) minus everything paid to the supplier.
        realized = round(_net_received(hold) - payouts_total, 2)

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
    if hold is None:
        payouts_total, _ = _payout_totals(payouts)
        return DealLedger(
            deal_id=deal_id, currency="", payouts_total=payouts_total, reconciled=True,
            payouts=[PayoutResponse.from_record(p) for p in payouts],
        )
    return await _ledger_for(hold, payouts)


async def _ledger_for(hold: EscrowHoldRecord, payouts: list[PayoutRecord]) -> DealLedger:
    outstanding, issues = _ledger_issues(hold)
    issues = issues + _payout_issues(hold, payouts)
    completed, pending = _payout_totals(payouts)
    net = _net_received(hold)
    owed = next((p.total_owed for p in payouts if p.total_owed is not None), None)
    if owed is None:
        owed = await _supplier_owed(hold.deal_id)
    return DealLedger(
        deal_id=hold.deal_id,
        currency=hold.currency,
        provider=hold.provider,
        escrow_id=hold.escrow_id,
        status=hold.status,
        principal=hold.amount,
        funded_amount=hold.funded_amount,
        shortfall=hold.shortfall,
        released_total=hold.released_total,
        refunded_total=hold.refunded_total,
        outstanding=outstanding,
        fees_total=hold.fees_total,
        net_to_portless=net,
        payouts_total=completed,
        payouts_pending=pending,
        available_for_payout=round(max(net - completed - pending, 0.0), 2),
        supplier_owed=owed,
        supplier_balance_due=(round(owed - completed, 2) if owed is not None else None),
        realized_margin=round(net - completed, 2) if (hold.tranches and hold.released_total) or completed else None,
        tranches=hold.tranches,
        payouts=[PayoutResponse.from_record(p) for p in payouts],
        inspection_deadline=hold.inspection_deadline,
        disputed=hold.disputed,
        settled=hold.settled_at is not None,
        reconciled=(len(issues) == 0),
        issues=issues,
    )


async def list_deal_ledgers() -> list[DealLedger]:
    """One ledger per escrow — the Money & Margin page's escrow view."""
    repo = get_repository()
    out: list[DealLedger] = []
    for hold in sorted(await repo.list_holds(), key=lambda h: h.created_at, reverse=True):
        payouts = await repo.list_payouts_by_deal(hold.deal_id)
        out.append(await _ledger_for(hold, payouts))
    return out


async def check_reconciliation() -> int:
    """Sweep every hold and emit a `deal.exception` **once** for any that fails its
    ledger invariant; reset the flag when a hold reconciles again. Returns the
    number newly flagged this pass. Safe to call repeatedly (idempotent)."""
    repo = get_repository()
    now = datetime.now(timezone.utc)
    flagged = 0
    for hold in await repo.list_holds():
        _, issues = _ledger_issues(hold)
        issues = issues + _payout_issues(hold, await repo.list_payouts_by_deal(hold.deal_id))
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
