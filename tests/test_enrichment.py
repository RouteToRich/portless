"""Counterparty enrichment (shared/orchestration/enrichment.py) + deal party API.

The Apify connectors are replaced by fakes; the Deals repository is the
in-memory implementation; the event bus is captured.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import DealRecord, DealStatus, StatusHistoryEntry
from services.deals.service import get_deal, update_party
from shared.domain.events import EventTopic
from shared.domain.models import Party
from shared.orchestration import enrichment

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _repo(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.service.get_repository", lambda: repo)
    return repo


@pytest.fixture
def published(monkeypatch):
    events = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


async def _deal(repo, parties=None, deal_id="DEAL-E1") -> DealRecord:
    deal = DealRecord(
        deal_id=deal_id, operator_id="op", org_id="portless", hs_code="090422", market="DE",
        status=DealStatus.MATCHING,
        parties=parties if parties is not None else [
            Party(role="buyer", legal_name="Acme Imports GmbH", country="DE"),
            Party(role="supplier", legal_name="Kerala Spice Co", country="IN"),
        ],
        status_history=[StatusHistoryEntry(status=DealStatus.MATCHING, transitioned_at=NOW)],
        created_at=NOW, updated_at=NOW,
    )
    await repo.save(deal)
    return deal


class _Fake:
    def __init__(self, payload, actor="rich_minds~fake"):
        self.payload = payload
        self.actor_ref = actor
        self.calls = []

    async def quote(self, request):
        self.calls.append(request)
        out = dict(self.payload)
        out.setdefault("actor", self.actor_ref)
        return out


def _wire(monkeypatch, contacts=None, screening=None, emails=None):
    fakes = {
        "apify_contact_finder": _Fake(contacts if contacts is not None else {"contacts": []}, "rich_minds~company-contact-finder-ai"),
        "apify_counterparty_check": _Fake(screening if screening is not None else {"results": []}, "rich_minds~counterparty-check-ai"),
        "apify_email_qualifier": _Fake(emails if emails is not None else {"emails": [], "stub": True}, "rich_minds~email-list-qualifier-ai"),
    }
    monkeypatch.setattr("shared.connectors.registry.get_connector", lambda name: fakes[name])
    return fakes


class TestEnrichDealParties:
    @pytest.mark.asyncio
    async def test_contacts_and_screening_land_on_parties(self, _repo, published, monkeypatch):
        await _deal(_repo)
        fakes = _wire(
            monkeypatch,
            contacts={"contacts": [
                {"input_name": "Acme Imports GmbH", "website": "https://acme.de", "email": "einkauf@acme.de",
                 "email_status": "ok", "phone": "+49 40 1", "address": "Hamburg", "vat_id": "DE123", "confidence": 0.92},
                {"input_name": "Kerala Spice Co", "website": "https://kerala.in", "email": "sales@kerala.in",
                 "email_status": "unverified", "confidence": 0.7},
            ]},
            screening={"results": [
                {"name": "Acme Imports GmbH", "verdict": "clear", "reason": "no hits", "lei": "LEI1", "vat_valid": True,
                 "legal_name": "Acme Imports GmbH"},
                {"name": "Kerala Spice Co", "verdict": "review", "reason": "weak name match on UK list"},
            ]},
            emails={"emails": [{"email": "sales@kerala.in", "status": "risky"}]},
        )
        result = await enrichment.enrich_deal_parties("DEAL-E1")
        assert result["blocked"] is False
        assert "rich_minds~company-contact-finder-ai" in result["actors_used"]
        deal = await get_deal("DEAL-E1")
        buyer = next(p for p in deal.parties if p.role == "buyer")
        supplier = next(p for p in deal.parties if p.role == "supplier")
        assert buyer.email == "einkauf@acme.de" and buyer.email_status == "ok" and buyer.website == "https://acme.de"
        assert buyer.vat_number == "DE123" and buyer.vat_valid is True and buyer.lei == "LEI1"
        assert buyer.sanctions_status == "clear" and buyer.contact_source == "apify:company-contact-finder-ai"
        assert buyer.verification_status == "registry_match"
        assert supplier.sanctions_status == "review" and supplier.email_status == "risky"  # qualifier downgraded it
        assert buyer.enriched_at is not None and buyer.sanctions_checked_at is not None
        # the qualifier only ran for the unverified address
        assert fakes["apify_email_qualifier"].calls[0]["emails"] == ["sales@kerala.in"]
        assert [e.topic for e in published] == [EventTopic.COUNTERPARTY_SCREENED]

    @pytest.mark.asyncio
    async def test_block_raises_exception_event(self, _repo, published, monkeypatch):
        await _deal(_repo)
        _wire(monkeypatch, screening={"results": [
            {"name": "Acme Imports GmbH", "verdict": "block", "reason": "OFAC SDN 97"},
        ]})
        result = await enrichment.enrich_deal_parties("DEAL-E1", contacts=False)
        assert result["blocked"] is True and result["buyer"]["sanctions_status"] == "block"
        topics = [e.topic for e in published]
        assert EventTopic.DEAL_EXCEPTION in topics
        exc = next(e for e in published if e.topic == EventTopic.DEAL_EXCEPTION)
        assert exc.payload["action_required"] == "sanctions_block_review"
        assert enrichment.screening_verdict(result["deal"], "buyer") == "block"

    @pytest.mark.asyncio
    async def test_operator_contact_is_never_overwritten(self, _repo, monkeypatch):
        await _deal(_repo, parties=[Party(role="buyer", legal_name="Acme Imports GmbH", country="DE",
                                         email="boss@acme.de", contact_source="operator")])
        _wire(monkeypatch, contacts={"contacts": [
            {"input_name": "Acme Imports GmbH", "email": "info@acme.de", "email_status": "ok", "website": "https://acme.de"},
        ]})
        await enrichment.enrich_deal_parties("DEAL-E1", screen=False, verify_emails=False)
        buyer = (await get_deal("DEAL-E1")).parties[0]
        assert buyer.email == "boss@acme.de" and buyer.contact_source == "operator"
        assert buyer.website == "https://acme.de"  # a missing website is still filled in

    @pytest.mark.asyncio
    async def test_stub_mode_marks_unchecked_with_warnings(self, _repo, monkeypatch):
        await _deal(_repo)
        _wire(monkeypatch, contacts={"contacts": [], "stub": True}, screening={"results": [], "stub": True})
        result = await enrichment.enrich_deal_parties("DEAL-E1")
        assert any("stub mode" in w for w in result["warnings"])
        buyer = (await get_deal("DEAL-E1")).parties[0]
        assert buyer.sanctions_status == "unchecked" and buyer.email_status == "unverified"
        assert enrichment.screening_is_fresh(await get_deal("DEAL-E1"), "buyer") is False

    @pytest.mark.asyncio
    async def test_connector_failure_becomes_warning(self, _repo, monkeypatch):
        await _deal(_repo)

        class _Boom:
            actor_ref = "rich_minds~x"

            async def quote(self, request):
                raise RuntimeError("apify down")

        monkeypatch.setattr("shared.connectors.registry.get_connector", lambda name: _Boom())
        result = await enrichment.enrich_deal_parties("DEAL-E1", verify_emails=False)
        assert any("failed" in w for w in result["warnings"])
        assert (await get_deal("DEAL-E1")).parties[0].sanctions_status == "unchecked"

    @pytest.mark.asyncio
    async def test_no_parties_no_event(self, _repo, published, monkeypatch):
        await _deal(_repo, parties=[])
        _wire(monkeypatch)
        result = await enrichment.enrich_deal_parties("DEAL-E1")
        assert result["buyer"] is None and published == []

    def test_freshness(self):
        deal = SimpleNamespace(parties=[SimpleNamespace(role="buyer", sanctions_status="clear",
                                                         sanctions_checked_at=NOW - timedelta(days=45))])
        assert enrichment.screening_is_fresh(deal, "buyer") is False
        assert enrichment.screening_is_fresh(deal, "buyer", max_age_days=60) is True
        assert enrichment.screening_verdict(SimpleNamespace(parties=[]), "buyer") == "unchecked"


class TestUpdateParty:
    @pytest.mark.asyncio
    async def test_operator_patch_stamps_source(self, _repo):
        await _deal(_repo)
        deal = await update_party("DEAL-E1", "buyer", {"email": "new@acme.de", "phone": "+49"}, source="operator")
        buyer = deal.parties[0]
        assert buyer.email == "new@acme.de" and buyer.contact_source == "operator"
        assert buyer.email_status == "unverified" and buyer.contact_confidence == 1.0

    @pytest.mark.asyncio
    async def test_creates_missing_party(self, _repo):
        await _deal(_repo, parties=[])
        deal = await update_party("DEAL-E1", "supplier", {"legal_name": "New Mill", "gstin": "33AAA"}, source="operator")
        assert deal.parties[0].role == "supplier" and deal.parties[0].country == "IN" and deal.parties[0].gstin == "33AAA"

    @pytest.mark.asyncio
    async def test_rejects_bad_role(self, _repo):
        await _deal(_repo)
        with pytest.raises(ValueError):
            await update_party("DEAL-E1", "broker", {"email": "x@y.z"})
