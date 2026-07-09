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


# ---------------------------------------------------------------------------
# Neutral staged-release escrow (Tazapay / Escrow.com) — target model
# ---------------------------------------------------------------------------

from services.escrow_guard.schemas import (  # noqa: E402
    CreateEscrowRequest,
    EscrowFundedWebhookPayload as _FundedPayload,
    RefundRequest,
    ReleaseTrancheRequest,
)
from services.escrow_guard.service import (  # noqa: E402
    create_escrow,
    handle_escrow_funded_webhook as _handle_funded,
    list_escrow_providers,
    refund,
    release_tranche,
)


async def _funded_escrow(deal_id: str, provider: str = "tazapay", amount: float = 50000.0) -> str:
    """Open an escrow (deal PI_ACCEPTED) then mark it FUNDED. Returns escrow_id."""
    deal = _mock_deal(deal_id=deal_id, status="PI_ACCEPTED")
    with (
        patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
        patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
    ):
        esc = await create_escrow(
            CreateEscrowRequest(deal_id=deal_id, provider=provider, amount=amount, currency="USD")
        )
    from services.escrow_guard.repository import get_repository
    repo = get_repository()
    h = await repo.get_hold(esc.escrow_id)
    await repo.save_hold(h.model_copy(update={"status": "FUNDED"}))
    return esc.escrow_id


class TestCreateEscrow:
    @pytest.mark.asyncio
    async def test_default_tranches(self):
        deal = _mock_deal(deal_id="DEAL-ESC-1", status="PI_ACCEPTED")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
        ):
            esc = await create_escrow(
                CreateEscrowRequest(deal_id="DEAL-ESC-1", provider="tazapay", amount=50000.0)
            )
        assert esc.status == "PENDING"
        assert esc.provider == "tazapay"
        assert esc.checkout_url is not None
        assert esc.ext_ref is not None
        assert len(esc.tranches) == 4
        assert sum(t.pct for t in esc.tranches) == pytest.approx(1.0)
        assert esc.tranches[0].amount == pytest.approx(15000.0)  # T1 = 30%

    @pytest.mark.asyncio
    async def test_escrow_com_provider(self):
        deal = _mock_deal(deal_id="DEAL-ESC-2", status="PI_ACCEPTED")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
        ):
            esc = await create_escrow(
                CreateEscrowRequest(deal_id="DEAL-ESC-2", provider="escrow_com", amount=10000.0)
            )
        assert esc.provider == "escrow_com"
        assert esc.checkout_url is not None

    @pytest.mark.asyncio
    async def test_transitions_pi_accepted_to_pending(self):
        deal = _mock_deal(deal_id="DEAL-ESC-3", status="PI_ACCEPTED")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()) as mock_trans,
        ):
            await create_escrow(CreateEscrowRequest(deal_id="DEAL-ESC-3", amount=1000.0))
        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][1] == "ESCROW_PENDING"

    @pytest.mark.asyncio
    async def test_wrong_state(self):
        deal = _mock_deal(deal_id="DEAL-ESC-4", status="DRAFT")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)):
            with pytest.raises(InvalidDealState):
                await create_escrow(CreateEscrowRequest(deal_id="DEAL-ESC-4", amount=1000.0))

    @pytest.mark.asyncio
    async def test_funded_webhook_routes_escrow_provider(self):
        """Funded webhook must resolve the escrow (non-Stripe) provider connector."""
        deal = _mock_deal(deal_id="DEAL-ESC-5", status="PI_ACCEPTED")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
        ):
            esc = await create_escrow(CreateEscrowRequest(deal_id="DEAL-ESC-5", provider="tazapay", amount=2000.0))
        payload = _FundedPayload(ext_ref=esc.ext_ref, status="AWAITING_FUNDING")
        with (
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.escrow_guard.service._emit", new=AsyncMock()),
        ):
            result = await _handle_funded(payload, {}, b"{}")
        assert result["status"] == "ok"
        assert mock_trans.call_args[0][1] == "ESCROW_FUNDED"


