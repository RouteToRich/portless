"""The supplier's facility address (asked, read, required before the inspection) and the
currency each counterparty is written to in (shared/orchestration/supplier_address.py,
shared/currency.py)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.negotiation_agent import briefing
from shared import currency
from shared.orchestration import continuations, notifications, supplier_address
from shared.orchestration import supplier_commitment as sc

# ---------------------------------------------------------------------------
# Currency by country
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("country,code", [("IN", "INR"), ("india", "INR"), ("AE", "USD"), ("VN", "USD"), (None, "USD")])
def test_party_currency_is_the_countrys_own_where_a_rate_exists(country, code):
    assert currency.party_currency(country) == code


def test_money_puts_the_local_figure_first_with_the_usd_contract_price():
    assert currency.money(910, "INR", 96.0) == "INR 87,360 (USD 910.00)"
    assert currency.money(910, "USD", None) == "USD 910.00"
    assert currency.money(910, "INR", None) == "USD 910.00"  # no rate → never a made-up local figure
    assert "96.00 INR per USD" in currency.rate_note("INR", 96.0) and currency.rate_note("USD", 1.0) == ""


@pytest.mark.parametrize("text,code", [
    ("Our price is USD 900 per MT FOB Chennai", "USD"),
    ("We can do Rs. 86,400 per tonne", "INR"),
    ("₹86,400 per MT ex-mill", "INR"),
    ("Price $900/MT", "USD"),
    ("USD 900 or INR 86,400", None),
    ("We confirm the order.", None),
])
def test_quoted_currency_reads_the_counterpartys_own_words(text, code):
    assert currency.quoted_currency(text) == code


def test_order_request_to_an_indian_supplier_is_in_inr_with_the_usd_price_and_rate():
    subject, body = notifications._tpl_supplier_order_request({
        "deal_id": "D1", "supplier_name": "Mill One", "price": 910.0, "quantity_mt": 20.0, "hs_code": "10063020",
        "hours": 24, "introduced": True, "party_currency": "INR", "fx_rate": 96.0, "address_needed": False})
    assert "INR 87,360 (USD 910.00)" in subject and "INR 87,360 (USD 910.00) per MT FOB" in body
    assert "Purchase Order and the payments are in USD" in body
    assert "full address of the facility" not in body


def test_order_request_to_a_usd_country_supplier_has_no_local_figure():
    _, body = notifications._tpl_supplier_order_request({
        "deal_id": "D1", "supplier_name": "Mill VN", "price": 910.0, "quantity_mt": 20.0, "hs_code": "10063020",
        "hours": 24, "introduced": True, "party_currency": "USD", "fx_rate": None})
    assert "USD 910.00 per MT FOB" in body and "INR" not in body


async def test_notify_party_fills_the_partys_currency_and_address_need(monkeypatch):
    deal = SimpleNamespace(parties=[SimpleNamespace(role="supplier", country="IN", address="Thanjavur, Tamil Nadu")])
    monkeypatch.setattr(currency, "usd_rate", AsyncMock(return_value=95.5))
    ctx: dict = {}
    await notifications._local_terms(ctx, "D1", "supplier", deal)
    assert ctx == {"party_currency": "INR", "fx_rate": 95.5, "address_needed": True}
    ctx = {"currency": "USD"}  # the currency of an amount (payout, refund) is never touched
    await notifications._local_terms(ctx, "D1", "buyer", SimpleNamespace(parties=[SimpleNamespace(role="buyer", country="AE")]))
    assert ctx == {"currency": "USD", "party_currency": "USD", "fx_rate": None}


async def test_supplier_agent_writes_in_the_suppliers_currency_and_mirrors_its_quote(monkeypatch):
    """The payload tells the model which currency to write in and which one the supplier quoted in."""
    from agents.negotiation_agent import graph
    captured = {}

    async def fake_chat(system, payload, caller, fallback):
        captured.update(payload)
        return fallback

    monkeypatch.setattr(graph, "_chat_json", fake_chat)
    monkeypatch.setattr(briefing, "supplier_brief", AsyncMock(return_value={
        "stage": briefing.FIRST_OFFER if hasattr(briefing, "FIRST_OFFER") else "first_offer_after_introduction",
        "company_name": "Mill One", "country": "IN", "what_they_told_us": {}, "our_earlier_messages": [],
        "missing_information": []}))
    monkeypatch.setattr(graph, "_knowledge_context", AsyncMock(return_value=""))
    session = SimpleNamespace(
        seller_id="S1", product_code="10063020", quantity_mt=20.0, cost_sheet=None, cost_envelope=None,
        buyer_last_offer=1225.0, buyer_target_price=1250.0, supplier_last_quote=900.0, supplier_price_basis="FOB",
        last_offer_to_supplier=None, round_no=1, trade_context="",
        policy=SimpleNamespace(margin_floor_pct=8.0, max_rounds=6, target_margin_pct=15.0))
    state = {"session": session, "party": "supplier", "inbound_message": "Our price is USD 900 per MT FOB Chennai.",
             "supplier_history": "", "usd_inr_rate": 96.0, "current_bid": 1225.0, "current_ask": 900.0}
    try:
        await graph.supplier_agent_node(state)
    except Exception:  # noqa: BLE001 — only the payload matters here
        pass
    assert captured.get("write_currency") == "INR" and captured.get("supplier_quoted_in") == "USD"


# ---------------------------------------------------------------------------
# Facility address
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("address,complete", [
    ("Thanjavur, Tamil Nadu", False),
    ("", False),
    ("Plot 14, SIPCOT Industrial Estate, Thanjavur 613005", True),
    ("2 Sample Street, Chennai", True),
    ("No. 5, Bypass Road, Thanjavur 613 007", True),
])
def test_an_address_is_complete_when_an_inspector_can_drive_to_it(address, complete):
    assert supplier_address.is_complete(address, "IN") is complete


@pytest.mark.parametrize("body,expected", [
    ("We confirm the order.\nFactory address: Plot 14, SIPCOT Industrial Estate,\nThanjavur, Tamil Nadu 613005\n\nRegards,\nArun",
     "Plot 14, SIPCOT Industrial Estate, Thanjavur, Tamil Nadu 613005"),
    ("Our mill is located at 22 Mill Road, Kumbakonam, Tamil Nadu 612001. Ready on 7 December.",
     "22 Mill Road, Kumbakonam, Tamil Nadu 612001"),
    ("Thanks.\nCauvery Rice Mills\nNo. 5, Bypass Road\nThanjavur 613 007\nPhone +91 98400 12345",
     "Cauvery Rice Mills, No. 5, Bypass Road, Thanjavur 613 007"),
    ("We confirm the order at USD 910. Call +91 9840012345.", None),
    ("Address: Thanjavur, Tamil Nadu", None),
    ("New text only.\n\nOn Mon, PortLess wrote:\n> Factory address: Plot 1, Road 2, Chennai 600001", None),
])
def test_the_address_is_read_from_the_suppliers_own_new_text(body, expected):
    assert supplier_address.parse(body) == expected


def test_negotiation_asks_for_the_facility_address_while_it_is_unknown():
    asks = briefing.supplier_missing({}, verified=True, address_known=False)
    assert any("facility" in a for a in asks)
    assert not any("facility" in a for a in briefing.supplier_missing({}, verified=True, address_known=True))


def test_po_email_asks_for_the_address_only_when_missing():
    po = SimpleNamespace(po_id="PO-1", supplier_name="Mill One", quantity_mt=20.0, agreed_price=910.0, currency="USD",
                         incoterm="FOB", total_value=18200.0, port_of_loading="Chennai", delivery_deadline="2026-12-20",
                         payment_terms="escrow", quality_spec=None, packing_spec=None, storage_uri=None)
    _, asked = notifications._tpl_po_issued({"deal_id": "D1", "po": po, "address_needed": True})
    _, plain = notifications._tpl_po_issued({"deal_id": "D1", "po": po, "address_needed": False})
    assert "3. send us the full address of the facility" in asked and "full address" not in plain


async def test_capture_records_a_new_address_but_never_overrides_an_operators(monkeypatch):
    party = SimpleNamespace(role="supplier", country="IN", address="Thanjavur, Tamil Nadu", contact_source="opportunity")
    deal = SimpleNamespace(parties=[party])
    monkeypatch.setattr("services.deals.service.get_deal", AsyncMock(return_value=deal))
    update = AsyncMock()
    monkeypatch.setattr("services.deals.service.update_party", update)
    found = await supplier_address.capture("D1", "Factory address: Plot 14, SIPCOT, Thanjavur 613005", by="e-mail")
    assert found == "Plot 14, SIPCOT, Thanjavur 613005"
    update.assert_awaited_once_with("D1", "supplier", {"address": found}, source=None)

    update.reset_mock()
    party.address, party.contact_source = "7 Operator Lane, Chennai 600002", "operator"
    assert await supplier_address.capture("D1", "Factory address: Plot 14, SIPCOT, Thanjavur 613005", by="e-mail") is None
    update.assert_not_awaited()


@pytest.fixture
def held(monkeypatch):
    monkeypatch.setenv("QC_FACILITY_ADDRESS_REQUIRED", "true")
    monkeypatch.setenv("QC_READINESS_DATE_REQUIRED", "false")
    sc.reset_for_tests()
    monkeypatch.setattr(sc, "_mongo", lambda: None)
    party = SimpleNamespace(role="supplier", party_id="S1", legal_name="Mill One", country="IN",
                            address="Thanjavur, Tamil Nadu", contact_source="opportunity")
    state = SimpleNamespace(party=party, deal=SimpleNamespace(deal_id="D1", status="PO_ISSUED", parties=[party]))
    monkeypatch.setattr("services.deals.service.get_deal", AsyncMock(side_effect=lambda _d: state.deal))
    state.notify = AsyncMock(return_value="EMAIL-1")
    monkeypatch.setattr(notifications, "notify_party", state.notify)
    state.qc = AsyncMock(return_value=SimpleNamespace(tpo_id="TPO-1", inspection_date="2026-12-08"))
    monkeypatch.setattr(continuations, "book_qc_for_deal", state.qc)
    return state


async def test_the_inspection_waits_for_the_address_and_asks_for_it_once(held):
    await sc.await_po_ack("D1", 20.0)
    msg = await sc.handle_supplier_reply("D1", "PO_ISSUED", "We acknowledge the purchase order.", "IN-1")
    held.qc.assert_not_awaited()
    assert "address" in msg
    kinds = [c.args[2] for c in held.notify.await_args_list]
    assert kinds == ["facility_address_request"]
    assert held.notify.await_args.args[3]["known"] == "Thanjavur, Tamil Nadu"
    assert await sc.request_facility_address("D1") is False  # asked once
    assert (await sc.get("D1")).facility_address_status == "awaiting"


async def test_the_suppliers_address_reply_books_the_inspection(held):
    await sc.await_po_ack("D1", 20.0)
    await sc.handle_supplier_reply("D1", "PO_ISSUED", "We acknowledge the purchase order.", "IN-1")
    held.party.address = "Plot 14, SIPCOT Industrial Estate, Thanjavur 613005"  # captured by the e-mail processor
    msg = await sc.handle_supplier_reply("D1", "PO_ISSUED", "Factory address: Plot 14, SIPCOT Industrial Estate, Thanjavur 613005", "IN-2")
    held.qc.assert_awaited_once_with("D1")
    assert "inspection booked" in msg and (await sc.get("D1")).facility_address_status == "received"


async def test_an_address_set_by_staff_books_the_waiting_inspection(held):
    await sc.await_po_ack("D1", 20.0)
    await sc.acknowledge_po("D1", by="ops")
    held.qc.assert_not_awaited()
    held.party.address = "Plot 14, SIPCOT Industrial Estate, Thanjavur 613005"
    assert (await sc.address_known("D1")).tpo_id == "TPO-1"


async def test_a_complete_address_books_at_once(held):
    held.party.address = "Plot 14, SIPCOT Industrial Estate, Thanjavur 613005"
    await sc.await_po_ack("D1", 20.0)
    await sc.acknowledge_po("D1", by="ops")
    held.qc.assert_awaited_once_with("D1")
    held.notify.assert_not_awaited()


async def test_switched_off_books_without_a_street_address(held, monkeypatch):
    monkeypatch.setenv("QC_FACILITY_ADDRESS_REQUIRED", "false")
    await sc.await_po_ack("D1", 20.0)
    await sc.acknowledge_po("D1", by="ops")
    held.qc.assert_awaited_once_with("D1")
