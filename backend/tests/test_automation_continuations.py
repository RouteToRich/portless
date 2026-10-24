"""Automation legs added to shared/orchestration/continuations.py + their saga
callbacks (services/hitl_approval/callbacks.py).

  DEAL_CREATED → DISPATCH_RFQ gate            QUOTE_RECEIVED → CONFIRM_SUPPLIER_SELECTION
  NEGOTIATION_FAILED → exception + re-match   PI_SENT → PI emailed to buyer
  PI_ACCEPTED → escrow + funding email        PO_ISSUED → PO emailed + tranche T1
  QC_BOOKED → supplier notice                 QC_PASSED → documents + tranche T2 + freight gate
  QC_FAILED → DISPUTE + exception             FREIGHT_BOOKED → FILE_CUSTOMS gate + buyer notice
  CUSTOMS_CLEARED → tranche T3 + notice       PAYOUT_COMPLETED → completion notices
  OPPORTUNITY_DETECTED → deal (opt-in)        register_continuations covers every topic
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import DealRecord, DealStatus, MatchResult
from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import autonomy_policy as ap
from shared.orchestration import continuations as c

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")
    ap.reset_for_tests()
    monkeypatch.setattr(ap, "_mongo_collection", lambda: None)
    for cls in ap.ACTION_CLASSES:
        monkeypatch.delenv(f"AUTONOMY_{cls.action}", raising=False)
    monkeypatch.delenv("AUTONOMY_MONEY_CEILING", raising=False)
    yield
    ap.reset_for_tests()


def _mr(supplier_id="SUP-1", decision="SOURCE", score=80.0, fob=1004.0, name="Mill A") -> MatchResult:
    return MatchResult(
        match_id=f"M-{supplier_id}", supplier_id=supplier_id, company_name=name,
        composite_score=score, gatekeeper_passed=True, arbitrage_score=0, proximity_score=0,
        reliability_score=0, decision=decision, fob_price_usd=fob,
    )


def _deal(status: str = "MATCHING", **overrides) -> SimpleNamespace:
    base = dict(
        deal_id="DEAL-1", status=SimpleNamespace(value=status), market="DE", hs_code="1006",
        autonomy_profile=SimpleNamespace(value="L0"), winning_supplier_id="SUP-1",
        opportunity_id=None, buyer_target_price_usd=1255.0,
        match_results=[_mr()], parties=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def published(monkeypatch):
    events: list[DomainEvent] = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


def _gate_calls(gate: AsyncMock) -> list:
    return [call.args[0] for call in gate.await_args_list]


# ---------------------------------------------------------------------------
# DEAL_CREATED → RFQ
# ---------------------------------------------------------------------------

class TestDealCreated:
    @pytest.mark.asyncio
    async def test_creates_rfq_gate_with_shortlist(self):
        gate = AsyncMock()
        deal = _deal(match_results=[_mr("SUP-2", "DISCARD", 50), _mr("SUP-1", "SOURCE", 80),
                                    _mr("SUP-3", "SOURCE", 70), _mr("SUP-4", "DISCARD", 10)])
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_deal_created(DomainEvent(
                topic=EventTopic.DEAL_CREATED, deal_id="DEAL-1", payload={"status": "MATCHING"},
            ))
        req = gate.await_args.args[0]
        assert req.action == "DISPATCH_RFQ"
        assert req.autonomy_level == "L2"
        assert req.idempotency_key == "auto-rfq-DEAL-1"
        ids = [s["supplier_id"] for s in req.callback_payload["suppliers"]]
        assert ids == ["SUP-1", "SUP-3", "SUP-2"]  # SOURCE first, by score, max 3
        assert req.callback_payload["target_price_usd"] == 1255.0
        assert req.callback_payload["hs_code"] == "1006"

    @pytest.mark.asyncio
    async def test_ignores_draft_deals_and_other_statuses(self):
        gate = AsyncMock()
        with patch("services.hitl_approval.service.create_approval", new=gate):
            await c.on_deal_created(DomainEvent(
                topic=EventTopic.DEAL_CREATED, deal_id="DEAL-1", payload={"status": "DRAFT"},
            ))
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_match_seed_and_derives_buyer_target(self):
        gate = AsyncMock()
        fin = AsyncMock()
        seed = SimpleNamespace(top_sellers=[
            {"supplier_id": "IM-9", "company_name": "Seed Co", "fob_price_usd": 1000.0,
             "source": "indiamart", "contact_id": "c-9"},
            {"supplier_id": None, "company_name": "broken"},
        ])
        deal = _deal(match_results=[], buyer_target_price_usd=None, opportunity_id="OPP-1")
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.trade_min.service.get_match_seed", new=AsyncMock(return_value=seed)),
            patch("services.deals.service.update_financials", new=fin),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("shared.orchestration.auto_flow.enabled", return_value=False),
        ):
            await c.on_deal_created(DomainEvent(
                topic=EventTopic.DEAL_CREATED, deal_id="DEAL-1", payload={"status": "MATCHING"},
            ))
        req = gate.await_args.args[0]
        assert req.callback_payload["suppliers"] == [
            {"supplier_id": "IM-9", "company_name": "Seed Co", "source": "indiamart", "contact_id": "c-9",
             "email": None},
        ]
        assert req.callback_payload["target_price_usd"] == 1250.0  # 1.25 × 1000
        assert fin.await_args.args[1].buyer_target_price_usd == 1250.0

    @pytest.mark.asyncio
    async def test_defers_to_auto_flow(self):
        gate = AsyncMock()
        deal = _deal(opportunity_id="OPP-1")
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("shared.orchestration.auto_flow.enabled", return_value=True),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_deal_created(DomainEvent(
                topic=EventTopic.DEAL_CREATED, deal_id="DEAL-1", payload={"status": "MATCHING"},
            ))
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_candidates_leaves_deal_parked(self, published):
        gate = AsyncMock()
        deal = _deal(match_results=[], opportunity_id=None)
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_deal_created(DomainEvent(
                topic=EventTopic.DEAL_CREATED, deal_id="DEAL-1", payload={"status": "MATCHING"},
            ))
        gate.assert_not_awaited()
        assert published == []


# ---------------------------------------------------------------------------
# QUOTE_RECEIVED → supplier selection
# ---------------------------------------------------------------------------

class TestQuoteReceived:
    @pytest.mark.asyncio
    async def test_sourced_quote_is_noop(self):
        gate = AsyncMock()
        with patch("services.hitl_approval.service.create_approval", new=gate):
            await c.on_quote_received(DomainEvent(
                topic=EventTopic.QUOTE_RECEIVED, deal_id="DEAL-1",
                payload={"supplier_id": "SUP-1", "re_scored_decision": "SOURCE"},
            ))
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discarded_quote_proposes_supplier(self):
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal())),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("services.sourcing.service.get_quote",
                  new=AsyncMock(return_value=SimpleNamespace(company_name="Quoting Co"))),
        ):
            await c.on_quote_received(DomainEvent(
                topic=EventTopic.QUOTE_RECEIVED, deal_id="DEAL-1",
                payload={"quote_id": "Q-1", "supplier_id": "SUP-7", "unit_price_usd": 990.0,
                         "re_scored_decision": "DISCARD", "re_scored_cms": 61.0,
                         "gst_verified": True, "iec_active": True},
            ))
        req = gate.await_args.args[0]
        assert req.action == "CONFIRM_SUPPLIER_SELECTION"
        assert req.autonomy_level == "L0"
        assert "match_engine_discard" in req.risk_flags
        assert req.callback_payload["supplier_id"] == "SUP-7"
        assert req.callback_payload["company_name"] == "Quoting Co"
        assert req.callback_payload["fob_price_usd"] == 990.0
        assert req.callback_payload["composite_score"] == 61.0
        assert req.financial_impact_usd == 24750.0
        assert req.idempotency_key == "auto-select-DEAL-1-SUP-7"

    @pytest.mark.asyncio
    async def test_verification_failure_forces_l0_even_when_policy_is_l2(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_CONFIRM_SUPPLIER_SELECTION", "L2")
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal())),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_quote_received(DomainEvent(
                topic=EventTopic.QUOTE_RECEIVED, deal_id="DEAL-1",
                payload={"supplier_id": "SUP-7", "unit_price_usd": 990.0,
                         "re_scored_decision": None, "gst_verified": False, "iec_active": True},
            ))
        req = gate.await_args.args[0]
        assert req.autonomy_level == "L0"
        assert {"gst_not_verified", "unscored_quote"} <= set(req.risk_flags)

    @pytest.mark.asyncio
    async def test_skips_when_deal_already_negotiating(self):
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("NEGOTIATING"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_quote_received(DomainEvent(
                topic=EventTopic.QUOTE_RECEIVED, deal_id="DEAL-1",
                payload={"supplier_id": "SUP-7", "re_scored_decision": "DISCARD"},
            ))
        gate.assert_not_awaited()


# ---------------------------------------------------------------------------
# NEGOTIATION_FAILED → triage + re-match
# ---------------------------------------------------------------------------

class TestNegotiationFailed:
    @pytest.mark.asyncio
    async def test_exception_and_rematch_gate(self, published):
        gate = AsyncMock()
        deal = _deal("NEGOTIATION_FAILED", match_results=[
            _mr("SUP-1", "SOURCE", 80), _mr("SUP-2", "SOURCE", 75, 1010.0, "Mill B"),
            _mr("SUP-3", "DISCARD", 90),
        ])
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_negotiation_failed(DomainEvent(
                topic=EventTopic.NEGOTIATION_FAILED, deal_id="DEAL-1", payload={"reason": "max rounds"},
            ))
        assert published[0].topic == EventTopic.DEAL_EXCEPTION
        assert published[0].payload["category"] == "NEGOTIATION_FAILED"
        req = gate.await_args.args[0]
        assert req.action == "CONFIRM_SUPPLIER_SELECTION"
        assert req.callback_payload["supplier_id"] == "SUP-2"  # best remaining SOURCE
        assert req.idempotency_key == "auto-rematch-DEAL-1-SUP-2"

    @pytest.mark.asyncio
    async def test_no_alternate_only_exception(self, published):
        gate = AsyncMock()
        deal = _deal("NEGOTIATION_FAILED", match_results=[_mr("SUP-1", "SOURCE", 80)])
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_negotiation_failed(DomainEvent(
                topic=EventTopic.NEGOTIATION_FAILED, deal_id="DEAL-1", payload={},
            ))
        assert len(published) == 1
        gate.assert_not_awaited()


# ---------------------------------------------------------------------------
# Notifications hooked into the legs
# ---------------------------------------------------------------------------

class TestNotificationLegs:
    @pytest.mark.asyncio
    async def test_pi_sent_notifies_buyer(self):
        pi = SimpleNamespace(pi_id="PI-1")
        dm_repo = AsyncMock()
        dm_repo.get_pi = AsyncMock(return_value=pi)
        notify = AsyncMock(return_value="EMAIL-N1")
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("PI_SENT"))),
            patch("services.dealmaker.repository.get_repository", return_value=dm_repo),
            patch("shared.orchestration.notifications.notify_pi_issued", new=notify),
        ):
            await c.on_pi_sent(DomainEvent(
                topic=EventTopic.PI_SENT, deal_id="DEAL-1", payload={"pi_id": "PI-1"},
            ))
        notify.assert_awaited_once()
        assert notify.await_args.args[1] is pi

    @pytest.mark.asyncio
    async def test_pi_sent_without_pi_emits_exception(self, published):
        dm_repo = AsyncMock()
        dm_repo.get_pi_by_deal = AsyncMock(return_value=None)
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("PI_SENT"))),
            patch("services.dealmaker.repository.get_repository", return_value=dm_repo),
        ):
            await c.on_pi_sent(DomainEvent(topic=EventTopic.PI_SENT, deal_id="DEAL-1", payload={}))
        assert published[0].payload["stage"] == "PI_SENT→NOTIFY_BUYER"

    @pytest.mark.asyncio
    async def test_pi_accepted_sends_funding_instructions(self):
        pi = SimpleNamespace(pi_id="PI-1", total_value=31375.0)
        dm_repo = AsyncMock()
        dm_repo.get_pi_by_deal = AsyncMock(return_value=pi)
        escrow = SimpleNamespace(escrow_id="ESC-1")
        notify = AsyncMock(return_value="EMAIL-N2")
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("PI_ACCEPTED"))),
            patch("services.dealmaker.repository.get_repository", return_value=dm_repo),
            patch("services.escrow_guard.service.create_escrow", new=AsyncMock(return_value=escrow)),
            patch("shared.orchestration.notifications.notify_escrow_opened", new=notify),
        ):
            await c.on_pi_accepted(DomainEvent(topic=EventTopic.PI_ACCEPTED, deal_id="DEAL-1", payload={}))
        notify.assert_awaited_once()
        assert notify.await_args.args[1] is escrow
        assert notify.await_args.args[2] == "PI-1"

    @pytest.mark.asyncio
    async def test_po_issued_notifies_supplier_and_gates_tranche(self):
        po = SimpleNamespace(po_id="PO-1")
        dm_repo = AsyncMock()
        dm_repo.get_po = AsyncMock(return_value=po)
        hold = SimpleNamespace(
            escrow_id="ESC-1", status="FUNDED", amount=30000.0, currency="USD",
            tranches=[SimpleNamespace(tranche_id="T1", trigger="PO_ISSUED", pct=0.3, amount=9000.0, status="PENDING"),
                      SimpleNamespace(tranche_id="T2", trigger="QC_PASSED", pct=0.4, amount=12000.0, status="PENDING")],
        )
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        notify = AsyncMock(return_value="EMAIL-N3")
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("PO_ISSUED"))),
            patch("services.shield_link.service.book_qc",
                  new=AsyncMock(return_value=SimpleNamespace(tpo_id="TPO-1", provider="qima"))),
            patch("services.dealmaker.repository.get_repository", return_value=dm_repo),
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("shared.orchestration.notifications.notify_po_issued", new=notify),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c.on_po_issued(DomainEvent(
                topic=EventTopic.PO_ISSUED, deal_id="DEAL-1", payload={"po_id": "PO-1"},
            ))
        notify.assert_awaited_once()
        req = gate.await_args.args[0]
        assert req.action == "RELEASE_ESCROW_TRANCHE"
        assert req.autonomy_level == "L0"
        assert req.financial_impact_usd == 9000.0
        assert req.callback_payload == {
            "deal_id": "DEAL-1", "escrow_id": "ESC-1", "tranche_id": "T1", "trigger": "PO_ISSUED",
            "proof_refs": ["PO-1"], "idempotency_key": "auto-tranche-DEAL-1-T1",
        }
        assert req.idempotency_key == "auto-tranche-gate-DEAL-1-T1"
        assert "money_movement" in req.risk_flags

    @pytest.mark.asyncio
    async def test_tranche_gate_respects_money_ceiling(self):
        hold = SimpleNamespace(
            escrow_id="ESC-1", status="FUNDED", amount=30000.0, currency="USD",
            tranches=[SimpleNamespace(tranche_id="T2", trigger="QC_PASSED", pct=0.4, amount=None, status="PENDING")],
        )
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        gate = AsyncMock()
        deal = _deal("QC_PASSED", autonomy_profile=SimpleNamespace(value="L3"))
        with (
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c._tranche_gate(deal, "QC_PASSED", ["crf-hash", ""], "test")
        req = gate.await_args.args[0]
        assert req.autonomy_level == "L1"            # L3 clamped by the money ceiling
        assert req.financial_impact_usd == 12000.0   # amount derived from pct
        assert req.callback_payload["proof_refs"] == ["crf-hash"]

    @pytest.mark.asyncio
    async def test_tranche_gate_noop_without_funded_escrow(self):
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=SimpleNamespace(status="PENDING", tranches=[]))
        gate = AsyncMock()
        with (
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("services.hitl_approval.service.create_approval", new=gate),
        ):
            await c._tranche_gate(_deal("PO_ISSUED"), "PO_ISSUED", [], "test")
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_qc_booked_notifies_supplier(self):
        dm_repo = AsyncMock()
        dm_repo.get_po_by_deal = AsyncMock(return_value=SimpleNamespace(po_id="PO-1"))
        notify = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("SUPPLIER_DISPATCH_READY"))),
            patch("services.dealmaker.repository.get_repository", return_value=dm_repo),
            patch("shared.orchestration.notifications.notify_qc_booked", new=notify),
        ):
            await c.on_qc_booked(DomainEvent(
                topic=EventTopic.QC_BOOKED, deal_id="DEAL-1",
                payload={"provider": "qima", "ext_ref": "QC-9", "inspection_date": "2026-11-01"},
            ))
        notify.assert_awaited_once()
        assert notify.await_args.args[1]["ext_ref"] == "QC-9"
        assert notify.await_args.args[2] == "PO-1"


# ---------------------------------------------------------------------------
# QC_PASSED / QC_FAILED / FREIGHT_BOOKED / CLEARED / PAID
# ---------------------------------------------------------------------------

class TestShipmentLegs:
    @pytest.mark.asyncio
    async def test_qc_passed_generates_docs_then_freight_gate_last(self):
        gate = AsyncMock()
        docs = AsyncMock(return_value=["DOC-CI", "DOC-PL"])
        hold = SimpleNamespace(
            escrow_id="ESC-1", status="FUNDED", amount=30000.0, currency="USD",
            tranches=[SimpleNamespace(tranche_id="T2", trigger="QC_PASSED", pct=0.4, amount=12000.0, status="PENDING")],
        )
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("QC_PASSED"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch.object(c, "_generate_documents", new=docs),
        ):
            await c.on_qc_passed(DomainEvent(
                topic=EventTopic.QC_PASSED, deal_id="DEAL-1",
                payload={"crf_hash": "abc", "report_url": "https://r", "ext_ref": "QC-9"},
            ))
        docs.assert_awaited_once_with("DEAL-1")
        actions = [r.action for r in _gate_calls(gate)]
        assert actions == ["RELEASE_ESCROW_TRANCHE", "BOOK_FREIGHT"]
        assert _gate_calls(gate)[0].callback_payload["proof_refs"] == ["abc", "https://r", "QC-9"]

    @pytest.mark.asyncio
    async def test_qc_passed_doc_failure_does_not_block_freight_gate(self):
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("QC_PASSED"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch.object(c, "_generate_documents", new=AsyncMock(side_effect=RuntimeError("drive down"))),
        ):
            await c.on_qc_passed(DomainEvent(topic=EventTopic.QC_PASSED, deal_id="DEAL-1", payload={}))
        assert gate.await_args.args[0].action == "BOOK_FREIGHT"

    @pytest.mark.asyncio
    async def test_qc_failed_moves_to_dispute_and_raises(self, published):
        upd = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("QC_FAILED"))),
            patch("services.deals.service.update_status", new=upd),
        ):
            await c.on_qc_failed(DomainEvent(
                topic=EventTopic.QC_FAILED, deal_id="DEAL-1",
                payload={"ext_ref": "QC-9", "notes": "moisture 16%", "tpo_id": "TPO-1"},
            ))
        assert upd.await_args.args[1].target_status == DealStatus.DISPUTE
        assert "moisture 16%" in upd.await_args.args[1].reason
        assert published[0].payload["category"] == "QC_FAILED"
        assert published[0].payload["stage"] == "QC_FAILED"

    @pytest.mark.asyncio
    async def test_freight_booked_gates_customs_with_documents_and_notifies(self):
        gate = AsyncMock()
        notify = AsyncMock()
        docs = SimpleNamespace(documents=[
            SimpleNamespace(doc_id="DOC-CI", status="RENDERED"),
            SimpleNamespace(doc_id="DOC-X", status="FAILED"),
        ])
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("IN_TRANSIT"))),
            patch("services.docugen.service.list_deal_documents", new=AsyncMock(return_value=docs)),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("shared.orchestration.notifications.notify_freight_booked", new=notify),
        ):
            await c.on_freight_booked(DomainEvent(
                topic=EventTopic.FREIGHT_BOOKED, deal_id="DEAL-1",
                payload={"provider": "cogoport", "ext_ref": "FR-1", "vessel": "MV X", "eta": "2026-12-01"},
            ))
        req = gate.await_args.args[0]
        assert req.action == "FILE_CUSTOMS"
        assert req.callback_payload["documents"] == ["DOC-CI"]
        assert req.callback_payload["idempotency_key"] == "auto-cha-DEAL-1"
        assert req.idempotency_key == "auto-customs-gate-DEAL-1"
        notify.assert_awaited_once()
        assert notify.await_args.args[1]["vessel"] == "MV X"

    @pytest.mark.asyncio
    async def test_freight_booked_renders_docs_when_missing(self):
        gate = AsyncMock()
        docs = AsyncMock(return_value=["DOC-NEW"])
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("IN_TRANSIT"))),
            patch("services.docugen.service.list_deal_documents",
                  new=AsyncMock(return_value=SimpleNamespace(documents=[]))),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch.object(c, "_generate_documents", new=docs),
            patch("shared.orchestration.notifications.notify_freight_booked", new=AsyncMock()),
        ):
            await c.on_freight_booked(DomainEvent(topic=EventTopic.FREIGHT_BOOKED, deal_id="DEAL-1", payload={}))
        assert gate.await_args.args[0].callback_payload["documents"] == ["DOC-NEW"]

    @pytest.mark.asyncio
    async def test_cleared_gates_tranche_notifies_then_payout_last(self):
        gate = AsyncMock()
        notify = AsyncMock()
        hold = SimpleNamespace(
            escrow_id="ESC-1", status="FUNDED", amount=30000.0, currency="USD",
            tranches=[SimpleNamespace(tranche_id="T3", trigger="CLEARED", pct=0.2, amount=6000.0, status="PENDING")],
        )
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        session = SimpleNamespace(current_ask=1004.0, quantity_mt=25.0)
        neg_repo = AsyncMock()
        neg_repo.get_session_by_deal = AsyncMock(return_value=session)
        orders = SimpleNamespace(orders=[SimpleNamespace(order_type="CHA", ext_ref="SB-123")])
        # The payout gate is capped at the escrow ledger's available funds; give it
        # a ledger with room so the amount stays the supplier ask x quantity.
        ledger = SimpleNamespace(payouts_total=0.0, payouts_pending=0.0, available_for_payout=30000.0,
                                 released_total=30000.0, supplier_owed=None)
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("CLEARED"))),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("services.escrow_guard.service.get_deal_ledger", new=AsyncMock(return_value=ledger)),
            patch("agents.negotiation_agent.repository.get_repository", return_value=neg_repo),
            patch("services.shield_link.service.list_deal_orders", new=AsyncMock(return_value=orders)),
            patch("shared.orchestration.notifications.notify_cargo_cleared", new=notify),
        ):
            await c.on_customs_cleared(DomainEvent(
                topic=EventTopic.CUSTOMS_CLEARED, deal_id="DEAL-1", payload={"ext_ref": "FR-1", "milestone": "ARRIVED"},
            ))
        reqs = _gate_calls(gate)
        assert [r.action for r in reqs] == ["RELEASE_ESCROW_TRANCHE", "RELEASE_SUPPLIER_PAYOUT"]
        assert reqs[0].callback_payload["proof_refs"] == ["FR-1", "SB-123"]
        assert reqs[1].callback_payload["amount"] == 25100.0
        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_payout_completed_notifies_both_parties(self):
        notify = AsyncMock(return_value=["a", "b"])
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("PAID"))),
            patch("shared.orchestration.notifications.notify_deal_completed", new=notify),
        ):
            await c.on_payout_completed(DomainEvent(topic=EventTopic.PAYOUT_COMPLETED, deal_id="DEAL-1", payload={}))
        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_payout_completed_skips_when_not_paid(self):
        notify = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("CLEARED"))),
            patch("shared.orchestration.notifications.notify_deal_completed", new=notify),
        ):
            await c.on_payout_completed(DomainEvent(topic=EventTopic.PAYOUT_COMPLETED, deal_id="DEAL-1", payload={}))
        notify.assert_not_awaited()


# ---------------------------------------------------------------------------
# OPPORTUNITY_DETECTED → deal (opt-in)
# ---------------------------------------------------------------------------

class TestOpportunityDetected:
    def _event(self, **overrides):
        payload = {"opportunity_id": "OPP-1", "arbitrage_rank": 75.0, "confidence_score": 70.0}
        payload.update(overrides)
        return DomainEvent(topic=EventTopic.OPPORTUNITY_DETECTED, payload=payload)

    @pytest.mark.asyncio
    async def test_off_by_default(self, monkeypatch):
        create = AsyncMock()
        with patch("services.deals.service.create_deal", new=create):
            await c.on_opportunity_detected(self._event())
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_deal_when_enabled(self, monkeypatch):
        from shared.config import settings
        monkeypatch.setattr(settings, "auto_deal_from_opportunity_enabled", True)
        opp = SimpleNamespace(opportunity_id="OPP-1", status="ACTIVE", source="comtrade", hs_code="1006", market="DE")
        create = AsyncMock(return_value=SimpleNamespace(deal_id="DEAL-NEW", status=SimpleNamespace(value="MATCHING")))
        with (
            patch("services.trade_min.service.get_opportunity", new=AsyncMock(return_value=opp)),
            patch("services.deals.service.list_deals", new=AsyncMock(return_value=[])),
            patch("services.deals.service.create_deal", new=create),
        ):
            await c.on_opportunity_detected(self._event())
        req = create.await_args.args[0]
        assert req.opportunity_id == "OPP-1" and req.hs_code == "1006" and req.market == "DE"
        assert req.operator_id == "system:auto-deal"
        assert create.await_args.kwargs["org_id"] == "portless"

    @pytest.mark.asyncio
    async def test_thresholds_simulated_and_duplicates_block(self, monkeypatch):
        from shared.config import settings
        monkeypatch.setattr(settings, "auto_deal_from_opportunity_enabled", True)
        create = AsyncMock()
        opp = SimpleNamespace(opportunity_id="OPP-1", status="ACTIVE", source="simulated", hs_code="1006", market="DE")
        with (
            patch("services.trade_min.service.get_opportunity", new=AsyncMock(return_value=opp)),
            patch("services.deals.service.list_deals", new=AsyncMock(return_value=[])),
            patch("services.deals.service.create_deal", new=create),
        ):
            await c.on_opportunity_detected(self._event(arbitrage_rank=10.0))   # below rank
            await c.on_opportunity_detected(self._event())                      # simulated source
            opp.source = "comtrade"
            with patch("services.deals.service.list_deals", new=AsyncMock(return_value=[
                SimpleNamespace(opportunity_id=None, hs_code="1006", market="DE",
                                status=SimpleNamespace(value="NEGOTIATING"), deal_id="DEAL-OLD"),
            ])):
                await c.on_opportunity_detected(self._event())                  # live duplicate
        create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Registration + saga callbacks
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_every_topic_subscribed(self):
        seen: list[EventTopic] = []

        class _Bus:
            def subscribe(self, topic, handler):
                seen.append(topic)

        c.register_continuations(_Bus())
        assert set(seen) >= {
            EventTopic.OPPORTUNITY_DETECTED, EventTopic.DEAL_CREATED, EventTopic.QUOTE_RECEIVED,
            EventTopic.NEGOTIATION_STARTED, EventTopic.NEGOTIATION_FAILED, EventTopic.NEGOTIATION_AGREED,
            EventTopic.PI_SENT, EventTopic.PI_ACCEPTED, EventTopic.ESCROW_FUNDED, EventTopic.PO_ISSUED,
            EventTopic.QC_BOOKED, EventTopic.QC_PASSED, EventTopic.QC_FAILED, EventTopic.FREIGHT_BOOKED,
            EventTopic.CUSTOMS_CLEARED, EventTopic.PAYOUT_COMPLETED, EventTopic.DEAL_EXCEPTION,
        }
        assert len(seen) == len(set(seen))


class TestCallbacks:
    @pytest.fixture
    def deals_repo(self, monkeypatch):
        repo = InMemoryFallbackRepository()
        monkeypatch.setattr("services.deals.repository._repository", repo)
        return repo

    def _record(self, status: DealStatus, results: list[MatchResult], winner: str | None) -> DealRecord:
        return DealRecord(
            deal_id="DEAL-1", operator_id="op", org_id="portless", hs_code="1006", market="DE",
            status=status, match_results=results, winning_supplier_id=winner,
            created_at=NOW, updated_at=NOW,
        )

    @pytest.mark.asyncio
    async def test_confirm_supplier_selection_from_matching(self, deals_repo):
        from services.hitl_approval.callbacks import execute_callback
        await deals_repo.save(self._record(DealStatus.MATCHING, [_mr("SUP-1", "DISCARD", 40)], None))
        out = await execute_callback("CONFIRM_SUPPLIER_SELECTION", "DEAL-1", {
            "supplier_id": "SUP-7", "company_name": "Quoting Co", "fob_price_usd": 990.0,
            "composite_score": 61.0, "buyer_target_price_usd": 1250.0,
        }, "APPR-1")
        assert out["status"] == "NEGOTIATING"
        deal = await deals_repo.get("DEAL-1")
        assert deal.status == DealStatus.NEGOTIATING
        assert deal.winning_supplier_id == "SUP-7" and deal.winning_cms == 61.0
        assert deal.buyer_target_price_usd == 1250.0
        new = next(m for m in deal.match_results if m.supplier_id == "SUP-7")
        assert new.decision == "SOURCE" and new.fob_price_usd == 990.0

    @pytest.mark.asyncio
    async def test_confirm_supplier_selection_rematch_after_failure(self, deals_repo):
        from services.hitl_approval.callbacks import execute_callback
        await deals_repo.save(self._record(
            DealStatus.NEGOTIATION_FAILED, [_mr("SUP-1"), _mr("SUP-2", fob=1010.0)], "SUP-1",
        ))
        await execute_callback("CONFIRM_SUPPLIER_SELECTION", "DEAL-1", {"supplier_id": "SUP-2"}, "APPR-2")
        deal = await deals_repo.get("DEAL-1")
        assert deal.status == DealStatus.NEGOTIATING
        assert deal.winning_supplier_id == "SUP-2"
        assert [h.status for h in deal.status_history[-2:]] == [DealStatus.MATCHING, DealStatus.NEGOTIATING]

    @pytest.mark.asyncio
    async def test_dispatch_rfq_and_tranche_and_pi_callbacks_forward_payloads(self):
        from services.hitl_approval.callbacks import execute_callback
        rfq = AsyncMock(return_value=SimpleNamespace(rfq_id="RFQ-1", status="COMPLETE", dispatched_count=2, failed_count=0))
        rel = AsyncMock(return_value=SimpleNamespace(released_total=9000.0, status="FUNDED"))
        acc = AsyncMock(return_value=SimpleNamespace(status="ACCEPTED"))
        with (
            patch("services.sourcing.service.dispatch_rfq", new=rfq),
            patch("services.escrow_guard.service.release_tranche", new=rel),
            patch("services.dealmaker.service.accept_pi", new=acc),
        ):
            out = await execute_callback("DISPATCH_RFQ", "DEAL-1", {
                "deal_id": "DEAL-1", "hs_code": "1006",
                "suppliers": [{"supplier_id": "S1", "source": "other"}, {"supplier_id": "S2", "source": "indiamart"}],
            }, "A")
            assert out["dispatched_count"] == 2
            assert [s.supplier_id for s in rfq.await_args.args[0].suppliers] == ["S1", "S2"]

            out = await execute_callback("RELEASE_ESCROW_TRANCHE", "DEAL-1", {
                "escrow_id": "ESC-1", "tranche_id": "T1", "proof_refs": ["PO-1"], "idempotency_key": "k",
            }, "B")
            assert out["released_total"] == 9000.0
            assert rel.await_args.args[0] == "ESC-1"
            assert rel.await_args.args[1].tranche_id == "T1"

            out = await execute_callback("CONFIRM_PI_ACCEPTANCE", "DEAL-1", {
                "pi_id": "PI-1", "accepted_by": "buyer@x", "buyer_email": "buyer@x", "notes": "n",
            }, "C")
            assert out["status"] == "ACCEPTED"
            assert acc.await_args.args[0] == "PI-1"
            assert acc.await_args.args[1].buyer_email == "buyer@x"

    @pytest.mark.asyncio
    async def test_send_deal_notification_uses_negotiation_sender(self):
        from services.hitl_approval.callbacks import execute_callback
        send = AsyncMock()
        with patch("agents.negotiation_agent.service.send_approved_email", new=send):
            out = await execute_callback("SEND_DEAL_NOTIFICATION", "DEAL-1",
                                         {"email_id": "EMAIL-N1", "kind": "pi_issued"}, "D")
        send.assert_awaited_once_with("EMAIL-N1", None)
        assert out["kind"] == "pi_issued"

    def test_soft_vs_hard_rejects(self):
        from services.hitl_approval.callbacks import SOFT_REJECT_ACTIONS
        assert {"SEND_NEGOTIATION_EMAIL", "CONFIRM_NEGOTIATION_AGREEMENT", "SEND_DEAL_NOTIFICATION",
                "DISPATCH_RFQ", "CONFIRM_SUPPLIER_SELECTION", "CONFIRM_PI_ACCEPTANCE",
                "PARSE_INBOUND_EMAIL", "CONFIRM_BUYER_ACCEPTANCE", "SEND_BUYER_OUTREACH"} == set(SOFT_REJECT_ACTIONS)
        assert "RELEASE_ESCROW_TRANCHE" not in SOFT_REJECT_ACTIONS
        assert "RELEASE_SUPPLIER_PAYOUT" not in SOFT_REJECT_ACTIONS
