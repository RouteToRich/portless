"""Controls around money and documents: Kill Switch, refund gate, proven
approvers, PI validity, PO margin, customs status, the PI's port, what drafts
may say about escrow, and an acceptance that names no price."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.negotiation_agent.service import claims_funds_already_held, draft_risk_flags, _email_gate_level
from services.dealmaker import service as dealmaker
from services.dealmaker.repository import InMemoryDealMakerRepository
from services.dealmaker.schemas import AcceptPiRequest, ExtendPiRequest, IssuePoRequest, PiRecord
from services.docugen import service as docugen
from services.docugen.schemas import CustomsFilingRequest
from services.email_gateway.schemas import SendEmailRequest
from services.email_gateway.sender import send_email
from services.email_processor import service as email_processor
from services.escrow_guard import service as escrow
from services.escrow_guard.repository import InMemoryEscrowGuardRepository
from services.escrow_guard.router import router as escrow_router
from services.escrow_guard.schemas import (
    AcceptEscrowRequest, EscrowHoldRecord, PayoutRequest, RefundRequest, ReleaseTrancheRequest, TrancheSpec,
)
from services.hitl_approval import callbacks, service as hitl
from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from services.hitl_approval.router import router as hitl_router
from services.hitl_approval.schemas import CreateApprovalRequest, ResolveApprovalRequest
from services.shield_link import service as shield
from services.shield_link.schemas import ChaFilingRequest, FreightBookingRequest, QcBookingRequest
from shared.auth import identity
from shared.auth.dependencies import AuthUser, get_current_user
from shared.orchestration import continuations, deal_watcher, extraction, kill_switch
from shared.orchestration.kill_switch import PlatformPaused
from tests.factories import NOW, make


@pytest.fixture(autouse=True)
def _running_platform():
    kill_switch.reset_for_tests()
    yield
    kill_switch.reset_for_tests()


@pytest.fixture
def hitl_repo(monkeypatch):
    repo = InMemoryHitlApprovalRepository()
    monkeypatch.setattr(hitl, "get_repository", lambda: repo)
    monkeypatch.setattr(hitl, "notify", AsyncMock())
    monkeypatch.setattr(hitl, "_emit", AsyncMock())
    monkeypatch.setattr(hitl, "execute_callback", AsyncMock())
    monkeypatch.setattr(hitl, "execute_reject_callback", AsyncMock())
    return repo


@pytest.fixture
def escrow_repo(monkeypatch):
    repo = InMemoryEscrowGuardRepository()
    monkeypatch.setattr(escrow, "get_repository", lambda: repo)
    return repo


def _hold(**fields):
    defaults = dict(
        escrow_id="ESC-1", deal_id="D1", status="FUNDED", provider="tazapay", amount=1000.0, funded_amount=1000.0,
        released_total=300.0, currency="USD",
        tranches=[TrancheSpec(tranche_id="T1", label="PO", pct=1.0, trigger="PO_ISSUED", amount=1000.0)],
    )
    return make(EscrowHoldRecord, **{**defaults, **fields})


def _gate(**fields):
    defaults = dict(deal_id="D1", action="ISSUE_PO", summary="s", autonomy_level="L0", callback_payload={})
    return CreateApprovalRequest(**{**defaults, **fields})


def _pi(**fields):
    defaults = dict(
        pi_id="PI-1", deal_id="D1", status="SENT", agreed_price=1200.0, currency="USD", quantity_mt=20.0,
        total_value=24000.0, incoterm="CIF", port_of_destination="Jebel Ali, AE", payment_terms="Escrow",
        validity_days=7, buyer_name="B", buyer_address="A", buyer_vat_number=None,
        issued_at=NOW, expires_at=NOW + timedelta(days=3650),
    )
    return make(PiRecord, **{**defaults, **fields})


class TestKillSwitch:
    async def _pause(self):
        await kill_switch.pause("ops@x.invalid", "bank incident")

    async def test_pause_records_who_and_why_and_resume_clears_it(self):
        await self._pause()
        state = await kill_switch.status()
        assert state["paused"] is True and state["by"] == "ops@x.invalid" and state["reason"] == "bank incident"
        await kill_switch.resume("second@x.invalid", "cleared")
        state = await kill_switch.status()
        assert state["paused"] is False
        assert [h["action"] for h in state["history"]] == ["resume", "pause"]

    async def test_second_pause_keeps_the_first_record(self):
        await self._pause()
        await kill_switch.pause("later@x.invalid", "another")
        state = await kill_switch.status()
        assert state["by"] == "ops@x.invalid" and len(state["history"]) == 1

    @pytest.mark.parametrize("call", [
        lambda: escrow.release_tranche("E", ReleaseTrancheRequest(tranche_id="T1")),
        lambda: escrow.accept_escrow("E", AcceptEscrowRequest()),
        lambda: escrow.refund("E", RefundRequest(reason="r")),
        lambda: escrow.request_payout(PayoutRequest(deal_id="D", payee_id="S", amount=1.0, idempotency_key="k")),
        lambda: dealmaker.issue_po(make(IssuePoRequest, deal_id="D")),
        lambda: shield.book_qc(make(QcBookingRequest, deal_id="D")),
        lambda: shield.book_freight(FreightBookingRequest(deal_id="D")),
        lambda: shield.file_cha(make(ChaFilingRequest, deal_id="D")),
        lambda: docugen.file_customs(make(CustomsFilingRequest, deal_id="D")),
        lambda: send_email(make(SendEmailRequest, to_address="a@x.invalid")),
    ], ids=["release", "accept", "refund", "payout", "po", "qc", "freight", "cha", "customs", "email"])
    async def test_nothing_leaves_while_paused(self, call):
        await self._pause()
        with pytest.raises(PlatformPaused) as refused:
            await call()
        assert refused.value.status_code == 423

    async def test_approve_is_refused_and_the_gate_stays_open(self, hitl_repo):
        created = await hitl.create_approval(_gate())
        await self._pause()
        with pytest.raises(PlatformPaused):
            await hitl.resolve_approval(created.approval_id, ResolveApprovalRequest(decision="APPROVE", resolved_by="a"))
        assert (await hitl.get_approval(created.approval_id)).status == "PENDING"

    async def test_reject_still_works(self, hitl_repo):
        created = await hitl.create_approval(_gate())
        await self._pause()
        done = await hitl.resolve_approval(created.approval_id, ResolveApprovalRequest(decision="REJECT", resolved_by="a"))
        assert done.status == "REJECTED"

    @pytest.mark.parametrize("level", ["L1", "L2", "L3"])
    async def test_no_gate_approves_itself(self, hitl_repo, level):
        await self._pause()
        created = await hitl.create_approval(_gate(autonomy_level=level))
        assert created.status == "PENDING" and created.autonomy_level == "L0" and "platform_paused" in created.risk_flags
        assert created.expires_at is None
        hitl.execute_callback.assert_not_called()

    async def test_gates_run_again_after_resume(self, hitl_repo):
        await self._pause()
        await kill_switch.resume("a", "ok")
        created = await hitl.create_approval(_gate(autonomy_level="L2"))
        assert created.status == "AUTO_APPROVED"

    async def test_inspection_window_does_not_auto_release(self, escrow_repo):
        await escrow_repo.save_hold(_hold(
            inspection_deadline=NOW - timedelta(days=1),
            tranches=[TrancheSpec(tranche_id="T4", label="A", pct=1.0, trigger="ACCEPTED", amount=100.0)],
        ))
        await self._pause()
        assert await escrow.check_inspection_windows() == 0
        assert (await escrow_repo.get_hold("ESC-1")).tranches[0].status == "PENDING"

    async def test_deal_watcher_waits(self):
        await self._pause()
        with patch("services.deals.repository.get_repository") as repo:
            counts = await deal_watcher.sweep_stalled_deals()
        repo.assert_not_called()
        assert counts["scanned"] == 0

    async def test_store_never_readable_counts_as_paused(self, monkeypatch):
        broken = SimpleNamespace(find_one=AsyncMock(side_effect=RuntimeError("down")))
        monkeypatch.setattr(kill_switch, "_collection", lambda: broken)
        assert await kill_switch.is_paused() is True

    async def test_a_pause_that_cannot_be_saved_does_not_happen(self, monkeypatch):
        store = SimpleNamespace(find_one=AsyncMock(return_value=None),
                                update_one=AsyncMock(side_effect=RuntimeError("down")))
        monkeypatch.setattr(kill_switch, "_collection", lambda: store)
        with pytest.raises(kill_switch.KillSwitchStoreError):
            await kill_switch.pause("a", "reason")
        assert (await kill_switch.status())["paused"] is False

    async def test_saved_state_is_what_every_process_reads(self, monkeypatch):
        store = SimpleNamespace(find_one=AsyncMock(return_value={"paused": True, "reason": "r", "by": "b", "at": "t"}))
        monkeypatch.setattr(kill_switch, "_collection", lambda: store)
        assert await kill_switch.is_paused() is True

    def _api(self, *, staff):
        app = FastAPI()
        app.include_router(hitl_router)
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="u", email="s@x.invalid", is_portless=staff)
        return TestClient(app)

    def test_route_needs_staff(self):
        assert self._api(staff=False).post("/hitl-approval/kill-switch", json={"paused": True, "reason": "abc"}).status_code == 403

    def test_route_needs_a_reason(self):
        assert self._api(staff=True).post("/hitl-approval/kill-switch", json={"paused": True, "reason": ""}).status_code == 422

    def test_route_records_the_signed_in_person(self):
        with patch("services.hitl_approval.router.notify", new=AsyncMock()):
            api = self._api(staff=True)
            body = api.post("/hitl-approval/kill-switch", json={"paused": True, "reason": "abc"}).json()
        assert body["paused"] is True and body["by"] == "s@x.invalid"
        assert api.get("/hitl-approval/kill-switch").json()["paused"] is True


class TestRefundGate:
    @pytest.fixture
    def api(self):
        app = FastAPI()
        app.include_router(escrow_router)
        return TestClient(app, raise_server_exceptions=False)

    def test_direct_refund_is_refused(self, api):
        assert api.post("/escrow-guard/escrows/ESC-1/refund", json={"reason": "r"}).status_code == 403

    def test_request_needs_a_reason(self, api):
        assert api.post("/escrow-guard/escrows/ESC-1/request-refund", json={"reason": " "}).status_code == 422

    async def test_request_opens_a_gate_for_the_undisbursed_amount(self, escrow_repo, hitl_repo):
        await escrow_repo.save_hold(_hold())
        opened = await escrow.request_refund("ESC-1", RefundRequest(reason="quality failed", requested_by="op@x.invalid"))
        gate = await hitl.get_approval(opened["approval_id"])
        assert gate.action == "REFUND_ESCROW" and gate.status == "PENDING" and gate.financial_impact_usd == 700.0
        assert gate.callback_payload["escrow_id"] == "ESC-1" and "op@x.invalid" in gate.summary

    async def test_second_request_reuses_the_waiting_gate(self, escrow_repo, hitl_repo):
        await escrow_repo.save_hold(_hold())
        first = await escrow.request_refund("ESC-1", RefundRequest(reason="a"))
        second = await escrow.request_refund("ESC-1", RefundRequest(reason="b"))
        assert first["approval_id"] == second["approval_id"]

    async def test_nothing_left_to_refund(self, escrow_repo, hitl_repo):
        await escrow_repo.save_hold(_hold(released_total=1000.0))
        with pytest.raises(escrow.InvalidDealState):
            await escrow.request_refund("ESC-1", RefundRequest(reason="a"))

    async def test_value_threshold_never_auto_approves_it(self, escrow_repo, hitl_repo, monkeypatch):
        monkeypatch.setenv("AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD", "1000000")
        await escrow_repo.save_hold(_hold())
        opened = await escrow.request_refund("ESC-1", RefundRequest(reason="a"))
        assert opened["status"] == "PENDING"

    async def test_large_refund_needs_two_approvers(self, escrow_repo, hitl_repo, monkeypatch):
        monkeypatch.setenv("FOUR_EYES_THRESHOLD_USD", "500")
        await escrow_repo.save_hold(_hold())
        opened = await escrow.request_refund("ESC-1", RefundRequest(reason="a"))
        assert (await hitl.get_approval(opened["approval_id"])).required_approvals == 2

    async def test_approve_runs_the_refund(self):
        done = SimpleNamespace(refunded_total=700.0, status="REFUNDED")
        with patch("services.escrow_guard.service.refund", new=AsyncMock(return_value=done)) as refund:
            result = await callbacks.execute_callback("REFUND_ESCROW", "D1", {"escrow_id": "ESC-1", "reason": "r"}, "A1")
        assert refund.await_args.args[0] == "ESC-1" and result["status"] == "REFUNDED"

    def test_reject_leaves_the_deal_alone(self):
        assert "REFUND_ESCROW" in callbacks.SOFT_REJECT_ACTIONS


class TestApproverIdentityWithoutMiddleware:
    """A standalone service has no AuthMiddleware in front of it."""

    @pytest.fixture
    def api(self, hitl_repo):
        app = FastAPI()
        app.include_router(hitl_router)
        return TestClient(app, raise_server_exceptions=False)

    async def _four_eyes_gate(self, monkeypatch):
        monkeypatch.setenv("FOUR_EYES_THRESHOLD_USD", "100")
        monkeypatch.setattr(identity, "check_account", lambda _identity: None)
        return await hitl.create_approval(_gate(action="RELEASE_SUPPLIER_PAYOUT", financial_impact_usd=500.0))

    @staticmethod
    def _resolve(api, gate, *, token=None, by="anyone"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return api.post(f"/hitl-approval/approvals/{gate.approval_id}/resolve",
                        json={"decision": "APPROVE", "resolved_by": by}, headers=headers)

    async def test_two_approver_gate_refuses_a_caller_with_no_token(self, api, monkeypatch):
        gate = await self._four_eyes_gate(monkeypatch)
        assert self._resolve(api, gate).status_code == 401
        assert (await hitl.get_approval(gate.approval_id)).approvals == []

    async def test_typed_names_cannot_make_two_approvers(self, api, monkeypatch):
        gate = await self._four_eyes_gate(monkeypatch)
        monkeypatch.setattr(identity, "ask_gateway", AsyncMock(return_value={"user_id": "1", "email": "one@x.invalid"}))
        monkeypatch.setattr("services.auth.organization.is_portless_user", lambda _e: True)
        identity.clear_cache()
        assert self._resolve(api, gate, token="t", by="alice").status_code == 200
        assert self._resolve(api, gate, token="t", by="bob").status_code == 409
        assert [s.approved_by for s in (await hitl.get_approval(gate.approval_id)).approvals] == ["one@x.invalid"]

    async def test_non_staff_token_is_refused(self, api, monkeypatch):
        gate = await self._four_eyes_gate(monkeypatch)
        monkeypatch.setattr(identity, "ask_gateway", AsyncMock(return_value={"user_id": "2", "email": "buyer@x.invalid"}))
        monkeypatch.setattr("services.auth.organization.is_portless_user", lambda _e: False)
        identity.clear_cache()
        assert self._resolve(api, gate, token="t2").status_code == 403


class TestPiValidity:
    @pytest.fixture
    def repo(self, monkeypatch):
        repo = InMemoryDealMakerRepository()
        monkeypatch.setattr(dealmaker, "get_repository", lambda: repo)
        monkeypatch.setattr(dealmaker, "_transition_deal", AsyncMock())
        monkeypatch.setattr(dealmaker, "_register_buyer_organization", AsyncMock())
        monkeypatch.setattr(dealmaker, "_emit", AsyncMock())
        return repo

    async def test_expired_pi_is_refused(self, repo):
        await repo.save_pi(_pi(expires_at=NOW - timedelta(days=1)))
        with pytest.raises(dealmaker.PiExpiredError):
            await dealmaker.accept_pi("PI-1", AcceptPiRequest())
        assert (await repo.get_pi("PI-1")).status == "SENT"

    async def test_expiry_without_timezone_is_read_as_utc(self, repo):
        await repo.save_pi(_pi(expires_at=(NOW - timedelta(days=1)).replace(tzinfo=None)))
        with pytest.raises(dealmaker.PiExpiredError):
            await dealmaker.accept_pi("PI-1", AcceptPiRequest())

    async def test_valid_pi_is_accepted(self, repo):
        await repo.save_pi(_pi())
        assert (await dealmaker.accept_pi("PI-1", AcceptPiRequest())).status == "ACCEPTED"

    async def test_extension_makes_it_acceptable_and_is_audited(self, repo, monkeypatch):
        await repo.save_pi(_pi(expires_at=NOW - timedelta(days=1), content_hash="old"))
        monkeypatch.setattr(dealmaker, "_fetch_deal", AsyncMock(return_value=SimpleNamespace(hs_code="100630", port_of_loading="Kandla")))
        monkeypatch.setattr(dealmaker, "_write_document", AsyncMock(return_value="file:///pi"))
        await dealmaker.extend_pi("PI-1", ExtendPiRequest(validity_days=5, reason="buyer asked"), "op@x.invalid")
        saved = await repo.get_pi("PI-1")
        assert saved.extensions[0]["by"] == "op@x.invalid" and saved.extensions[0]["reason"] == "buyer asked"
        assert saved.content_hash != "old" and saved.validity_days == 5
        assert (await dealmaker.accept_pi("PI-1", AcceptPiRequest())).status == "ACCEPTED"

    async def test_accepted_pi_cannot_be_extended(self, repo):
        await repo.save_pi(_pi(status="ACCEPTED"))
        with pytest.raises(dealmaker.PiStateError):
            await dealmaker.extend_pi("PI-1", ExtendPiRequest(reason="abc"), "a")

    async def test_late_email_acceptance_goes_to_a_person(self, repo, monkeypatch):
        await repo.save_pi(_pi(expires_at=NOW - timedelta(days=1)))
        monkeypatch.setattr("services.deals.service.get_deal", AsyncMock(return_value=SimpleNamespace(status="PI_SENT")))
        monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: repo)
        operator = AsyncMock(return_value="APPR-1")
        acceptance = AsyncMock()
        monkeypatch.setattr(email_processor, "_create_price_hitl", operator)
        monkeypatch.setattr(email_processor, "_create_pi_acceptance_hitl", acceptance)
        store = SimpleNamespace(save=AsyncMock())
        monkeypatch.setattr(email_processor, "get_repository", lambda: store)
        record = SimpleNamespace(deal_id="D1", party="buyer", from_address="b@x.invalid", body_text="We accept",
                                 inbound_id="I1", negotiation_id="N1", model_copy=lambda update: record)
        await email_processor._handle_post_agreement_reply(
            record, SimpleNamespace(body_text="We accept the Proforma Invoice"), SimpleNamespace(party="buyer"),
        )
        acceptance.assert_not_called()
        assert "pi_expired" in operator.await_args.kwargs["risk_flags"]


class TestPoMargin:
    @pytest.fixture
    def repo(self, monkeypatch):
        repo = InMemoryDealMakerRepository()
        monkeypatch.setattr(dealmaker, "get_repository", lambda: repo)
        return repo

    @staticmethod
    def _deal(monkeypatch, target=None):
        deal = SimpleNamespace(status="ESCROW_FUNDED", buyer_target_price_usd=target, hs_code="100630")
        monkeypatch.setattr(dealmaker, "_fetch_deal", AsyncMock(return_value=deal))

    @staticmethod
    def _po(price):
        return make(IssuePoRequest, deal_id="D1", agreed_price=price, quantity_mt=20.0, idempotency_key=None)

    async def test_no_buyer_price_anywhere_is_refused(self, repo, monkeypatch):
        self._deal(monkeypatch)
        with pytest.raises(dealmaker.MarginUnknownError):
            await dealmaker.issue_po(self._po(1000.0))

    async def test_pi_price_is_used_when_the_target_is_unset(self, repo, monkeypatch):
        self._deal(monkeypatch)
        await repo.save_pi(_pi(agreed_price=1050.0))
        with pytest.raises(dealmaker.MarginGuardError):
            await dealmaker.issue_po(self._po(1000.0))


class TestCustomsStatus:
    @pytest.mark.parametrize("deal_status", ["MATCHING", "PI_SENT", "ESCROW_FUNDED", "QC_FAILED", "CANCELLED", "PAID"])
    async def test_cha_refused_outside_the_shipping_window(self, monkeypatch, deal_status):
        monkeypatch.setattr(shield, "get_repository", lambda: SimpleNamespace())
        monkeypatch.setattr(shield, "_fetch_deal", AsyncMock(return_value=SimpleNamespace(status=deal_status)))
        with pytest.raises(shield.InvalidDealState):
            await shield.file_cha(ChaFilingRequest(deal_id="D1", documents=[], idempotency_key=None))

    @pytest.mark.parametrize("deal_status", ["MATCHING", "CANCELLED"])
    async def test_docugen_refused_outside_the_shipping_window(self, monkeypatch, deal_status):
        monkeypatch.setattr(docugen, "_fetch_deal", AsyncMock(return_value=SimpleNamespace(status=deal_status)))
        with pytest.raises(docugen.DocuGenError):
            await docugen.file_customs(make(CustomsFilingRequest, deal_id="D1"))

    def test_the_shipping_window(self):
        assert shield.CUSTOMS_FILING_STATUSES == {"QC_PASSED", "IN_TRANSIT", "CLEARED"}


class TestDestinationPort:
    @pytest.mark.parametrize("text, port", [
        ("Our destination port is Jebel Ali, UAE.", "Jebel Ali"),
        ("USD 1,294.09 per MT CIF Jebel Ali.", "Jebel Ali"),
        ("Port of discharge: Nhava Sheva", "Nhava Sheva"),
        ("POD: Hamburg", "Hamburg"),
        ("CIF your destination port", None),
        ("We look forward to Monday at Ten", None),
        ("", None),
        (None, None),
    ])
    def test_parse(self, text, port):
        assert extraction.parse_destination_port(text) == port

    @staticmethod
    def _mail(body, *, party="buyer", direction="inbound", minute=0):
        from agents.negotiation_agent.email_history import EmailDirection
        return SimpleNamespace(body=body, party=party, direction=EmailDirection(direction),
                               created_at=NOW + timedelta(minutes=minute))

    @pytest.fixture
    def pi_spy(self, monkeypatch):
        spy = AsyncMock(return_value="PI")
        monkeypatch.setattr("services.dealmaker.service.issue_pi", spy)
        return spy

    @staticmethod
    def _mails(monkeypatch, mails):
        repo = SimpleNamespace(list_by_deal=AsyncMock(return_value=mails))
        monkeypatch.setattr("agents.negotiation_agent.email_history.get_email_repository", lambda: repo)

    async def _issue(self, pi_spy):
        await continuations.issue_pi_for_deal("D1", agreed_price=1200.0, quantity_mt=20.0, market="AE")
        return pi_spy.await_args.args[0].port_of_destination

    async def test_latest_buyer_mail_wins(self, monkeypatch, pi_spy):
        self._mails(monkeypatch, [
            self._mail("Our destination port is Dammam.", minute=1),
            self._mail("Our destination port is Jebel Ali, UAE.", minute=2),
        ])
        assert await self._issue(pi_spy) == "Jebel Ali, AE"

    async def test_our_own_and_the_suppliers_mail_do_not_count(self, monkeypatch, pi_spy):
        monkeypatch.setattr("services.deals.service.get_deal", AsyncMock(return_value=SimpleNamespace(buyer_candidates=[])))
        self._mails(monkeypatch, [
            self._mail("CIF Rotterdam", direction="outbound"),
            self._mail("Destination port is Mundra", party="supplier"),
        ])
        assert await self._issue(pi_spy) == "Port to be confirmed by buyer, AE"

    async def test_outreach_requirement_is_the_fallback(self, monkeypatch, pi_spy):
        cand = SimpleNamespace(status="selected", requirement=SimpleNamespace(destination_port="Valencia"))
        monkeypatch.setattr("services.deals.service.get_deal", AsyncMock(return_value=SimpleNamespace(buyer_candidates=[cand])))
        self._mails(monkeypatch, [])
        assert await self._issue(pi_spy) == "Valencia, AE"

    async def test_unreadable_history_never_stops_the_pi(self, monkeypatch, pi_spy):
        repo = SimpleNamespace(list_by_deal=AsyncMock(side_effect=RuntimeError("down")))
        monkeypatch.setattr("agents.negotiation_agent.email_history.get_email_repository", lambda: repo)
        assert await self._issue(pi_spy) == "Port to be confirmed by buyer, AE"

    @pytest.mark.parametrize("printed, expected", [
        ("Jebel Ali, AE", "Jebel Ali, AE"),
        ("Port to be confirmed by buyer, AE", None),
    ])
    async def test_freight_uses_only_a_named_port(self, monkeypatch, printed, expected):
        repo = SimpleNamespace(get_pi_by_deal=AsyncMock(return_value=SimpleNamespace(port_of_destination=printed)))
        monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: repo)
        assert await continuations._pi_destination_port("D1") == expected


class TestEscrowWordingInDrafts:
    @pytest.mark.parametrize("body", [
        "The deal is attractive because the overseas buyer’s funds are already deposited in a neutral escrow account.",
        "Your payment is now held in escrow.",
        "The money has been deposited.",
    ])
    def test_claims_are_caught(self, body):
        assert claims_funds_already_held(body)

    @pytest.mark.parametrize("body", [
        "We place the purchase order only after the buyer's money is already deposited in escrow.",
        "Once the funds are now in escrow we will issue the order.",
        "You pay into escrow after accepting the proforma invoice.",
        "Payment secured in escrow before the purchase order.",
        "",
    ])
    def test_promises_are_fine(self, body):
        assert not claims_funds_already_held(body)

    def test_a_claim_forces_a_person_to_read_it(self):
        flags = draft_risk_flags("The buyer's funds are already deposited.")
        assert "premature_funds_claim" in flags
        assert _email_gate_level(SimpleNamespace(round_no=3), flags) == "L0"

    def test_supplier_terms_do_not_state_it_as_fact(self):
        from agents.negotiation_agent.briefing import supplier_terms
        assert "already" not in supplier_terms()


class TestAcceptanceWithNoPrice:
    @staticmethod
    def _session(monkeypatch, **fields):
        session = SimpleNamespace(**{"last_offer_to_buyer": None, "last_offer_to_supplier": None, **fields})
        repo = SimpleNamespace(get_session=AsyncMock(return_value=session))
        monkeypatch.setattr("agents.negotiation_agent.repository.get_repository", lambda: repo)

    @staticmethod
    def _intent(monkeypatch, intent):
        monkeypatch.setattr("agents.negotiation_agent.graph.classify_inbound_intent",
                            AsyncMock(return_value={"intent": intent}))

    async def _price(self, party="buyer", body="We accept your offer and wish to proceed."):
        return await email_processor._price_of_bare_acceptance("N1", party, body)

    async def test_accepts_our_last_offer_to_the_buyer(self, monkeypatch):
        self._session(monkeypatch, last_offer_to_buyer=1294.09, last_offer_to_supplier=960.0)
        self._intent(monkeypatch, "ACCEPT")
        assert await self._price() == 1294.09

    async def test_accepts_our_last_offer_to_the_supplier(self, monkeypatch):
        self._session(monkeypatch, last_offer_to_buyer=1294.09, last_offer_to_supplier=960.0)
        self._intent(monkeypatch, "ACCEPT")
        assert await self._price(party="supplier") == 960.0

    @pytest.mark.parametrize("intent", ["QUESTION", "COUNTER_OFFER", "REJECT", "OTHER"])
    async def test_anything_else_goes_to_a_person(self, monkeypatch, intent):
        self._session(monkeypatch, last_offer_to_buyer=1294.09)
        self._intent(monkeypatch, intent)
        assert await self._price() is None

    async def test_nothing_was_offered_yet(self, monkeypatch):
        self._session(monkeypatch)
        self._intent(monkeypatch, "ACCEPT")
        assert await self._price() is None

    async def test_gateway_down_uses_the_strict_keyword_guard(self, monkeypatch):
        from agents.negotiation_agent.graph import _INTENT_UNAVAILABLE
        self._session(monkeypatch, last_offer_to_buyer=1294.09)
        self._intent(monkeypatch, _INTENT_UNAVAILABLE)
        assert await self._price() == 1294.09
        assert await self._price(body="We accept if you can lower it.") is None

    async def test_a_failure_goes_to_a_person(self, monkeypatch):
        monkeypatch.setattr("agents.negotiation_agent.repository.get_repository",
                            lambda: SimpleNamespace(get_session=AsyncMock(side_effect=RuntimeError("down"))))
        assert await self._price() is None
