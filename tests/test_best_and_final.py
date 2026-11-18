"""Best-and-final round with the other quoted suppliers (shared/orchestration/best_and_final.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import DealRecord, DealStatus, SupplierOffer
from shared.orchestration import best_and_final as bf

NOW = datetime.now(UTC)


def _offer(sid, price, email=True, days_old=1, **kw):
    return SupplierOffer(offer_id=f"OF-{sid}", supplier_id=sid, company_name=f"Mill {sid}",
                         email=f"sales@{sid.lower()}.in" if email else None, fob_price_usd_per_mt=price,
                         quote_id=f"Q-{sid}", validity_days=10, received_at=NOW - timedelta(days=days_old), **kw)


def _deal(offers):
    return DealRecord(deal_id="P1", operator_id="op", org_id="o", hs_code="090422", market="AE",
                      status=DealStatus.MATCHING, created_at=NOW, updated_at=NOW, supplier_offers=offers)


@pytest.fixture(autouse=True)
def _rfq(monkeypatch):
    monkeypatch.setattr("services.sourcing.service.get_quote",
                        AsyncMock(side_effect=lambda qid: SimpleNamespace(rfq_id=f"RFQ-{qid[2:]}0000")))


async def test_plan_asks_every_dearer_supplier_with_a_thread():
    deal = _deal([_offer("A", 760.0), _offer("B", 790.0), _offer("C", 810.0), _offer("D", 800.0, email=False),
                  _offer("E", 700.0, days_old=20), _offer("F", 740.0, verification_failed=True)])
    p = await bf.plan(deal, NOW)
    assert p["best_price_usd_per_mt"] == 760.0 and p["best_supplier_id"] == "A"
    assert [s["supplier_id"] for s in p["suppliers"]] == ["B", "C"]  # D no e-mail, E expired, F failed check
    assert p["suppliers"][0]["rfq_id"] == "RFQ-B0000"


async def test_nothing_to_ask_with_one_quote_or_no_reachable_other():
    with pytest.raises(bf.NothingToAsk):
        await bf.plan(_deal([_offer("A", 760.0)]), NOW)
    with pytest.raises(bf.NothingToAsk):
        await bf.plan(_deal([_offer("A", 760.0), _offer("B", 790.0, email=False)]), NOW)


def test_the_email_never_names_the_competitor():
    text = bf.body("Mill B", 760.0, 25, "black pepper", "2026-11-21")
    assert "USD 760.00 per MT FOB" in text and "25 MT of black pepper" in text and "2026-11-21" in text
    assert "Mill A" not in text and "quotes" not in text.lower()


async def test_gate_then_send_on_the_rfq_threads(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    await repo.save(_deal([_offer("A", 760.0), _offer("B", 790.0)]))
    import services.hitl_approval.repository as hitl_repo
    from services.hitl_approval.repository import InMemoryHitlApprovalRepository
    hitl_repo._repository = InMemoryHitlApprovalRepository()
    try:
        approval_id = await bf.open_gate("P1")
        gate = await hitl_repo._repository.get(approval_id)
        assert gate.action == "SEND_BEST_AND_FINAL" and gate.status == "PENDING"
        assert "Mill B" in gate.summary and gate.callback_payload["best_price_usd_per_mt"] == 760.0
    finally:
        hitl_repo._repository = None
    send = AsyncMock(return_value=SimpleNamespace(outcome="sent"))
    monkeypatch.setattr("services.email_gateway.sender.send_email", send)
    monkeypatch.setattr("agents.negotiation_agent.email_history.record_deal_email", AsyncMock())
    out = await bf.send("P1", gate.callback_payload)
    assert out == {"sent": ["B"], "not_delivered": []}
    req = send.await_args.args[0]
    assert req.to_address == "sales@b.in" and "[RFQ:RFQ-B0000]" in req.subject and "best and final" in req.subject
    assert "Mill A" not in req.body
