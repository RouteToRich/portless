"""Supplier quotes are reconfirmed before they expire (shared/orchestration/quote_reconfirm.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import DealRecord, DealStatus, SupplierOffer
from shared.orchestration import quote_reconfirm as qr

NOW = datetime.now(UTC)


def _offer(days_old: float, validity: int = 10, **kw) -> SupplierOffer:
    base = dict(offer_id="OF-1", supplier_id="S1", company_name="Mill One", email="sales@mill.in",
                fob_price_usd_per_mt=760.0, validity_days=validity, quote_id="Q-1",
                received_at=NOW - timedelta(days=days_old))
    base.update(kw)
    return SupplierOffer(**base)


def _deal(offers, status=DealStatus.MATCHING, **kw):
    return DealRecord(deal_id="P1", operator_id="op", org_id="o", hs_code="090422", market="AE", status=status,
                      created_at=NOW, updated_at=NOW, supplier_offers=offers, **kw)


@pytest.fixture
def world(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    send = AsyncMock(return_value=SimpleNamespace(outcome="sent"))
    monkeypatch.setattr("services.email_gateway.sender.send_email", send)
    monkeypatch.setattr("services.sourcing.service.get_quote", AsyncMock(return_value=SimpleNamespace(rfq_id="RFQ-ABC123")))
    monkeypatch.setattr("agents.negotiation_agent.email_history.record_deal_email", AsyncMock())
    monkeypatch.setenv("QUOTE_RECONFIRM_DAYS_BEFORE", "3")
    return SimpleNamespace(repo=repo, send=send)


async def test_a_quote_about_to_expire_is_reconfirmed_once_on_its_rfq_thread(world):
    await world.repo.save(_deal([_offer(days_old=8)]))  # 2 days left
    assert (await qr.sweep(NOW))["asked"] == 1
    req = world.send.await_args.args[0]
    assert req.to_address == "sales@mill.in" and "[RFQ:RFQ-ABC123]" in req.subject
    assert "USD 760.00 per MT FOB still stands" in req.body
    offer = (await world.repo.get("P1")).supplier_offers[0]
    assert offer.reconfirm_status == "asked" and offer.reconfirm_asked_at
    assert (await qr.sweep(NOW))["asked"] == 0


async def test_too_early_parked_or_past_prospect_are_left_alone(world):
    await world.repo.save(_deal([_offer(days_old=2)]))
    assert (await qr.sweep(NOW))["asked"] == 0
    await world.repo.save(_deal([_offer(days_old=8)], parked_at=NOW))
    assert (await qr.sweep(NOW))["asked"] == 0
    await world.repo.save(_deal([_offer(days_old=8)], status=DealStatus.NEGOTIATING))
    assert (await qr.sweep(NOW))["asked"] == 0
    world.send.assert_not_awaited()


async def test_silence_marks_the_quote_expired(world):
    await world.repo.save(_deal([_offer(days_old=11, reconfirm_asked_at=NOW - timedelta(days=3), reconfirm_status="asked")]))
    assert (await qr.sweep(NOW))["expired"] == 1
    assert (await world.repo.get("P1")).supplier_offers[0].reconfirm_status == "expired"


@pytest.mark.parametrize("text,ok", [
    ("Yes, we confirm the price stands until end of month.", True),
    ("Price remains valid.", True),
    ("Sorry, we cannot hold this price; new price is 790.", False),
    ("We confirm a revised price.", False),
    ("Thanks for your mail.", False),
])
def test_what_counts_as_a_reconfirmation(text, ok):
    assert qr.reads_as_confirmation(text) is ok


async def test_a_confirming_reply_restarts_the_validity(world):
    await world.repo.save(_deal([_offer(days_old=8, reconfirm_asked_at=NOW, reconfirm_status="asked")]))
    rfq = SimpleNamespace(deal_id="P1", dispatch_results=[SimpleNamespace(supplier_id="S1", email="sales@mill.in")])
    assert await qr.confirm_from_reply(rfq, "sales@mill.in", "Yes, we confirm the price stands.")
    offer = (await world.repo.get("P1")).supplier_offers[0]
    assert offer.reconfirm_status == "confirmed" and offer.received_at >= NOW - timedelta(seconds=5)
    assert qr.expires_at(offer) > NOW + timedelta(days=9)


async def test_a_reply_from_someone_else_or_never_asked_changes_nothing(world):
    await world.repo.save(_deal([_offer(days_old=8)]))
    rfq = SimpleNamespace(deal_id="P1", dispatch_results=[SimpleNamespace(supplier_id="S1", email="sales@mill.in")])
    assert not await qr.confirm_from_reply(rfq, "sales@mill.in", "We confirm the price stands.")  # never asked
    assert not await qr.confirm_from_reply(rfq, "stranger@other.com", "We confirm the price stands.")


async def test_an_offer_without_an_rfq_thread_is_not_asked(world, monkeypatch):
    await world.repo.save(_deal([_offer(days_old=8, quote_id=None)]))
    assert (await qr.sweep(NOW))["asked"] == 0
