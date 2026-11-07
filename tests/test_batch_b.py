"""Batch B automation: buyer outreach funnel, catalogue / corridors, carrier
tracking + delivery-triggered inspection window, resolver playbooks, Apify
daily run budget, watcher follow-ups / fallback / tracking poll."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Bind every service module BEFORE any test patches a repository accessor:
# a module first imported inside a ``patch(...)`` block would keep the mock as
# its module-level ``get_repository`` for the rest of the session.
import agents.negotiation_agent.repository  # noqa: F401
import services.deals.service  # noqa: F401
import services.email_processor.service  # noqa: F401
import services.escrow_guard.service  # noqa: F401
import services.exception_resolver.agent  # noqa: F401
import services.exception_resolver.service  # noqa: F401
import services.hitl_approval.callbacks  # noqa: F401
import services.hitl_approval.service  # noqa: F401
import services.shield_link.service  # noqa: F401
import shared.orchestration.continuations  # noqa: F401
import shared.orchestration.notifications  # noqa: F401
import shared.orchestration.playbooks  # noqa: F401
from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import BuyerCandidate, DealRecord, DealStatus
from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import autonomy_policy as ap
from shared.orchestration import outreach

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_ON_DEAL_CREATED", "false")
    for var in ("OUTREACH_ENABLED", "OUTREACH_PARALLEL", "OUTREACH_FOLLOWUP_DAYS", "OUTREACH_NO_REPLY_DAYS",
                "APIFY_MAX_RUNS_PER_DAY", "EXCEPTION_PLAYBOOKS_ENABLED", "QC_REINSPECTION_MAX",
                "ESCROW_FUNDING_GRACE_DAYS", "INSPECTION_WINDOW_FALLBACK_DAYS", "TRACKING_POLL_HOURS"):
        monkeypatch.delenv(var, raising=False)
    ap.reset_for_tests()
    monkeypatch.setattr(ap, "_mongo_collection", lambda: None)
    yield
    ap.reset_for_tests()


@pytest.fixture
def deals_repo(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    return repo


@pytest.fixture
def published(monkeypatch):
    events: list[DomainEvent] = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


def _cand(cid: str, email: str | None = None, **kw) -> BuyerCandidate:
    return BuyerCandidate(candidate_id=cid, legal_name=f"Buyer {cid}", country="DE", email=email,
                          pitch_angle="They import Indian pepper every quarter.", **kw)


def _deal(deal_id="DEAL-OUT-1", status=DealStatus.MATCHING, candidates=None, **kw) -> DealRecord:
    return DealRecord(
        deal_id=deal_id, operator_id="op", org_id="portless", hs_code="090411", market="DE",
        status=status, created_at=NOW, updated_at=NOW, buyer_candidates=candidates or [], **kw,
    )


# ---------------------------------------------------------------------------
# Outreach funnel
# ---------------------------------------------------------------------------

class TestOutreachBasics:
    def test_token_and_classifier(self):
        assert outreach.parse_token("Re: Pepper from India [BUY:DEAL-ABC12:c-1]") == ("DEAL-ABC12", "c-1")
        assert outreach.parse_token("no token") is None
        assert outreach.classify_reply("Yes, interested. Please send your best price for 20 MT CIF Hamburg.") == "interested"
        assert outreach.classify_reply("Not interested, please remove us from your list.") == "declined"
        assert outreach.classify_reply("Who gave you this address?") == "unclear"
        assert outreach.classify_reply(None) == "unclear"

    def test_waiting_for_buyer(self):
        assert outreach.waiting_for_buyer(SimpleNamespace(buyer_candidates=[], parties=[])) is False
        pending = SimpleNamespace(buyer_candidates=[_cand("a", "a@x.de")], parties=[])
        assert outreach.waiting_for_buyer(pending) is True
        selected = SimpleNamespace(buyer_candidates=[_cand("a", "a@x.de", status="selected")], parties=[])
        assert outreach.waiting_for_buyer(selected) is False
        operator = SimpleNamespace(
            buyer_candidates=[_cand("a", "a@x.de")],
            parties=[SimpleNamespace(role="buyer", email="ops@buyer.de", contact_source="operator")],
        )
        assert outreach.waiting_for_buyer(operator) is False

    def test_draft_carries_token_pitch_and_optout(self):
        deal = _deal(quantity_mt=20.0)
        subject, body = outreach.draft(deal, _cand("c1", "c1@x.de"))
        assert "[BUY:DEAL-OUT-1:c1]" in subject
        assert "They import Indian pepper every quarter." in body and "20 MT" in body
        assert "not interested" in body
        fsubject, fbody = outreach.draft(deal, _cand("c1", "c1@x.de"), followup=True)
        assert fsubject.startswith("Re:") and "[BUY:DEAL-OUT-1:c1]" in fsubject and "Following up" in fbody


class TestOutreachFlow:
    @pytest.mark.asyncio
    async def test_start_outreach_gates_first_candidates_with_address(self, deals_repo):
        deal = _deal(candidates=[_cand("a", "a@x.de"), _cand("b", None), _cand("c", "c@x.de"), _cand("d", "d@x.de")])
        await deals_repo.save(deal)
        gate = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-1"))
        with patch("services.hitl_approval.service.create_approval", new=gate):
            sent = await outreach.start_outreach(deal)
        assert sent == 2  # OUTREACH_PARALLEL default 2; candidate b has no address
        reqs = [c.args[0] for c in gate.await_args_list]
        assert [r.callback_payload["candidate_id"] for r in reqs] == ["a", "c"]
        assert all(r.action == "SEND_BUYER_OUTREACH" and r.autonomy_level == "L2" for r in reqs)
        assert reqs[0].idempotency_key == "outreach-intro-DEAL-OUT-1-a"
        assert "[BUY:DEAL-OUT-1:a]" in reqs[0].callback_payload["subject"]

    @pytest.mark.asyncio
    async def test_send_outreach_marks_candidate_contacted(self, deals_repo):
        await deals_repo.save(_deal(candidates=[_cand("a", "a@x.de")]))
        send = AsyncMock(return_value=SimpleNamespace(outcome=SimpleNamespace(value="sent")))
        with patch("services.email_gateway.sender.send_email", new=send):
            res = await outreach.send_outreach("DEAL-OUT-1", "a", "a@x.de", "Subj [BUY:DEAL-OUT-1:a]", "Body")
        req = send.await_args.args[0]
        assert req.to_address == "a@x.de" and req.negotiation_id == "" and req.email_id == res["email_id"]
        assert res["delivered"] is True
        deal = await deals_repo.get("DEAL-OUT-1")
        assert deal.buyer_candidates[0].status == "contacted" and deal.buyer_candidates[0].contacted_at is not None

    @pytest.mark.asyncio
    async def test_logged_only_email_is_not_contacted_and_can_be_retried(self, deals_repo):
        """No mailbox → the e-mail is only logged: the candidate stays pending with
        the reason, and the next start_outreach opens a fresh gate for it."""
        await deals_repo.save(_deal(candidates=[_cand("a", "a@x.de")]))
        send = AsyncMock(return_value=SimpleNamespace(outcome=SimpleNamespace(value="logged")))
        with patch("services.email_gateway.sender.send_email", new=send):
            res = await outreach.send_outreach("DEAL-OUT-1", "a", "a@x.de", "Subj", "Body")
        assert res["delivered"] is False
        deal = await deals_repo.get("DEAL-OUT-1")
        cand = deal.buyer_candidates[0]
        assert cand.status == "pending" and cand.contacted_at is None
        assert "no outbound mailbox" in cand.send_error and cand.send_attempts == 1
        gate = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-2"))
        with patch("services.hitl_approval.service.create_approval", new=gate):
            assert await outreach.start_outreach(deal) == 1
        assert gate.await_args.args[0].idempotency_key == "outreach-intro-DEAL-OUT-1-a-a1"

    @pytest.mark.asyncio
    async def test_interested_reply_sets_buyer_party_and_resumes_negotiation(self, deals_repo, published, monkeypatch):
        monkeypatch.setenv("MATCHMAKING_ENABLED", "false")  # legacy: first "yes" becomes the buyer
        deal = _deal(status=DealStatus.NEGOTIATING, candidates=[_cand("a", "a@x.de", status="contacted", website="x.de")])
        await deals_repo.save(deal)
        neg_repo = AsyncMock()
        neg_repo.get_session_by_deal = AsyncMock(return_value=None)
        resumed = AsyncMock()
        with (
            patch("agents.negotiation_agent.repository.get_repository", return_value=neg_repo),
            patch("shared.orchestration.continuations.on_negotiation_started", new=resumed),
        ):
            res = await outreach.handle_reply("DEAL-OUT-1", "a", "buyer@x.de", "Interested — please send your price.")
        assert res["verdict"] == "interested" and res["buyer_set"] is True
        deal = await deals_repo.get("DEAL-OUT-1")
        assert deal.buyer_candidates[0].status == "selected"
        buyer = next(p for p in deal.parties if p.role.value == "buyer")
        assert buyer.email == "buyer@x.de" and buyer.contact_source.startswith("outreach") and buyer.legal_name == "Buyer a"
        assert outreach.waiting_for_buyer(deal) is False
        resumed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_declined_reply_advances_then_exhausts(self, deals_repo, published):
        deal = _deal(candidates=[_cand("a", "a@x.de", status="contacted"), _cand("b", "b@x.de")])
        await deals_repo.save(deal)
        gate = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-2"))
        with patch("services.hitl_approval.service.create_approval", new=gate):
            # exact address: a and b share the x.de domain, so a colleague's address
            # would be ambiguous and is held for an operator (see test_negotiation_routing)
            res = await outreach.handle_reply("DEAL-OUT-1", None, "a@x.de", "No thanks, we are not buying this year.")
        assert res["verdict"] == "declined" and res["advanced"] == 1 and res["exhausted"] is False
        assert gate.await_args.args[0].callback_payload["candidate_id"] == "b"
        # b declines too → nobody left → deal.exception
        with patch("services.hitl_approval.service.create_approval", new=gate):
            res = await outreach.handle_reply("DEAL-OUT-1", "b", "b@x.de", "Not interested.")
        assert res["exhausted"] is True
        exc = [e for e in published if e.topic == EventTopic.DEAL_EXCEPTION]
        assert exc and exc[-1].payload["stage"] == "OUTREACH:exhausted" and exc[-1].payload["category"] == "NEGOTIATION_FAILED"

    @pytest.mark.asyncio
    async def test_followups_then_no_reply_then_next(self, deals_repo, published):
        deal = _deal(candidates=[
            _cand("a", "a@x.de", status="contacted", contacted_at=NOW - timedelta(days=4)),
            _cand("b", "b@x.de", status="followed_up", contacted_at=NOW - timedelta(days=12),
                  followed_up_at=NOW - timedelta(days=8)),
            _cand("c", "c@x.de"),
        ])
        await deals_repo.save(deal)
        gate = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-3"))
        with patch("services.hitl_approval.service.create_approval", new=gate):
            counts = await outreach.sweep_followups(NOW)
        assert counts == {"followups": 1, "no_reply": 1, "advanced": 1}
        reqs = [c.args[0] for c in gate.await_args_list]
        assert reqs[0].callback_payload["kind"] == "followup" and reqs[0].callback_payload["candidate_id"] == "a"
        assert reqs[1].callback_payload["candidate_id"] == "c"
        deal = await deals_repo.get("DEAL-OUT-1")
        assert {c.candidate_id: c.status for c in deal.buyer_candidates}["b"] == "no_reply"

    @pytest.mark.asyncio
    async def test_negotiation_waits_while_funnel_open(self, deals_repo):
        from shared.orchestration import continuations as c
        deal = _deal(status=DealStatus.NEGOTIATING, candidates=[_cand("a", "a@x.de", status="contacted")])
        await deals_repo.save(deal)
        started = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("agents.negotiation_agent.service.start_negotiation", new=started, create=True),
        ):
            await c.on_negotiation_started(DomainEvent(topic=EventTopic.NEGOTIATION_STARTED, deal_id="DEAL-OUT-1", payload={}))
        started.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_outreach_reply_mapped_by_subject_token(self, deals_repo, monkeypatch):
        import services.email_processor.repository as ep_repo_mod
        from services.email_processor.repository import InMemoryEmailProcessorRepository
        from services.email_processor.schemas import InboundEmailSource, InboundEmailStatus, InboundRawEmail
        from services.email_processor.service import process_inbound
        ep_repo_mod._repository = InMemoryEmailProcessorRepository()
        try:
            handled = AsyncMock(return_value={"verdict": "interested", "matched": True, "buyer_set": True})
            with patch("shared.orchestration.outreach.handle_reply", new=handled):
                result = await process_inbound(InboundRawEmail(
                    from_address="buyer@x.de", to_address="ops@portless.io",
                    subject="Re: Pepper from India [BUY:DEAL-OUT-1:a]", body_text="Interested, send price",
                    source=InboundEmailSource.MANUAL, received_at=NOW,
                ))
            assert result.status == InboundEmailStatus.PROCESSED and "interested" in result.message
            assert handled.await_args.args[:2] == ("DEAL-OUT-1", "a")
            unclear = AsyncMock(return_value={"verdict": "unclear", "matched": True})
            gate = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-U"))
            with (
                patch("shared.orchestration.outreach.handle_reply", new=unclear),
                patch("services.hitl_approval.service.create_approval", new=gate),
            ):
                result = await process_inbound(InboundRawEmail(
                    from_address="buyer@x.de", to_address="ops@portless.io",
                    subject="Re: [BUY:DEAL-OUT-1:a]", body_text="Who are you?",
                    source=InboundEmailSource.MANUAL, received_at=NOW,
                ))
            assert result.status == InboundEmailStatus.NEEDS_OPERATOR
            assert "outreach_reply" in gate.await_args.args[0].risk_flags
        finally:
            ep_repo_mod._repository = None

    @pytest.mark.asyncio
    async def test_deal_service_candidate_helpers(self, deals_repo):
        from services.deals.service import add_buyer_candidate, update_buyer_candidate
        await deals_repo.save(_deal())
        deal = await add_buyer_candidate("DEAL-OUT-1", _cand("z", "z@x.de"))
        assert [c.candidate_id for c in deal.buyer_candidates] == ["z"]
        with pytest.raises(ValueError):
            await add_buyer_candidate("DEAL-OUT-1", _cand("z", "z@x.de"))
        deal = await update_buyer_candidate("DEAL-OUT-1", "z", {"status": "declined", "reply_preview": "no"})
        assert deal.buyer_candidates[0].status == "declined"
        with pytest.raises(ValueError):
            await update_buyer_candidate("DEAL-OUT-1", "nope", {"status": "declined"})


# ---------------------------------------------------------------------------
# Catalogue / corridors
# ---------------------------------------------------------------------------

class TestCatalogue:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        from shared import catalogue
        catalogue.reset_for_tests()
        monkeypatch.setattr(catalogue, "_mongo_collection", lambda: None)
        yield
        catalogue.reset_for_tests()

    @pytest.mark.asyncio
    async def test_set_and_lookups(self):
        from shared import catalogue
        cat = await catalogue.set_catalogue([
            {"product_id": "pepper", "hs_code": "0904.11", "name": "black pepper",
             "products": ["black pepper", "piper nigrum"], "markets": ["de", "US", "ae "], "default_quantity_mt": 20},
            {"product_id": "rice", "hs_code": "100630", "name": "basmati rice", "markets": ["AE"], "active": False},
        ], updated_by="tester")
        assert cat.products[0].hs_code == "090411" and cat.products[0].markets == ["AE", "DE", "US"]
        assert catalogue.product_name_for("090411") == "black pepper"
        assert catalogue.products_for("0904") == ["black pepper", "piper nigrum"]
        assert catalogue.default_quantity_for("090411") == 20
        assert catalogue.scan_defaults() == (["090411"], ["AE", "DE", "US"])  # inactive rice excluded
        assert len(catalogue.corridors()) == 3
        with pytest.raises(ValueError):
            await catalogue.set_catalogue([{"product_id": "x", "hs_code": "12", "name": "bad"}])
        with pytest.raises(ValueError):
            await catalogue.set_catalogue([
                {"product_id": "dup", "hs_code": "090411", "name": "a"},
                {"product_id": "dup", "hs_code": "090412", "name": "b"},
            ])

    @pytest.mark.asyncio
    async def test_scan_pairs_and_keywords(self):
        from services.trade_min.config import DEFAULT_CONFIG
        from services.trade_min.schemas import ScanRequest
        from services.trade_min.service import _keyword_for, _products_for, _scan_pairs
        from shared import catalogue
        defaults = _scan_pairs(ScanRequest(), DEFAULT_CONFIG)
        assert len(defaults) == len(DEFAULT_CONFIG.default_hs_codes) * len(DEFAULT_CONFIG.default_markets)
        assert _keyword_for("090422") != "090422"  # static map still answers
        await catalogue.set_catalogue([
            {"product_id": "pepper", "hs_code": "090411", "name": "black pepper", "markets": ["DE", "US"]},
            {"product_id": "turmeric", "hs_code": "091030", "name": "turmeric", "products": ["turmeric powder"], "markets": ["AE"]},
        ])
        assert _scan_pairs(ScanRequest(), DEFAULT_CONFIG) == [("090411", "DE"), ("090411", "US"), ("091030", "AE")]
        assert _scan_pairs(ScanRequest(target_markets=["GB"]), DEFAULT_CONFIG) == [("090411", "GB"), ("091030", "GB")]
        assert _scan_pairs(ScanRequest(hs_codes=["100630"]), DEFAULT_CONFIG) == [("100630", "AE"), ("100630", "DE"), ("100630", "US")]
        assert _keyword_for("091030") == "turmeric" and _products_for("091030") == ["turmeric powder"]


# ---------------------------------------------------------------------------
# Apify: run budget + carrier tracking
# ---------------------------------------------------------------------------

class TestApifyBudgetAndTracking:
    def test_run_budget(self, monkeypatch):
        from shared.connectors import apify
        apify.reset_run_budget_for_tests()
        assert apify.run_budget_left() is True
        monkeypatch.setenv("APIFY_MAX_RUNS_PER_DAY", "2")
        apify._count_run(); apify._count_run()
        assert apify.runs_today() == 2 and apify.run_budget_left() is False
        assert apify.describe_actors()["runs_today"] == 2
        apify.reset_run_budget_for_tests()

    def test_carrier_milestone_normalisation(self):
        from shared.connectors.apify import carrier_milestone
        assert carrier_milestone("Delivered to consignee") == "DELIVERED"
        assert carrier_milestone("Customs cleared at Hamburg") == "CLEARED"
        assert carrier_milestone("Vessel arrival / discharged") == "ARRIVED"
        assert carrier_milestone("Loaded on vessel") == "DEPARTED"
        assert carrier_milestone("in_transit") == "IN_TRANSIT"
        assert carrier_milestone("booking confirmed") is None and carrier_milestone("") is None

    @pytest.mark.asyncio
    async def test_tracking_connector_stub_and_rows(self, monkeypatch):
        from shared.connectors.apify import ApifyCarrierTrackingConnector
        from shared.connectors.registry import get_connector
        conn = get_connector("apify_carrier_tracking")
        assert isinstance(conn, ApifyCarrierTrackingConnector) and conn.describe()["external"] is True
        stub = await conn.quote({"container_number": "MSKU1234567"})
        assert stub["stub"] is True and stub["shipments"] == []
        with pytest.raises(ValueError):
            await conn.quote({})
        rows = [{"containerNumber": "MSKU1234567", "carrier": "Maersk", "status": "Delivered", "location": "Hamburg",
                 "eventAt": "2026-10-20T10:00:00Z", "events": [{"status": "Discharged"}]}]
        monkeypatch.setattr(ApifyCarrierTrackingConnector, "configured", property(lambda self: True))
        monkeypatch.setattr(ApifyCarrierTrackingConnector, "run", AsyncMock(return_value=rows))
        out = await conn.quote({"container_number": "MSKU1234567", "carrier": "MAEU"})
        assert out["total"] == 1 and out["shipments"][0]["milestone"] == "DELIVERED"
        assert out["shipments"][0]["delivered"] is True and out["shipments"][0]["location"] == "Hamburg"


# ---------------------------------------------------------------------------
# Shield-link milestones + tracking poll
# ---------------------------------------------------------------------------

class TestMilestones:
    @pytest.fixture(autouse=True)
    def _repo(self):
        import services.shield_link.repository as repo_mod
        from services.shield_link.repository import InMemoryShieldLinkRepository
        repo_mod._repository = InMemoryShieldLinkRepository()
        repo_mod._motor_client = None
        yield
        repo_mod._repository = None

    async def _booked(self, deal_id="DEAL-ML-1"):
        from services.shield_link.schemas import FreightBookingRequest
        from services.shield_link.service import book_freight
        deal = SimpleNamespace(deal_id=deal_id, status=SimpleNamespace(value="QC_PASSED"), hs_code="090411")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            return await book_freight(FreightBookingRequest(deal_id=deal_id))

    @pytest.mark.asyncio
    async def test_delivered_records_and_clears_in_transit_deal(self):
        from services.shield_link.schemas import RecordMilestoneRequest
        from services.shield_link.service import record_milestone
        tpo = await self._booked()
        deal = SimpleNamespace(deal_id="DEAL-ML-1", status=SimpleNamespace(value="IN_TRANSIT"))
        emit, trans = AsyncMock(), AsyncMock()
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=trans),
            patch("services.shield_link.service._emit", new=emit),
        ):
            res = await record_milestone(tpo.tpo_id, RecordMilestoneRequest(milestone="DELIVERED", location="Hamburg"))
            stale = await record_milestone(tpo.ext_ref, RecordMilestoneRequest(milestone="ARRIVED"))
        assert res["status"] == "ok" and stale["status"] == "ignored"
        trans.assert_awaited_once_with("DEAL-ML-1", "CLEARED", f"Cargo DELIVERED — ref {tpo.ext_ref}")
        topics = [c.args[0] for c in emit.await_args_list]
        assert topics == [EventTopic.FREIGHT_MILESTONE, EventTopic.CUSTOMS_CLEARED]
        assert emit.await_args_list[0].args[2]["milestone"] == "DELIVERED" and emit.await_args_list[0].args[2]["source"] == "operator"
        from services.shield_link.repository import get_repository
        saved = await get_repository().get(tpo.tpo_id)
        assert saved.last_milestone == "DELIVERED" and saved.delivered_at is not None and saved.status == "COMPLETED"

    @pytest.mark.asyncio
    async def test_poll_tracking_applies_actor_milestone(self):
        from services.shield_link.service import poll_tracking
        tpo = await self._booked("DEAL-ML-2")
        assert (await poll_tracking("DEAL-NONE"))["status"] == "no_booking"
        assert (await poll_tracking("DEAL-ML-2"))["status"] == "stub"  # no APIFY_TOKEN in tests
        conn = SimpleNamespace(quote=AsyncMock(return_value={"shipments": [
            {"milestone": "ARRIVED", "location": "Rotterdam", "timestamp": "2026-10-21T08:00:00Z", "raw_status": "Discharged"},
        ]}))
        deal = SimpleNamespace(deal_id="DEAL-ML-2", status=SimpleNamespace(value="IN_TRANSIT"))
        with (
            patch("shared.connectors.registry.get_connector", return_value=conn),
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            first = await poll_tracking("DEAL-ML-2")
            second = await poll_tracking("DEAL-ML-2")
        assert first["status"] == "ok" and first["milestone"] == "ARRIVED" and first["tpo_id"] == tpo.tpo_id
        assert second["status"] == "unchanged"
        assert conn.quote.await_args.args[0]["booking_ref"] == tpo.ext_ref

    @pytest.mark.asyncio
    async def test_reinspection_booking_needs_dispatch_ready_and_skips_transition(self):
        from services.shield_link.schemas import QcBookingRequest
        from services.shield_link.service import InvalidDealState, book_qc
        trans, emit = AsyncMock(), AsyncMock()
        ready = SimpleNamespace(deal_id="DEAL-RI-1", status=SimpleNamespace(value="SUPPLIER_DISPATCH_READY"), hs_code="090411")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=ready)),
            patch("services.shield_link.service._transition_deal", new=trans),
            patch("services.shield_link.service._emit", new=emit),
            patch("services.shield_link.service._buyer_opted_qc_provider", new=AsyncMock(return_value=None)),
        ):
            with pytest.raises(InvalidDealState):
                await book_qc(QcBookingRequest(deal_id="DEAL-RI-1"))
            booked = await book_qc(QcBookingRequest(deal_id="DEAL-RI-1", idempotency_key="reinspect-1"), reinspection=True)
        assert booked.status == "BOOKED"
        trans.assert_not_awaited()
        assert emit.await_args.args[2]["reinspection"] is True


# ---------------------------------------------------------------------------
# Escrow: delivery window continuation + clear_dispute
# ---------------------------------------------------------------------------

class TestInspectionWindowContinuations:
    @pytest.mark.asyncio
    async def test_delivered_milestone_opens_window(self):
        from shared.orchestration import continuations as c
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=SimpleNamespace(escrow_id="ESC-1"))
        opened = AsyncMock()
        with (
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("services.escrow_guard.service.open_inspection_window", new=opened),
        ):
            await c.on_freight_milestone(DomainEvent(topic=EventTopic.FREIGHT_MILESTONE, deal_id="D1", payload={"milestone": "ARRIVED"}))
            opened.assert_not_awaited()
            await c.on_freight_milestone(DomainEvent(topic=EventTopic.FREIGHT_MILESTONE, deal_id="D1", payload={"milestone": "DELIVERED"}))
        opened.assert_awaited_once()
        assert opened.await_args.args[0] == "ESC-1" and "DELIVERED" in opened.await_args.kwargs["reason"]

    @pytest.mark.asyncio
    async def test_window_opened_notifies_buyer(self):
        from shared.orchestration import continuations as c
        hold = SimpleNamespace(escrow_id="ESC-1")
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        notify = AsyncMock(return_value="EMAIL-W")
        deal = SimpleNamespace(deal_id="D1")
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("shared.orchestration.notifications.notify_inspection_window_open", new=notify),
        ):
            await c.on_escrow_inspection_opened(DomainEvent(
                topic=EventTopic.ESCROW_INSPECTION_OPENED, deal_id="D1",
                payload={"final_amount": 5000.0, "deadline": "2026-11-01T00:00:00+00:00"},
            ))
        notify.assert_awaited_once_with(deal, hold, 5000.0, "2026-11-01T00:00:00+00:00")

    @pytest.mark.asyncio
    async def test_clear_dispute(self):
        import services.escrow_guard.repository as repo_mod
        from services.escrow_guard.repository import InMemoryEscrowGuardRepository
        from services.escrow_guard.schemas import CreateEscrowRequest, DisputeEscrowRequest
        from services.escrow_guard.service import clear_dispute, create_escrow, dispute_escrow
        repo_mod._repository = InMemoryEscrowGuardRepository()
        repo_mod._motor_client = None
        try:
            deal = SimpleNamespace(deal_id="DEAL-CD-1", status=SimpleNamespace(value="PI_ACCEPTED"))
            with (
                patch("services.escrow_guard.service._fetch_deal", new=AsyncMock(return_value=deal)),
                patch("services.escrow_guard.service._transition_deal", new=AsyncMock()),
                patch("services.escrow_guard.service._emit", new=AsyncMock()),
            ):
                esc = await create_escrow(CreateEscrowRequest(deal_id="DEAL-CD-1", provider="tazapay", amount=10000.0, currency="USD"))
                h = await repo_mod._repository.get_hold(esc.escrow_id)
                await repo_mod._repository.save_hold(h.model_copy(update={"status": "FUNDED"}))
                d = await dispute_escrow(esc.escrow_id, DisputeEscrowRequest(reason="damaged"))
                assert d.disputed is True
                cleared = await clear_dispute(esc.escrow_id, reason="re-inspection agreed")
                again = await clear_dispute(esc.escrow_id)
            assert cleared.disputed is False and again.disputed is False
        finally:
            repo_mod._repository = None


# ---------------------------------------------------------------------------
# Resolver playbooks
# ---------------------------------------------------------------------------

class TestResolverPlaybooks:
    def _record(self, source_event="", context=None, detail="x"):
        from services.exception_resolver.schemas import ExceptionRecord
        return ExceptionRecord(deal_id="DEAL-PB-1", source_event=source_event, source_service="t",
                               raw_detail=detail, context=context or {})

    @pytest.mark.asyncio
    async def test_qc_failed_reinspect_then_limit(self):
        from services.exception_resolver import agent
        with patch("shared.orchestration.playbooks.failed_inspections", new=AsyncMock(return_value=1)):
            analysis, meta = await agent._deterministic_analysis(self._record("QC_FAILED", {"category": "QC_FAILED"}))
        assert analysis.decision.value == "ESCALATE" and meta["playbook"] == "reinspect" and meta["attempt"] == 1
        with patch("shared.orchestration.playbooks.failed_inspections", new=AsyncMock(return_value=2)):
            analysis, meta = await agent._deterministic_analysis(self._record("QC_FAILED", {"category": "QC_FAILED"}))
        assert analysis.decision.value == "CANCEL" and meta["playbook"] is None

    @pytest.mark.asyncio
    async def test_payment_failed_variants(self):
        from services.exception_resolver import agent
        a, m = await agent._deterministic_analysis(self._record("ESCROW_PARTIALLY_FUNDED", {"shortfall": 1200.0}))
        assert a.category.value == "PAYMENT_FAILED" and a.decision.value == "AUTO_FIX"
        since_old = (NOW - timedelta(days=20)).isoformat()
        a, m = await agent._deterministic_analysis(self._record("ESCROW_PENDING:unfunded", {"since": since_old}))
        assert a.decision.value == "ESCALATE" and m["playbook"] == "cancel_unfunded"
        since_new = (NOW - timedelta(days=3)).isoformat()
        a, m = await agent._deterministic_analysis(self._record("PI_SENT:expired", {"since": since_new}))
        assert a.decision.value == "MONITOR"

    @pytest.mark.asyncio
    async def test_negotiation_failed_and_unknown(self):
        from services.exception_resolver import agent
        a, m = await agent._deterministic_analysis(self._record("negotiation.failed", {"category": "NEGOTIATION_FAILED"}))
        assert a.decision.value == "AUTO_FIX"
        a, m = await agent._deterministic_analysis(self._record("OUTREACH:exhausted", {"category": "NEGOTIATION_FAILED"}))
        assert a.decision.value == "ESCALATE" and m["playbook"] is None
        assert await agent._deterministic_analysis(self._record("something.odd", {})) is None

    @pytest.mark.asyncio
    async def test_escalation_carries_playbook(self):
        from services.exception_resolver import agent
        from services.exception_resolver.schemas import AgentAnalysis, ExceptionCategory, ExceptionSeverity, RemediationDecision
        analysis = AgentAnalysis(category=ExceptionCategory.QC_FAILED, severity=ExceptionSeverity.HIGH, summary="QC failed",
                                 root_cause="r", remediation_steps=["s"], decision=RemediationDecision.ESCALATE,
                                 decision_rationale="d", estimated_resolution_hours=1)
        gate = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-PB"))
        with patch("services.hitl_approval.service.create_approval", new=gate):
            res = await agent._escalate_to_hitl(self._record(), analysis, {"playbook": "reinspect", "attempt": 1, "reason": "off spec"})
        req = gate.await_args.args[0]
        assert res["playbook"] == "reinspect" and req.callback_payload["playbook"] == "reinspect"
        assert req.idempotency_key == "resolve-reinspect-DEAL-PB-1-1" and "playbook:reinspect" in req.risk_flags
        assert "APPROVE runs playbook" in req.summary

    @pytest.mark.asyncio
    async def test_raise_exception_uses_playbook_without_llm(self, monkeypatch):
        import services.exception_resolver.repository as repo_mod
        from services.exception_resolver.repository import InMemoryExceptionRepository
        from services.exception_resolver.schemas import RaiseExceptionRequest
        from services.exception_resolver.service import raise_exception
        repo_mod._repository = InMemoryExceptionRepository()
        repo_mod._motor_client = None
        try:
            gate = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-QC"))
            llm = AsyncMock(side_effect=AssertionError("LLM must not be called"))
            with (
                patch("services.exception_resolver.service._emit", new=AsyncMock()),
                patch("services.hitl_approval.service.create_approval", new=gate),
                patch("shared.orchestration.playbooks.failed_inspections", new=AsyncMock(return_value=1)),
                patch("services.exception_resolver.agent.LLMClient.chat", new=llm, create=True),
            ):
                res = await raise_exception(RaiseExceptionRequest(
                    deal_id="DEAL-PB-1", source_event="QC_FAILED", source_service="continuations",
                    detail="QC inspection failed: moisture 14%", context={"category": "QC_FAILED"},
                ))
            assert res.status.value == "ESCALATED" and res.approval_id == "APPR-QC"
            assert gate.await_args.args[0].callback_payload["playbook"] == "reinspect"
            llm.assert_not_awaited()
        finally:
            repo_mod._repository = None

    @pytest.mark.asyncio
    async def test_playbooks_disabled_falls_to_llm(self, monkeypatch):
        from services.exception_resolver import agent
        monkeypatch.setenv("EXCEPTION_PLAYBOOKS_ENABLED", "false")
        fake = AsyncMock(side_effect=agent.LLMExhaustedException("down"))
        with patch.object(agent.LLMClient, "chat", new=fake):
            out = await agent._node_analyze({"record": self._record("QC_FAILED", {"category": "QC_FAILED"}),
                                             "analysis": None, "execution_result": {}, "error": None, "playbook": None})
        fake.assert_awaited_once()
        assert out["analysis"].category.value == "UNKNOWN"


class TestPlaybookExecution:
    @pytest.mark.asyncio
    async def test_book_reinspection(self):
        from shared.orchestration import playbooks
        deal = SimpleNamespace(deal_id="DEAL-PB-2", status=SimpleNamespace(value="DISPUTE"), hs_code="090411", quantity_mt=20.0)
        upd = AsyncMock()
        hold = SimpleNamespace(escrow_id="ESC-9", disputed=True)
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        clear = AsyncMock()
        book = AsyncMock(return_value=SimpleNamespace(tpo_id="TPO-QC-2", ext_ref="SGS-2"))
        rework = AsyncMock(return_value="EMAIL-R")
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.deals.service.update_status", new=upd),
            patch("shared.orchestration.playbooks.failed_inspections", new=AsyncMock(return_value=1)),
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("services.escrow_guard.service.clear_dispute", new=clear),
            patch("services.shield_link.service.book_qc", new=book),
            patch("shared.orchestration.notifications.notify_qc_rework", new=rework),
        ):
            res = await playbooks.book_reinspection("DEAL-PB-2", reason="moisture too high")
        assert res["action"] == "reinspection_booked" and res["attempt"] == 1
        assert upd.await_args.args[1].target_status.value == "SUPPLIER_DISPATCH_READY"
        clear.assert_awaited_once_with("ESC-9", reason="re-inspection #1 agreed")
        assert book.await_args.kwargs["reinspection"] is True
        assert book.await_args.args[0].idempotency_key == "reinspect-DEAL-PB-2-1"
        rework.assert_awaited_once()
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
             patch("shared.orchestration.playbooks.failed_inspections", new=AsyncMock(return_value=2)):
            with pytest.raises(ValueError):
                await playbooks.book_reinspection("DEAL-PB-2")

    @pytest.mark.asyncio
    async def test_cancel_unfunded_refunds_partial_and_notifies(self):
        from shared.orchestration import playbooks
        pending = SimpleNamespace(deal_id="DEAL-PB-3", status=SimpleNamespace(value="ESCROW_PENDING"))
        cancelled = SimpleNamespace(deal_id="DEAL-PB-3", status=SimpleNamespace(value="CANCELLED"))
        get_deal = AsyncMock(side_effect=[pending, cancelled])
        upd = AsyncMock()
        hold = SimpleNamespace(escrow_id="ESC-3", status="PARTIALLY_FUNDED", funded_amount=4000.0, currency="EUR")
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        refund = AsyncMock(return_value=SimpleNamespace(refunded_total=4000.0))
        notify = AsyncMock(return_value=["E1", "E2"])
        with (
            patch("services.deals.service.get_deal", new=get_deal),
            patch("services.deals.service.update_status", new=upd),
            patch("services.escrow_guard.repository.get_repository", return_value=eg_repo),
            patch("services.escrow_guard.service.refund", new=refund),
            patch("shared.orchestration.notifications.notify_deal_cancelled", new=notify),
        ):
            res = await playbooks.cancel_unfunded_deal("DEAL-PB-3", reason="unfunded 20 days")
        assert res == {"action": "deal_cancelled", "refunded": 4000.0, "currency": "EUR", "notices": ["E1", "E2"]}
        assert upd.await_args.args[1].target_status.value == "CANCELLED"
        notify.assert_awaited_once_with(cancelled, "unfunded 20 days", 4000.0, "EUR")
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(deal_id="x", status=SimpleNamespace(value="PO_ISSUED")))):
            with pytest.raises(ValueError):
                await playbooks.cancel_unfunded_deal("DEAL-PB-3")

    @pytest.mark.asyncio
    async def test_resolve_exception_callback_dispatches_playbooks(self):
        from services.hitl_approval.callbacks import _cb_resolve_exception, _cb_send_buyer_outreach, _rcb_send_buyer_outreach
        cancel = AsyncMock(return_value={"action": "deal_cancelled"})
        reinspect = AsyncMock(return_value={"action": "reinspection_booked"})
        with (
            patch("shared.orchestration.playbooks.cancel_unfunded_deal", new=cancel),
            patch("shared.orchestration.playbooks.book_reinspection", new=reinspect),
        ):
            assert (await _cb_resolve_exception("D1", {"playbook": "cancel_unfunded", "reason": "r"}))["action"] == "deal_cancelled"
            assert (await _cb_resolve_exception("D1", {"playbook": "reinspect"}))["action"] == "reinspection_booked"
            assert (await _cb_resolve_exception("D1", {}))["action"] == "RESOLVE_EXCEPTION"
        cancel.assert_awaited_once_with("D1", reason="r")
        send = AsyncMock(return_value={"outcome": "sent"})
        skip = AsyncMock(return_value={"advanced": 1})
        with (
            patch("shared.orchestration.outreach.send_outreach", new=send),
            patch("shared.orchestration.outreach.skip_candidate", new=skip),
        ):
            await _cb_send_buyer_outreach("D1", {"candidate_id": "a", "to_address": "a@x.de", "subject": "S", "body": "B",
                                                 "modified_body": "Edited", "kind": "intro"})
            await _rcb_send_buyer_outreach("D1", {"candidate_id": "a"})
        assert send.await_args.args == ("D1", "a", "a@x.de", "S", "Edited")
        skip.assert_awaited_once()

    def test_new_transitions(self):
        from shared.domain.enums import DealStatus as DS
        from shared.domain.state_machine import can_transition
        assert can_transition(DS.DISPUTE, DS.SUPPLIER_DISPATCH_READY)
        assert can_transition(DS.ESCROW_PENDING, DS.CANCELLED) and can_transition(DS.PI_SENT, DS.CANCELLED)
        assert can_transition(DS.PI_ACCEPTED, DS.CANCELLED)
        assert not can_transition(DS.ESCROW_FUNDED, DS.CANCELLED)


# ---------------------------------------------------------------------------
# Deal watcher: tracking poll, window fallback, funnel-owned MATCHING
# ---------------------------------------------------------------------------

class TestWatcherBatchB:
    @pytest.mark.asyncio
    async def test_in_transit_polls_tracking_once_per_window(self, deals_repo, published, monkeypatch):
        from shared.orchestration import deal_watcher as dw
        dw.reset_for_tests()
        await deals_repo.save(_deal("DEAL-W1", DealStatus.IN_TRANSIT))
        poll = AsyncMock(return_value={"status": "ok", "milestone": "ARRIVED"})
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=None)
        monkeypatch.setattr("services.escrow_guard.repository.get_repository", lambda: eg_repo)
        with patch("services.shield_link.service.poll_tracking", new=poll):
            c1 = await dw.sweep_stalled_deals(NOW)
            c2 = await dw.sweep_stalled_deals(NOW + timedelta(hours=1))
            await dw.sweep_stalled_deals(NOW + timedelta(hours=13))
        assert c1["tracking_updates"] == 1 and "tracking_updates" not in c2
        assert poll.await_count == 2
        dw.reset_for_tests()

    @pytest.mark.asyncio
    async def test_cleared_fallback_opens_window(self, deals_repo, published, monkeypatch):
        from shared.orchestration import deal_watcher as dw
        dw.reset_for_tests()
        old = NOW - timedelta(days=22)
        deal = _deal("DEAL-W2", DealStatus.CLEARED)
        await deals_repo.save(deal.model_copy(update={"updated_at": old}))
        hold = SimpleNamespace(escrow_id="ESC-W2", status="FUNDED", inspection_deadline=None,
                               tranches=[SimpleNamespace(trigger="ACCEPTED", status="PENDING")])
        eg_repo = AsyncMock()
        eg_repo.get_hold_by_deal = AsyncMock(return_value=hold)
        monkeypatch.setattr("services.escrow_guard.repository.get_repository", lambda: eg_repo)
        opened = AsyncMock()
        with patch("services.escrow_guard.service.open_inspection_window", new=opened):
            counts = await dw.sweep_stalled_deals(NOW)
        assert counts["window_fallbacks"] == 1 and counts["exceptions"] == 0
        assert opened.await_args.args[0] == "ESC-W2" and opened.await_args.kwargs["trigger"] == "fallback"
        # window already running → nothing, no stall exception either
        hold.inspection_deadline = NOW + timedelta(days=1)
        opened.reset_mock()
        counts = await dw.sweep_stalled_deals(NOW)
        opened.assert_not_awaited()
        assert counts["exceptions"] == 0
        dw.reset_for_tests()

    @pytest.mark.asyncio
    async def test_matching_with_open_funnel_is_not_a_stall(self, deals_repo, published):
        from shared.orchestration import deal_watcher as dw
        dw.reset_for_tests()
        old = NOW - timedelta(days=10)
        funnel = _deal("DEAL-W3", DealStatus.MATCHING, candidates=[_cand("a", "a@x.de", status="contacted", contacted_at=NOW)])
        bare = _deal("DEAL-W4", DealStatus.MATCHING)
        await deals_repo.save(funnel.model_copy(update={"updated_at": old}))
        await deals_repo.save(bare.model_copy(update={"updated_at": old}))
        counts = await dw.sweep_stalled_deals(NOW)
        assert counts["exceptions"] == 1
        assert published[0].deal_id == "DEAL-W4"
        dw.reset_for_tests()
