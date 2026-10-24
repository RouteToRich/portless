"""Escrow money chain without gaps (services/escrow_guard/service.py).

  • funding shortfall keeps the escrow PARTIALLY_FUNDED (no PO against money
    that is not there); the balance webhook completes it
  • provider webhooks are replay-safe (event_id)
  • tranches release in order, never while the deal is in dispute
  • fees accrue per release; the last release settles the escrow
  • supplier payouts never exceed net funds received; PAID only when the
    supplier got the full PO value; the balance follows settlement
  • the ledger exposes every number the UI shows and flags breaches
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.escrow_guard.repository import InMemoryEscrowGuardRepository, get_repository
from services.escrow_guard.schemas import (
    AcceptEscrowRequest,
    CreateEscrowRequest,
    EscrowFundedWebhookPayload,
    EscrowHoldRequest,
    PayoutRequest,
    PayoutWebhookPayload,
    RefundRequest,
    ReleaseTrancheRequest,
)
from services.escrow_guard.service import (
    InvalidDealState,
    accept_escrow,
    create_escrow,
    create_hold,
    get_deal_ledger,
    handle_escrow_funded_webhook,
    handle_payout_webhook,
    list_deal_ledgers,
    refund,
    release_tranche,
    request_payout,
)
from shared.domain.events import EventTopic


def _deal(deal_id="DEAL-M1", status="PI_ACCEPTED"):
    return SimpleNamespace(deal_id=deal_id, status=SimpleNamespace(value=status))


@pytest.fixture(autouse=True)
def _repo():
    import services.escrow_guard.repository as repo_mod
    repo_mod._repository = InMemoryEscrowGuardRepository()
    repo_mod._motor_client = None
    yield
    repo_mod._repository = None


@pytest.fixture
def emitted():
    events: list[tuple[EventTopic, str, dict]] = []

    async def _emit(topic, deal_id, payload):
        events.append((topic, deal_id, payload))

    with patch("services.escrow_guard.service._emit", new=_emit):
        yield events


async def _open(deal_id="DEAL-M1", amount=50000.0, status="PI_ACCEPTED"):
    with (
        patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=_deal(deal_id, status))),
        patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
    ):
        return await create_escrow(CreateEscrowRequest(deal_id=deal_id, provider="tazapay", amount=amount))


async def _fund(esc, amount=None, event_id=None):
    payload = EscrowFundedWebhookPayload(ext_ref=esc.ext_ref, status="FUNDED",
                                        amount=amount if amount is not None else esc.amount, event_id=event_id)
    trans = AsyncMock()
    with (
        patch("services.escrow_guard.service._transition_deal", new=trans),
        patch("shared.orchestration.notifications.notify_funding_shortfall", new=AsyncMock()),
    ):
        out = await handle_escrow_funded_webhook(payload, {}, b"{}")
    return out, trans


class TestFunding:
    @pytest.mark.asyncio
    async def test_shortfall_keeps_deal_waiting_then_completes(self, emitted):
        esc = await _open()
        assert esc.fee_pct == 1.0  # tazapay quote
        out, trans = await _fund(esc, amount=30000.0, event_id="ev-1")
        assert out["status"] == "shortfall" and out["shortfall"] == 20000.0
        trans.assert_not_awaited()  # deal stays ESCROW_PENDING
        hold = await get_repository().get_hold(esc.escrow_id)
        assert hold.status == "PARTIALLY_FUNDED" and hold.funded_amount == 30000.0 and hold.shortfall == 20000.0
        topics = [t for t, _, _ in emitted]
        assert EventTopic.DEAL_EXCEPTION in topics and EventTopic.ESCROW_FUNDING_SHORTFALL in topics
        assert EventTopic.ESCROW_FUNDED not in topics
        # a release against a partially funded escrow is refused
        with pytest.raises(InvalidDealState):
            await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id="T1"))
        # the balance arrives: the provider reports the new total
        out2, trans2 = await _fund(esc, amount=50000.0, event_id="ev-2")
        assert out2["status"] == "ok"
        trans2.assert_awaited_once()
        hold = await get_repository().get_hold(esc.escrow_id)
        assert hold.status == "FUNDED" and hold.shortfall is None and hold.processed_events == ["ev-1", "ev-2"]

    @pytest.mark.asyncio
    async def test_replayed_event_is_ignored(self, emitted):
        esc = await _open()
        _, trans = await _fund(esc, event_id="ev-same")
        trans.assert_awaited_once()
        out, trans2 = await _fund(esc, event_id="ev-same")
        assert out.get("replayed") is True
        trans2.assert_not_awaited()
        assert sum(1 for t, _, _ in emitted if t == EventTopic.ESCROW_FUNDED) == 1

    @pytest.mark.asyncio
    async def test_overfunding_proceeds_but_flags(self, emitted):
        esc = await _open()
        out, trans = await _fund(esc, amount=50500.0)
        assert out["status"] == "ok"
        trans.assert_awaited_once()
        exc = [p for t, _, p in emitted if t == EventTopic.DEAL_EXCEPTION]
        assert exc and exc[0]["reason"] == "funding_amount_mismatch"
        ledger = await get_deal_ledger("DEAL-M1")
        assert ledger.reconciled is False and any("funded" in i for i in ledger.issues)


class TestTrancheOrderAndSettlement:
    @pytest.mark.asyncio
    async def test_out_of_order_release_rejected(self, emitted):
        esc = await _open()
        await _fund(esc)
        with pytest.raises(InvalidDealState) as e:
            await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id="T2"))
        assert "T1 still PENDING" in str(e.value)

    @pytest.mark.asyncio
    async def test_disputed_deal_blocks_release(self, emitted):
        esc = await _open()
        await _fund(esc)
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=_deal(status="DISPUTE"))):
            with pytest.raises(InvalidDealState):
                await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id="T1"))

    @pytest.mark.asyncio
    async def test_fees_window_and_settlement(self, emitted, monkeypatch):
        from services.escrow_guard.service import settings as eg_settings
        monkeypatch.setattr(eg_settings, "inspection_window_trigger", "proof")
        esc = await _open()
        await _fund(esc)
        for tid in ("T1", "T2"):
            await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id=tid, proof_refs=[f"P-{tid}"]))
        r3 = await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id="T3"))
        assert r3.inspection_deadline is not None and r3.settled_at is None
        assert r3.fees_total == pytest.approx(450.0)  # 1% of 45,000 released
        rel = [p for t, _, p in emitted if t == EventTopic.ESCROW_TRANCHE_RELEASED]
        assert [p["tranche_id"] for p in rel] == ["T1", "T2", "T3"]
        assert rel[0]["next_pending_tranche"] == "T2" and rel[2]["inspection_window_opened"] is True
        assert rel[2]["next_pending_trigger"] == "ACCEPTED"
        with patch("services.escrow_guard.service._patch_deal_realized_margin", new=AsyncMock()):
            final = await accept_escrow(esc.escrow_id, AcceptEscrowRequest(accepted_by="buyer@acme.de"))
        assert final.status == "RELEASED" and final.settled_at is not None
        assert final.fees_total == pytest.approx(500.0)
        settled = [p for t, _, p in emitted if t == EventTopic.ESCROW_SETTLED]
        assert len(settled) == 1 and settled[0]["net_to_portless"] == pytest.approx(49500.0)
        ledger = await get_deal_ledger("DEAL-M1")
        assert ledger.settled is True and ledger.reconciled is True and ledger.net_to_portless == pytest.approx(49500.0)

    @pytest.mark.asyncio
    async def test_refund_requires_undisbursed_balance(self, emitted):
        esc = await _open()
        await _fund(esc)
        for tid in ("T1", "T2", "T3"):
            await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id=tid))
        with patch("services.escrow_guard.service._patch_deal_realized_margin", new=AsyncMock()):
            await accept_escrow(esc.escrow_id, AcceptEscrowRequest())
        with pytest.raises(InvalidDealState):
            await refund(esc.escrow_id, RefundRequest(reason="too late"))

    @pytest.mark.asyncio
    async def test_partially_funded_can_be_refunded(self, emitted):
        esc = await _open()
        await _fund(esc, amount=30000.0)
        r = await refund(esc.escrow_id, RefundRequest(reason="buyer withdrew"))
        assert r.status == "REFUNDED" and r.refunded_total == pytest.approx(30000.0)


class TestSupplierPayouts:
    async def _released_escrow(self, deal_id="DEAL-M2"):
        esc = await _open(deal_id)
        await _fund(esc)
        for tid in ("T1", "T2", "T3"):
            await release_tranche(esc.escrow_id, ReleaseTrancheRequest(tranche_id=tid))
        return esc

    @pytest.mark.asyncio
    async def test_payout_capped_at_net_released(self, emitted):
        esc = await self._released_escrow()
        cleared = _deal("DEAL-M2", "CLEARED")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=cleared)):
            with pytest.raises(InvalidDealState) as e:
                await request_payout(PayoutRequest(deal_id="DEAL-M2", payee_id="SUP", amount=46000.0,
                                                   idempotency_key="k1", total_owed=46000.0))
            assert "exceeds funds released" in str(e.value)
            p = await request_payout(PayoutRequest(deal_id="DEAL-M2", payee_id="SUP", amount=44550.0,
                                                   idempotency_key="k2", total_owed=46000.0))
        assert p.status == "PROCESSING" and p.kind == "supplier_payout" and p.total_owed == 46000.0
        hold = await get_repository().get_hold(esc.escrow_id)
        assert hold.status == "FUNDED"  # a payout never flips a staged escrow
        ledger = await get_deal_ledger("DEAL-M2")
        assert ledger.available_for_payout == pytest.approx(0.0)
        assert ledger.payouts_pending == pytest.approx(44550.0)
        assert ledger.supplier_balance_due == pytest.approx(46000.0)  # nothing COMPLETED yet

    @pytest.mark.asyncio
    async def test_paid_only_when_supplier_fully_paid(self, emitted):
        esc = await self._released_escrow("DEAL-M3")
        cleared = _deal("DEAL-M3", "CLEARED")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=cleared)):
            p1 = await request_payout(PayoutRequest(deal_id="DEAL-M3", payee_id="SUP", amount=44550.0,
                                                    idempotency_key="k1", total_owed=46000.0))
        trans = AsyncMock()
        with (
            patch("services.escrow_guard.service._transition_deal", new=trans),
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=cleared)),
            patch("shared.connectors.payments.PayoneerConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.escrow_guard.service._patch_deal_realized_margin", new=AsyncMock()),
        ):
            await handle_payout_webhook(PayoutWebhookPayload(ext_ref=p1.ext_ref, status="COMPLETED", event_id="p-1"), {}, b"{}")
        trans.assert_not_awaited()  # still owed 1,450 → deal stays CLEARED
        done = [p for t, _, p in emitted if t == EventTopic.PAYOUT_COMPLETED]
        assert done[-1]["fully_paid"] is False and done[-1]["balance_due"] == pytest.approx(1450.0)
        # final tranche settles → the balance can be paid
        with patch("services.escrow_guard.service._patch_deal_realized_margin", new=AsyncMock()):
            await accept_escrow(esc.escrow_id, AcceptEscrowRequest())
        ledger = await get_deal_ledger("DEAL-M3")
        assert ledger.available_for_payout == pytest.approx(4950.0)
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=cleared)):
            p2 = await request_payout(PayoutRequest(deal_id="DEAL-M3", payee_id="SUP", amount=1450.0,
                                                    idempotency_key="k2", kind="supplier_balance", total_owed=46000.0))
        trans2 = AsyncMock()
        with (
            patch("services.escrow_guard.service._transition_deal", new=trans2),
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=cleared)),
            patch("shared.connectors.payments.PayoneerConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.escrow_guard.service._patch_deal_realized_margin", new=AsyncMock()),
        ):
            await handle_payout_webhook(PayoutWebhookPayload(ext_ref=p2.ext_ref, status="COMPLETED"), {}, b"{}")
        trans2.assert_awaited_once()
        assert trans2.await_args.args[1] == "PAID"
        ledger = await get_deal_ledger("DEAL-M3")
        assert ledger.supplier_balance_due == pytest.approx(0.0) and ledger.realized_margin == pytest.approx(3500.0)
        assert sorted(p.kind for p in ledger.payouts) == ["supplier_balance", "supplier_payout"]

    @pytest.mark.asyncio
    async def test_payout_webhook_replay(self, emitted):
        await self._released_escrow("DEAL-M4")
        cleared = _deal("DEAL-M4", "CLEARED")
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=cleared)):
            p1 = await request_payout(PayoutRequest(deal_id="DEAL-M4", payee_id="SUP", amount=1000.0, idempotency_key="k"))
        with (
            patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
            patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=cleared)),
            patch("shared.connectors.payments.PayoneerConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.escrow_guard.service._patch_deal_realized_margin", new=AsyncMock()),
        ):
            await handle_payout_webhook(PayoutWebhookPayload(ext_ref=p1.ext_ref, status="PROCESSING", event_id="e1"), {}, b"{}")
            r = await handle_payout_webhook(PayoutWebhookPayload(ext_ref=p1.ext_ref, status="PROCESSING", event_id="e1"), {}, b"{}")
        assert r.get("replayed") is True

    @pytest.mark.asyncio
    async def test_legacy_stripe_hold_still_pays_in_full(self, emitted):
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=_deal("DEAL-L1", "ESCROW_PENDING"))):
            hold = await create_hold(EscrowHoldRequest(deal_id="DEAL-L1", amount=10000.0))
        repo = get_repository()
        h = await repo.get_hold(hold.escrow_id)
        await repo.save_hold(h.model_copy(update={"status": "FUNDED"}))
        with patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=_deal("DEAL-L1", "CLEARED"))):
            p = await request_payout(PayoutRequest(deal_id="DEAL-L1", payee_id="SUP", amount=9200.0, idempotency_key="l"))
        assert p.status == "PROCESSING"
        assert (await repo.get_hold(hold.escrow_id)).status == "RELEASED"
        ledgers = await list_deal_ledgers()
        assert [l.deal_id for l in ledgers] == ["DEAL-L1"] and ledgers[0].payouts_pending == pytest.approx(9200.0)


class TestStripeReplayWindow:
    @pytest.mark.asyncio
    async def test_old_timestamp_rejected(self, monkeypatch):
        from shared.connectors.payments import StripeTreasuryConnector
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec")
        conn = StripeTreasuryConnector()
        body = b'{"id":"evt_1"}'

        def sig(ts: int) -> str:
            mac = hmac.new(b"whsec", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
            return f"t={ts},v1={mac}"

        assert await conn.webhook_verify({"stripe-signature": sig(int(time.time()))}, body) is True
        assert await conn.webhook_verify({"stripe-signature": sig(int(time.time()) - 3600)}, body) is False
