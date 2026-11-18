"""Pre-arranged trade: buyer and supplier known, straight to the negotiation."""
from __future__ import annotations

import pytest

from services.deals.repository import InMemoryFallbackRepository
from shared.orchestration import prearranged


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", r)
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "false")
    return r


async def test_starts_at_negotiating_with_both_parties_and_the_prices(repo):
    deal = await prearranged.start(
        by="ops@portless.in", org_id="portless", hs_code="10063020", market="AE", quantity_mt=25.0,
        buyer={"legal_name": "Gulf Foods LLC", "email": "buy@gulf.ae"},
        supplier={"legal_name": "Mill One Pvt Ltd", "email": "sales@mill.in", "gstin": "33AAAAA0000A1Z5"},
        buyer_price_usd=1100.0, supplier_price_usd=900.0, supplier_quote_written=True)
    assert getattr(deal.status, "value", deal.status) == "NEGOTIATING"
    assert {getattr(p.role, "value", p.role) for p in deal.parties} == {"buyer", "supplier"}
    assert deal.buyer_target_price_usd == 1100.0 and deal.quantity_mt == 25.0 and deal.prearranged_by == "ops@portless.in"
    assert deal.match_results[0].fob_price_usd == 900.0 and deal.winning_supplier_id == deal.match_results[0].supplier_id
    assert deal.supplier_offers[0].price_basis == "quoted"
    from shared.orchestration.continuations import _negotiation_seed
    assert _negotiation_seed(deal)[:2] == (1100.0, 900.0)


async def test_no_margin_is_refused(repo):
    with pytest.raises(ValueError, match="no margin"):
        await prearranged.start(by="ops", org_id="portless", hs_code="1006", market="AE", quantity_mt=10,
                                buyer={"legal_name": "B"}, supplier={"legal_name": "S"},
                                buyer_price_usd=900.0, supplier_price_usd=950.0)
