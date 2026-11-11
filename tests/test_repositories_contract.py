"""Every service repository: in-memory and Mongo backends obey the same contract.

Mongo runs on mongomock, so nothing here touches a live database.
"""
from __future__ import annotations

from datetime import timedelta
from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from agents.negotiation_agent import repository as negotiation
from agents.negotiation_agent.schemas import NegotiationRound, NegotiationSession
from services.auth import repository as auth
from services.auth.schemas import OrganizationRecord, UserRecord
from services.dealmaker import repository as dealmaker
from services.dealmaker.schemas import PiRecord, PoRecord
from services.deals import repository as deals
from services.deals.schemas import DealRecord, DealStatus, StatusHistoryEntry
from services.docugen import repository as docugen
from services.docugen.schemas import DocumentRecord
from services.email_processor import repository as email_repo
from services.email_processor.schemas import InboundEmailRecord, InboundEmailStatus
from services.exception_resolver import repository as exceptions
from services.exception_resolver.schemas import ExceptionRecord, ExceptionStatus
from services.hitl_approval import repository as hitl
from services.hitl_approval.schemas import ApprovalRecord
from services.shield_link import repository as shield
from services.shield_link.schemas import ThirdPartyOrderRecord
from services.sourcing import repository as sourcing
from services.sourcing.schemas import QuoteRecord, RfqRecord
from services.trade_min import repository as trade
from services.trade_min.schemas import EnrichedOpportunity
from shared.domain.enums import DocumentType
from tests.factories import NOW, make

BACKENDS = ["memory", "mongo"]
EARLIER = NOW - timedelta(days=1)


@pytest.fixture
def db():
    return AsyncMongoMockClient()["portless_test"]


@pytest.fixture
def _mock_motor():
    with patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=lambda *_a, **_k: AsyncMongoMockClient()):
        yield


class TestUsers:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return auth.InMemoryUserRepository() if request.param == "memory" else auth.MongoUserRepository(db["users"])

    async def test_email_lookup_ignores_case_and_spaces(self, repo):
        await repo.create(make(UserRecord, user_id="U1", email="Ops@Acme.com"))
        assert (await repo.get_by_email(" OPS@acme.COM ")).user_id == "U1"

    async def test_get_by_id(self, repo):
        await repo.create(make(UserRecord, user_id="U1", email="a@b.c"))
        assert (await repo.get_by_id("U1")).email.lower() == "a@b.c"

    async def test_missing_user(self, repo):
        assert await repo.get_by_id("nope") is None
        assert await repo.get_by_email("nope@x.y") is None
        assert await repo.update_org("nope", "O1") is None

    async def test_oldest_first(self, repo):
        await repo.create(make(UserRecord, user_id="U2", email="b@b.c", created_at=NOW))
        await repo.create(make(UserRecord, user_id="U1", email="a@b.c", created_at=EARLIER))
        assert [u.user_id for u in await repo.list_all()] == ["U1", "U2"]

    async def test_update_org(self, repo):
        await repo.create(make(UserRecord, user_id="U1", email="a@b.c"))
        assert (await repo.update_org("U1", "O9")).org_id == "O9"
        assert (await repo.get_by_id("U1")).org_id == "O9"

    async def test_malformed_mongo_user_is_skipped(self, db):
        repo = auth.MongoUserRepository(db["users"])
        await db["users"].insert_one({"user_id": "broken"})
        assert await repo.list_all() == []


class TestOrganizations:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return auth.InMemoryOrganizationRepository() if request.param == "memory" else auth.MongoOrganizationRepository(db["orgs"])

    async def test_get(self, repo):
        await repo.create(make(OrganizationRecord, org_id="O1"))
        assert (await repo.get("O1")).org_id == "O1"
        assert await repo.get("nope") is None

    async def test_name_lookup_is_case_insensitive(self, repo):
        await repo.create(make(OrganizationRecord, org_id="O1", name="Acme Oils"))
        assert (await repo.get_by_name(" acme OILS ")).org_id == "O1"
        assert await repo.get_by_name("Acme") is None

    async def test_name_is_not_a_pattern(self, repo):
        await repo.create(make(OrganizationRecord, org_id="O1", name="Acme"))
        assert await repo.get_by_name("A.me") is None

    async def test_oldest_first(self, repo):
        await repo.create(make(OrganizationRecord, org_id="O2", created_at=NOW))
        await repo.create(make(OrganizationRecord, org_id="O1", created_at=EARLIER))
        assert [o.org_id for o in await repo.list_all()] == ["O1", "O2"]

    async def test_known_email_is_normalised_and_unique(self, repo):
        await repo.create(make(OrganizationRecord, org_id="O1"))
        await repo.add_known_email("O1", " A@B.c ")
        assert (await repo.add_known_email("O1", "a@b.c")).known_emails == ["a@b.c"]

    async def test_known_email_on_missing_org(self, repo):
        assert await repo.add_known_email("nope", "a@b.c") is None

    async def test_malformed_mongo_org_is_skipped(self, db):
        await db["orgs"].insert_one({"org_id": "broken"})
        assert await auth.MongoOrganizationRepository(db["orgs"]).list_all() == []