class TestReleaseTranche:
    @pytest.mark.asyncio
    async def test_release_first_tranche(self):
        escrow_id = await _funded_escrow("DEAL-REL-1")
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            r = await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T1", proof_refs=["CRF-abc"]))
        assert r.released_total == pytest.approx(15000.0)
        assert r.status == "FUNDED"  # not all released yet
        t1 = next(t for t in r.tranches if t.tranche_id == "T1")
        assert t1.status == "RELEASED"
        assert t1.proof_refs == ["CRF-abc"]

    @pytest.mark.asyncio
    async def test_release_idempotent(self):
        escrow_id = await _funded_escrow("DEAL-REL-2")
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T1"))
            r2 = await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T1"))
        assert r2.released_total == pytest.approx(15000.0)

    @pytest.mark.asyncio
    async def test_release_all_marks_released(self):
        """Proof-gated tranches via release_tranche; final tranche via buyer accept."""
        from services.escrow_guard.schemas import AcceptEscrowRequest
        from services.escrow_guard.service import accept_escrow
        escrow_id = await _funded_escrow("DEAL-REL-3")
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            for tid in ("T1", "T2", "T3"):
                await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id=tid))
            r = await accept_escrow(escrow_id, AcceptEscrowRequest(accepted_by="buyer@x.com"))
        assert r.status == "RELEASED"
        assert r.released_total == pytest.approx(50000.0)

    @pytest.mark.asyncio
    async def test_release_requires_funded(self):
        """Releasing before the escrow is funded is rejected."""
        deal = _mock_deal(deal_id="DEAL-REL-4", status="PI_ACCEPTED")
        with (
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
        ):
            esc = await create_escrow(CreateEscrowRequest(deal_id="DEAL-REL-4", amount=1000.0))
        with pytest.raises(InvalidDealState):
            await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id="T1"))

    @pytest.mark.asyncio
    async def test_release_unknown_tranche(self):
        escrow_id = await _funded_escrow("DEAL-REL-5")
        with pytest.raises(HoldNotFoundError):
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T9"))


class TestRefund:
    @pytest.mark.asyncio
    async def test_refund_undisbursed_balance(self):
        escrow_id = await _funded_escrow("DEAL-RF-1")
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T1"))  # 15000 released
            r = await refund(escrow_id, RefundRequest(reason="QC failed"))
        assert r.status == "REFUNDED"
        assert r.refunded_total == pytest.approx(35000.0)  # 50000 - 15000

    @pytest.mark.asyncio
    async def test_refund_idempotent(self):
        escrow_id = await _funded_escrow("DEAL-RF-2")
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            r1 = await refund(escrow_id, RefundRequest(reason="x"))
            r2 = await refund(escrow_id, RefundRequest(reason="x"))
        assert r1.refunded_total == r2.refunded_total == pytest.approx(50000.0)


class TestListProviders:
    @pytest.mark.asyncio
    async def test_lists_both_providers(self):
        providers = await list_escrow_providers()
        names = {p["provider"] for p in providers}
        assert names == {"tazapay", "escrow_com"}
        assert all(p["payment_methods"] for p in providers)
        assert sum(1 for p in providers if p["default"]) == 1


# ---------------------------------------------------------------------------
# Final-tranche release authority: buyer acceptance + inspection window
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from services.escrow_guard.schemas import (  # noqa: E402
    AcceptEscrowRequest,
    DisputeEscrowRequest,
)
from services.escrow_guard.service import (  # noqa: E402
    accept_escrow,
    check_inspection_windows,
    dispute_escrow,
)


async def _release_proof_tranches(escrow_id: str) -> None:
    """Release the three proof-gated tranches (T1/T2/T3), opening the window."""
    with patch("services.escrow_guard.service._emit", new=AsyncMock()):
        for tid in ("T1", "T2", "T3"):
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id=tid))


