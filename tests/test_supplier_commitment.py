"""The supplier commits before PortLess commits to the buyer (shared/orchestration/supplier_commitment.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.negotiation_agent.pricing import PriceBand, indicative_range
from shared.orchestration import continuations, notifications
from shared.orchestration import supplier_commitment as sc


class FakeSession(SimpleNamespace):
    def model_copy(self, update=None):
        return FakeSession(**{**vars(self), **(update or {})})


def _session(**kw):
    base = dict(negotiation_id="N1", seller_id="SUP-1", seller_email="sales@mill-one.invalid",
                supplier_quote_price=1000.0, supplier_last_quote=1010.0, current_ask=952.32,
                last_offer_to_supplier=952.32, quantity_mt=20.0, logistics_cost_usd=142.5,
                cost_envelope=SimpleNamespace(total=lambda: 17.5),
                policy=SimpleNamespace(margin_floor_pct=8.0))
    return FakeSession(**{**base, **kw})


def _deal(**kw):
    supplier = SimpleNamespace(role="supplier", party_id="SUP-1", legal_name="Mill One Pvt Ltd")
    base = dict(deal_id="D1", hs_code="10063020", status="NEGOTIATION_AGREED", parties=[supplier],
                match_results=[], supplier_offers=[], opportunity_id=None)
    return SimpleNamespace(**{**base, **kw})


def _match(sid, name, price, score):
    return SimpleNamespace(supplier_id=sid, company_name=name, fob_price_usd=price,
                           composite_score=score, decision="SOURCE")


def _offer(sid, name, price, email):
    return SimpleNamespace(supplier_id=sid, company_name=name, fob_price_usd_per_mt=price,
                           email=email, verification_failed=False)


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("SUPPLIER_CONFIRMATION_REQUIRED", "true")
    monkeypatch.setenv("PO_ACK_REQUIRED", "true")
    sc.reset_for_tests()
    monkeypatch.setattr(sc, "_mongo", lambda: None)
    state = SimpleNamespace(deal=_deal(), session=_session(), saved=[])

    async def get_deal(_deal_id):
        return state.deal

    async def get_session(_deal_id):
        return state.session

    class NegRepo:
        async def save_session(self, s):
            state.session = s
            state.saved.append(s)

    async def set_supplier_party(deal_id, sid, *, company_name=None, email=None, **_kw):
        state.deal.parties = [SimpleNamespace(role="supplier", party_id=sid, legal_name=company_name)]
        return state.deal

    monkeypatch.setattr("services.deals.service.get_deal", get_deal)
    monkeypatch.setattr("services.deals.service.set_supplier_party", set_supplier_party)
    monkeypatch.setattr("services.deals.service._supplier_contact", AsyncMock(return_value={}))
    monkeypatch.setattr(sc, "_session", get_session)
    monkeypatch.setattr("agents.negotiation_agent.repository.get_repository", lambda: NegRepo())
    state.notify = AsyncMock(return_value="EMAIL-N1")
    monkeypatch.setattr(notifications, "notify_party", state.notify)
    state.gate = AsyncMock()
    monkeypatch.setattr(continuations, "_create_gate", state.gate)
    state.pi = AsyncMock(return_value=SimpleNamespace(pi_id="PI-1"))
    monkeypatch.setattr(continuations, "pi_after_compliance", state.pi)
    state.exception = AsyncMock()
    monkeypatch.setattr(continuations, "_publish_exception", state.exception)
    state.qc = AsyncMock(return_value=SimpleNamespace(tpo_id="TPO-1"))
    monkeypatch.setattr(continuations, "book_qc_for_deal", state.qc)
    return state


async def _ask(state):
    return await sc.ready_for_pi("D1", agreed_price=1278.94, quantity_mt=20.0, market="AE")


# (a) before the PI ---------------------------------------------------------------

async def test_switched_off_lets_the_pi_through(monkeypatch):
    monkeypatch.setenv("SUPPLIER_CONFIRMATION_REQUIRED", "false")
    assert await sc.ready_for_pi("D1", agreed_price=1.0, quantity_mt=1.0, market=None) is True


async def test_buyer_agreement_sends_the_supplier_a_firm_request_at_its_own_price(wired):
    assert await _ask(wired) is False
    args, kwargs = wired.notify.await_args
    assert args[:3] == ("D1", "supplier", "supplier_order_request")
    assert args[3]["price"] == 1010.0          # what the supplier itself stated, never our 952.32 counter
    assert args[3]["introduced"] is True
    rec = await sc.get("D1")
    assert rec.status == "requested" and rec.current.supplier_id == "SUP-1"


async def test_pi_keeps_waiting_without_a_second_request(wired):
    await _ask(wired)
    assert await _ask(wired) is False
    assert wired.notify.await_count == 1


@pytest.mark.parametrize(("body", "verdict"), [
    ("We confirm the order. Port of loading Chennai, 25 days.", "confirmed"),
    ("Confirmed, please proceed with the purchase order.", "confirmed"),
    ("We regret we cannot supply this lot any more.", "declined"),
    ("We can do it at USD 1,050 per MT instead.", "unclear"),
    ("What packaging do you need?", "unclear"),
])
async def test_reading_the_reply(body, verdict):
    got, _price = await sc.read_order_reply(body, 1010.0)
    assert got == verdict


async def test_confirmation_opens_the_approval(wired):
    await _ask(wired)
    msg = await sc.handle_supplier_reply("D1", "NEGOTIATION_AGREED", "We confirm the order.", "IN-1")
    assert "confirmed" in msg
    kwargs = wired.gate.await_args
    assert kwargs.args[1] == "CONFIRM_SUPPLIER_ORDER"
    assert kwargs.kwargs["callback_payload"]["price"] == 1010.0


async def test_approve_records_the_price_and_issues_the_pi(wired):
    await _ask(wired)
    await sc.record_confirmed("D1", supplier_id="SUP-1", price=1010.0)
    assert wired.session.supplier_last_quote == 1010.0 and wired.session.current_ask == 1010.0
    wired.pi.assert_awaited_once()
    assert wired.pi.await_args.kwargs["agreed_price"] == 1278.94
    assert await _ask(wired) is True


async def test_approval_for_a_supplier_that_no_longer_holds_the_request_is_refused(wired):
    await _ask(wired)
    with pytest.raises(RuntimeError):
        await sc.record_confirmed("D1", supplier_id="SUP-OTHER", price=900.0)


async def test_no_pi_is_issued_before_the_supplier_confirms(wired, monkeypatch):
    issue = AsyncMock()
    monkeypatch.setattr("services.dealmaker.service.issue_pi", issue)
    monkeypatch.setattr(continuations, "_buyer_destination_port", AsyncMock(return_value="Jebel Ali"))
    assert await continuations.issue_pi_for_deal("D1", agreed_price=1278.94, quantity_mt=20.0, market="AE") is None
    issue.assert_not_awaited()
    wired.notify.assert_awaited_once()   # the firm order request went out instead


# (c) standby supplier ------------------------------------------------------------

async def test_decline_moves_to_the_best_standby_that_keeps_the_margin(wired):
    wired.deal.match_results = [_match("SUP-1", "Mill One", 1000.0, 90), _match("SUP-2", "Mill Two", 1150.0, 85),
                                _match("SUP-3", "Mill Three", 990.0, 80), _match("SUP-4", "Mill Four", 980.0, 70)]
    wired.deal.supplier_offers = [_offer("SUP-2", "Mill Two", 1150.0, "s2@two.invalid"),
                                  _offer("SUP-3", "Mill Three", 990.0, "s3@three.invalid")]
    await _ask(wired)
    msg = await sc.handle_supplier_reply("D1", "NEGOTIATION_AGREED", "We regret we cannot supply.", "IN-2")
    assert "next supplier" in msg
    rec = await sc.get("D1")
    assert rec.attempts[0].outcome == "declined"
    assert rec.current.supplier_id == "SUP-3"            # SUP-2 breaks the 8 % floor, SUP-4 has no address
    reasons = {s["supplier_id"]: s["reason"] for s in rec.skipped}
    assert "floor" in reasons["SUP-2"]
    args = wired.notify.await_args.args
    assert args[3]["price"] == 990.0 and args[3]["introduced"] is False
    assert wired.session.seller_id == "SUP-3" and wired.session.seller_email == "s3@three.invalid"
    assert wired.deal.parties[0].party_id == "SUP-3"


async def test_nobody_left_raises_one_exception_and_keeps_the_pi_back(wired):
    await _ask(wired)
    assert await sc.mark_and_advance("D1", "declined") == "exhausted"
    wired.exception.assert_awaited_once()
    assert wired.exception.await_args.args[1] == "SUPPLIER:no_confirmation"
    assert await _ask(wired) is False


async def test_silence_past_the_deadline_moves_on(wired, monkeypatch):
    monkeypatch.setenv("SUPPLIER_CONFIRMATION_HOURS", "24")
    wired.deal.supplier_offers = [_offer("SUP-3", "Mill Three", 990.0, "s3@three.invalid")]
    await _ask(wired)
    assert await sc.sweep(datetime.now(timezone.utc) + timedelta(hours=1)) == 0
    assert await sc.sweep(datetime.now(timezone.utc) + timedelta(hours=25)) == 1
    rec = await sc.get("D1")
    assert rec.attempts[0].outcome == "no_reply" and rec.current.supplier_id == "SUP-3"


async def test_late_confirmation_from_the_replaced_supplier_is_left_for_an_operator(wired):
    wired.deal.supplier_offers = [_offer("SUP-3", "Mill Three", 990.0, "s3@three.invalid")]
    await _ask(wired)
    await sc.handle_supplier_reply("D1", "NEGOTIATION_AGREED", "We regret we cannot supply.", "IN-1",
                                   from_address="sales@mill-one.invalid")
    assert (await sc.get("D1")).current.supplier_id == "SUP-3"
    # Mill One changes its mind after the order moved: it no longer holds the request.
    out = await sc.handle_supplier_reply("D1", "NEGOTIATION_AGREED", "We confirm the order.", "IN-2",
                                         from_address="Sales <sales@mill-one.invalid>")
    assert out is None
    wired.gate.assert_not_awaited()
    assert await sc.sent_by_replaced_supplier("D1", "sales@mill-one.invalid") is True
    assert await sc.sent_by_replaced_supplier("D1", "s3@three.invalid") is False
    # The standby itself can still confirm.
    await sc.handle_supplier_reply("D1", "NEGOTIATION_AGREED", "We confirm the order.", "IN-3",
                                   from_address="s3@three.invalid")
    assert wired.gate.await_args.kwargs["callback_payload"]["supplier_id"] == "SUP-3"


async def test_operator_set_supplier_is_not_replaced(wired, monkeypatch):
    wired.deal.supplier_offers = [_offer("SUP-3", "Mill Three", 990.0, "s3@three.invalid")]

    async def keep(deal_id, sid, **_kw):
        return wired.deal                     # set_supplier_party keeps an operator-entered supplier
    monkeypatch.setattr("services.deals.service.set_supplier_party", keep)
    await _ask(wired)
    assert await sc.mark_and_advance("D1", "declined") == "exhausted"
    rec = await sc.get("D1")
    assert "operator-set" in rec.skipped[-1]["reason"]


# (b) PO acknowledgement ----------------------------------------------------------

async def test_inspection_waits_for_the_po_acknowledgement(wired):
    await sc.await_po_ack("D1", 20.0)
    assert await sc.handle_supplier_reply("D1", "PO_ISSUED", "What is the delivery address?", "IN-3") is None
    wired.qc.assert_not_awaited()
    msg = await sc.handle_supplier_reply("D1", "PO_ISSUED", "We acknowledge the PO; production starts Monday.", "IN-4")
    assert "inspection booked" in msg
    wired.qc.assert_awaited_once_with("D1")


async def test_po_ack_is_recorded_once(wired):
    await sc.await_po_ack("D1", 20.0)
    await sc.acknowledge_po("D1", by="ops")
    await sc.acknowledge_po("D1", by="ops again")
    assert wired.qc.await_count == 1


async def test_po_issued_does_not_book_the_inspection_before_the_ack(monkeypatch, wired):
    deal = _deal(status="PO_ISSUED")
    monkeypatch.setattr(continuations, "_get_deal_in_status", AsyncMock(return_value=deal))
    monkeypatch.setattr(continuations, "_negotiation_numbers", AsyncMock(return_value=(1010.0, 20.0)))
    monkeypatch.setattr(continuations, "_tranche_gate", AsyncMock())
    monkeypatch.setattr(notifications, "notify_po_issued", AsyncMock())
    monkeypatch.setattr("services.dealmaker.repository.get_repository",
                        lambda: SimpleNamespace(get_po=AsyncMock(return_value=None),
                                                get_po_by_deal=AsyncMock(return_value=None)))
    from shared.domain.events import DomainEvent, EventTopic
    await continuations.on_po_issued(DomainEvent(topic=EventTopic.PO_ISSUED, deal_id="D1", payload={"po_id": "PO-1"}))
    wired.qc.assert_not_awaited()
    assert (await sc.get("D1")).po_ack == "awaiting"


# templates and the indicative range ------------------------------------------------

def test_order_request_states_the_terms_and_nothing_of_the_buyer():
    subject, body = notifications._tpl_supplier_order_request({
        "deal_id": "D1", "supplier_name": "Mill Three", "price": 990.0, "quantity_mt": 20.0,
        "hs_code": "10063020", "hours": 24, "introduced": False})
    assert "USD 990.00" in subject and "20 MT" in subject
    assert "your quotation of USD 990.00" in body and "We confirm the order" in body
    assert "within 24 hours" in body and "Kind regards," in body
    assert "1,278" not in body and "buyer's price" not in body.lower()


def test_indicative_range_stays_above_the_floor():
    band = PriceBand(1259.19, 1316.43, 1316.43, False, "opening")
    low, high = indicative_range(band)
    assert (low, high) == (1290.0, 1320.0)
    assert low >= band.low
