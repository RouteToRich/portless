"""Supplier do-not-contact is honoured by every supplier e-mail path (review board B-04)."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.trade_min import directory as dr
from services.trade_min.schemas import CounterpartyRecord
from shared.orchestration import contact_guard as cg

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
async def blocked():
    dr.reset_directory()
    store = await dr.get_directory()
    for name, site, email in (("Stop Mill", "stopmill.in", "sales@stopmill.in"), ("Gmail Mill", "gmailmill.in", "mill99@gmail.com")):
        await store.upsert(CounterpartyRecord(id=dr.record_key("supplier", name, f"https://{site}"), role="supplier",
                                              name=name, website=f"https://{site}", email=email, source="manual",
                                              first_seen_at=NOW, last_seen_at=NOW, do_not_contact=True))
    yield
    dr.reset_directory()


@pytest.mark.parametrize("email,expected", [("sales@stopmill.in", True), ("other@stopmill.in", True),
                                           ("mill99@gmail.com", True), ("someone@gmail.com", False),
                                           ("sales@goodmill.in", False), (None, False)])
async def test_address_and_company_domain_but_never_a_free_mail_domain(email, expected):
    assert await cg.supplier_blocked(email) is expected


async def test_the_rfq_is_not_sent(monkeypatch):
    from services.sourcing import service as svc
    from services.sourcing.schemas import RfqRequest, SupplierRef
    send = AsyncMock()
    monkeypatch.setattr(svc, "_dispatch_by_email", send)
    r = await svc._dispatch_one("RFQ-1", SupplierRef(supplier_id="S", email="sales@stopmill.in", source="other"), "m",
                                RfqRequest(deal_id="D", hs_code="100630", suppliers=[SupplierRef(supplier_id="S", source="other")]))
    assert not r.success and "do-not-contact" in r.error
    send.assert_not_awaited()


async def test_best_and_final_skips_them(monkeypatch):
    from shared.orchestration import best_and_final as bf
    send = AsyncMock(return_value=SimpleNamespace(outcome="sent"))
    monkeypatch.setattr("services.email_gateway.sender.send_email", send)
    monkeypatch.setattr("agents.negotiation_agent.email_history.record_deal_email", AsyncMock())
    out = await bf.send("D1", {"hs_code": "100630", "quantity_mt": 25, "best_price_usd_per_mt": 700.0, "respond_by": "x",
                               "suppliers": [{"supplier_id": "A", "email": "sales@stopmill.in", "rfq_id": "RFQ-AAAAAA"},
                                             {"supplier_id": "B", "email": "sales@goodmill.in", "rfq_id": "RFQ-BBBBBB"}]})
    assert out == {"sent": ["B"], "not_delivered": ["A"]} and send.await_count == 1