class TestReleaseAuthority:
    @pytest.mark.asyncio
    async def test_release_tranche_rejects_final(self):
        """PortLess cannot self-release the final ACCEPTED-trigger tranche."""
        escrow_id = await _funded_escrow("DEAL-AUTH-1")
        with pytest.raises(InvalidDealState):
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T4"))

    @pytest.mark.asyncio
    async def test_accept_releases_final(self):
        escrow_id = await _funded_escrow("DEAL-AUTH-2")
        await _release_proof_tranches(escrow_id)
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            r = await accept_escrow(escrow_id, AcceptEscrowRequest(accepted_by="buyer@x.com"))
        assert r.status == "RELEASED"
        assert r.released_total == pytest.approx(50000.0)
        assert r.buyer_accepted_at is not None

    @pytest.mark.asyncio
    async def test_window_opens_after_proof_tranches(self):
        escrow_id = await _funded_escrow("DEAL-AUTH-3")
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T1"))
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T2"))
            r = await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T3"))
        assert r.inspection_deadline is not None

    @pytest.mark.asyncio
    async def test_inspection_window_autorelease(self):
        escrow_id = await _funded_escrow("DEAL-AUTH-4")
        await _release_proof_tranches(escrow_id)
        future = datetime.now(timezone.utc) + timedelta(hours=1000)
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            count = await check_inspection_windows(now=future)
        assert count == 1
        from services.escrow_guard.repository import get_repository
        hold = await get_repository().get_hold(escrow_id)
        assert hold.status == "RELEASED"
        assert all(t.status == "RELEASED" for t in hold.tranches)

    @pytest.mark.asyncio
    async def test_dispute_blocks_release_and_accept(self):
        escrow_id = await _funded_escrow("DEAL-AUTH-5")
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            d = await dispute_escrow(escrow_id, DisputeEscrowRequest(reason="damaged in transit"))
        assert d.disputed is True
        with pytest.raises(InvalidDealState):
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T1"))
        with pytest.raises(InvalidDealState):
            await accept_escrow(escrow_id, AcceptEscrowRequest())

    @pytest.mark.asyncio
    async def test_window_skips_disputed(self):
        escrow_id = await _funded_escrow("DEAL-AUTH-6")
        await _release_proof_tranches(escrow_id)
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            await dispute_escrow(escrow_id, DisputeEscrowRequest(reason="hold"))
            future = datetime.now(timezone.utc) + timedelta(hours=1000)
            count = await check_inspection_windows(now=future)
        assert count == 0


# ---------------------------------------------------------------------------
# Funding reconciliation + per-trade ledger
# ---------------------------------------------------------------------------

from services.escrow_guard.schemas import TrancheInput  # noqa: E402
from services.escrow_guard.service import check_reconciliation, get_deal_ledger  # noqa: E402


async def _open_escrow(deal_id: str, amount: float = 2000.0, provider: str = "tazapay", schedule=None):
    deal = _mock_deal(deal_id=deal_id, status="PI_ACCEPTED")
    with (
        patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
        patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
    ):
        return await create_escrow(CreateEscrowRequest(
            deal_id=deal_id, provider=provider, amount=amount, tranche_schedule=schedule,
        ))