class TestDealMaker:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        if request.param == "memory":
            return dealmaker.InMemoryDealMakerRepository()
        return dealmaker.MongoDealMakerRepository(db["pi"], db["po"])

    @staticmethod
    def _pi(**kw):
        return make(PiRecord, **{"pi_id": "PI1", "deal_id": "D1", "idempotency_key": "k1", **kw})

    @staticmethod
    def _po(**kw):
        return make(PoRecord, **{"po_id": "PO1", "deal_id": "D1", "idempotency_key": "k1", **kw})

    async def test_pi_lookups(self, repo):
        await repo.save_pi(self._pi())
        assert (await repo.get_pi("PI1")).deal_id == "D1"
        assert (await repo.get_pi_by_deal("D1")).pi_id == "PI1"
        assert (await repo.get_pi_by_idempotency_key("k1")).pi_id == "PI1"

    async def test_pi_missing(self, repo):
        assert await repo.get_pi("x") is None
        assert await repo.get_pi_by_deal("x") is None
        assert await repo.get_pi_by_idempotency_key("x") is None

    async def test_superseded_pi_is_not_the_deals_pi(self, repo):
        await repo.save_pi(self._pi(status="SUPERSEDED"))
        assert await repo.get_pi_by_deal("D1") is None

    async def test_save_replaces_pi(self, repo):
        await repo.save_pi(self._pi(total_value=1.0))
        await repo.save_pi(self._pi(total_value=2.0))
        assert (await repo.get_pi("PI1")).total_value == 2.0

    async def test_po_lookups(self, repo):
        await repo.save_po(self._po())
        assert (await repo.get_po("PO1")).deal_id == "D1"
        assert (await repo.get_po_by_deal("D1")).po_id == "PO1"
        assert (await repo.get_po_by_idempotency_key("k1")).po_id == "PO1"

    async def test_po_missing(self, repo):
        assert await repo.get_po("x") is None
        assert await repo.get_po_by_deal("x") is None
        assert await repo.get_po_by_idempotency_key("x") is None

    async def test_cancelled_po_is_not_the_deals_po(self, repo):
        await repo.save_po(self._po(status="CANCELLED"))
        assert await repo.get_po_by_deal("D1") is None

    async def test_other_deal_is_not_returned(self, repo):
        await repo.save_pi(self._pi())
        await repo.save_po(self._po())
        assert await repo.get_pi_by_deal("D2") is None
        assert await repo.get_po_by_deal("D2") is None


class TestSourcing:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return sourcing.InMemorySourcingRepository() if request.param == "memory" else sourcing.MongoSourcingRepository(db["r"], db["q"])

    @staticmethod
    def _rfq(**kw):
        return make(RfqRecord, **{"rfq_id": "R1", "deal_id": "D1", **kw})

    @staticmethod
    def _quote(**kw):
        return make(QuoteRecord, **{"quote_id": "Q1", "rfq_id": "R1", "deal_id": "D1", **kw})

    async def test_rfq(self, repo):
        await repo.save_rfq(self._rfq())
        assert (await repo.get_rfq("R1")).deal_id == "D1"
        assert await repo.get_rfq("x") is None

    async def test_rfqs_are_scoped_to_the_deal(self, repo):
        await repo.save_rfq(self._rfq())
        await repo.save_rfq(self._rfq(rfq_id="R2", deal_id="D2"))
        assert [r.rfq_id for r in await repo.list_rfqs("D1")] == ["R1"]

    async def test_quote(self, repo):
        await repo.save_quote(self._quote())
        assert (await repo.get_quote("Q1")).rfq_id == "R1"
        assert await repo.get_quote("x") is None

    async def test_quote_filters_and_newest_first(self, repo):
        await repo.save_quote(self._quote(received_at=EARLIER))
        await repo.save_quote(self._quote(quote_id="Q2", received_at=NOW))
        await repo.save_quote(self._quote(quote_id="Q3", rfq_id="R2", deal_id="D2", received_at=NOW))
        assert [q.quote_id for q in await repo.list_quotes(deal_id="D1")] == ["Q2", "Q1"]
        assert [q.quote_id for q in await repo.list_quotes(rfq_id="R2")] == ["Q3"]
        assert len(await repo.list_quotes()) == 3

    async def test_malformed_mongo_documents_are_skipped(self, db):
        repo = sourcing.MongoSourcingRepository(db["r"], db["q"])
        await db["r"].insert_one({"deal_id": "D1"})
        await db["q"].insert_one({"deal_id": "D1"})
        assert await repo.list_rfqs("D1") == []
        assert await repo.list_quotes("D1") == []


