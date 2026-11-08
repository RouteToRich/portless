"""A block-level compliance hold stops money leaving, on every path.

Covers the approval gate, the escrow service behind it, the inspection-window
auto-release, the store-failure case, and the audited false-alarm clearance.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.deals import router as deals_router
from services.deals.repository import InMemoryFallbackRepository
from services.escrow_guard import repository as escrow_repo
from services.escrow_guard import service as escrow
from services.escrow_guard.repository import InMemoryEscrowGuardRepository
from services.escrow_guard.schemas import (
    AcceptEscrowRequest,
    EscrowHoldRecord,
    PayoutRequest,
    RefundRequest,
    ReleaseTrancheRequest,
    TrancheSpec,
)
from services.hitl_approval import repository as hitl_repo
from services.hitl_approval import router as hitl_router
from services.hitl_approval import service as hitl
from services.hitl_approval.repository import InMemoryHitlApprovalRepository
from services.hitl_approval.schemas import CreateApprovalRequest, ResolveApprovalRequest
from shared.auth.dependencies import AuthUser, get_current_user
from shared.orchestration import compliance as cp

NOW = datetime.now(UTC)
STAFF = AuthUser(user_id="U-STAFF", email="ops@portless.io", org_id="portless", is_portless=True)
CUSTOMER = AuthUser(user_id="U-ORG", email="a@acme.com", org_id="acme.com", is_portless=False)
MONEY_ACTIONS = sorted(cp.MONEY_RELEASE_ACTIONS)
BLOCKS = [
    "seal_mismatch", "loading_not_verified", "shipment_hs_mismatch", "hs_scomet", "hs_prohibited",
    "destination_embargoed", "buyer_country_embargoed", "payer_country_embargoed", "supplier_dgft_denied",
]
REASON = "Carrier confirmed a typing error on the bill of lading"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    cp.reset_for_tests()
    hitl_repo._repository = InMemoryHitlApprovalRepository()
    escrow_repo._repository = InMemoryEscrowGuardRepository()
    monkeypatch.setattr(hitl, "notify", AsyncMock())
    monkeypatch.setattr(hitl, "_emit", AsyncMock())
    monkeypatch.setattr(escrow, "_emit", AsyncMock())
    monkeypatch.setattr(escrow, "_settle_if_complete", AsyncMock(side_effect=lambda hold, _now: hold))
    monkeypatch.setattr(escrow, "_deal_status", AsyncMock(return_value="CLEARED"))
    monkeypatch.setattr(escrow, "_fetch_deal", AsyncMock(return_value=SimpleNamespace(status="CLEARED")))
    yield
    cp.reset_for_tests()
    hitl_repo._repository = None
    escrow_repo._repository = None


@pytest.fixture
def callback(monkeypatch):
    mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(hitl, "execute_callback", mock)
    return mock


async def _holds(*codes, deal_id="D1", **fields):
    record = await cp.get_record(deal_id)
    record.holds = list(codes)
    for name, value in fields.items():
        setattr(record, name, value)
    await cp.save_record(record)


async def _gate(action="RELEASE_SUPPLIER_PAYOUT", **kw):
    created = await hitl.create_approval(CreateApprovalRequest(
        deal_id="D1", action=action, autonomy_level="L0", financial_impact_usd=100.0, **kw,
    ))
    return created.approval_id


def _decision(decision="APPROVE", by="ops"):
    return ResolveApprovalRequest(decision=decision, resolved_by=by)


def _funded(**kw):
    specs = [("T1", "PO_ISSUED", 0.3), ("T2", "QC_PASSED", 0.4), ("T3", "CLEARED", 0.2), ("T4", "ACCEPTED", 0.1)]
    released = kw.pop("released", ())
    base = {
        "escrow_id": "ESC-1", "deal_id": "D1", "provider": "tazapay", "amount": 1000.0, "currency": "USD",
        "status": "FUNDED", "ext_ref": "ref-1", "created_at": NOW, "updated_at": NOW, "idempotency_key": "i-1",
        "tranches": [
            TrancheSpec(tranche_id=t, trigger=g, pct=p, amount=1000.0 * p, status="RELEASED" if t in released else "PENDING")
            for t, g, p in specs
        ],
    }
    return EscrowHoldRecord(**{**base, **kw})


async def _saved(**kw):
    await escrow_repo.get_repository().save_hold(_funded(**kw))


PAYOUT = PayoutRequest(deal_id="D1", payee_id="S", amount=100.0, idempotency_key="k")


class TestOpenBlocks:
    @pytest.mark.parametrize("code", BLOCKS)
    async def test_block_codes_count(self, code):
        await _holds(code)
        assert await cp.open_blocks("D1") == [code]

    @pytest.mark.parametrize("code", ["payer_not_buyer", "hs_unverified", "shipment_no_photos", "related_parties"])
    async def test_review_codes_do_not(self, code):
        await _holds(code)
        assert await cp.open_blocks("D1") == []

    async def test_no_record_means_no_block(self):
        assert await cp.open_blocks("never-seen") == []

    async def test_unreadable_store_refuses_the_money(self):
        store = SimpleNamespace(find_one=AsyncMock(side_effect=RuntimeError("mongo down")))
        with patch.object(cp, "_collection", return_value=store):
            assert await cp.open_blocks("D1") == [cp.STORE_UNAVAILABLE_BLOCK]


class TestApprovalGate:
    @pytest.mark.parametrize("action", MONEY_ACTIONS)
    async def test_money_gate_is_refused_and_stays_open(self, action, callback):
        await _holds("seal_mismatch")
        approval_id = await _gate(action)
        with pytest.raises(hitl.ComplianceBlockedError, match="seal_mismatch"):
            await hitl.resolve_approval(approval_id, _decision())
        assert (await hitl.get_approval(approval_id)).status == "PENDING"
        callback.assert_not_awaited()

    @pytest.mark.parametrize("action", MONEY_ACTIONS)
    async def test_review_hold_does_not_refuse(self, action, callback):
        await _holds("payer_not_buyer")
        approval_id = await _gate(action)
        assert (await hitl.resolve_approval(approval_id, _decision())).status == "APPROVED"

    @pytest.mark.parametrize("action", ["ISSUE_PO", "BOOK_FREIGHT", "FILE_CUSTOMS", "RESOLVE_EXCEPTION"])
    async def test_other_gates_can_still_be_approved(self, action, callback):
        await _holds("seal_mismatch")
        approval_id = await _gate(action)
        assert (await hitl.resolve_approval(approval_id, _decision())).status == "APPROVED"

    async def test_reject_is_always_possible(self, callback):
        await _holds("seal_mismatch")
        approval_id = await _gate()
        assert (await hitl.resolve_approval(approval_id, _decision("REJECT"))).status == "REJECTED"

    async def test_four_eyes_takes_no_first_signature(self, callback, monkeypatch):
        monkeypatch.setenv("FOUR_EYES_THRESHOLD_USD", "50")
        await _holds("seal_mismatch")
        approval_id = await _gate()
        with pytest.raises(hitl.ComplianceBlockedError):
            await hitl.resolve_approval(approval_id, _decision())
        assert (await hitl.get_approval(approval_id)).approvals == []

    async def test_gate_can_be_approved_once_the_hold_is_cleared(self, callback):
        await _holds("seal_mismatch", bl_seal_numbers=["S9"])
        approval_id = await _gate()
        with pytest.raises(hitl.ComplianceBlockedError):
            await hitl.resolve_approval(approval_id, _decision())
        await cp.clear_block("D1", "seal_mismatch", by="ops", reason=REASON)
        assert (await hitl.resolve_approval(approval_id, _decision())).status == "APPROVED"

    def test_http_refusal_is_a_conflict(self):
        app = FastAPI()
        app.include_router(hitl_router.router)
        error = hitl.ComplianceBlockedError("A1", ["seal_mismatch"])
        with patch.object(hitl_router, "resolve_approval", new=AsyncMock(side_effect=error)):
            response = TestClient(app).post(
                "/hitl-approval/approvals/A1/resolve", json={"decision": "APPROVE", "resolved_by": "ops"},
            )
        assert response.status_code == 409 and "seal_mismatch" in response.json()["detail"]


class TestEscrowRefusesDirectCalls:
    async def test_tranche(self):
        await _saved()
        await _holds("seal_mismatch")
        with pytest.raises(escrow.InvalidDealState, match="compliance block: seal_mismatch"):
            await escrow.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T1", proof_refs=["P"]))
        assert (await escrow_repo.get_repository().get_hold("ESC-1")).tranches[0].status == "PENDING"

    async def test_tranche_released_after_clearing(self):
        await _saved()
        await _holds("seal_mismatch", bl_seal_numbers=["S9"])
        await cp.clear_block("D1", "seal_mismatch", by="ops", reason=REASON)
        await escrow.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T1", proof_refs=["P"]))
        assert (await escrow_repo.get_repository().get_hold("ESC-1")).tranches[0].status == "RELEASED"

    async def test_already_released_tranche_stays_idempotent(self):
        await _saved(released=("T1",))
        await _holds("seal_mismatch")
        await escrow.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T1"))

    async def test_payout(self):
        await _saved(released=("T1", "T2"), released_total=700.0)
        await _holds("hs_scomet")
        with pytest.raises(escrow.InvalidDealState, match="hs_scomet"):
            await escrow.request_payout(PAYOUT)
        assert await escrow_repo.get_repository().list_payouts_by_deal("D1") == []

    async def test_buyer_acceptance(self):
        await _saved(released=("T1", "T2", "T3"), released_total=900.0)
        await _holds("seal_mismatch")
        with pytest.raises(escrow.InvalidDealState, match="seal_mismatch"):
            await escrow.accept_escrow("ESC-1", AcceptEscrowRequest())
        assert (await escrow_repo.get_repository().get_hold("ESC-1")).buyer_accepted_at is None

    async def test_refund_is_the_way_out_and_stays_open(self):
        await _saved()
        await _holds("payer_country_embargoed")
        assert (await escrow.refund("ESC-1", RefundRequest())).status != "FUNDED"


class TestInspectionWindowAutoRelease:
    LAPSED = NOW - timedelta(hours=1)

    async def test_held_while_blocked_then_released(self):
        await _saved(released=("T1", "T2", "T3"), released_total=900.0, inspection_deadline=self.LAPSED)
        await _holds("seal_mismatch", bl_seal_numbers=["S9"])
        assert await escrow.check_inspection_windows() == 0
        assert (await escrow_repo.get_repository().get_hold("ESC-1")).tranches[3].status == "PENDING"
        await cp.clear_block("D1", "seal_mismatch", by="ops", reason=REASON)
        assert await escrow.check_inspection_windows() == 1
        assert (await escrow_repo.get_repository().get_hold("ESC-1")).tranches[3].status == "RELEASED"


class TestClearBlock:
    async def test_clears_with_a_full_audit_entry(self):
        await _holds("seal_mismatch", "payer_unknown", bl_seal_numbers=["S9"],
                     loading=cp.LoadingRecord(container_numbers=["C1"], seal_numbers=["S1"]))
        record = await cp.clear_block("D1", "seal_mismatch", by="ops", reason=REASON, evidence_ref="DOC-7")
        assert record.holds == ["payer_unknown"]
        entry = record.hold_clearances[0]
        assert (entry.code, entry.cleared_by, entry.reason, entry.evidence_ref, entry.seal_numbers) == (
            "seal_mismatch", "ops", REASON, "DOC-7", ["S9"],
        )
        assert record.cleared_holds == [], "a clearance is not a blanket acceptance of the finding"

    async def test_inspector_seals_are_not_listed_as_accepted(self):
        await _holds("seal_mismatch", bl_seal_numbers=["S1", "S9"],
                     loading=cp.LoadingRecord(container_numbers=["C1"], seal_numbers=["S1"]))
        record = await cp.clear_block("D1", "seal_mismatch", by="ops", reason=REASON)
        assert record.accepted_seal_numbers == ["S9"]

    async def _mismatch_cleared(self):
        await _holds(loading=cp.LoadingRecord(container_numbers=["C1"], seal_numbers=["S1"]))
        assert (await cp.check_seal("D1", ["S9"])).code == "seal_mismatch"
        await cp.clear_block("D1", "seal_mismatch", by="ops", reason=REASON)

    async def test_same_seal_reported_again_stays_cleared(self):
        await self._mismatch_cleared()
        assert await cp.check_seal("D1", ["S9"]) is None
        assert await cp.open_blocks("D1") == []

    async def test_a_different_seal_raises_it_again(self):
        await self._mismatch_cleared()
        assert (await cp.check_seal("D1", ["S10"])).code == "seal_mismatch"
        assert await cp.open_blocks("D1") == ["seal_mismatch"]

    @pytest.mark.parametrize("code", [c for c in BLOCKS if c != "seal_mismatch"])
    async def test_other_blocks_cannot_be_cleared(self, code):
        await _holds(code)
        with pytest.raises(cp.HoldNotClearableError):
            await cp.clear_block("D1", code, by="ops", reason=REASON)
        assert (await cp.get_record("D1")).holds == [code]

    async def test_a_hold_that_is_not_open(self):
        with pytest.raises(cp.HoldNotOpenError):
            await cp.clear_block("D1", "seal_mismatch", by="ops", reason=REASON)

    async def test_unreadable_store_changes_nothing(self):
        store = SimpleNamespace(find_one=AsyncMock(side_effect=RuntimeError("mongo down")))
        with patch.object(cp, "_collection", return_value=store), pytest.raises(RuntimeError):
            await cp.clear_block("D1", "seal_mismatch", by="ops", reason=REASON)


class TestClearRoute:
    @pytest.fixture
    def deal_id(self, monkeypatch):
        repo = InMemoryFallbackRepository()
        monkeypatch.setattr("services.deals.service.get_repository", lambda: repo)
        monkeypatch.setattr("services.deals.service._fetch_opportunity", AsyncMock(return_value=None))
        created = self._client().post("/deals", json={"operator_id": "x", "hs_code": "1520", "market": "ES"})
        return created.json()["deal_id"]

    @staticmethod
    def _client(user=STAFF):
        app = FastAPI()
        app.include_router(deals_router.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app, raise_server_exceptions=False)

    @staticmethod
    def _url(deal_id, code="seal_mismatch"):
        return f"/deals/{deal_id}/compliance/holds/{code}/clear"

    async def test_records_the_signed_in_operator(self, deal_id):
        await _holds("seal_mismatch", deal_id=deal_id, bl_seal_numbers=["S9"])
        response = self._client().post(self._url(deal_id), json={"reason": REASON, "evidence_ref": "DOC-7", "cleared_by": "forged"})
        assert response.status_code == 200 and response.json()["holds"] == []
        assert response.json()["hold_clearances"][0]["cleared_by"] == "ops@portless.io"

    async def test_customers_cannot_clear(self, deal_id):
        await _holds("seal_mismatch", deal_id=deal_id)
        assert self._client(CUSTOMER).post(self._url(deal_id), json={"reason": REASON}).status_code == 403
        assert (await cp.get_record(deal_id)).holds == ["seal_mismatch"]

    @pytest.mark.parametrize("body", [{}, {"reason": "ok"}, {"reason": " " * 20}])
    async def test_a_real_reason_is_required(self, deal_id, body):
        await _holds("seal_mismatch", deal_id=deal_id)
        assert self._client().post(self._url(deal_id), json=body).status_code == 422

    async def test_embargo_cannot_be_waived(self, deal_id):
        await _holds("destination_embargoed", deal_id=deal_id)
        assert self._client().post(self._url(deal_id, "destination_embargoed"), json={"reason": REASON}).status_code == 409

    def test_hold_not_open_is_404(self, deal_id):
        assert self._client().post(self._url(deal_id), json={"reason": REASON}).status_code == 404

    def test_unknown_deal_is_404(self, deal_id):
        assert self._client().post(self._url("DEAL-NOPE"), json={"reason": REASON}).status_code == 404