class TestFundingReconciliation:
    @pytest.mark.asyncio
    async def test_funded_amount_recorded(self):
        esc = await _open_escrow("DEAL-FR-1", amount=2000.0)
        payload = _FundedPayload(ext_ref=esc.ext_ref, status="FUNDED", amount=2000, currency="USD")
        with (
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
            patch("services.escrow_guard.service._emit", new=AsyncMock()) as mock_emit,
        ):
            await _handle_funded(payload, {}, b"{}")
        from services.escrow_guard.repository import get_repository
        hold = await get_repository().get_hold(esc.escrow_id)
        assert hold.funded_amount == pytest.approx(2000.0)
        topics = [c[0][0].value for c in mock_emit.call_args_list]
        assert "deal.exception" not in topics  # amounts match → no mismatch event

    @pytest.mark.asyncio
    async def test_funding_mismatch_emits_exception(self):
        esc = await _open_escrow("DEAL-FR-2", amount=2000.0)
        payload = _FundedPayload(ext_ref=esc.ext_ref, status="FUNDED", amount=1800, currency="USD")
        with (
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
            patch("services.escrow_guard.service._emit", new=AsyncMock()) as mock_emit,
        ):
            await _handle_funded(payload, {}, b"{}")
        topics = [c[0][0].value for c in mock_emit.call_args_list]
        assert "deal.exception" in topics

    @pytest.mark.asyncio
    async def test_over_release_guard(self):
        """A schedule whose pcts exceed 1.0 is blocked from over-disbursing."""
        esc = await _open_escrow("DEAL-FR-3", amount=1000.0, schedule=[
            TrancheInput(tranche_id="A", trigger="PO_ISSUED", pct=0.6),
            TrancheInput(tranche_id="B", trigger="QC_PASSED", pct=0.6),
        ])
        from services.escrow_guard.repository import get_repository
        repo = get_repository()
        h = await repo.get_hold(esc.escrow_id)
        await repo.save_hold(h.model_copy(update={"status": "FUNDED"}))
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id="A"))  # 600
            with pytest.raises(InvalidDealState):
                await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id="B"))  # +600 > 1000

    @pytest.mark.asyncio
    async def test_ledger_reconciled_after_partial_release(self):
        escrow_id = await _funded_escrow("DEAL-FR-4")  # 50000, default 30/40/20/10
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            await release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id="T1"))  # 15000
        ledger = await get_deal_ledger("DEAL-FR-4")
        assert ledger.reconciled is True
        assert ledger.issues == []
        assert ledger.principal == pytest.approx(50000.0)
        assert ledger.released_total == pytest.approx(15000.0)
        assert ledger.outstanding == pytest.approx(35000.0)

    @pytest.mark.asyncio
    async def test_ledger_empty_deal(self):
        ledger = await get_deal_ledger("DEAL-FR-NONE")
        assert ledger.reconciled is True
        assert ledger.principal == 0.0
        assert ledger.released_total == 0.0


class TestReconciliationSweep:
    @pytest.mark.asyncio
    async def test_flags_unreconciled_once_then_resets(self):
        esc = await _open_escrow("DEAL-RS-1", amount=1000.0)
        from services.escrow_guard.repository import get_repository
        repo = get_repository()
        h = await repo.get_hold(esc.escrow_id)
        # Corrupt the ledger: released_total exceeds the principal.
        await repo.save_hold(h.model_copy(update={"status": "FUNDED", "released_total": 1200.0}))

        with patch("services.escrow_guard.service._emit", new=AsyncMock()) as mock_emit:
            n1 = await check_reconciliation()
            n2 = await check_reconciliation()  # already flagged → no duplicate
        assert n1 == 1
        assert n2 == 0
        topics = [c[0][0].value for c in mock_emit.call_args_list]
        assert topics.count("deal.exception") == 1
        assert (await repo.get_hold(esc.escrow_id)).reconciliation_flagged is True

        # Fix the ledger → the flag resets on the next sweep.
        fixed = (await repo.get_hold(esc.escrow_id)).model_copy(update={"released_total": 0.0})
        await repo.save_hold(fixed)
        with patch("services.escrow_guard.service._emit", new=AsyncMock()):
            n3 = await check_reconciliation()
        assert n3 == 0
        assert (await repo.get_hold(esc.escrow_id)).reconciliation_flagged is False

    @pytest.mark.asyncio
    async def test_reconciled_hold_not_flagged(self):
        await _funded_escrow("DEAL-RS-2")  # clean 30/40/20/10 hold
        with patch("services.escrow_guard.service._emit", new=AsyncMock()) as mock_emit:
            n = await check_reconciliation()
        assert n == 0
        assert mock_emit.await_count == 0
