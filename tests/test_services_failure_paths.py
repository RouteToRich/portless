"""DealMaker, Deals and Email Processor: lookups, party data, and every failure path."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.dealmaker import repository as dm_repo
from services.dealmaker import service as dm
from services.dealmaker.schemas import PiRecord, PoRecord
from services.deals import service as deals
from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import (
    BuyerCandidate,
    BuyerRequirement,
    DealRecord,
    DealStatus,
    UpdateStatusRequest,
)
from services.email_processor import repository as ep_repo
from services.email_processor import service as ep
from services.email_processor.schemas import (
    InboundEmailRecord,
    InboundEmailStatus,
    InboundRawEmail,
    MappingResult,
)
from shared.connectors.google_drive import GoogleDriveError
from shared.domain.enums import PartyRole
from shared.domain.models import Party
from tests.factories import make


def _party(role=PartyRole.BUYER, **kw):
    return Party(**{"role": role, "legal_name": "Acme Oils Ltd", "country": "ES", **kw})


# ---------------------------------------------------------------------------
# DealMaker
# ---------------------------------------------------------------------------

class TestDealMakerParties:
    @pytest.mark.parametrize(("role", "label"), [(PartyRole.BUYER, "buyer"), (PartyRole.SUPPLIER, "supplier")])
    def test_party_name_is_the_legal_name(self, role, label):
        assert dm._party_name(SimpleNamespace(parties=[_party(role)]), label) == "Acme Oils Ltd"

    def test_no_party(self):
        deal = SimpleNamespace(parties=[_party(PartyRole.SUPPLIER)])
        assert dm._party_name(deal, "buyer") is None and dm._party_address(deal, "buyer") is None and dm._party_vat(deal, "buyer") is None
        assert dm._party_name(SimpleNamespace(), "buyer") is None

    def test_duck_typed_name_still_works(self):
        assert dm._party_name(SimpleNamespace(parties=[SimpleNamespace(role="BUYER", name="N")]), "buyer") == "N"
        assert dm._party_name(SimpleNamespace(parties=[SimpleNamespace(role="buyer", name=None, company_name="C")]), "buyer") == "C"

    def test_address_and_vat(self):
        deal = SimpleNamespace(parties=[_party(address="Calle 1", vat_number="ES1")])
        assert dm._party_address(deal, "buyer") == "Calle 1" and dm._party_vat(deal, "buyer") == "ES1"

    def test_supplier_registrations(self):
        deal = SimpleNamespace(parties=[_party(), _party(PartyRole.SUPPLIER, gstin="G1", iec="I1")])
        assert dm._party_gstin(deal) == "G1" and dm._party_iec(deal) == "I1"

    def test_no_supplier_registrations(self):
        deal = SimpleNamespace(parties=[_party()])
        assert dm._party_gstin(deal) is None and dm._party_iec(deal) is None


class TestDealMakerDeals:
    async def test_fetch(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value="deal")):
            assert await dm._fetch_deal("D") == "deal"

    async def test_fetch_not_found(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("Deal Not Found"))), pytest.raises(dm.DealNotFoundError):
            await dm._fetch_deal("D")

    async def test_fetch_other_error_propagates(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("mongo"))), pytest.raises(RuntimeError, match="mongo"):
            await dm._fetch_deal("D")

    async def test_transition(self):
        with patch("services.deals.service.update_status", new=AsyncMock()) as update:
            await dm._transition_deal("D", "PI_SENT", "why")
        assert update.await_args.args[1].target_status == DealStatus.PI_SENT

    async def test_transition_failure_propagates(self):
        with patch("services.deals.service.update_status", new=AsyncMock(side_effect=RuntimeError("no"))), pytest.raises(RuntimeError):
            await dm._transition_deal("D", "PI_SENT", "why")

    async def test_emit_failure_is_contained(self):
        with patch.object(dm, "get_publisher", side_effect=RuntimeError):
            await dm._emit(dm.EventTopic.PI_SENT, "D", {})

    async def test_emit(self):
        publisher = MagicMock(publish=AsyncMock())
        with patch.object(dm, "get_publisher", return_value=publisher):
            await dm._emit(dm.EventTopic.PI_SENT, "D", {"a": 1})
        assert publisher.publish.await_args.args[0].payload == {"a": 1}


class TestDealMakerStorage:
    def test_storage_dir_falls_back_when_not_creatable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dm.settings, "dealmaker_storage_dir", str(tmp_path / "x"))
        real = Path.mkdir
        calls = {"n": 0}

        def flaky(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("no")
            return real(self, *a, **k)

        with patch.object(Path, "mkdir", flaky):
            assert dm._storage_dir().name == "dealmaker"

    async def test_drive_is_the_primary_store(self):
        drive = MagicMock(is_configured=MagicMock(return_value=True), upload_document=MagicMock(return_value={"web_view_link": "L", "file_id": "F"}))
        with patch.object(dm, "get_drive_connector", return_value=drive):
            assert await dm._write_document("D", "a.html", "x") == "L"

    async def test_drive_link_fallback_uri(self):
        drive = MagicMock(is_configured=MagicMock(return_value=True), upload_document=MagicMock(return_value={"web_view_link": "", "file_id": "F"}))
        with patch.object(dm, "get_drive_connector", return_value=drive):
            assert await dm._write_document("D", "a.html", "x") == "gdrive://F"

    async def test_drive_failure_falls_back_to_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dm.settings, "dealmaker_storage_dir", str(tmp_path))
        drive = MagicMock(is_configured=MagicMock(return_value=True), upload_document=MagicMock(side_effect=GoogleDriveError("x")))
        with patch.object(dm, "get_drive_connector", return_value=drive):
            uri = await dm._write_document("D", "a.html", "content")
        assert uri.startswith("file:") and (tmp_path / "a.html").read_text() == "content"


class TestDealMakerLookups:
    @pytest.fixture(autouse=True)
    def repo(self):
        dm_repo._repository = dm_repo.InMemoryDealMakerRepository()
        yield dm_repo._repository
        dm_repo._repository = None

    async def test_get_pi(self, repo):
        await repo.save_pi(make(PiRecord, pi_id="P1"))
        assert (await dm.get_pi("P1")).pi_id == "P1"
        with pytest.raises(dm.PiNotFoundError):
            await dm.get_pi("x")

    async def test_get_po(self, repo):
        await repo.save_po(make(PoRecord, po_id="P1"))
        assert (await dm.get_po("P1")).po_id == "P1"
        with pytest.raises(dm.PoNotFoundError) as err:
            await dm.get_po("x")
        assert err.value.po_id == "x"

    async def test_po_file(self, repo):
        await repo.save_po(make(PoRecord, po_id="P1", deal_id="D1", storage_uri="file:///x"))
        with patch("services.docugen.service.load_stored_document", new=AsyncMock(return_value=(b"x", "text/html"))):
            assert await dm.get_po_file("P1") == (b"x", "text/html", "D1-P1.html")

    @pytest.mark.parametrize(("call", "error"), [(dm.get_po_file, dm.PoNotFoundError), (dm.get_pi_file, dm.PiNotFoundError)])
    async def test_missing_file_record(self, call, error):
        with pytest.raises(error):
            await call("x")

    async def test_est_margin_is_patched(self, repo):
        await repo.save_pi(make(PiRecord, pi_id="P1", deal_id="D1", total_value=1000.0))
        with patch("services.deals.service.update_financials", new=AsyncMock()) as patch_margin:
            await dm._patch_deal_est_margin("D1", make(PoRecord, po_id="P2", deal_id="D1", total_value=800.0))
        assert patch_margin.await_args.args[1].est_margin_usd == 200.0

    async def test_est_margin_failure_is_contained(self, repo):
        await repo.save_pi(make(PiRecord, pi_id="P1", deal_id="D1"))
        with patch("services.deals.service.update_financials", new=AsyncMock(side_effect=RuntimeError)):
            await dm._patch_deal_est_margin("D1", make(PoRecord, po_id="P2", deal_id="D1"))

    @pytest.mark.parametrize(("call", "request_kw"), [
        (dm.issue_pi, {"agreed_price": 1.0, "quantity_mt": 1.0, "port_of_destination": "X", "payment_terms": "T"}),
    ])
    async def test_wrong_state_refuses_the_pi(self, call, request_kw):
        from services.dealmaker.schemas import IssuePiRequest
        with patch.object(dm, "_fetch_deal", new=AsyncMock(return_value=SimpleNamespace(status="DRAFT"))), pytest.raises(dm.InvalidDealState):
            await call(make(IssuePiRequest, deal_id="D", **request_kw))

    async def test_wrong_state_refuses_the_po(self):
        from services.dealmaker.schemas import IssuePoRequest
        with patch.object(dm, "_fetch_deal", new=AsyncMock(return_value=SimpleNamespace(status="DRAFT"))), pytest.raises(dm.InvalidDealState):
            await dm.issue_po(make(IssuePoRequest, deal_id="D"))


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(monkeypatch):
    store = InMemoryFallbackRepository()
    monkeypatch.setattr(deals, "get_repository", lambda: store)
    monkeypatch.setattr(deals, "_emit_event", AsyncMock())
    return store


async def _deal(store, **kw):
    deal = make(DealRecord, **{"deal_id": "D1", **kw})
    await store.save(deal)
    return deal


class TestDealsHelpers:
    async def test_match_seed(self):
        seed = MagicMock(model_dump=lambda: {"a": 1})
        with patch("services.trade_min.service.get_match_seed", new=AsyncMock(return_value=seed)):
            assert await deals._fetch_match_seed("O") == {"a": 1}
        with patch("services.trade_min.service.get_match_seed", new=AsyncMock(return_value=None)):
            assert await deals._fetch_match_seed("O") is None
        with patch("services.trade_min.service.get_match_seed", new=AsyncMock(side_effect=RuntimeError)):
            assert await deals._fetch_match_seed("O") is None

    async def test_mark_converted(self):
        with patch("services.trade_min.service.mark_opportunity_converted", new=AsyncMock()) as mark:
            await deals._mark_opportunity_converted("O")
        mark.assert_awaited_once()
        with patch("services.trade_min.service.mark_opportunity_converted", new=AsyncMock(side_effect=RuntimeError)):
            await deals._mark_opportunity_converted("O")

    async def test_fetch_opportunity(self):
        with patch("services.trade_min.service.get_opportunity", new=AsyncMock(return_value="O")):
            assert await deals._fetch_opportunity("O") == "O"
        with patch("services.trade_min.service.get_opportunity", new=AsyncMock(side_effect=RuntimeError)):
            assert await deals._fetch_opportunity("O") is None

    async def test_emit_failure_is_contained(self):
        with patch.object(deals, "get_publisher", side_effect=RuntimeError):
            await deals._emit_event(deals.EventTopic.DEAL_CREATED, "D", {})

    async def test_score_needs_a_price(self):
        assert await deals._score_seller("D", {}, 900.0) is None

    async def test_score_failure_is_none(self):
        with patch("services.match_engine.service.score_match", new=AsyncMock(side_effect=RuntimeError)):
            assert await deals._score_seller("D", {"fob_price_usd": 700.0}, 900.0) is None

    @pytest.mark.parametrize(("quantity", "unit", "expected"), [(0, "mt", None), (-1, "mt", None), (2204.62, "lbs", 1.0), (5, "bags", None), ("x", "mt", None)])
    def test_quantity(self, quantity, unit, expected):
        assert deals._quantity_to_mt(quantity, unit) == expected


class TestDealsMissing:
    @pytest.mark.parametrize(("call", "args"), [
        (deals.update_financials, ("x", deals.UpdateFinancialsRequest())),
        (deals.update_party, ("x", "buyer", {})),
        (deals.update_buyer_candidate, ("x", "C", {})),
        (deals.set_selected_pair, ("x", {})),
        (deals.add_buyer_candidate, ("x", make(BuyerCandidate, candidate_id="C"))),
        (deals.upsert_supplier_offer, ("x", {"offer_id": "O", "supplier_id": "S"})),
            ])
    async def test_unknown_deal(self, repo, call, args):
        with pytest.raises(deals.DealNotFoundError):
            await call(*args)

    async def test_update_status_lost_write(self, repo):
        await _deal(repo, status=DealStatus.MATCHING)
        with patch.object(repo, "mark_status", new=AsyncMock(return_value=False)), pytest.raises(deals.DealNotFoundError):
            await deals.update_status("D1", UpdateStatusRequest(target_status=DealStatus.NEGOTIATING))

    async def test_update_status_deal_vanishes(self, repo):
        await _deal(repo, status=DealStatus.MATCHING)
        real_get, calls = repo.get, {"n": 0}

        async def flaky(deal_id):
            calls["n"] += 1
            return await real_get(deal_id) if calls["n"] == 1 else None

        with patch.object(repo, "get", new=flaky), pytest.raises(deals.DealNotFoundError):
            await deals.update_status("D1", UpdateStatusRequest(target_status=DealStatus.NEGOTIATING))

    async def test_enrich_checks_the_deal_first(self, repo):
        with pytest.raises(deals.DealNotFoundError):
            await deals.enrich_deal("x")

    async def test_enrich(self, repo):
        await _deal(repo)
        with patch("shared.orchestration.enrichment.enrich_deal_parties", new=AsyncMock(return_value={"ok": 1})) as enrich:
            assert await deals.enrich_deal("D1", screen=False) == {"ok": 1}
        assert enrich.await_args.kwargs == {"screen": False, "contacts": True}

    async def test_learning_failures_are_contained(self, repo):
        deal = await _deal(repo, selected_pair={"a": 1})
        with patch("shared.orchestration.outcomes.on_status", new=AsyncMock(side_effect=RuntimeError)), \
             patch("shared.orchestration.memory.remember_deal_summary", new=AsyncMock(side_effect=RuntimeError)):
            await deals._learn_from_status(deal, "PAID", "r")


class TestDealsContacts:
    async def test_supplier_contact_from_the_opportunity(self, repo):
        deal = await _deal(repo, opportunity_id="O1")
        seller = SimpleNamespace(candidate_id="S1", company_name="C", email="e@c.co", website="w", phone="p", gstin="g", iec="i", country="IN", source="s")
        other = SimpleNamespace(candidate_id="S2")
        with patch("services.sourcing.service.list_rfqs", new=AsyncMock(return_value=[])), \
             patch.object(deals, "_fetch_opportunity", new=AsyncMock(return_value=SimpleNamespace(seller_candidates=[other, seller]))):
            info = await deals._supplier_contact(deal, "S1")
        assert (info["legal_name"], info["email"], info["gstin"], info["iec"]) == ("C", "e@c.co", "g", "i")

    async def test_supplier_contact_lookup_failures(self, repo):
        deal = await _deal(repo, opportunity_id="O1")
        with patch("services.sourcing.service.list_rfqs", new=AsyncMock(side_effect=RuntimeError)), \
             patch.object(deals, "_fetch_opportunity", new=AsyncMock(side_effect=RuntimeError)):
            assert await deals._supplier_contact(deal, "S1") == {}

    async def test_operator_requirement_is_stamped(self, repo):
        await _deal(repo, buyer_candidates=[make(BuyerCandidate, candidate_id="C1")])
        updated = await deals.set_buyer_requirement("D1", "C1", {"target_price_usd_per_mt": 900.0})
        requirement = updated.buyer_candidates[0].requirement
        assert (requirement.source, requirement.target_price_basis) == ("operator", "stated")

    async def test_requirement_keeps_a_stated_basis(self, repo):
        await _deal(repo, buyer_candidates=[make(BuyerCandidate, candidate_id="C1")])
        updated = await deals.set_buyer_requirement("D1", "C1", {"target_price_usd_per_mt": 900.0, "target_price_basis": "counter"})
        assert updated.buyer_candidates[0].requirement.target_price_basis == "counter"

    async def test_requirement_without_a_price(self, repo):
        await _deal(repo, buyer_candidates=[make(BuyerCandidate, candidate_id="C1")])
        updated = await deals.set_buyer_requirement("D1", "C1", BuyerRequirement(grade="USP"), source="email")
        assert updated.buyer_candidates[0].requirement.source == "email"

    async def test_selected_pair_without_extras(self, repo):
        await _deal(repo, quantity_mt=7.0)
        updated = await deals.set_selected_pair("D1", {"a": 1})
        assert updated.selected_pair == {"a": 1} and updated.quantity_mt == 7.0

    async def test_selected_pair_with_extras(self, repo):
        await _deal(repo)
        updated = await deals.set_selected_pair("D1", {"a": 1}, quantity_mt=9.0, buyer_price_source="reply")
        assert updated.quantity_mt == 9.0 and updated.buyer_price_source == "reply"


# ---------------------------------------------------------------------------
# Email Processor
# ---------------------------------------------------------------------------

@pytest.fixture
def inbox():
    ep_repo._repository = ep_repo.InMemoryEmailProcessorRepository()
    yield ep_repo._repository
    ep_repo._repository = None


def _raw(**kw):
    return make(InboundRawEmail, **{"from_address": "ann@acme.com", "body_text": "price USD 900", "subject": "Re", **kw})


def _rec(**kw):
    return make(InboundEmailRecord, **{"inbound_id": "I1", "from_address": "ann@acme.com", "body_text": "body", "status": InboundEmailStatus.RECEIVED if hasattr(InboundEmailStatus, "RECEIVED") else next(iter(InboundEmailStatus)), **kw})


class TestPriceExtraction:
    async def test_rate_from_market_intel(self):
        with patch("agents.negotiation_agent.market_intel.get_usd_inr_rate", new=AsyncMock(return_value=80.0)):
            assert await ep._usd_inr_rate() == 80.0

    @pytest.mark.parametrize("failure", [AsyncMock(side_effect=RuntimeError), AsyncMock(return_value=0)])
    async def test_rate_falls_back(self, failure):
        from agents.negotiation_agent.config import settings as neg
        with patch("agents.negotiation_agent.market_intel.get_usd_inr_rate", new=failure):
            assert await ep._usd_inr_rate() == float(neg.fallback_usd_inr_rate)

    async def test_rate_last_resort(self):
        with patch("agents.negotiation_agent.market_intel.get_usd_inr_rate", new=AsyncMock(side_effect=RuntimeError)), \
             patch("agents.negotiation_agent.config.settings", new=SimpleNamespace()):
            assert await ep._usd_inr_rate() == 96.0

    async def test_inr_is_converted_before_bounds(self):
        with patch.object(ep, "_usd_inr_rate", new=AsyncMock(return_value=80.0)):
            assert await ep._extract_price("We can do ₹80,000 per MT") == 1000.0

    async def test_implausible_amount_is_skipped(self):
        assert await ep._extract_price("$0.0001") is None

    async def test_no_amount(self):
        assert await ep._extract_price("thanks a lot") is None

    def test_self_echo(self, monkeypatch):
        from services.email_gateway.config import settings
        monkeypatch.setattr(settings, "gmail_sender_address", "Me@P.io")
        assert ep._is_self_echo("me@p.io") is True and ep._is_self_echo("x@y.z") is False and ep._is_self_echo("") is False

    def test_empty_sender_never_matches(self, monkeypatch):
        from services.email_gateway.config import settings
        monkeypatch.setattr(settings, "gmail_sender_address", "")
        assert ep._is_self_echo("a@b.c") is False


class TestInboundLookups:
    async def test_get(self, inbox):
        await inbox.save(_rec())
        assert (await ep.get_inbound("I1")).inbound_id == "I1"
        with pytest.raises(ValueError, match="not found"):
            await ep.get_inbound("x")

    async def test_list(self, inbox):
        await inbox.save(_rec())
        assert len(await ep.list_inbound()) == 1

    async def test_duplicate_message_is_not_reprocessed(self, inbox):
        await inbox.save(_rec(message_id="<m1>"))
        result = await ep._process_inbound(_raw(message_id="<m1>"))
        assert result.inbound_id == "I1" and result.message == "Duplicate — already processed"

    async def test_memory_failure_never_breaks_inbound(self, inbox):
        result = make(ep.ProcessingResult, inbound_id="I9", message="ok")
        with patch.object(ep, "_process_inbound", new=AsyncMock(return_value=result)), \
             patch("shared.orchestration.memory.remember_email", new=AsyncMock(side_effect=RuntimeError)):
            assert (await ep.process_inbound(_raw())).inbound_id == "I9"


class TestHitlCreation:
    async def test_price_gate_failure_returns_empty(self):
        with patch("services.hitl_approval.service.create_approval", new=AsyncMock(side_effect=RuntimeError)):
            assert await ep._create_price_hitl(_rec()) == ""

    async def test_pi_gate_failure_returns_empty(self):
        with patch("services.hitl_approval.service.create_approval", new=AsyncMock(side_effect=RuntimeError)):
            assert await ep._create_pi_acceptance_hitl(_rec(), SimpleNamespace(pi_id="P", deal_id="D", total_value=1.0)) == ""


class TestUnverifiedSender:
    @pytest.mark.parametrize(("party", "text"), [(None, "could not be determined"), ("buyer", "looks like the buyer")])
    async def test_held_for_an_operator(self, inbox, party, text):
        hitl = AsyncMock(return_value="A1")
        with patch.object(ep, "_create_price_hitl", new=hitl):
            await ep._hold_unverified_sender(_rec(), make(MappingResult, negotiation_id="N", party=party, sender_check="unknown"))
        assert text in hitl.await_args.kwargs["summary"]
        assert "sender_not_verified" in hitl.await_args.kwargs["risk_flags"]


class TestOutreachReply:
    async def test_handler_failure_goes_to_an_operator(self, inbox):
        with patch("shared.orchestration.outreach.handle_reply", new=AsyncMock(side_effect=RuntimeError("x"))), \
             patch.object(ep, "_create_price_hitl", new=AsyncMock(return_value="A")) as hitl:
            await ep._handle_outreach_reply(_rec(), _raw(), make(MappingResult, outreach_deal_id="D", outreach_candidate_id="C"))
        hitl.assert_awaited_once()

    async def test_wrong_sender_is_flagged(self, inbox):
        with patch("shared.orchestration.outreach.handle_reply", new=AsyncMock(return_value={"verdict": "unclear", "sender_mismatch": True})), \
             patch.object(ep, "_create_price_hitl", new=AsyncMock(return_value="A")) as hitl:
            await ep._handle_outreach_reply(_rec(), _raw(), make(MappingResult, outreach_deal_id="D", outreach_candidate_id="C"))
        assert "sender_not_verified" in hitl.await_args.kwargs["risk_flags"]


class TestRfqReply:
    MAP = make(MappingResult, rfq_id="R1")

    async def test_no_price_goes_to_an_operator(self, inbox):
        with patch("services.sourcing.service.get_rfq", new=AsyncMock(return_value=SimpleNamespace(deal_id="D"))), \
             patch.object(ep, "_extract_price", new=AsyncMock(return_value=None)), \
             patch.object(ep, "_create_price_hitl", new=AsyncMock(return_value="A")):
            result = await ep._handle_rfq_reply(_rec(), _raw(), self.MAP, None)
        assert result.status == InboundEmailStatus.NEEDS_OPERATOR

    async def test_rfq_lookup_failure_goes_to_an_operator(self, inbox):
        with patch("services.sourcing.service.get_rfq", new=AsyncMock(side_effect=RuntimeError)), \
             patch.object(ep, "_create_price_hitl", new=AsyncMock(return_value="A")):
            result = await ep._handle_rfq_reply(_rec(), _raw(), self.MAP, None)
        assert result.status == InboundEmailStatus.NEEDS_OPERATOR

    async def test_quote_failure_goes_to_an_operator(self, inbox):
        with patch("services.sourcing.service.get_rfq", new=AsyncMock(return_value=SimpleNamespace(deal_id="D"))), \
             patch("services.sourcing.service.receive_quote_by_email", new=AsyncMock(side_effect=RuntimeError("x"))), \
             patch.object(ep, "_create_price_hitl", new=AsyncMock(return_value="A")):
            result = await ep._handle_rfq_reply(_rec(), _raw(), self.MAP, 700.0)
        assert result.status == InboundEmailStatus.NEEDS_OPERATOR


class TestDeliveryReply:
    async def test_escrow_lookup_failure(self, inbox):
        with patch("services.escrow_guard.repository.get_repository", side_effect=RuntimeError):
            assert await ep._handle_delivery_reply(_rec(deal_id="D"), _raw(), True) is None

    async def test_no_escrow(self, inbox):
        repo = MagicMock(get_hold_by_deal=AsyncMock(return_value=None))
        with patch("services.escrow_guard.repository.get_repository", return_value=repo):
            assert await ep._handle_delivery_reply(_rec(deal_id="D"), _raw(), True) is None

    @staticmethod
    def _hold():
        tranche = SimpleNamespace(trigger="ACCEPTED", status="PENDING", amount=100.0)
        return SimpleNamespace(escrow_id="E", tranches=[tranche], amount=1000.0, disputed=False)

    async def test_not_accepted_and_no_complaint_falls_through(self, inbox):
        repo = MagicMock(get_hold_by_deal=AsyncMock(return_value=self._hold()))
        with patch("services.escrow_guard.repository.get_repository", return_value=repo):
            assert await ep._handle_delivery_reply(_rec(deal_id="D"), _raw(body_text="thanks for the update"), False) is None

    async def test_gate_failure_falls_through(self, inbox):
        repo = MagicMock(get_hold_by_deal=AsyncMock(return_value=self._hold()))
        with patch("services.escrow_guard.repository.get_repository", return_value=repo), \
             patch("services.hitl_approval.service.create_approval", new=AsyncMock(side_effect=RuntimeError)):
            assert await ep._handle_delivery_reply(_rec(deal_id="D"), _raw(body_text="received, we accept the goods"), True) is None


class TestPostAgreementReply:
    async def test_lookup_failures_still_go_to_an_operator(self, inbox):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError)), \
             patch("services.dealmaker.repository.get_repository", side_effect=RuntimeError), \
             patch("agents.negotiation_agent.guards.is_explicit_acceptance", side_effect=RuntimeError), \
             patch.object(ep, "_create_price_hitl", new=AsyncMock(return_value="A")) as hitl:
            result = await ep._handle_post_agreement_reply(_rec(deal_id="D"), _raw(), make(MappingResult, negotiation_id="N"))
        hitl.assert_awaited_once()
        assert result.status == InboundEmailStatus.NEEDS_OPERATOR


class TestDocumentParties:
    """What each counterparty sees on the documents issued to it."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        dm_repo._repository = dm_repo.InMemoryDealMakerRepository()
        self.written = {}

        async def write(_deal_id, _filename, content):
            self.written["html"] = content
            return "file:///x"

        monkeypatch.setattr(dm, "_write_document", write)
        monkeypatch.setattr(dm, "_transition_deal", AsyncMock())
        monkeypatch.setattr(dm, "_emit", AsyncMock())
        monkeypatch.setattr(dm, "_patch_deal_est_margin", AsyncMock())
        yield
        dm_repo._repository = None

    @staticmethod
    def _deal(status):
        parties = [_party(legal_name="Overseas Buyer GmbH", address="Berlin"), _party(PartyRole.SUPPLIER, legal_name="Chennai Oils Pvt", country="IN")]
        return SimpleNamespace(status=SimpleNamespace(value=status), hs_code="290543", parties=parties, buyer_target_price_usd=1000.0, port_of_loading="Chennai")

    async def test_pi_names_the_real_buyer(self):
        from services.dealmaker.schemas import IssuePiRequest
        with patch.object(dm, "_fetch_deal", new=AsyncMock(return_value=self._deal("NEGOTIATION_AGREED"))):
            pi = await dm.issue_pi(make(IssuePiRequest, deal_id="D", agreed_price=900.0, quantity_mt=20.0))
        assert pi.buyer_name == "Overseas Buyer GmbH"
        assert "Overseas Buyer GmbH" in self.written["html"] and "Chennai Oils Pvt" not in self.written["html"]

    async def test_po_names_the_supplier_and_never_the_overseas_buyer(self):
        from services.dealmaker.schemas import IssuePoRequest
        with patch.object(dm, "_fetch_deal", new=AsyncMock(return_value=self._deal("ESCROW_FUNDED"))):
            await dm.issue_po(make(IssuePoRequest, deal_id="D", agreed_price=700.0, quantity_mt=20.0))
        html = self.written["html"]
        assert "Chennai Oils Pvt" in html and dm.settings.portless_entity_name in html
        assert "Overseas Buyer GmbH" not in html, "the supplier must never see who PortLess sells to"