class TestHitl:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return hitl.InMemoryHitlApprovalRepository() if request.param == "memory" else hitl.MongoHitlApprovalRepository(db["a"])

    @staticmethod
    def _approval(**kw):
        return make(ApprovalRecord, **{"approval_id": "A1", "deal_id": "D1", "status": "PENDING", "idempotency_key": "k1", **kw})

    async def test_lookups(self, repo):
        await repo.save(self._approval())
        assert (await repo.get("A1")).deal_id == "D1"
        assert (await repo.get_by_idempotency_key("k1")).approval_id == "A1"
        assert await repo.get("x") is None
        assert await repo.get_by_idempotency_key("x") is None

    async def test_status_filter(self, repo):
        await repo.save(self._approval())
        await repo.save(self._approval(approval_id="A2", status="APPROVED", idempotency_key="k2"))
        assert [a.approval_id for a in await repo.list_by_status("APPROVED")] == ["A2"]
        assert len(await repo.list_by_status(None)) == 2

    async def test_deal_filter(self, repo):
        await repo.save(self._approval())
        await repo.save(self._approval(approval_id="A2", deal_id="D2", idempotency_key="k2"))
        assert [a.approval_id for a in await repo.list_by_deal("D1")] == ["A1"]

    async def test_only_pending_and_expired(self, repo):
        await repo.save(self._approval(approval_id="old", expires_at=EARLIER, idempotency_key="1"))
        await repo.save(self._approval(approval_id="future", expires_at=NOW + timedelta(days=1), idempotency_key="2"))
        await repo.save(self._approval(approval_id="never", expires_at=None, idempotency_key="3"))
        await repo.save(self._approval(approval_id="done", status="APPROVED", expires_at=EARLIER, idempotency_key="4"))
        assert [a.approval_id for a in await repo.list_pending_expired(NOW.isoformat())] == ["old"]

    async def test_naive_expiry_is_treated_as_utc(self):
        repo = hitl.InMemoryHitlApprovalRepository()
        await repo.save(self._approval(expires_at=EARLIER.replace(tzinfo=None)))
        assert len(await repo.list_pending_expired(NOW.isoformat())) == 1


class TestShieldLink:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return shield.InMemoryShieldLinkRepository() if request.param == "memory" else shield.MongoShieldLinkRepository(db["t"])

    @staticmethod
    def _order(**kw):
        return make(ThirdPartyOrderRecord, **{"tpo_id": "T1", "deal_id": "D1", "order_type": "QC", "ext_ref": "e1", "idempotency_key": "k1", **kw})

    async def test_lookups(self, repo):
        await repo.save(self._order())
        assert (await repo.get("T1")).deal_id == "D1"
        assert (await repo.get_by_ext_ref("e1")).tpo_id == "T1"
        assert (await repo.get_by_idempotency_key("k1")).tpo_id == "T1"
        assert (await repo.get_by_deal_and_type("D1", "QC")).tpo_id == "T1"

    async def test_missing(self, repo):
        assert await repo.get("x") is None
        assert await repo.get_by_ext_ref("x") is None
        assert await repo.get_by_idempotency_key("x") is None
        assert await repo.get_by_deal_and_type("D1", "QC") is None

    async def test_failed_order_does_not_count(self, repo):
        await repo.save(self._order(status="FAILED"))
        assert await repo.get_by_deal_and_type("D1", "QC") is None

    async def test_type_and_deal_are_respected(self, repo):
        await repo.save(self._order())
        assert await repo.get_by_deal_and_type("D1", "FREIGHT") is None
        assert await repo.get_by_deal_and_type("D2", "QC") is None

    async def test_list_by_deal(self, repo):
        await repo.save(self._order())
        await repo.save(self._order(tpo_id="T2", deal_id="D2", ext_ref="e2", idempotency_key="k2"))
        assert [t.tpo_id for t in await repo.list_by_deal("D1")] == ["T1"]


