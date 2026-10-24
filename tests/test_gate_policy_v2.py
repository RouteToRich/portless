"""Autonomy policy v2 (timeouts, on-timeout defaults, value threshold) and the
Batch-A automation rules built on it:

  • resolve_gate: L1 timeout + default per action, env/override precedence,
    money threshold → L2 only with conditions met, global vs per-action threshold
  • _create_gate forwards expires_at / default_on_timeout from the spec
  • supplier auto-source: GST+IEC checked and passed + price inside the corridor → L2
  • deal quantity from the buyer lead; PI/PO payment terms from the escrow schedule
  • buyer goods acceptance / complaint by e-mail after delivery
  • supplier reminders in the deal watcher
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import autonomy_policy as ap
from shared.orchestration import continuations as c

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_ON_DEAL_CREATED", "false")
    ap.reset_for_tests()
    monkeypatch.setattr(ap, "_mongo_collection", lambda: None)
    for cls in ap.ACTION_CLASSES:
        for suffix in ("", "_TIMEOUT_SECONDS", "_ON_TIMEOUT", "_AUTO_APPROVE_BELOW_USD"):
            monkeypatch.delenv(f"AUTONOMY_{cls.action}{suffix}", raising=False)
    monkeypatch.delenv("AUTONOMY_MONEY_CEILING", raising=False)
    monkeypatch.delenv("AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD", raising=False)
    yield
    ap.reset_for_tests()


def _deal(status="CLEARED", **overrides):
    base = dict(
        deal_id="DEAL-1", status=SimpleNamespace(value=status), market="DE", hs_code="1006",
        autonomy_profile=SimpleNamespace(value="L0"), winning_supplier_id="SUP-1", opportunity_id=None,
        buyer_target_price_usd=1255.0, margin_floor_pct=8.0, quantity_mt=None, match_results=[], parties=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# resolve_gate
# ---------------------------------------------------------------------------

class TestResolveGate:
    def test_defaults_per_action(self):
        g = ap.resolve_gate("SEND_NEGOTIATION_EMAIL")
        assert g.level == "L0" and g.timeout_seconds == 4 * 3600 and g.default_on_timeout == "APPROVE"
        m = ap.resolve_gate("RELEASE_SUPPLIER_PAYOUT")
        assert m.level == "L0" and m.timeout_seconds == 86400 and m.default_on_timeout == "REJECT"
        assert m.auto_approve_below_usd is None and m.threshold_applied is False

    def test_env_and_override_precedence(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_ISSUE_PO_TIMEOUT_SECONDS", "7200")
        monkeypatch.setenv("AUTONOMY_ISSUE_PO_ON_TIMEOUT", "reject")
        g = ap.resolve_gate("ISSUE_PO")
        assert g.timeout_seconds == 7200 and g.default_on_timeout == "REJECT"
        ap._overrides["ISSUE_PO"] = {"timeout_seconds": 600, "default_on_timeout": "APPROVE", "level": "L1"}
        g = ap.resolve_gate("ISSUE_PO")
        assert g.level == "L1" and g.timeout_seconds == 600 and g.default_on_timeout == "APPROVE"
        assert g.expires_at_iso(NOW) == (NOW + timedelta(seconds=600)).isoformat()
        assert ap.resolve_gate("DISPATCH_RFQ").expires_at_iso() is None  # L2 never expires

    def test_money_threshold_requires_conditions(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD", "20000")
        under = ap.resolve_gate("RELEASE_ESCROW_TRANCHE", amount=15000.0, conditions_ok=True)
        assert under.level == "L2" and under.source == "value_threshold" and under.threshold_applied
        assert ap.resolve_gate("RELEASE_ESCROW_TRANCHE", amount=15000.0, conditions_ok=False).level == "L0"
        assert ap.resolve_gate("RELEASE_ESCROW_TRANCHE", amount=25000.0, conditions_ok=True).level == "L0"
        assert ap.resolve_gate("RELEASE_ESCROW_TRANCHE", amount=None, conditions_ok=True).level == "L0"
        # non-money actions never use the threshold
        assert ap.resolve_gate("ISSUE_PO", amount=10.0).level == "L0"

    def test_per_action_threshold_beats_global(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD", "50000")
        monkeypatch.setenv("AUTONOMY_RELEASE_SUPPLIER_PAYOUT_AUTO_APPROVE_BELOW_USD", "1000")
        assert ap.resolve_gate("RELEASE_SUPPLIER_PAYOUT", amount=5000.0).level == "L0"
        assert ap.resolve_gate("RELEASE_ESCROW_TRANCHE", amount=5000.0).level == "L2"

    @pytest.mark.asyncio
    async def test_set_policy_gates_and_threshold(self):
        await ap.set_policy({}, gates={"RELEASE_ESCROW_TRANCHE": {"auto_approve_below_usd": 2500, "timeout_seconds": 3600}},
                            money_auto_approve_below_usd=10000)
        assert ap.money_auto_approve_below_usd() == 10000.0
        assert ap.auto_approve_below_usd("RELEASE_ESCROW_TRANCHE") == 2500.0
        assert ap.auto_approve_below_usd("RELEASE_SUPPLIER_PAYOUT") == 10000.0
        view = ap.effective_policy()["RELEASE_ESCROW_TRANCHE"]
        assert view["timeout_seconds"] == 3600 and view["threshold_override"] == 2500.0
        with pytest.raises(ValueError):
            await ap.set_policy({}, gates={"ISSUE_PO": {"auto_approve_below_usd": 100}})
        with pytest.raises(ValueError):
            await ap.set_policy({}, gates={"ISSUE_PO": {"default_on_timeout": "MAYBE"}})
        await ap.set_policy({}, gates={"RELEASE_ESCROW_TRANCHE": None}, money_auto_approve_below_usd=None)
        assert ap.money_auto_approve_below_usd() is None and ap.current_overrides() == {}

    @pytest.mark.asyncio
    async def test_v1_level_contract_kept(self):
        assert await ap.set_policy({"DISPATCH_RFQ": "L1"}) == {"DISPATCH_RFQ": "L1"}
        assert ap.resolve_level("DISPATCH_RFQ") == "L1"
        assert {a.value for a in __import__("shared.domain.enums", fromlist=["ApprovalAction"]).ApprovalAction} == {
            cls.action for cls in ap.ACTION_CLASSES
        }


# ---------------------------------------------------------------------------
# _create_gate uses the spec
# ---------------------------------------------------------------------------

class TestCreateGate:
    @pytest.mark.asyncio
    async def test_l1_gate_carries_timeout_and_default(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_ISSUE_PO", "L1")
        monkeypatch.setenv("AUTONOMY_ISSUE_PO_TIMEOUT_SECONDS", "1800")
        gate = AsyncMock()
        with patch("services.hitl_approval.service.create_approval", new=gate):
            await c._create_gate("DEAL-1", "ISSUE_PO", "s", 100.0, {}, "k", deal=_deal("ESCROW_FUNDED"))
        req = gate.await_args.args[0]
        assert req.autonomy_level == "L1" and req.default_on_timeout == "APPROVE"
        assert req.expires_at is not None
        assert datetime.fromisoformat(req.expires_at) - datetime.now(timezone.utc) < timedelta(seconds=1900)

    @pytest.mark.asyncio
    async def test_money_gate_under_threshold_auto_approves_with_proof(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD", "20000")
        gate = AsyncMock()
        hold = SimpleNamespace(
            escrow_id="ESC-1", status="FUNDED", amount=30000.0, currency="USD", released_total=0.0,
            refunded_total=0.0, fees_total=0.0, funded_amount=30000.0,
            tranches=[SimpleNamespace(tranche_id="T1", trigger="PO_ISSUED", pct=0.3, amount=9000.0, status="PENDING")],
        )
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        with (
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c._tranche_gate(_deal("PO_ISSUED"), "PO_ISSUED", ["PO-1"], "t")
            await c._tranche_gate(_deal("PO_ISSUED"), "PO_ISSUED", [""], "t", key_suffix="-noproof")
        with_proof, no_proof = [call.args[0] for call in gate.await_args_list]
        assert with_proof.autonomy_level == "L2" and "auto_approved_below_value_threshold" in with_proof.risk_flags
        assert no_proof.autonomy_level == "L0"  # no proof refs → conditions not met

    @pytest.mark.asyncio
    async def test_forced_level_wins(self):
        gate = AsyncMock()
        with patch("services.hitl_approval.service.create_approval", new=gate):
            await c._create_gate("DEAL-1", "DISPATCH_RFQ", "s", None, {}, "k", autonomy_level="L0", deal=_deal())
        assert gate.await_args.args[0].autonomy_level == "L0"


# ---------------------------------------------------------------------------
# Supplier auto-source + quantity + terms
# ---------------------------------------------------------------------------

class TestAutoSourceAndQuantity:
    async def _quote(self, payload, deal=None):
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal or _deal("MATCHING"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("services.sourcing.service.get_quote", new=AsyncMock(side_effect=RuntimeError("no repo"))),
        ):
            await c.on_quote_received(DomainEvent(topic=EventTopic.QUOTE_RECEIVED, deal_id="DEAL-1", payload=payload))
        return gate.await_args.args[0]

    @pytest.mark.asyncio
    async def test_verified_in_corridor_is_auto_sourced(self):
        req = await self._quote({"supplier_id": "SUP-9", "unit_price_usd": 1100.0, "re_scored_decision": "DISCARD",
                                 "gst_verified": True, "gst_checked": True, "iec_active": True, "iec_checked": True})
        assert req.autonomy_level == "L2" and "auto_sourced_verified_in_corridor" in req.risk_flags
        assert "auto-sourced" in req.summary

    @pytest.mark.asyncio
    async def test_unchecked_registry_still_asks(self):
        req = await self._quote({"supplier_id": "SUP-9", "unit_price_usd": 1100.0, "re_scored_decision": "DISCARD",
                                 "gst_verified": True, "iec_active": True})  # defaults, never checked
        assert req.autonomy_level == "L0"

    @pytest.mark.asyncio
    async def test_outside_corridor_asks(self):
        req = await self._quote({"supplier_id": "SUP-9", "unit_price_usd": 1200.0, "re_scored_decision": "DISCARD",
                                 "gst_verified": True, "gst_checked": True, "iec_active": True, "iec_checked": True})
        assert req.autonomy_level == "L0"  # 1200 > 1255 × 0.92

    @pytest.mark.asyncio
    async def test_rule_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("SOURCING_AUTO_SOURCE_VERIFIED", "false")
        req = await self._quote({"supplier_id": "SUP-9", "unit_price_usd": 1100.0, "re_scored_decision": "DISCARD",
                                 "gst_verified": True, "gst_checked": True, "iec_active": True, "iec_checked": True})
        assert req.autonomy_level == "L0"

    def test_deal_quantity_and_terms(self):
        assert c._deal_quantity(_deal(quantity_mt=40.0)) == 40.0
        assert c._deal_quantity(_deal(quantity_mt=None)) == c._DEFAULT_QUANTITY_MT
        terms = c.payment_terms_for("tazapay")
        assert "30% on Purchase Order" in terms and "10% on delivery acceptance" in terms and "Tazapay" in terms

    @pytest.mark.asyncio
    async def test_rfq_payload_uses_deal_quantity(self):
        gate = AsyncMock()
        deal = _deal("MATCHING", quantity_mt=40.0, match_results=[SimpleNamespace(
            supplier_id="SUP-1", company_name="Mill", decision="SOURCE", composite_score=80, fob_price_usd=1000.0)])
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("shared.orchestration.auto_flow.enabled", return_value=False),
        ):
            await c.on_deal_created(DomainEvent(topic=EventTopic.DEAL_CREATED, deal_id="DEAL-1", payload={"status": "MATCHING"}))
        assert gate.await_args.args[0].callback_payload["quantity_mt"] == 40.0

    def test_quantity_from_buyer_lead(self):
        from services.deals.service import _buyer_candidates_from_seed, _quantity_to_mt
        assert _quantity_to_mt(20, "tons") == 20.0 and _quantity_to_mt(500, "kg") == 0.5
        assert _quantity_to_mt(10, "pieces") is None and _quantity_to_mt("x", "kg") is None
        cands = _buyer_candidates_from_seed({"market": "DE", "top_buyers": [
            {"buyer_name": "Acme", "buyer_country": "DE", "email": "a@acme.de", "quantity": 25, "quantity_unit": "tons", "fit_score": 80},
            {"buyer_name": "acme", "buyer_country": "DE"},
            {"buyer_name": "", "buyer_country": "DE"},
        ]})
        assert len(cands) == 1 and cands[0].quantity_mt == 25.0 and cands[0].status == "pending"


# ---------------------------------------------------------------------------
# Buyer goods acceptance / complaint after delivery (e-mail)
# ---------------------------------------------------------------------------

class TestDeliveryReply:
    def _hold(self, disputed=False, pending=True):
        return SimpleNamespace(
            escrow_id="ESC-1", currency="USD", disputed=disputed,
            tranches=[SimpleNamespace(tranche_id="T4", trigger="ACCEPTED", status="PENDING" if pending else "RELEASED", amount=3750.0)],
        )

    async def _run(self, body, hold, status="CLEARED"):
        import services.email_processor.repository as ep_repo_mod
        from services.email_processor.repository import InMemoryEmailProcessorRepository
        from services.email_processor.schemas import InboundEmailRecord, InboundEmailSource, InboundEmailStatus, InboundRawEmail
        from services.email_processor.service import _handle_post_agreement_reply
        ep_repo_mod._repository = InMemoryEmailProcessorRepository()
        try:
            raw = InboundRawEmail(from_address="buyer@acme.de", to_address="ops@portless.io", subject="Re: delivery",
                                  body_text=body, source=InboundEmailSource.MANUAL, received_at=NOW)
            record = InboundEmailRecord(inbound_id="IN-1", from_address=raw.from_address, to_address=raw.to_address,
                                        subject=raw.subject, body_text=body, source=raw.source,
                                        status=InboundEmailStatus.MAPPED, received_at=NOW, created_at=NOW, updated_at=NOW,
                                        deal_id="DEAL-1", party="buyer", negotiation_id="n1")
            await ep_repo_mod._repository.save(record)
            mapping = SimpleNamespace(party="buyer", session_status="AGREED")
            gate = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-9"))
            dispute = AsyncMock()
            eg_repo = AsyncMock()
            eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
            with (
                patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal(status))),
                patch("services.dealmaker.repository.get_repository", return_value=AsyncMock(get_pi_by_deal=AsyncMock(return_value=None))),
                patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
                patch("services.escrow_guard.service.dispute_escrow", new=dispute),
                patch("services.hitl_approval.service.create_approval", new=gate),
            ):
                result = await _handle_post_agreement_reply(record, raw, mapping)
            return result, gate, dispute
        finally:
            ep_repo_mod._repository = None

    @pytest.mark.asyncio
    async def test_acceptance_creates_buyer_acceptance_gate(self):
        result, gate, dispute = await self._run("Goods received in good order. We accept the goods, thank you.", self._hold())
        req = gate.await_args.args[0]
        assert req.action == "CONFIRM_BUYER_ACCEPTANCE" and req.callback_payload["escrow_id"] == "ESC-1"
        assert req.financial_impact_usd == 3750.0 and req.idempotency_key == "buyer-accept-ESC-1"
        assert result.status.value == "processed"
        dispute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_complaint_disputes_escrow(self):
        result, gate, dispute = await self._run("The cargo arrived damaged, several bags torn. We dispute this shipment.", self._hold())
        dispute.assert_awaited_once()
        assert dispute.await_args.args[0] == "ESC-1"
        req = gate.await_args.args[0]
        assert req.action == "PARSE_INBOUND_EMAIL" and "escrow_disputed" in req.risk_flags
        assert result.status.value == "needs_operator"

    @pytest.mark.asyncio
    async def test_nothing_pending_falls_back_to_operator(self):
        result, gate, dispute = await self._run("We accept the goods", self._hold(pending=False))
        assert gate.await_args.args[0].action == "PARSE_INBOUND_EMAIL"


# ---------------------------------------------------------------------------
# Supplier reminders
# ---------------------------------------------------------------------------

class TestSupplierReminders:
    @pytest.mark.asyncio
    async def test_po_and_qc_reminders_then_exception(self, monkeypatch):
        from shared.orchestration import deal_watcher as dw
        dw.reset_for_tests()
        po_notify = AsyncMock(return_value="EMAIL-1")
        qc_notify = AsyncMock(return_value="EMAIL-2")
        published = []

        class _Pub:
            async def publish(self, event):
                published.append(event)

        monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
        deals = [
            SimpleNamespace(deal_id="P1", status=SimpleNamespace(value="PO_ISSUED"), updated_at=NOW - timedelta(hours=50)),
            SimpleNamespace(deal_id="Q1", status=SimpleNamespace(value="SUPPLIER_DISPATCH_READY"), updated_at=NOW - timedelta(hours=80)),
            SimpleNamespace(deal_id="Q2", status=SimpleNamespace(value="SUPPLIER_DISPATCH_READY"), updated_at=NOW - timedelta(hours=400)),
        ]
        repo = AsyncMock()
        repo.list_active = AsyncMock(return_value=deals)
        with (
            patch("services.deals.repository.get_repository", return_value=repo),
            patch("shared.orchestration.notifications.notify_po_reminder", new=po_notify),
            patch("shared.orchestration.notifications.notify_qc_readiness_reminder", new=qc_notify),
        ):
            counts = await dw.sweep_stalled_deals(NOW)
        assert counts["reminders"] == 2 and counts["exceptions"] == 1
        assert po_notify.await_args.args[1] == 1 and qc_notify.await_args.args[1] == 1
        assert published[0].payload["stage"] == "SUPPLIER_DISPATCH_READY:inspection_overdue"
        dw.reset_for_tests()
