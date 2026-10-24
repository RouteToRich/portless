"""Batch C controls: four-eyes approvals, double-entry journal, deal timeline +
evidence pack, Telegram push, daily digest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import services.deals.service  # noqa: F401  (bind before any repository patch)
import services.escrow_guard.service  # noqa: F401
import services.hitl_approval.service  # noqa: F401
from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import BuyerCandidate, DealRecord, DealStatus, StatusHistoryEntry
from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from services.hitl_approval.schemas import CreateApprovalRequest, ResolveApprovalRequest

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("FOUR_EYES_THRESHOLD_USD", "HITL_TELEGRAM_BOT_TOKEN", "HITL_TELEGRAM_CHAT_ID", "DIGEST_EMAIL_TO",
                "DIGEST_HOUR_UTC", "DAILY_DIGEST_ENABLED"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def hitl(monkeypatch):
    import services.hitl_approval.repository as repo_mod
    repo_mod._repository = InMemoryHitlApprovalRepository()
    repo_mod._motor_client = None
    monkeypatch.setattr("services.hitl_approval.service.notify", AsyncMock())
    monkeypatch.setattr("services.hitl_approval.service._emit", AsyncMock())
    callback = AsyncMock()
    monkeypatch.setattr("services.hitl_approval.service.execute_callback", callback)
    yield SimpleNamespace(repo=repo_mod._repository, callback=callback)
    repo_mod._repository = None


# ---------------------------------------------------------------------------
# Four-eyes
# ---------------------------------------------------------------------------

class TestFourEyes:
    @pytest.mark.asyncio
    async def test_money_gate_above_threshold_needs_two_distinct_approvers(self, hitl, monkeypatch):
        from services.hitl_approval.service import SameApproverError, create_approval, resolve_approval
        monkeypatch.setenv("FOUR_EYES_THRESHOLD_USD", "10000")
        created = await create_approval(CreateApprovalRequest(
            deal_id="DEAL-4E", action="RELEASE_SUPPLIER_PAYOUT", financial_impact_usd=15000.0,
            autonomy_level="L2", callback_payload={"deal_id": "DEAL-4E", "amount": 15000.0},
        ))
        assert created.status == "PENDING" and created.required_approvals == 2 and created.approvals_remaining == 2
        assert created.autonomy_level == "L0" and "four_eyes_required" in created.risk_flags
        hitl.callback.assert_not_called()

        first = await resolve_approval(created.approval_id, ResolveApprovalRequest(decision="APPROVE", resolved_by="ops@a"))
        assert first.status == "PENDING" and first.approvals_remaining == 1 and first.approvals[0].approved_by == "ops@a"
        hitl.callback.assert_not_called()
        with pytest.raises(SameApproverError):
            await resolve_approval(created.approval_id, ResolveApprovalRequest(decision="APPROVE", resolved_by="ops@a"))

        final = await resolve_approval(created.approval_id, ResolveApprovalRequest(decision="APPROVE", resolved_by="cfo@b"))
        assert final.status == "APPROVED" and [s.approved_by for s in final.approvals] == ["ops@a", "cfo@b"]
        assert final.resolved_by == "cfo@b" and final.approvals_remaining == 0
        hitl.callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_reject_by_one_operator_closes_four_eyes_gate(self, hitl, monkeypatch):
        from services.hitl_approval.service import create_approval, resolve_approval
        monkeypatch.setenv("FOUR_EYES_THRESHOLD_USD", "10000")
        created = await create_approval(CreateApprovalRequest(
            deal_id="DEAL-4E2", action="RELEASE_ESCROW_TRANCHE", financial_impact_usd=20000.0, autonomy_level="L0",
        ))
        await resolve_approval(created.approval_id, ResolveApprovalRequest(decision="APPROVE", resolved_by="ops@a"))
        rejected = await resolve_approval(created.approval_id, ResolveApprovalRequest(decision="REJECT", resolved_by="cfo@b"))
        assert rejected.status == "REJECTED" and len(rejected.approvals) == 1

    @pytest.mark.asyncio
    async def test_below_threshold_or_non_money_is_single_approver(self, hitl, monkeypatch):
        from services.hitl_approval.service import create_approval, four_eyes_required
        monkeypatch.setenv("FOUR_EYES_THRESHOLD_USD", "10000")
        assert four_eyes_required("RELEASE_SUPPLIER_PAYOUT", 9999.0) is False
        assert four_eyes_required("ISSUE_PO", 50000.0) is False
        assert four_eyes_required("RELEASE_SUPPLIER_PAYOUT", None) is False
        small = await create_approval(CreateApprovalRequest(
            deal_id="DEAL-4E3", action="RELEASE_SUPPLIER_PAYOUT", financial_impact_usd=5000.0, autonomy_level="L2",
        ))
        assert small.status == "AUTO_APPROVED" and small.required_approvals == 1
        monkeypatch.delenv("FOUR_EYES_THRESHOLD_USD")
        assert four_eyes_required("RELEASE_SUPPLIER_PAYOUT", 1e9) is False


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def _tranche(tid, trigger, pct, amount, status, released_at=None):
    return SimpleNamespace(tranche_id=tid, trigger=trigger, pct=pct, amount=amount, status=status,
                           released_at=released_at, ext_ref=f"X-{tid}", proof_refs=[])


def _hold(**kw):
    base = dict(
        deal_id="DEAL-J1", escrow_id="ESC-J1", provider="tazapay", currency="USD", amount=50000.0, status="FUNDED",
        funded_amount=50000.0, funded_at=NOW, ext_ref="TZ-1", shortfall=None, refunded_total=0.0, fee_pct=1.0,
        fees_total=450.0, released_total=45000.0, provider_payload={}, updated_at=NOW,
        tranches=[
            _tranche("T1", "PO_ISSUED", 0.3, 15000.0, "RELEASED", NOW + timedelta(days=1)),
            _tranche("T2", "QC_PASSED", 0.4, 20000.0, "RELEASED", NOW + timedelta(days=10)),
            _tranche("T3", "CLEARED", 0.2, 10000.0, "RELEASED", NOW + timedelta(days=30)),
            _tranche("T4", "ACCEPTED", 0.1, 5000.0, "PENDING"),
        ],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _ledger(**kw):
    base = dict(
        outstanding=5000.0, issues=[], supplier_owed=46000.0,
        payouts=[SimpleNamespace(payout_id="PAY-1", payee_id="SUP-1", amount=40000.0, currency="USD", status="COMPLETED",
                                 ext_ref="PN-1", kind="supplier_payout", completed_at=NOW + timedelta(days=31),
                                 created_at=NOW + timedelta(days=30)),
                 SimpleNamespace(payout_id="PAY-2", payee_id="SUP-1", amount=4550.0, currency="USD", status="REQUESTED",
                                 ext_ref=None, kind="supplier_balance", completed_at=None, created_at=NOW + timedelta(days=32))],
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestJournal:
    def test_entries_balance_and_match_ledger(self):
        from services.escrow_guard.journal import journal_csv, journal_from
        j = journal_from(_hold(), _ledger(), po_issued_at=NOW + timedelta(hours=12), now=NOW + timedelta(days=40))
        assert j.balanced and not j.issues
        assert j.debits_total == j.credits_total
        events = [e.event for e in j.entries]
        assert events[:2] == ["escrow_funded", "po_issued"]
        assert "tranche_released:T1" in events and "revenue_recognised:T3" in events and "supplier_payout:supplier_payout" in events
        assert "supplier_payout:supplier_balance" not in events  # REQUESTED payouts are not cash yet
        assert j.balances["escrow_trust"] == 5000.0  # == ledger outstanding
        assert j.balances["sales_revenue"] == -45000.0 and j.balances["provider_fees"] == 450.0
        assert j.balances["portless_cash"] == pytest.approx(45000.0 - 450.0 - 40000.0)
        assert j.balances["supplier_payable"] == pytest.approx(-(46000.0 - 40000.0))
        csv_text = journal_csv(j)
        assert csv_text.splitlines()[0].startswith("deal_id,seq,at,event,debit_account,credit_account,amount")
        assert len(csv_text.splitlines()) == len(j.entries) + 1

    def test_refund_and_mismatch_are_reported(self):
        from services.escrow_guard.journal import journal_from
        hold = _hold(status="REFUNDED", refunded_total=5000.0, tranches=_hold().tranches)
        j = journal_from(hold, _ledger(outstanding=0.0), now=NOW)
        assert any(e.event == "refund_to_buyer" for e in j.entries) and j.balances["escrow_trust"] == 0.0 and j.balanced
        off = journal_from(_hold(), _ledger(outstanding=9999.0, issues=["ledger drift"]), now=NOW)
        assert not off.balanced and any("escrow_trust balance" in i for i in off.issues) and "ledger: ledger drift" in off.issues

    @pytest.mark.asyncio
    async def test_build_journal_uses_live_hold_and_ledger(self, monkeypatch):
        from services.escrow_guard import journal as jmod
        from services.escrow_guard.service import HoldNotFoundError
        repo = AsyncMock()
        repo.get_hold_by_deal = AsyncMock(return_value=_hold())
        monkeypatch.setattr("services.escrow_guard.repository.get_repository", lambda: repo)
        with (
            patch("services.escrow_guard.service.get_deal_ledger", new=AsyncMock(return_value=_ledger())),
            patch("services.dealmaker.repository.get_repository", return_value=AsyncMock(get_po_by_deal=AsyncMock(return_value=SimpleNamespace(issued_at=NOW)))),
        ):
            j = await jmod.build_journal("DEAL-J1")
        assert j.deal_id == "DEAL-J1" and j.balanced and j.entries[1].at == NOW
        repo.get_hold_by_deal = AsyncMock(return_value=None)
        with pytest.raises(HoldNotFoundError):
            await jmod.build_journal("DEAL-NONE")


# ---------------------------------------------------------------------------
# Timeline + evidence pack
# ---------------------------------------------------------------------------

def _deal_record():
    return DealRecord(
        deal_id="DEAL-EV1", operator_id="op", org_id="portless", hs_code="090411", market="DE",
        status=DealStatus.CLEARED, created_at=NOW - timedelta(days=40), updated_at=NOW,
        status_history=[StatusHistoryEntry(status=DealStatus.MATCHING, transitioned_at=NOW - timedelta(days=39), reason="scored"),
                        StatusHistoryEntry(status=DealStatus.NEGOTIATING, transitioned_at=NOW - timedelta(days=35))],
        buyer_candidates=[BuyerCandidate(candidate_id="b1", legal_name="Acme", country="DE", email="a@acme.de",
                                         status="selected", contacted_at=NOW - timedelta(days=38),
                                         replied_at=NOW - timedelta(days=37), reply_preview="interested")],
    )


def _data():
    deal = _deal_record()
    approval = SimpleNamespace(approval_id="APPR-1", action="ISSUE_PO", autonomy_level="L0", summary="PO 46k",
                               status="APPROVED", created_at=NOW - timedelta(days=20), resolved_at=NOW - timedelta(days=19),
                               resolution_note="ok", resolved_by="ops@a", approvals=[])
    pi = SimpleNamespace(pi_id="PI-1", total_value=50000.0, currency="USD", issued_at=NOW - timedelta(days=30),
                         expires_at=NOW - timedelta(days=20))
    po = SimpleNamespace(po_id="PO-1", total_value=46000.0, currency="USD", issued_at=NOW - timedelta(days=19),
                         net_margin_pct=8.7, margin_floor_pct=8.0, confirmed_at=None)
    doc = SimpleNamespace(doc_id="DOC-1", doc_type=SimpleNamespace(value="CI"), status="RENDERED",
                          rendered_at=NOW - timedelta(days=12), created_at=NOW - timedelta(days=12), content_hash="abc123def4567890")
    order = SimpleNamespace(tpo_id="TPO-QC-1", order_type="QC", provider="sgs", status="COMPLETED", ext_ref="SGS-1",
                            created_at=NOW - timedelta(days=18), qc_completed_at=NOW - timedelta(days=13), qc_result="PASS",
                            qc_report_url=None, last_milestone=None, last_milestone_at=None, last_milestone_source=None)
    hold = SimpleNamespace(escrow_id="ESC-1", provider="tazapay", amount=50000.0, currency="USD", created_at=NOW - timedelta(days=29),
                           funded_at=NOW - timedelta(days=25), funded_amount=50000.0, shortfall=None,
                           tranches=[_tranche("T1", "PO_ISSUED", 0.3, 15000.0, "RELEASED", NOW - timedelta(days=18))],
                           delivery_confirmed_at=None, inspection_opened_at=None, inspection_trigger=None, inspection_deadline=None,
                           buyer_accepted_at=None, disputed_at=None, settled_at=None)
    ledger = SimpleNamespace(payouts=[SimpleNamespace(payout_id="PAY-1", amount=14000.0, currency="USD", kind="supplier_payout",
                                                      status="COMPLETED", ext_ref="PN-1", created_at=NOW - timedelta(days=2),
                                                      completed_at=NOW - timedelta(days=1))])
    exc = SimpleNamespace(exception_id="EXC-1", category=SimpleNamespace(value="DOCUMENT_ERROR"), raw_detail="render failed",
                          source_event="docugen", raised_at=NOW - timedelta(days=12, hours=1), resolved_at=NOW - timedelta(days=12),
                          status=SimpleNamespace(value="RESOLVED"), resolution_note="regenerated")
    email = SimpleNamespace(email_id="EMAIL-1", direction=SimpleNamespace(value="outbound"), status=SimpleNamespace(value="sent"),
                            subject="Offer round 1", from_address="ops@portless.io", to_address="a@acme.de",
                            sent_at=NOW - timedelta(days=34), created_at=NOW - timedelta(days=34), body="Dear buyer")
    rnd = SimpleNamespace(round_id="R1", round_no=1, party="init", inbound_price=None, outbound_message="opening offer",
                          created_at=NOW - timedelta(days=34, hours=1))
    return {"deal": deal, "approvals": [approval], "pi": pi, "po": po, "documents": [doc], "orders": [order], "hold": hold,
            "ledger": ledger, "exceptions": [exc], "emails": [email], "negotiation": {"session": None, "rounds": [rnd]},
            "rfqs": [], "quotes": [], "journal": None}


class TestEvidence:
    def test_timeline_is_sorted_and_complete(self):
        from shared.orchestration.evidence import _timeline_from
        events = _timeline_from(_data())
        ats = [e["at"] for e in events]
        assert ats == sorted(ats) and events[0]["title"] == "Deal created"
        kinds = {e["kind"] for e in events}
        assert {"deal", "status", "outreach", "negotiation", "email", "gate", "document", "operations", "money", "exception"} <= kinds
        assert any("Tranche T1 released" in e["title"] for e in events)
        assert any(e["title"].startswith("Supplier payout completed") for e in events)
        assert all(e["at"] for e in events)

    @pytest.mark.asyncio
    async def test_evidence_pack_hash_and_html(self, monkeypatch):
        from shared.orchestration import evidence
        repo = InMemoryFallbackRepository()
        monkeypatch.setattr("services.deals.repository._repository", repo)
        await repo.save(_deal_record())
        import services.hitl_approval.repository as hitl_repo_mod
        hitl_repo_mod._repository = InMemoryHitlApprovalRepository()
        try:
            pack = await evidence.build_evidence_pack("DEAL-EV1")
        finally:
            hitl_repo_mod._repository = None
        assert pack["deal"]["deal_id"] == "DEAL-EV1" and len(pack["content_hash"]) == 64
        assert pack["escrow"] is None and pack["journal"] is None
        assert pack["timeline"] and pack["timeline"][0]["kind"] == "deal"
        rehash = evidence._pack_hash(pack)
        assert rehash == pack["content_hash"]
        html_doc = evidence.render_evidence_html(pack)
        assert "Evidence pack — deal DEAL-EV1" in html_doc and pack["content_hash"] in html_doc and "Acme" in html_doc
        tl = await evidence.build_timeline("DEAL-EV1")
        assert tl["deal_id"] == "DEAL-EV1" and tl["count"] == len(tl["events"]) >= 4
        with pytest.raises(LookupError):
            await evidence.build_timeline("DEAL-MISSING")


# ---------------------------------------------------------------------------
# Telegram + digest
# ---------------------------------------------------------------------------

class TestTelegramAndDigest:
    @pytest.mark.asyncio
    async def test_telegram_send(self, monkeypatch):
        from shared import telegram
        assert telegram.configured() is False and await telegram.send_message("x") is False
        monkeypatch.setenv("HITL_TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("HITL_TELEGRAM_CHAT_ID", "42")
        calls = []

        class _Resp:
            status_code = 200
            text = "ok"

        class _Client:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None):
                calls.append((url, json))
                return _Resp()

        monkeypatch.setattr(telegram.httpx, "AsyncClient", _Client)
        assert await telegram.send_message("hello") is True
        assert calls[0][0].endswith("/bot123:abc/sendMessage") and calls[0][1]["chat_id"] == "42"

    def test_notification_telegram_text(self):
        from services.hitl_approval.notification import _telegram_text, _telegram_worthy
        a = {"status": "PENDING", "action": "RELEASE_SUPPLIER_PAYOUT", "deal_id": "D1", "autonomy_level": "L0",
             "financial_impact_usd": 15000.0, "summary": "Pay supplier", "deep_link": "http://ui/approvals/A1",
             "approvals": [{"approved_by": "ops@a"}]}
        assert _telegram_worthy("approval_created", a) and _telegram_worthy("approval_countersign_needed", a)
        assert not _telegram_worthy("approval_created", {**a, "status": "AUTO_APPROVED"})
        assert not _telegram_worthy("approval_resolved", a)
        text = _telegram_text("approval_countersign_needed", a)
        assert "Second approver" in text and "15,000.00 USD" in text and "ops@a" in text and "http://ui/approvals/A1" in text

    @pytest.mark.asyncio
    async def test_build_and_render_digest(self, monkeypatch):
        from shared.orchestration import digest
        approvals = [SimpleNamespace(approval_id="A1", deal_id="D1", action="ISSUE_PO", created_at=NOW - timedelta(hours=30),
                                     financial_impact_usd=46000.0, deep_link="l", required_approvals=1, approvals=[]),
                     SimpleNamespace(approval_id="A2", deal_id="D2", action="RELEASE_SUPPLIER_PAYOUT", created_at=NOW - timedelta(hours=2),
                                     financial_impact_usd=15000.0, deep_link="l", required_approvals=2, approvals=[SimpleNamespace(approved_by="x")])]
        hitl_repo = AsyncMock()
        hitl_repo.list_by_status = AsyncMock(return_value=approvals)
        deals_repo = AsyncMock()
        deals_repo.list_active = AsyncMock(return_value=[
            SimpleNamespace(deal_id="D1", status=SimpleNamespace(value="ESCROW_FUNDED"), updated_at=NOW - timedelta(days=10), buyer_candidates=[], parties=[]),
            SimpleNamespace(deal_id="D3", status=SimpleNamespace(value="MATCHING"), updated_at=NOW,
                            buyer_candidates=[BuyerCandidate(candidate_id="c", legal_name="C", country="DE", email="c@c.de", status="contacted")], parties=[]),
        ])
        er_repo = AsyncMock()
        er_repo.list_by_status = AsyncMock(side_effect=lambda st: [SimpleNamespace(exception_id="E1", deal_id="D1", category=None,
                                                                                    raised_at=NOW - timedelta(hours=5))] if st.value == "ESCALATED" else [])
        ledgers = [SimpleNamespace(currency="USD", outstanding=5000.0, released_total=45000.0, net_to_portless=44550.0, payouts_total=40000.0,
                                   supplier_balance_due=6000.0, reconciled=False, deal_id="D1", disputed=False, inspection_deadline=NOW, settled=False)]
        with (
            patch("services.hitl_approval.repository.get_repository", return_value=hitl_repo),
            patch("services.deals.repository.get_repository", return_value=deals_repo),
            patch("services.exception_resolver.repository.get_repository", return_value=er_repo),
            patch("services.escrow_guard.service.list_deal_ledgers", new=AsyncMock(return_value=ledgers)),
        ):
            d = await digest.build_digest(NOW)
        assert d["approvals"]["pending_total"] == 2 and d["approvals"]["by_action"]["RELEASE_SUPPLIER_PAYOUT"]["countersign"] == 1
        assert d["approvals"]["oldest_hours"] == 30.0
        assert d["deals"]["active"] == 2 and d["deals"]["stalled"][0]["deal_id"] == "D1" and d["deals"]["waiting_for_buyer"] == 1
        assert d["exceptions"]["open_total"] == 1 and d["money"]["unreconciled"] == ["D1"] and d["money"]["windows_open"] == 1
        text = digest.render_digest_text(d)
        assert "GATES WAITING: 2" in text and "ISSUE_PO: 1" in text and "STALLED (1)" in text and "UNRECONCILED: D1" in text
        assert "still owed to suppliers 6,000" in text

    @pytest.mark.asyncio
    async def test_send_digest_and_schedule(self, monkeypatch):
        from shared.orchestration import digest
        monkeypatch.setenv("HITL_TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("HITL_TELEGRAM_CHAT_ID", "c")
        monkeypatch.setenv("DIGEST_EMAIL_TO", "ops@portless.io")
        monkeypatch.setenv("DIGEST_HOUR_UTC", "06:30")
        sent_tg = AsyncMock(return_value=True)
        sent_mail = AsyncMock(return_value=SimpleNamespace(outcome=SimpleNamespace(value="logged")))
        with (
            patch("shared.orchestration.digest.build_digest", new=AsyncMock(return_value={"generated_at": NOW.isoformat(), "approvals": {}, "deals": {}, "exceptions": {}, "money": {}, "apify": {}, "warnings": []})),
            patch("shared.telegram.send_message", new=sent_tg),
            patch("services.email_gateway.sender.send_email", new=sent_mail),
        ):
            res = await digest.send_daily_digest(NOW)
        assert res["telegram"] is True and res["email"] == [{"to": "ops@portless.io", "outcome": "logged"}]
        assert sent_mail.await_args.args[0].subject.startswith("PortLess daily digest")
        assert digest.digest_hour_utc() == (6, 30)
        secs = digest.seconds_until_next(NOW)  # 09:00 → next day 06:30
        assert secs == pytest.approx(21.5 * 3600)
        monkeypatch.setenv("DIGEST_HOUR_UTC", "bogus")
        assert digest.digest_hour_utc() == (6, 0)