class TestDeals:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return deals.InMemoryFallbackRepository() if request.param == "memory" else deals.MongoDealRepository(db["d"])

    @staticmethod
    def _deal(**kw):
        return make(DealRecord, **{"deal_id": "D1", "operator_id": "op", "org_id": "own", **kw})

    async def test_save_get(self, repo):
        await repo.save(self._deal())
        assert (await repo.get("D1")).operator_id == "op"
        assert await repo.get("x") is None

    async def test_newest_first_with_limit(self, repo):
        await repo.save(self._deal(deal_id="old", created_at=EARLIER))
        await repo.save(self._deal(deal_id="new", created_at=NOW))
        assert [d.deal_id for d in await repo.list_active()] == ["new", "old"]
        assert [d.deal_id for d in await repo.list_active(limit=1)] == ["new"]

    async def test_status_and_operator_filters(self, repo):
        await repo.save(self._deal(status=DealStatus.MATCHING))
        await repo.save(self._deal(deal_id="D2", status=DealStatus.PAID, operator_id="other"))
        assert [d.deal_id for d in await repo.list_active(status=DealStatus.PAID)] == ["D2"]
        assert [d.deal_id for d in await repo.list_active(operator_id="op")] == ["D1"]

    @pytest.mark.parametrize("org", ["own", "buy", "sell"])
    async def test_every_party_org_sees_the_deal(self, repo, org):
        await repo.save(self._deal(buyer_org_id="buy", seller_org_id="sell"))
        assert len(await repo.list_active(org_id=org)) == 1

    async def test_stranger_org_sees_nothing(self, repo):
        await repo.save(self._deal(buyer_org_id="buy", seller_org_id="sell"))
        assert await repo.list_active(org_id="stranger") == []

    async def test_mark_status_appends_history(self, repo):
        await repo.save(self._deal())
        entry = make(StatusHistoryEntry, status=DealStatus.PAID, transitioned_at=NOW)
        assert await repo.mark_status("D1", entry) is True
        deal = await repo.get("D1")
        assert deal.status == DealStatus.PAID and deal.status_history[-1].status == DealStatus.PAID

    async def test_mark_status_on_missing_deal(self, repo):
        assert await repo.mark_status("x", make(StatusHistoryEntry, status=DealStatus.PAID, transitioned_at=NOW)) is False

    async def test_set_fields_touches_only_those_fields(self, repo):
        from shared.domain.enums import QcStatus
        await repo.save(self._deal(status=DealStatus.SUPPLIER_DISPATCH_READY))
        # a concurrent transition lands first; set_fields must not undo it
        await repo.mark_status("D1", make(StatusHistoryEntry, status=DealStatus.QC_FAILED, transitioned_at=NOW))
        assert await repo.set_fields("D1", {"qc_status": QcStatus.FAILED, "qc_status_detail": "QIMA FAIL",
                                            "qc_status_at": NOW}) is True
        deal = await repo.get("D1")
        assert deal.status == DealStatus.QC_FAILED and deal.qc_status == QcStatus.FAILED
        assert deal.qc_status_detail == "QIMA FAIL" and deal.status_history[-1].status == DealStatus.QC_FAILED
        assert await repo.set_fields("missing", {"qc_status": None}) is False

    async def test_malformed_mongo_deal_is_skipped(self, db):
        await db["d"].insert_one({"deal_id": "broken", "created_at": "x"})
        assert await deals.MongoDealRepository(db["d"]).list_active() == []


