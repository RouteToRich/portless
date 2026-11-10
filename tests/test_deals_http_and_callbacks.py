"""Deal API routes and the saga callbacks behind every approval.

  routes     → tenancy, every status mapping, compliance / evidence / outreach /
               match-board endpoints, operator-only HS policy
  callbacks  → each ApprovalAction runs the right downstream call with the
               right payload; failures surface as deal.exception; soft / hard
               rejects stay in their lanes; compliance acceptance rules
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.deals import router as deals_router
from services.deals import service as deals_service
from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import MatchResult
from services.deals.service import DealNotFoundError
from services.hitl_approval import callbacks as cb
from shared.auth.dependencies import AuthUser, get_current_user
from shared.domain.enums import ApprovalAction
from shared.domain.events import EventTopic
from shared.orchestration import compliance as cp

STAFF = AuthUser(user_id="U-STAFF", email="ops@portless.io", org_id="portless", is_portless=True)
CUSTOMER = AuthUser(user_id="U-ORG", email="a@acme.com", org_id="acme.com", is_portless=False)
UNASSIGNED = AuthUser(user_id="U-NEW", email="n@x.com", org_id=None, is_portless=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.service.get_repository", lambda: repo)
    monkeypatch.setattr("services.deals.service._fetch_opportunity", AsyncMock(return_value=None))
    cp.reset_for_tests()
    yield
    cp.reset_for_tests()


def _client(user: AuthUser = STAFF) -> TestClient:
    app = FastAPI()
    app.include_router(deals_router.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def api():
    return _client()


@pytest.fixture
def deal_id(api):
    response = api.post("/deals", json={"operator_id": "ignored", "hs_code": "1520", "market": "ES"})
    assert response.status_code == 201
    return response.json()["deal_id"]


def _board(*pairs, offers=()):
    return SimpleNamespace(pairs=list(pairs), offers=list(offers), model_dump=lambda mode="json": {"pairs": len(pairs)})


def _pair(candidate="C1", offer="O1", basis="quoted", landed=True, blockers=(), buyer=900.0, supplier=700.0, firm=True):
    return SimpleNamespace(
        candidate_id=candidate, offer_id=offer, landed=landed, blockers=list(blockers), firm=firm,
        supplier_price=SimpleNamespace(basis=basis, value=supplier), buyer_price=SimpleNamespace(value=buyer),
    )


PAIR_REF = {"candidate_id": "C1", "offer_id": "O1"}


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

class TestTenancy:
    def _deal(self, **orgs):
        return SimpleNamespace(**{"org_id": "owner", "buyer_org_id": "buyer", "seller_org_id": "seller", **orgs})

    @pytest.mark.parametrize("org", ["owner", "buyer", "seller"])
    async def test_every_party_org_can_read(self, org):
        deal = self._deal()
        with patch.object(deals_router.service, "get_deal", new=AsyncMock(return_value=deal)):
            assert await deals_router._org_deal("D", AuthUser(user_id="u", org_id=org)) is deal

    async def test_other_org_gets_404_not_403(self):
        with (
            patch.object(deals_router.service, "get_deal", new=AsyncMock(return_value=self._deal())),
            pytest.raises(HTTPException) as err,
        ):
            await deals_router._org_deal("D", AuthUser(user_id="u", org_id="intruder"))
        assert err.value.status_code == 404, "a 403 would confirm the deal exists"

    async def test_staff_reach_every_deal(self):
        deal = self._deal()
        with patch.object(deals_router.service, "get_deal", new=AsyncMock(return_value=deal)):
            assert await deals_router._org_deal("D", STAFF) is deal

    async def test_user_without_org_is_refused(self):
        with pytest.raises(HTTPException) as err:
            await deals_router._org_deal("D", UNASSIGNED)
        assert err.value.status_code == 403

    def test_unknown_deal_is_404(self, api):
        assert api.get("/deals/NOPE").status_code == 404

    def test_customer_only_lists_their_org(self, api, deal_id):
        customer = _client(CUSTOMER)
        created = customer.post("/deals", json={"operator_id": "x", "hs_code": "1520", "market": "ES"}).json()
        assert created["org_id"] == "acme.com"
        assert [d["deal_id"] for d in customer.get("/deals").json()] == [created["deal_id"]]
        assert len(api.get("/deals").json()) == 2

    def test_customer_cannot_read_another_orgs_deal(self, deal_id):
        assert _client(CUSTOMER).get(f"/deals/{deal_id}").status_code == 404

    def test_list_without_org_is_refused(self):
        assert _client(UNASSIGNED).get("/deals").status_code == 403


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

class TestDealRoutes:
    def test_creator_identity_is_forced(self, api, deal_id):
        deal = api.get(f"/deals/{deal_id}").json()
        assert deal["operator_id"] == "U-STAFF" and deal["org_id"] == "portless"

    def test_list_filters_and_limits(self, api, deal_id):
        assert len(api.get("/deals", params={"status": "DRAFT"}).json()) == 1
        assert api.get("/deals", params={"status": "PAID"}).json() == []
        assert api.get("/deals", params={"status": "BOGUS"}).status_code == 422
        for limit in (0, 501):
            assert api.get("/deals", params={"limit": limit}).status_code == 422

    def test_status_transition(self, api, deal_id):
        moved = api.patch(f"/deals/{deal_id}/status", json={"target_status": "MATCHING", "reason": "go"})
        assert moved.status_code == 200 and moved.json()["status"] == "MATCHING"

    def test_illegal_transition_is_422_and_changes_nothing(self, api, deal_id):
        assert api.patch(f"/deals/{deal_id}/status", json={"target_status": "PAID"}).status_code == 422
        assert api.get(f"/deals/{deal_id}").json()["status"] == "DRAFT"

    def test_status_race_with_deletion(self, api, deal_id):
        with patch.object(deals_router.service, "update_status", new=AsyncMock(side_effect=DealNotFoundError(deal_id))):
            assert api.patch(f"/deals/{deal_id}/status", json={"target_status": "MATCHING"}).status_code == 404

    def test_financials(self, api, deal_id):
        assert api.patch(f"/deals/{deal_id}/financials", json={"realized_margin_usd": 12.5}).json()["realized_margin_usd"] == 12.5
        assert api.patch("/deals/NOPE/financials", json={"realized_margin_usd": 1}).status_code == 404

    def test_party_correction_errors(self, api, deal_id):
        url = f"/deals/{deal_id}/parties/buyer"
        with patch.object(deals_router.service, "update_party", new=AsyncMock(side_effect=ValueError("bad role"))):
            assert api.patch(url, json={"email": "a@b.c"}).status_code == 422
        with patch.object(deals_router.service, "update_party", new=AsyncMock(side_effect=DealNotFoundError(deal_id))):
            assert api.patch(url, json={"email": "a@b.c"}).status_code == 404

    def test_party_correction_is_marked_operator(self, api, deal_id):
        real = api.get(f"/deals/{deal_id}").json()
        update = AsyncMock(return_value=deals_service.DealRecord(**real))
        with patch.object(deals_router.service, "update_party", new=update):
            assert api.patch(f"/deals/{deal_id}/parties/buyer", json={"email": "a@b.c"}).status_code == 200
        assert update.await_args.args == (deal_id, "buyer", {"email": "a@b.c"})
        assert update.await_args.kwargs == {"source": "operator"}

    def test_enrich(self, api, deal_id):
        deal = api.get(f"/deals/{deal_id}").json()
        run = AsyncMock(return_value={"deal_id": deal_id, "deal": deal, "warnings": ["w"]})
        with patch.object(deals_router.service, "enrich_deal", new=run):
            response = api.post(f"/deals/{deal_id}/enrich", params={"screen": "false"})
        assert response.json()["warnings"] == ["w"]
        assert run.await_args.kwargs == {"screen": False, "contacts": True}

    def test_enrich_race_with_deletion(self, api, deal_id):
        with patch.object(deals_router.service, "enrich_deal", new=AsyncMock(side_effect=DealNotFoundError(deal_id))):
            assert api.post(f"/deals/{deal_id}/enrich").status_code == 404

    def test_match_scores_best_first(self, api, deal_id):
        scores = [
            MatchResult(match_id=f"M{score}", supplier_id="S", composite_score=score, gatekeeper_passed=True,
                        arbitrage_score=0.0, proximity_score=0.0, reliability_score=0.0, decision="SOURCE")
            for score in (0.2, 0.9, 0.5)
        ]
        deal = SimpleNamespace(match_results=scores)
        with patch.object(deals_router, "_org_deal", new=AsyncMock(return_value=deal)):
            body = api.get(f"/deals/{deal_id}/match-scores").json()
        assert [row["composite_score"] for row in body] == [0.9, 0.5, 0.2]

    def test_match_scores_empty(self, api, deal_id):
        assert api.get(f"/deals/{deal_id}/match-scores").json() == []

    def test_health(self, api):
        assert api.get("/deals/health").json() == {"status": "ok", "service": "deals"}


class TestHsPolicyRoutes:
    def test_list_includes_the_seed(self, api):
        body = api.get("/deals/compliance/hs-policy").json()
        assert body["confirmed"] == [] and body["seed"]["93"]["status"] == "scomet"

    def test_confirm_records_who(self, api):
        entry = api.put("/deals/compliance/hs-policy/1520", json={"status": "free"}).json()
        assert entry["status"] == "free" and entry["confirmed_by"] == "ops@portless.io"

    def test_unknown_status_refused(self, api):
        assert api.put("/deals/compliance/hs-policy/1520", json={"status": "fine-by-me"}).status_code == 422

    def test_licensed_needs_the_licence(self, api):
        assert api.put("/deals/compliance/hs-policy/1520", json={"status": "licensed"}).status_code == 422
        ok = api.put("/deals/compliance/hs-policy/1520", json={"status": "licensed", "licence_ref": "L-1"})
        assert ok.status_code == 200 and ok.json()["licence_ref"] == "L-1"

    @pytest.mark.parametrize("user", [CUSTOMER, UNASSIGNED], ids=["customer", "unassigned"])
    def test_staff_only(self, user):
        client = _client(user)
        assert client.get("/deals/compliance/hs-policy").status_code == 403
        assert client.put("/deals/compliance/hs-policy/1520", json={"status": "free"}).status_code == 403

    def test_route_is_not_read_as_a_deal_id(self, api):
        assert api.get("/deals/compliance/hs-policy").status_code == 200


class TestComplianceAndEvidenceRoutes:
    def test_record(self, api, deal_id):
        assert api.get(f"/deals/{deal_id}/compliance").json()["holds"] == []

    def test_manual_check_uses_negotiated_price(self, api, deal_id):
        with patch("shared.orchestration.continuations._negotiation_numbers", new=AsyncMock(return_value=(700.0, 20.0))):
            result = api.post(f"/deals/{deal_id}/compliance/check").json()
        assert result["stage"] == "manual" and result["status"] == "review"

    def test_timeline(self, api, deal_id):
        with patch("shared.orchestration.evidence.build_timeline", new=AsyncMock(return_value={"events": []})):
            assert api.get(f"/deals/{deal_id}/timeline").json() == {"events": []}
        with patch("shared.orchestration.evidence.build_timeline", new=AsyncMock(side_effect=LookupError("gone"))):
            assert api.get(f"/deals/{deal_id}/timeline").status_code == 404

    def test_evidence_pack_formats(self, api, deal_id):
        with patch("shared.orchestration.evidence.build_evidence_pack", new=AsyncMock(return_value={"sha256": "x"})), \
                patch("shared.orchestration.evidence.render_evidence_html", return_value="<h1>pack</h1>"):
            assert api.get(f"/deals/{deal_id}/evidence-pack").json() == {"sha256": "x"}
            html = api.get(f"/deals/{deal_id}/evidence-pack", params={"format": "html"})
        assert html.headers["content-type"].startswith("text/html") and "pack" in html.text

    def test_evidence_pack_missing(self, api, deal_id):
        with patch("shared.orchestration.evidence.build_evidence_pack", new=AsyncMock(side_effect=LookupError("gone"))):
            assert api.get(f"/deals/{deal_id}/evidence-pack").status_code == 404


class TestOutreachRoutes:
    CANDIDATE: ClassVar[dict] = {"candidate_id": "C1", "legal_name": "Buyer", "country": "DE"}

    def test_add_candidate(self, api, deal_id):
        added = api.post(f"/deals/{deal_id}/buyer-candidates", json=self.CANDIDATE)
        assert added.status_code == 201 and added.json()["buyer_candidates"][0]["candidate_id"] == "C1"

    @pytest.mark.parametrize(("error", "code"), [(ValueError("dup"), 422), (DealNotFoundError("D"), 404)])
    def test_add_candidate_errors(self, api, deal_id, error, code):
        with patch.object(deals_router.service, "add_buyer_candidate", new=AsyncMock(side_effect=error)):
            assert api.post(f"/deals/{deal_id}/buyer-candidates", json=self.CANDIDATE).status_code == code

    def test_start(self, api, deal_id):
        with patch("shared.orchestration.outreach.start_outreach", new=AsyncMock(return_value=2)), \
                patch("shared.orchestration.outreach.waiting_for_buyer", return_value=True):
            assert api.post(f"/deals/{deal_id}/outreach/start").json() == {
                "deal_id": deal_id, "contacted": 2, "waiting_for_buyer": True,
            }

    def test_skip(self, api, deal_id):
        with patch("shared.orchestration.outreach.skip_candidate", new=AsyncMock(return_value={"next": "C2"})) as skip:
            body = api.post(f"/deals/{deal_id}/buyer-candidates/C1/skip").json()
        assert body == {"deal_id": deal_id, "candidate_id": "C1", "next": "C2"}
        assert skip.await_args.args == (deal_id, "C1", "skipped by operator")

    def test_skip_unknown_candidate(self, api, deal_id):
        with patch("shared.orchestration.outreach.skip_candidate", new=AsyncMock(side_effect=ValueError("no"))):
            assert api.post(f"/deals/{deal_id}/buyer-candidates/C9/skip").status_code == 422


class TestMatchBoardRoutes:
    def _record(self, api, deal_id):
        return deals_service.DealRecord(**api.get(f"/deals/{deal_id}").json())

    def test_board(self, api, deal_id):
        with patch("shared.orchestration.matchmaking.build_board", new=AsyncMock(return_value=_board(_pair()))):
            assert api.get(f"/deals/{deal_id}/match-board").json() == {"pairs": 1}

    def test_requirement_with_price_feeds_the_price_book(self, api, deal_id):
        record = self._record(api, deal_id)
        price = AsyncMock()
        with patch.object(deals_router.service, "set_buyer_requirement", new=AsyncMock(return_value=record)), \
                patch("shared.pricebook.record", new=price):
            response = api.put(f"/deals/{deal_id}/buyer-candidates/C1/requirement",
                               json={"target_price_usd_per_mt": 900, "incoterm": "CIF", "grade": "A"})
        assert response.status_code == 200
        observation = price.await_args.args[0]
        assert observation.side == "buyer" and observation.kind == "buyer_target" and observation.counterparty_id == "C1"
        assert observation.price_usd_per_mt == pytest.approx(900.0)

    def test_requirement_without_price_records_nothing(self, api, deal_id):
        record = self._record(api, deal_id)
        price = AsyncMock()
        with patch.object(deals_router.service, "set_buyer_requirement", new=AsyncMock(return_value=record)), \
                patch("shared.pricebook.record", new=price):
            api.put(f"/deals/{deal_id}/buyer-candidates/C1/requirement", json={"grade": "A"})
        price.assert_not_awaited()

    def test_requirement_for_unknown_candidate(self, api, deal_id):
        with patch.object(deals_router.service, "set_buyer_requirement", new=AsyncMock(side_effect=ValueError("no"))):
            assert api.put(f"/deals/{deal_id}/buyer-candidates/C9/requirement", json={}).status_code == 422

    def test_offer_is_marked_operator_and_priced(self, api, deal_id):
        record = self._record(api, deal_id)
        upsert, price = AsyncMock(return_value=record), AsyncMock()
        with patch.object(deals_router.service, "upsert_supplier_offer", new=upsert), patch("shared.pricebook.record", new=price):
            response = api.post(f"/deals/{deal_id}/supplier-offers",
                                json={"offer_id": "O1", "supplier_id": "S1", "fob_price_usd_per_mt": 700})
        assert response.status_code == 201
        assert upsert.await_args.args[1].source == "operator"
        assert price.await_args.args[0].kind == "supplier_offer"

    def test_offer_without_price_records_nothing(self, api, deal_id):
        record = self._record(api, deal_id)
        price = AsyncMock()
        with patch.object(deals_router.service, "upsert_supplier_offer", new=AsyncMock(return_value=record)), \
                patch("shared.pricebook.record", new=price):
            api.post(f"/deals/{deal_id}/supplier-offers", json={"offer_id": "O1", "supplier_id": "S1"})
        price.assert_not_awaited()

    def test_evaluate(self, api, deal_id):
        with patch("shared.orchestration.matchmaking.evaluate", new=AsyncMock(return_value={"opened": 1})):
            assert api.post(f"/deals/{deal_id}/match/evaluate").json() == {"opened": 1}

    @pytest.mark.parametrize("path", ["indicative-offer", "confirm"])
    def test_pair_not_on_board(self, api, deal_id, path):
        with patch("shared.orchestration.matchmaking.build_board", new=AsyncMock(return_value=_board())):
            assert api.post(f"/deals/{deal_id}/match/{path}", json=PAIR_REF).status_code == 404

    @pytest.mark.parametrize("pair", [
        _pair(landed=False), _pair(basis="estimated"), _pair(blockers=["moq"]),
    ], ids=["not_landed", "not_quoted", "blocked"])
    def test_indicative_offer_refused(self, api, deal_id, pair):
        propose = AsyncMock()
        with patch("shared.orchestration.matchmaking.build_board", new=AsyncMock(return_value=_board(pair))), \
                patch("shared.orchestration.matchmaking.propose_indicative_offer", new=propose):
            assert api.post(f"/deals/{deal_id}/match/indicative-offer", json=PAIR_REF).status_code == 422
        propose.assert_not_awaited()

    def test_indicative_offer_needs_an_address(self, api, deal_id):
        with patch("shared.orchestration.matchmaking.build_board", new=AsyncMock(return_value=_board(_pair()))), \
                patch("shared.orchestration.matchmaking.propose_indicative_offer", new=AsyncMock(return_value=None)):
            assert api.post(f"/deals/{deal_id}/match/indicative-offer", json=PAIR_REF).status_code == 422

    def test_indicative_offer_passes_lead_time(self, api, deal_id):
        board = _board(_pair(), offers=[{"offer_id": "O1", "lead_time_days": 21}])
        propose = AsyncMock(return_value="APR-1")
        with patch("shared.orchestration.matchmaking.build_board", new=AsyncMock(return_value=board)), \
                patch("shared.orchestration.matchmaking.propose_indicative_offer", new=propose):
            body = api.post(f"/deals/{deal_id}/match/indicative-offer", json=PAIR_REF).json()
        assert body == {"deal_id": deal_id, "approval_id": "APR-1"}
        assert propose.await_args.args[3] == 21

    @pytest.mark.parametrize("pair", [
        _pair(blockers=["grade conflict"]), _pair(buyer=None), _pair(supplier=None),
    ], ids=["blocked", "no_buyer_price", "no_supplier_price"])
    def test_confirm_refused(self, api, deal_id, pair):
        propose = AsyncMock()
        with patch("shared.orchestration.matchmaking.build_board", new=AsyncMock(return_value=_board(pair))), \
                patch("shared.orchestration.matchmaking.propose_match", new=propose):
            assert api.post(f"/deals/{deal_id}/match/confirm", json=PAIR_REF).status_code == 422
        propose.assert_not_awaited()

    def test_confirm(self, api, deal_id):
        with patch("shared.orchestration.matchmaking.build_board", new=AsyncMock(return_value=_board(_pair()))), \
                patch("shared.orchestration.matchmaking.propose_match", new=AsyncMock(return_value="APR-2")):
            assert api.post(f"/deals/{deal_id}/match/confirm", json=PAIR_REF).json()["approval_id"] == "APR-2"


# ---------------------------------------------------------------------------
# Saga callbacks
# ---------------------------------------------------------------------------

def _as_namespace(**kw):
    return SimpleNamespace(**kw)


@pytest.fixture
def published(monkeypatch):
    events = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


class TestApproveCallbacks:
    async def test_book_freight(self):
        call = AsyncMock(return_value=SimpleNamespace(tpo_id="T", status="BOOKED"))
        with patch("services.shield_link.service.book_freight", new=call), \
                patch("services.shield_link.schemas.FreightBookingRequest", new=_as_namespace):
            out = await cb.execute_callback("BOOK_FREIGHT", "D", {"deal_id": "D", "origin": "X"}, "A")
        assert out == {"tpo_id": "T", "status": "BOOKED"}
        assert call.await_args.args[0].origin == "X"

    async def test_issue_po(self):
        call = AsyncMock(return_value=SimpleNamespace(po_id="PO", status="ISSUED"))
        with patch("services.dealmaker.service.issue_po", new=call), \
                patch("services.dealmaker.schemas.IssuePoRequest", new=_as_namespace):
            assert await cb.execute_callback("ISSUE_PO", "D", {"deal_id": "D"}, "A") == {"po_id": "PO", "status": "ISSUED"}

    async def test_file_customs(self):
        call = AsyncMock(return_value=SimpleNamespace(tpo_id="C", status="FILED"))
        with patch("services.shield_link.service.file_cha", new=call), \
                patch("services.shield_link.schemas.ChaFilingRequest", new=_as_namespace):
            assert await cb.execute_callback("FILE_CUSTOMS", "D", {"deal_id": "D"}, "A") == {"tpo_id": "C", "status": "FILED"}

    async def test_payout_drops_fields_the_request_does_not_take(self):
        call = AsyncMock(return_value=SimpleNamespace(payout_id="P", status="PROCESSING", kind="supplier_payout"))
        payload = {"deal_id": "D", "payee_id": "S", "amount": 5.0, "idempotency_key": "k",
                   "compliance_note": "x", "resolved_by": "ops", "total_owed": 9.0}
        with patch("services.escrow_guard.service.request_payout", new=call):
            out = await cb.execute_callback("RELEASE_SUPPLIER_PAYOUT", "D", payload, "A")
        assert out["payout_id"] == "P"
        assert call.await_args.args[0].total_owed == 9.0

    async def test_negotiation_email_as_drafted(self):
        call = AsyncMock()
        with patch("agents.negotiation_agent.service.send_approved_email", new=call):
            out = await cb.execute_callback("SEND_NEGOTIATION_EMAIL", "D", {"email_id": "E"}, "A")
        assert out == {"email_id": "E", "status": "sent"}
        call.assert_awaited_once_with("E", None)

    async def test_negotiation_email_edited_by_operator(self):
        call = AsyncMock()
        with patch("agents.negotiation_agent.service.send_approved_email", new=call):
            out = await cb.execute_callback("SEND_NEGOTIATION_EMAIL", "D", {"email_id": "E", "modified_body": "new"}, "A")
        assert out["status"] == "modified_sent"
        call.assert_awaited_once_with("E", "new")

    async def test_confirm_agreement_is_the_finalize_path(self):
        call = AsyncMock()
        with patch("agents.negotiation_agent.service.finalize_agreement", new=call):
            out = await cb.execute_callback("CONFIRM_NEGOTIATION_AGREEMENT", "D", {"negotiation_id": "N", "agreed_price": 900.0}, "A")
        assert out == {"negotiation_id": "N", "status": "agreed"}
        call.assert_awaited_once_with("N", 900.0)

    async def test_indicative_offer_uses_operator_text(self):
        call = AsyncMock(return_value={"sent": True})
        with patch("shared.orchestration.matchmaking.send_indicative_offer", new=call):
            await cb.execute_callback("SEND_INDICATIVE_OFFER", "D", {"body": "old", "modified_body": "new"}, "A")
        assert call.await_args.args[1]["body"] == "new"

    async def test_indicative_offer_as_drafted(self):
        call = AsyncMock(return_value={"sent": True})
        with patch("shared.orchestration.matchmaking.send_indicative_offer", new=call):
            await cb.execute_callback("SEND_INDICATIVE_OFFER", "D", {"body": "old"}, "A")
        assert call.await_args.args[1]["body"] == "old"

    async def test_confirm_match(self):
        with patch("shared.orchestration.matchmaking.confirm_match", new=AsyncMock(return_value={"status": "NEGOTIATING"})):
            assert await cb.execute_callback("CONFIRM_MATCH", "D", {"candidate_id": "C"}, "A") == {"status": "NEGOTIATING"}

    @pytest.mark.parametrize("status", ["NEGOTIATING", "PI_SENT"])
    async def test_supplier_confirmed_on_a_moved_deal_does_not_transition(self, status):
        deal = SimpleNamespace(status=SimpleNamespace(value=status))
        update = AsyncMock()
        with patch("services.deals.service.record_match_result", new=AsyncMock()), \
                patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
                patch("services.deals.service.update_status", new=update):
            out = await cb.execute_callback("CONFIRM_SUPPLIER_SELECTION", "D", {"supplier_id": "S"}, "A")
        assert out["status"] == status
        update.assert_not_awaited()

    @pytest.mark.parametrize("action", ["CONFIRM_COUNTERPARTY_RISK", "CONFIRM_TRADE_COMPLIANCE"])
    async def test_pi_withheld_when_recheck_finds_something(self, action):
        with patch("shared.orchestration.continuations.pi_after_compliance", new=AsyncMock(return_value=None)):
            out = await cb.execute_callback(action, "D", {"agreed_price": 900, "codes": []}, "A")
        assert out == {"status": "pi_withheld_by_compliance"}

    async def test_inbound_email_is_acknowledged_only(self):
        assert await cb.execute_callback("PARSE_INBOUND_EMAIL", "D", {"inbound_id": "I"}, "A") == {
            "inbound_id": "I", "status": "acknowledged",
        }

    async def test_unknown_action_does_nothing(self, published):
        assert await cb.execute_callback("NOT_AN_ACTION", "D", {}, "A") is None
        assert published == []

    async def test_failure_becomes_a_deal_exception(self, published):
        with patch("agents.negotiation_agent.service.send_approved_email", new=AsyncMock(side_effect=RuntimeError("smtp"))):
            assert await cb.execute_callback("SEND_NEGOTIATION_EMAIL", "D", {"email_id": "E"}, "APR") is None
        event = published[0]
        assert event.topic == EventTopic.DEAL_EXCEPTION
        assert event.payload["action_required"] == "manual_retry_or_intervention"
        assert event.payload["approval_id"] == "APR" and event.payload["error"] == "smtp"

    async def test_missing_payload_key_becomes_a_deal_exception(self, published):
        assert await cb.execute_callback("RELEASE_ESCROW_TRANCHE", "D", {}, "APR") is None
        assert published[0].topic == EventTopic.DEAL_EXCEPTION

    async def test_bus_failure_during_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr("shared.bus.get_publisher", lambda: (_ for _ in ()).throw(RuntimeError("bus")))
        assert await cb.execute_callback("RELEASE_ESCROW_TRANCHE", "D", {}, "APR") is None


class TestRejectCallbacks:
    async def test_email_draft_rejected(self):
        call = AsyncMock()
        with patch("agents.negotiation_agent.service.reject_email", new=call):
            out = await cb.execute_reject_callback("SEND_NEGOTIATION_EMAIL", "D", {"email_id": "E"}, "A")
        assert out == {"email_id": "E", "status": "rejected"}
        call.assert_awaited_once_with("E")

    async def test_notification_shares_the_draft_lifecycle(self):
        call = AsyncMock()
        with patch("agents.negotiation_agent.service.reject_email", new=call):
            await cb.execute_reject_callback("SEND_DEAL_NOTIFICATION", "D", {"email_id": "E"}, "A")
        call.assert_awaited_once_with("E")

    async def test_agreement_rejected_reopens(self):
        call = AsyncMock()
        with patch("agents.negotiation_agent.service.reject_agreement", new=call):
            out = await cb.execute_reject_callback("CONFIRM_NEGOTIATION_AGREEMENT", "D", {"negotiation_id": "N"}, "A")
        assert out == {"negotiation_id": "N", "status": "reopened"}

    async def test_match_rejected_feeds_the_outcome_tracker(self):
        mark = AsyncMock()
        with patch("shared.orchestration.outcomes.mark_decision", new=mark):
            out = await cb.execute_reject_callback("CONFIRM_MATCH", "D", {"candidate_id": "C", "offer_id": "O"}, "A")
        assert out["status"] == "declined"
        mark.assert_awaited_once_with("D", "C", "O", "rejected")

    @pytest.mark.parametrize("action", [
        "DISPATCH_RFQ", "CONFIRM_SUPPLIER_SELECTION", "CONFIRM_PI_ACCEPTANCE", "PARSE_INBOUND_EMAIL",
        "CONFIRM_BUYER_ACCEPTANCE", "SEND_INDICATIVE_OFFER",
    ])
    async def test_routine_decline_changes_nothing(self, action):
        assert await cb.execute_reject_callback(action, "D", {}, "A") == {"deal_id": "D", "status": "declined"}

    @pytest.mark.parametrize("action", ["ISSUE_PO", "RELEASE_ESCROW_TRANCHE", "NOT_AN_ACTION"])
    async def test_no_reject_handler(self, action):
        assert await cb.execute_reject_callback(action, "D", {}, "A") is None

    async def test_failure_is_logged_only(self, published):
        with patch("agents.negotiation_agent.service.reject_email", new=AsyncMock(side_effect=RuntimeError("x"))):
            assert await cb.execute_reject_callback("SEND_NEGOTIATION_EMAIL", "D", {"email_id": "E"}, "A") is None
        assert published == []


class TestCallbackRegistry:
    MONEY_OR_COMMITMENT: ClassVar[set] = {
        "ISSUE_PO", "BOOK_FREIGHT", "FILE_CUSTOMS", "RELEASE_SUPPLIER_PAYOUT", "RELEASE_ESCROW_TRANCHE",
        "RESOLVE_EXCEPTION", "CONFIRM_COUNTERPARTY_RISK", "CONFIRM_TRADE_COMPLIANCE",
    }

    @pytest.mark.parametrize("action", sorted(MONEY_OR_COMMITMENT))
    def test_refusing_money_is_always_a_hard_reject(self, action):
        assert action not in cb.SOFT_REJECT_ACTIONS

    @pytest.mark.parametrize("action", sorted(MONEY_OR_COMMITMENT))
    def test_money_actions_have_a_handler(self, action):
        assert action in cb._CALLBACK_MAP

    @pytest.mark.parametrize("action", sorted(a.value for a in ApprovalAction))
    def test_every_declared_action_has_a_handler(self, action):
        assert action in cb._CALLBACK_MAP

    def test_every_soft_reject_has_an_approve_path(self):
        assert cb.SOFT_REJECT_ACTIONS <= set(cb._CALLBACK_MAP)


class TestComplianceAcceptance:
    async def _holds(self, *holds):
        record = await cp.get_record("D")
        record.holds = list(holds)
        await cp.save_record(record)

    async def test_review_flag_is_accepted_on_approve(self):
        await self._holds("payer_not_buyer")
        await cb.execute_callback("RESOLVE_EXCEPTION", "D", {"compliance_accept": ["payer_not_buyer"], "resolved_by": "ops"}, "A")
        record = await cp.get_record("D")
        assert record.holds == [] and record.cleared_holds == ["payer_not_buyer"]

    async def test_no_flags_touches_nothing(self):
        await self._holds("payer_not_buyer")
        await cb.execute_callback("RESOLVE_EXCEPTION", "D", {}, "A")
        assert (await cp.get_record("D")).holds == ["payer_not_buyer"]

    @pytest.mark.parametrize("code", [
        "seal_mismatch", "loading_not_verified", "shipment_hs_mismatch", "hs_scomet", "hs_prohibited",
        "destination_embargoed", "buyer_country_embargoed", "supplier_country_embargoed",
        "payer_country_embargoed", "supplier_dgft_denied",
    ])
    async def test_block_level_hold_survives_an_unrelated_approval(self, code):
        await self._holds(code)
        await cb.execute_callback("RESOLVE_EXCEPTION", "D", {"compliance_accept": [code], "resolved_by": "ops"}, "A")
        record = await cp.get_record("D")
        assert code in record.holds and code not in record.cleared_holds
        assert f"compliance:{code}" in await cp.hold_flags("D"), "later gates must still stop for a human"

    async def test_approval_accepts_review_flags_and_keeps_blocks(self):
        await self._holds("payer_not_buyer", "seal_mismatch")
        await cb.execute_callback(
            "RESOLVE_EXCEPTION", "D", {"compliance_accept": ["payer_not_buyer", "seal_mismatch"], "resolved_by": "ops"}, "A",
        )
        record = await cp.get_record("D")
        assert record.holds == ["seal_mismatch"] and record.cleared_holds == ["payer_not_buyer"]

    async def test_severity_configured_to_block_is_not_accepted(self):
        record = await cp.get_record("D")
        record.holds = ["buyer_screening_unchecked"]
        record.trade = cp.CheckResult(stage="pre_pi", status="block", findings=[
            cp.Finding(code="buyer_screening_unchecked", severity="block", message="never screened"),
        ])
        await cp.save_record(record)
        await cp.accept_findings("D", ["buyer_screening_unchecked"], by="ops")
        assert (await cp.get_record("D")).holds == ["buyer_screening_unchecked"]

    async def test_same_code_at_review_severity_is_accepted(self):
        record = await cp.get_record("D")
        record.holds = ["buyer_screening_unchecked"]
        record.trade = cp.CheckResult(stage="pre_pi", status="review", findings=[
            cp.Finding(code="buyer_screening_unchecked", severity="review", message="never screened"),
        ])
        await cp.save_record(record)
        await cp.accept_findings("D", ["buyer_screening_unchecked"], by="ops")
        assert (await cp.get_record("D")).holds == []
