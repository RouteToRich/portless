"""Tests for Escrow-Guard service: hold creation, funded webhook, payout, payout webhook."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from services.escrow_guard.repository import InMemoryEscrowGuardRepository
from services.escrow_guard.schemas import (
    EscrowFundedWebhookPayload,
    EscrowHoldRequest,
    PayoutRequest,
    PayoutWebhookPayload,
)
from services.escrow_guard.service import (
    ConnectorCallError,
    DealNotFoundError,
    HoldNotFoundError,
    InvalidDealState,
    PayoutNotFoundError,
    WebhookVerifyError,
    create_hold,
    get_deal_summary,
    get_hold,
    get_hold_by_deal,
    get_payout,
    handle_escrow_funded_webhook,
    handle_payout_webhook,
    request_payout,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mock_deal(deal_id: str = "DEAL-EG-01", status: str = "ESCROW_PENDING") -> object:
    class FakeDealStatus:
        def __init__(self, v: str) -> None:
            self.value = v

        def __str__(self) -> str:
            return self.value

    class FakeDeal:
        def __init__(self) -> None:
            self.deal_id = deal_id
            self.status = FakeDealStatus(status)

    return FakeDeal()


@pytest.fixture(autouse=True)
def use_in_memory_repo():
    import services.escrow_guard.repository as repo_mod
    repo_mod._repository = InMemoryEscrowGuardRepository()
    repo_mod._motor_client = None
    yield
    repo_mod._repository = None


# ---------------------------------------------------------------------------
# Create hold
# ---------------------------------------------------------------------------

class TestCreateHold:
    @pytest.mark.asyncio
    async def test_create_hold_success(self):
        deal = _mock_deal(status="ESCROW_PENDING")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)):
            req = EscrowHoldRequest(deal_id="DEAL-EG-01", amount=50000.0, currency="USD")
            result = await create_hold(req)
        assert result.status == "PENDING"
        assert result.ext_ref is not None
        assert result.escrow_id.startswith("ESC-")

    @pytest.mark.asyncio
    async def test_create_hold_wrong_state(self):
        deal = _mock_deal(status="DRAFT")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)):
            with pytest.raises(InvalidDealState):
                await create_hold(EscrowHoldRequest(deal_id="DEAL-EG-01", amount=1000.0, currency="USD"))

    @pytest.mark.asyncio
    async def test_create_hold_deal_not_found(self):
        with patch(
            "services.escrow_guard.service._fetch_deal",
            new=AsyncMock(side_effect=DealNotFoundError("DEAL-MISSING")),
        ):
            with pytest.raises(DealNotFoundError):
                await create_hold(EscrowHoldRequest(deal_id="DEAL-MISSING", amount=1000.0, currency="USD"))

    @pytest.mark.asyncio
    async def test_create_hold_idempotent(self):
        deal = _mock_deal(status="ESCROW_PENDING")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)):
            req = EscrowHoldRequest(deal_id="DEAL-EG-01", amount=1000.0, currency="USD", idempotency_key="idem-hold-1")
            r1 = await create_hold(req)
            r2 = await create_hold(req)
        assert r1.escrow_id == r2.escrow_id

    @pytest.mark.asyncio
    async def test_create_hold_deduped_by_deal(self):
        """Second call with same deal_id returns existing non-failed hold."""
        deal = _mock_deal(status="ESCROW_PENDING")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)):
            r1 = await create_hold(EscrowHoldRequest(deal_id="DEAL-EG-01", amount=1000.0, currency="USD"))
            r2 = await create_hold(EscrowHoldRequest(deal_id="DEAL-EG-01", amount=1000.0, currency="USD"))
        assert r1.escrow_id == r2.escrow_id

    @pytest.mark.asyncio
    async def test_create_hold_connector_error(self):
        from shared.connectors.payments import ConnectorError as PayConnError
        deal = _mock_deal(status="ESCROW_PENDING")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch(
                "shared.connectors.payments.StripeTreasuryConnector.book",
                new=AsyncMock(side_effect=PayConnError("Stripe timeout")),
            ),
        ):
            with pytest.raises(ConnectorCallError):
                await create_hold(EscrowHoldRequest(deal_id="DEAL-EG-01", amount=1000.0, currency="USD"))


# ---------------------------------------------------------------------------
# Escrow funded webhook
# ---------------------------------------------------------------------------

class TestEscrowFundedWebhook:
    @pytest.mark.asyncio
    async def test_funded_webhook_transitions_deal(self):
        deal = _mock_deal(status="ESCROW_PENDING")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)):
            hold = await create_hold(EscrowHoldRequest(deal_id="DEAL-EG-01", amount=1000.0, currency="USD"))

        payload = EscrowFundedWebhookPayload(ext_ref=hold.ext_ref, status="requires_capture")
        with (
            patch("shared.connectors.payments.StripeTreasuryConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.escrow_guard.service._emit", new=AsyncMock()),
        ):
            result = await handle_escrow_funded_webhook(payload, {}, b"{}")
        assert result["status"] == "ok"
        mock_trans.assert_awaited_once_with("DEAL-EG-01", "ESCROW_FUNDED", pytest.approx("Escrow funded — ref " + hold.ext_ref, rel=0))

    @pytest.mark.asyncio
    async def test_funded_webhook_idempotent(self):
        """Calling funded webhook twice should not re-transition."""
        deal = _mock_deal(status="ESCROW_PENDING")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)):
            hold = await create_hold(EscrowHoldRequest(deal_id="DEAL-EG-01", amount=1000.0, currency="USD"))

        payload = EscrowFundedWebhookPayload(ext_ref=hold.ext_ref, status="succeeded")
        with (
            patch("shared.connectors.payments.StripeTreasuryConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.escrow_guard.service._emit", new=AsyncMock()),
        ):
            await handle_escrow_funded_webhook(payload, {}, b"{}")
            r2 = await handle_escrow_funded_webhook(payload, {}, b"{}")
        assert r2["status"] == "ok"
        # Second call is a no-op (hold already FUNDED, returns early)
        assert mock_trans.call_count == 1

    @pytest.mark.asyncio
    async def test_funded_webhook_bad_signature(self):
        deal = _mock_deal(status="ESCROW_PENDING")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)):
            hold = await create_hold(EscrowHoldRequest(deal_id="DEAL-EG-01", amount=1000.0, currency="USD"))

        payload = EscrowFundedWebhookPayload(ext_ref=hold.ext_ref, status="succeeded")
        with patch("shared.connectors.payments.StripeTreasuryConnector.webhook_verify", new=AsyncMock(return_value=False)):
            with pytest.raises(WebhookVerifyError):
                await handle_escrow_funded_webhook(payload, {}, b"{}")

    @pytest.mark.asyncio
    async def test_funded_webhook_unknown_ext_ref(self):
        payload = EscrowFundedWebhookPayload(ext_ref="UNKNOWN-PI", status="succeeded")
        with patch("shared.connectors.payments.StripeTreasuryConnector.webhook_verify", new=AsyncMock(return_value=True)):
            with pytest.raises(HoldNotFoundError):
                await handle_escrow_funded_webhook(payload, {}, b"{}")


# ---------------------------------------------------------------------------
# Request payout
# ---------------------------------------------------------------------------

class TestRequestPayout:
    async def _setup_funded_hold(self, deal_id: str = "DEAL-EG-01") -> str:
        """Create and fund a hold, returning the deal_id."""
        deal_pending = _mock_deal(deal_id=deal_id, status="ESCROW_PENDING")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal_pending)):
            hold = await create_hold(EscrowHoldRequest(deal_id=deal_id, amount=50000.0, currency="USD"))
        # Mark hold as FUNDED directly
        from services.escrow_guard.repository import get_repository
        repo = get_repository()
        h = await repo.get_hold(hold.escrow_id)
        funded = h.model_copy(update={"status": "FUNDED"})
        await repo.save_hold(funded)
        return deal_id

    @pytest.mark.asyncio
    async def test_request_payout_success(self):
        deal_id = await self._setup_funded_hold()
        deal_cleared = _mock_deal(deal_id=deal_id, status="CLEARED")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal_cleared)),
            patch("services.escrow_guard.service._emit", new=AsyncMock()),
        ):
            req = PayoutRequest(
                deal_id=deal_id,
                payee_id="PAYONEER-PAYEE-123",
                amount=48000.0,
                currency="USD",
                idempotency_key="idem-payout-1",
            )
            result = await request_payout(req)
        assert result.status == "PROCESSING"
        assert result.payout_id.startswith("PAY-")
        assert result.ext_ref is not None

    @pytest.mark.asyncio
    async def test_request_payout_wrong_state(self):
        deal_id = await self._setup_funded_hold()
        deal_wrong = _mock_deal(deal_id=deal_id, status="IN_TRANSIT")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal_wrong)):
            with pytest.raises(InvalidDealState):
                await request_payout(
                    PayoutRequest(
                        deal_id=deal_id,
                        payee_id="P-123",
                        amount=1000.0,
                        currency="USD",
                        idempotency_key="idem-payout-wrong",
                    )
                )

    @pytest.mark.asyncio
    async def test_request_payout_no_funded_hold(self):
        """Attempt payout without a funded hold fails."""
        deal_id = "DEAL-NO-HOLD"
        deal_cleared = _mock_deal(deal_id=deal_id, status="CLEARED")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal_cleared)):
            with pytest.raises(InvalidDealState):
                await request_payout(
                    PayoutRequest(
                        deal_id=deal_id,
                        payee_id="P-123",
                        amount=1000.0,
                        currency="USD",
                        idempotency_key="idem-payout-no-hold",
                    )
                )

    @pytest.mark.asyncio
    async def test_request_payout_idempotent(self):
        deal_id = await self._setup_funded_hold("DEAL-EG-IDEM")
        deal_cleared = _mock_deal(deal_id=deal_id, status="CLEARED")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal_cleared)),
            patch("services.escrow_guard.service._emit", new=AsyncMock()),
        ):
            req = PayoutRequest(
                deal_id=deal_id,
                payee_id="P-123",
                amount=1000.0,
                currency="USD",
                idempotency_key="idem-payout-idem",
            )
            r1 = await request_payout(req)
            r2 = await request_payout(req)
        assert r1.payout_id == r2.payout_id


# ---------------------------------------------------------------------------
# Payout webhook
# ---------------------------------------------------------------------------

class TestPayoutWebhook:
    async def _create_payout(self, deal_id: str = "DEAL-EG-PW") -> str:
        """Create a funded hold then request payout. Returns payout ext_ref."""
        deal_pending = _mock_deal(deal_id=deal_id, status="ESCROW_PENDING")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal_pending)):
            hold = await create_hold(EscrowHoldRequest(deal_id=deal_id, amount=1000.0, currency="USD"))
        from services.escrow_guard.repository import get_repository
        repo = get_repository()
        h = await repo.get_hold(hold.escrow_id)
        await repo.save_hold(h.model_copy(update={"status": "FUNDED"}))

        deal_cleared = _mock_deal(deal_id=deal_id, status="CLEARED")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal_cleared)),
            patch("services.escrow_guard.service._emit", new=AsyncMock()),
        ):
            payout = await request_payout(
                PayoutRequest(deal_id=deal_id, payee_id="P-X", amount=900.0, currency="USD", idempotency_key="idem-pw-1")
            )
        return payout.ext_ref

    @pytest.mark.asyncio
    async def test_payout_completed_transitions_paid(self):
        ext_ref = await self._create_payout()
        payload = PayoutWebhookPayload(ext_ref=ext_ref, status="COMPLETED")
        with (
            patch("shared.connectors.payments.PayoneerConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.escrow_guard.service._emit", new=AsyncMock()),
        ):
            result = await handle_payout_webhook(payload, {}, b"{}")
        assert result["status"] == "ok"
        mock_trans.assert_awaited_once()
        args = mock_trans.call_args[0]
        assert args[1] == "PAID"

    @pytest.mark.asyncio
    async def test_payout_failed_emits_exception_event(self):
        ext_ref = await self._create_payout("DEAL-EG-FAIL")
        payload = PayoutWebhookPayload(ext_ref=ext_ref, status="FAILED", notes="Payoneer rejected")
        with (
            patch("shared.connectors.payments.PayoneerConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.escrow_guard.service._emit", new=AsyncMock()) as mock_emit,
        ):
            result = await handle_payout_webhook(payload, {}, b"{}")
        assert result["status"] == "ok"
        mock_trans.assert_not_awaited()  # PAID transition should NOT occur on failure
        topics = [call[0][0].value for call in mock_emit.call_args_list]
        assert "deal.exception" in topics

    @pytest.mark.asyncio
    async def test_payout_webhook_bad_signature(self):
        ext_ref = await self._create_payout("DEAL-EG-SIG")
        payload = PayoutWebhookPayload(ext_ref=ext_ref, status="COMPLETED")
        with patch("shared.connectors.payments.PayoneerConnector.webhook_verify", new=AsyncMock(return_value=False)):
            with pytest.raises(WebhookVerifyError):
                await handle_payout_webhook(payload, {}, b"{}")


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

class TestReadHelpers:
    @pytest.mark.asyncio
    async def test_get_hold_not_found(self):
        with pytest.raises(HoldNotFoundError):
            await get_hold("ESC-NOPE")

    @pytest.mark.asyncio
    async def test_get_hold_by_deal_not_found(self):
        with pytest.raises(HoldNotFoundError):
            await get_hold_by_deal("DEAL-NO-HOLD")

    @pytest.mark.asyncio
    async def test_get_payout_not_found(self):
        with pytest.raises(PayoutNotFoundError):
            await get_payout("PAY-NOPE")

    @pytest.mark.asyncio
    async def test_deal_summary_empty(self):
        summary = await get_deal_summary("DEAL-EG-EMPTY")
        assert summary.hold is None
        assert summary.payouts == []