class TestDocuGen:
    TYPE = next(iter(DocumentType))

    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return docugen.InMemoryDocuGenRepository() if request.param == "memory" else docugen.MongoDocuGenRepository(db["x"])

    def _doc(self, **kw):
        return make(DocumentRecord, **{"doc_id": "X1", "deal_id": "D1", "doc_type": self.TYPE, "idempotency_key": "k1", **kw})

    async def test_lookups(self, repo):
        await repo.save(self._doc())
        assert (await repo.get("X1")).deal_id == "D1"
        assert (await repo.get_by_idempotency_key("k1")).doc_id == "X1"
        assert (await repo.get_by_deal_and_type("D1", self.TYPE)).doc_id == "X1"

    async def test_missing(self, repo):
        assert await repo.get("x") is None
        assert await repo.get_by_idempotency_key("x") is None
        assert await repo.get_by_deal_and_type("D1", self.TYPE) is None

    async def test_failed_document_does_not_count(self, repo):
        await repo.save(self._doc(status="FAILED"))
        assert await repo.get_by_deal_and_type("D1", self.TYPE) is None

    async def test_list_by_deal(self, repo):
        await repo.save(self._doc())
        await repo.save(self._doc(doc_id="X2", deal_id="D2", idempotency_key="k2"))
        assert [d.doc_id for d in await repo.list_by_deal("D1")] == ["X1"]


class TestExceptions:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return exceptions.InMemoryExceptionRepository() if request.param == "memory" else exceptions.MongoExceptionRepository(db["e"])

    @staticmethod
    def _record(**kw):
        return make(ExceptionRecord, **{"exception_id": "E1", "deal_id": "D1", "raised_at": NOW, **kw})

    async def test_save_get(self, repo):
        await repo.save(self._record())
        assert (await repo.get("E1")).deal_id == "D1"
        assert await repo.get("x") is None

    async def test_by_deal(self, repo):
        await repo.save(self._record())
        await repo.save(self._record(exception_id="E2", deal_id="D2"))
        assert [e.exception_id for e in await repo.list_by_deal("D1")] == ["E1"]

    async def test_by_status(self, repo):
        await repo.save(self._record(status=ExceptionStatus.OPEN))
        await repo.save(self._record(exception_id="E2", status=ExceptionStatus.RESOLVED))
        assert [e.exception_id for e in await repo.list_by_status(ExceptionStatus.RESOLVED)] == ["E2"]

    async def test_list_all_newest_first_with_limit(self, repo):
        await repo.save(self._record(exception_id="old", raised_at=EARLIER))
        await repo.save(self._record(exception_id="new", raised_at=NOW))
        assert [e.exception_id for e in await repo.list_all()] == ["new", "old"]
        assert [e.exception_id for e in await repo.list_all(limit=1)] == ["new"]


class TestOpportunities:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, db):
        return trade.InMemoryFallbackRepository() if request.param == "memory" else trade.MongoOpportunityRepository(db["o"])

    @staticmethod
    def _opp(**kw):
        return make(EnrichedOpportunity, **{"opportunity_id": "O1", "hs_code": "1520", "market": "ES", "status": "ACTIVE", "arbitrage_rank": 5.0, **kw})

    async def test_save_get(self, repo):
        await repo.save(self._opp())
        assert (await repo.get("O1")).hs_code == "1520"
        assert await repo.get("x") is None

    async def test_id_is_required(self, repo):
        with pytest.raises(ValueError, match="opportunity_id"):
            await repo.save(self._opp(opportunity_id=""))

    async def test_active_respects_status_rank_and_limit(self, repo):
        await repo.save(self._opp())
        await repo.save(self._opp(opportunity_id="O2", status="MARGINAL", arbitrage_rank=9.0))
        await repo.save(self._opp(opportunity_id="O3", status="CONVERTED"))
        await repo.save(self._opp(opportunity_id="O4", arbitrage_rank=1.0))
        assert {o.opportunity_id for o in await repo.list_active(100, 2.0)} == {"O1", "O2"}
        assert len(await repo.list_active(1, 0.0)) == 1

    async def test_hs_market_lookup(self, repo):
        await repo.save(self._opp())
        await repo.save(self._opp(opportunity_id="O2", market="DE"))
        assert [o.opportunity_id for o in await repo.list_by_hs_market("1520", "ES")] == ["O1"]

    async def test_mark_converted(self, repo):
        await repo.save(self._opp())
        assert await repo.mark_converted("O1") is True
        assert (await repo.get("O1")).status == "CONVERTED"
        assert await repo.mark_converted("x") is False

    async def test_clear(self, repo):
        await repo.save(self._opp())
        assert await repo.clear() == 1
        assert await repo.get("O1") is None

    async def test_expired_opportunity_is_not_active_in_memory(self):
        repo = trade.InMemoryFallbackRepository()
        await repo.save(self._opp(expires_at=EARLIER))
        assert await repo.list_active(10, 0.0) == []

    async def test_malformed_mongo_documents_are_skipped(self, db):
        repo = trade.MongoOpportunityRepository(db["o"])
        await db["o"].insert_one({"status": "ACTIVE", "arbitrage_rank": 9.0, "hs_code": "1520", "market": "ES"})
        assert await repo.list_active(10, 0.0) == []
        assert await repo.list_by_hs_market("1520", "ES") == []


