"""Start a prospect from a supplier (services/trade_min/supply_first.py)."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.trade_min import directory as dr
from services.trade_min import relationships as rl
from services.trade_min import supply_first as sf
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation

NOW = datetime.now(UTC)


@pytest.fixture
def world(monkeypatch):
    dr.reset_directory()
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[]))
    monkeypatch.setattr(rl, "_tracks", AsyncMock(return_value={}))
    start = AsyncMock(return_value=2)
    monkeypatch.setattr("shared.orchestration.outreach.start_outreach", start)
    yield repo, start
    dr.reset_directory()


def _rec(role, name, site, country, hs=("100630",), **kw):
    return CounterpartyRecord(id=dr.record_key(role, name, f"https://{site}"), role=role, name=name,
                              website=f"https://{site}", country=country, email=kw.pop("email", f"desk@{site}"),
                              source=kw.pop("source", "apify_company_search"), hs_codes=list(hs), markets=[country],
                              first_seen_at=NOW, last_seen_at=NOW, **kw)


async def _setup():
    store = await dr.get_directory()
    sup = await store.upsert(_rec("supplier", "Thanjavur Rice Mill", "tjrice.in", "IN", source="manual",
                                  estimated_fob_price_usd=700.0, manual={"moq_mt": 25, "port_of_loading": "INMAA"}))
    for b in (_rec("buyer", "Gulf Foods", "gulf.ae", "AE"),
              _rec("buyer", "Euro Rice", "eurorice.de", "DE"),
              _rec("buyer", "Blocked Co", "blocked.ae", "AE", do_not_contact=True),
              _rec("buyer", "Failed Co", "failed.ae", "AE", validation=CounterpartyValidation(status="failed")),
              _rec("buyer", "Spice Only", "spice.ae", "AE", hs=("090422",)),
              _rec("buyer", "No Mail", "nomail.ae", "AE", email=None)):
        await store.upsert(b)
    return sup


async def test_supply_first_prospect_with_directory_buyers(world):
    repo, start = world
    sup = await _setup()
    out = await sf.start(sup.id, "AE", by="ops@portless.in", org_id="portless", quantity_mt=50)
    assert out["buyers"] == 2 and out["introductions"] == 2 and out["hs_code"] == "100630"
    deal = await repo.get(out["deal_id"])
    assert deal.status.value == "MATCHING" and deal.market == "AE" and deal.quantity_mt == 50
    assert [c.legal_name for c in deal.buyer_candidates] == ["Gulf Foods", "Euro Rice"]  # market first
    assert deal.parties[0].role.value == "supplier" and deal.parties[0].legal_name == "Thanjavur Rice Mill"
    (offer,) = deal.supplier_offers
    assert offer.fob_price_usd_per_mt == 700.0 and offer.moq_mt == 25 and offer.source == "directory_usual_price"
    start.assert_awaited_once()


async def test_refusals(world):
    store = await dr.get_directory()
    sup = await _setup()
    with pytest.raises(LookupError):
        await sf.start("supplier:nope", "AE", by="ops", org_id="o")
    with pytest.raises(sf.CannotStart, match="no contactable buyer"):
        await sf.start(sup.id, "AE", by="ops", org_id="o", hs_code="071220")
    with pytest.raises(sf.CannotStart, match="2-letter"):
        await sf.start(sup.id, "UAE", by="ops", org_id="o")
    bad = await store.upsert(_rec("supplier", "Bad Mill", "bad.in", "IN", do_not_contact=True))
    with pytest.raises(sf.CannotStart):
        await sf.start(bad.id, "AE", by="ops", org_id="o")
