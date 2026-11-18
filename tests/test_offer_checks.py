"""A supplier offer entered by staff is checked and re-scores the board (shared/orchestration/offer_checks.py)."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from services.deals.schemas import SupplierOffer
from services.trade_min import directory as dr
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation
from shared.orchestration import offer_checks as oc

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh():
    dr.reset_directory()
    yield
    dr.reset_directory()


def _offer(**kw):
    base = dict(offer_id="O1", supplier_id="mill.in", company_name="Mill One", email="sales@mill.in",
                fob_price_usd_per_mt=700.0)
    base.update(kw)
    return SupplierOffer(**base)


async def test_a_fresh_directory_check_is_reused(monkeypatch):
    store = await dr.get_directory()
    await store.upsert(CounterpartyRecord(
        id="supplier:mill.in", role="supplier", name="Mill One", website="https://mill.in", email="sales@mill.in",
        source="manual", first_seen_at=NOW, last_seen_at=NOW,
        validation=CounterpartyValidation(status="verified", sanctions="clear", email="ok", gst="active",
                                          iec="unchecked", checked_at=NOW)))
    run = AsyncMock()
    monkeypatch.setattr("services.trade_min.validation.validate_record", run)
    out = await oc.check_offer(_offer())
    assert (out.gst_verified, out.iec_active, out.verification_failed) == (True, None, False)
    run.assert_not_awaited()


async def test_an_unknown_supplier_is_checked_now_and_a_failure_blocks_it(monkeypatch):
    async def fake(rec):
        rec.validation = CounterpartyValidation(status="failed", sanctions="block", email="ok", gst="inactive")
        return rec
    monkeypatch.setattr("services.trade_min.validation.validate_record", fake)
    out = await oc.check_offer(_offer(supplier_id="new.in", email="x@new.in", company_name="New Mill"))
    assert out.verification_failed and out.gst_verified is False


async def test_a_check_that_breaks_leaves_the_offer_as_it_was(monkeypatch):
    monkeypatch.setattr("services.trade_min.validation.validate_record", AsyncMock(side_effect=RuntimeError("apify down")))
    offer = _offer(supplier_id="z.in", email="a@z.in", company_name="Zed")
    assert await oc.check_offer(offer) == offer