class TestEmailProcessor:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, _mock_motor):
        return email_repo.InMemoryEmailProcessorRepository() if request.param == "memory" else email_repo.MongoEmailProcessorRepository("mongodb://x", "d", "c")

    @staticmethod
    def _mail(**kw):
        return make(InboundEmailRecord, **{"inbound_id": "I1", "message_id": "m1", "negotiation_id": "N1", **kw})

    async def test_lookups(self, repo):
        await repo.save(self._mail())
        assert (await repo.get("I1")).message_id == "m1"
        assert (await repo.find_by_message_id("m1")).inbound_id == "I1"
        assert await repo.get("x") is None
        assert await repo.find_by_message_id("x") is None

    async def test_filters_and_limit(self, repo):
        statuses = list(InboundEmailStatus)
        await repo.save(self._mail(status=statuses[0]))
        await repo.save(self._mail(inbound_id="I2", message_id="m2", negotiation_id="N2", status=statuses[1]))
        assert [m.inbound_id for m in await repo.list_all(status=statuses[1])] == ["I2"]
        assert [m.inbound_id for m in await repo.list_all(negotiation_id="N1")] == ["I1"]
        assert len(await repo.list_all(limit=1)) == 1

    async def test_indexes(self, _mock_motor):
        await email_repo.MongoEmailProcessorRepository("mongodb://x", "d", "c")._ensure_indexes()


class TestNegotiation:
    @pytest.fixture(params=BACKENDS)
    def repo(self, request, _mock_motor):
        return negotiation.InMemoryNegotiationRepository() if request.param == "memory" else negotiation.MongoNegotiationRepository("mongodb://x", "d", "s", "r")

    @staticmethod
    def _session(**kw):
        return make(NegotiationSession, **{"negotiation_id": "N1", "deal_id": "D1", **kw})

    @staticmethod
    def _round(**kw):
        return make(NegotiationRound, **{"round_id": "R1", "negotiation_id": "N1", "deal_id": "D1", "round_no": 1, **kw})

    async def test_session_lookups(self, repo):
        await repo.save_session(self._session())
        assert (await repo.get_session("N1")).deal_id == "D1"
        assert (await repo.get_session_by_deal("D1")).negotiation_id == "N1"
        assert await repo.get_session("x") is None
        assert await repo.get_session_by_deal("x") is None

    async def test_list_sessions(self, repo):
        await repo.save_session(self._session())
        await repo.save_session(self._session(negotiation_id="N2", deal_id="D2"))
        assert [s.negotiation_id for s in await repo.list_sessions("D2")] == ["N2"]
        assert len(await repo.list_sessions()) == 2

    async def test_list_by_status(self, repo):
        session = self._session()
        await repo.save_session(session)
        assert len(await repo.list_sessions_by_status([session.status])) == 1
        assert await repo.list_sessions_by_status(["__none__"]) == []

    async def test_rounds_are_scoped_and_ordered(self, repo):
        await repo.save_round(self._round())
        await repo.save_round(self._round(round_id="R2", round_no=2))
        await repo.save_round(self._round(round_id="R3", negotiation_id="N2"))
        assert [r.round_no for r in await repo.list_rounds("N1")] == [1, 2]

    async def test_a_deal_can_only_be_claimed_once(self, repo):
        assert await repo.claim_deal("D1", "N1") is True
        assert await repo.claim_deal("D1", "N1") is True, "the owner may claim again"
        assert await repo.claim_deal("D1", "N2") is False

    async def test_indexes(self, _mock_motor):
        await negotiation.MongoNegotiationRepository("mongodb://x", "d", "s", "r")._ensure_indexes()


# ---------------------------------------------------------------------------
# Lifecycle: which backend is selected, fallback, close
# ---------------------------------------------------------------------------

def _motor(ping=None):
    client = MagicMock()
    client.admin.command = AsyncMock(side_effect=ping)
    client.__getitem__.return_value = AsyncMongoMockClient()["portless_test"]
    return client


LIFECYCLE = [
    ("services.dealmaker.repository", "dealmaker_mongo_uri", "InMemoryDealMakerRepository", "MongoDealMakerRepository"),
    ("services.sourcing.repository", "sourcing_mongo_uri", "InMemorySourcingRepository", "MongoSourcingRepository"),
    ("services.hitl_approval.repository", "hitl_approval_mongo_uri", "InMemoryHitlApprovalRepository", "MongoHitlApprovalRepository"),
    ("services.shield_link.repository", "shield_link_mongo_uri", "InMemoryShieldLinkRepository", "MongoShieldLinkRepository"),
    ("services.deals.repository", "deals_mongo_uri", "InMemoryFallbackRepository", "MongoDealRepository"),
    ("services.docugen.repository", "docugen_mongo_uri", "InMemoryDocuGenRepository", "MongoDocuGenRepository"),
    ("services.exception_resolver.repository", "exception_resolver_mongo_uri", "InMemoryExceptionRepository", "MongoExceptionRepository"),
]


@pytest.mark.parametrize(("path", "attr", "memory", "mongo"), LIFECYCLE)
class TestLifecycle:
    @pytest.fixture
    def module(self, path):
        module = import_module(path)
        saved = module._repository, module._motor_client
        module._repository, module._motor_client = None, None
        yield module
        module._repository, module._motor_client = saved

    async def test_not_initialised(self, module, attr, memory, mongo):
        with pytest.raises(RuntimeError, match="not initialised"):
            module.get_repository()

    async def test_no_uri_uses_memory(self, module, attr, memory, mongo, monkeypatch):
        monkeypatch.setattr(module.settings, attr, "")
        await module.init_repository()
        assert isinstance(module.get_repository(), getattr(module, memory))

    async def test_uri_uses_mongo_and_closes(self, module, attr, memory, mongo, monkeypatch):
        monkeypatch.setattr(module.settings, attr, "mongodb://user:pw@host/db")
        client = _motor()
        with patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=client):
            await module.init_repository()
        assert isinstance(module.get_repository(), getattr(module, mongo))
        await module.close_repository()
        client.close.assert_called_once()
        assert module._motor_client is None

    async def test_unreachable_mongo_falls_back(self, module, attr, memory, mongo, monkeypatch):
        monkeypatch.setattr(module.settings, attr, "mongodb://host/db")
        with patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=_motor(ping=RuntimeError("down"))):
            await module.init_repository()
        assert isinstance(module.get_repository(), getattr(module, memory))

    async def test_close_without_client_is_safe(self, module, attr, memory, mongo):
        await module.close_repository()


class TestAuthLifecycle:
    @pytest.fixture(autouse=True)
    def _reset(self):
        saved = auth._user_repository, auth._org_repository, auth._motor_client
        auth._user_repository, auth._org_repository, auth._motor_client = None, None, None
        yield
        auth._user_repository, auth._org_repository, auth._motor_client = saved

    def test_not_initialised(self):
        with pytest.raises(RuntimeError):
            auth.get_repository()
        with pytest.raises(RuntimeError):
            auth.get_org_repository()

    async def test_no_uri(self, monkeypatch):
        monkeypatch.setattr(auth, "_MONGO_URI", "")
        await auth.init_repository()
        assert isinstance(auth.get_repository(), auth.InMemoryUserRepository)
        assert isinstance(auth.get_org_repository(), auth.InMemoryOrganizationRepository)

    async def test_mongo_and_close(self, monkeypatch):
        monkeypatch.setattr(auth, "_MONGO_URI", "mongodb://host/db")
        client = _motor()
        with patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=client):
            await auth.init_repository()
        assert isinstance(auth.get_repository(), auth.MongoUserRepository)
        assert isinstance(auth.get_org_repository(), auth.MongoOrganizationRepository)
        await auth.close_repository()
        client.close.assert_called_once()

    async def test_unreachable_falls_back(self, monkeypatch):
        monkeypatch.setattr(auth, "_MONGO_URI", "mongodb://host/db")
        with patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=_motor(ping=RuntimeError("down"))):
            await auth.init_repository()
        assert isinstance(auth.get_repository(), auth.InMemoryUserRepository)

    async def test_close_without_client(self):
        await auth.close_repository()


class TestEmailProcessorLifecycle:
    @pytest.fixture(autouse=True)
    def _reset(self):
        saved = email_repo._repository
        email_repo._repository = None
        yield
        email_repo._repository = saved

    def test_not_initialised(self):
        with pytest.raises(RuntimeError):
            email_repo.get_repository()

    async def test_no_uri(self, monkeypatch):
        monkeypatch.setattr(email_repo.settings, "email_processor_mongo_uri", "")
        await email_repo.init_repository()
        assert isinstance(email_repo.get_repository(), email_repo.InMemoryEmailProcessorRepository)

    async def test_mongo_then_close(self, monkeypatch, _mock_motor):
        monkeypatch.setattr(email_repo.settings, "email_processor_mongo_uri", "mongodb://x")
        await email_repo.init_repository()
        assert isinstance(email_repo.get_repository(), email_repo.MongoEmailProcessorRepository)
        await email_repo.close_repository()
        assert email_repo._repository is None

    async def test_index_failure_falls_back(self, monkeypatch, _mock_motor):
        monkeypatch.setattr(email_repo.settings, "email_processor_mongo_uri", "mongodb://x")
        with patch.object(email_repo.MongoEmailProcessorRepository, "_ensure_indexes", new=AsyncMock(side_effect=RuntimeError("no"))):
            await email_repo.init_repository()
        assert isinstance(email_repo.get_repository(), email_repo.InMemoryEmailProcessorRepository)


class TestNegotiationLifecycle:
    @pytest.fixture(autouse=True)
    def _reset(self):
        saved = negotiation._repo
        yield
        negotiation._repo = saved

    async def test_no_uri(self, monkeypatch):
        monkeypatch.setattr(negotiation.settings, "negotiation_mongo_uri", "")
        await negotiation.init_repository()
        assert isinstance(negotiation.get_repository(), negotiation.InMemoryNegotiationRepository)

    async def test_mongo(self, monkeypatch, _mock_motor):
        monkeypatch.setattr(negotiation.settings, "negotiation_mongo_uri", "mongodb://x")
        await negotiation.init_repository()
        assert isinstance(negotiation.get_repository(), negotiation.MongoNegotiationRepository)

    async def test_failure_falls_back(self, monkeypatch, _mock_motor):
        monkeypatch.setattr(negotiation.settings, "negotiation_mongo_uri", "mongodb://x")
        with patch.object(negotiation.MongoNegotiationRepository, "_ensure_indexes", new=AsyncMock(side_effect=RuntimeError("no"))):
            await negotiation.init_repository()
        assert isinstance(negotiation.get_repository(), negotiation.InMemoryNegotiationRepository)


class TestTradeLifecycle:
    @pytest.fixture(autouse=True)
    def _reset(self):
        saved = trade._repository, trade._motor_client
        trade._repository, trade._motor_client = None, None
        yield
        trade._repository, trade._motor_client = saved

    async def test_no_uri_and_cached(self, monkeypatch):
        monkeypatch.setattr(trade.settings, "trade_mongo_uri", "")
        first = await trade.get_repository()
        assert isinstance(first, trade.InMemoryFallbackRepository)
        assert await trade.init_repository() is first
        assert await trade.get_repository() is first

    async def test_mongo_and_close(self, monkeypatch):
        monkeypatch.setattr(trade.settings, "trade_mongo_uri", "mongodb://x")
        client = _motor()
        with patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=client):
            repo = await trade.init_repository()
        assert isinstance(repo, trade.MongoOpportunityRepository)
        await trade.close_repository()
        client.close.assert_called_once()
        assert trade._repository is None

    async def test_failure_falls_back(self, monkeypatch):
        monkeypatch.setattr(trade.settings, "trade_mongo_uri", "mongodb://x")
        with patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=RuntimeError("no")):
            assert isinstance(await trade.init_repository(), trade.InMemoryFallbackRepository)
        assert trade._motor_client is None

    async def test_proxy_resets_a_mongo_backend_to_memory(self, db):
        trade._repository, trade._motor_client = trade.MongoOpportunityRepository(db["o"]), object()
        trade.repository.clear()
        assert isinstance(trade._repository, trade.InMemoryFallbackRepository) and trade._motor_client is None

    async def test_proxy_empties_the_memory_store(self):
        trade._repository = trade.InMemoryFallbackRepository()
        trade._repository._store["x"] = 1
        trade.repository.clear()
        assert trade._repository._store == {}
