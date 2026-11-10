"""Every PortLess cost in the price: cost sheet, margin on the supplier's own
price, the books, the pre-trade market check, and price explanations."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.orchestration import market_check, trade_costs


def _sheet(**kw):
    base = dict(quantity_mt=20.0, market="AE", freight_usd_per_mt=142.5, freight_is_quote=False,
                qc_usd_per_mt=17.5, qc_is_quote=False, supplier_address="Plot 4, GT Road, Karnal 132001, Haryana",
                supplier_price_basis="FOB", escrow_fee_pct=1.0, usd_inr_rate=96.0)
    return trade_costs.build(**{**base, **kw})


# supplier location ---------------------------------------------------------------

@pytest.mark.parametrize(("address", "state", "port"), [
    ("Plot 4, GT Road, Karnal 132001", "Haryana", "Mundra"),
    ("12 Mill Street, Kakinada, AP", "Andhra Pradesh", "Kakinada"),
    ("Industrial Estate, Tamil Nadu", "Tamil Nadu", "Chennai"),
    ("SIDCO, 600032", "Tamil Nadu", "Chennai"),
])
def test_supplier_location_to_port(address, state, port):
    route = trade_costs.locate_supplier(address)
    assert (route.state, route.port) == (state, port) and route.km


def test_unknown_location_is_said_to_be_rough():
    sheet = _sheet(supplier_address=None, supplier_price_basis="EXW")
    assert any("location unknown" in w for w in sheet.warnings)


# the sheet ----------------------------------------------------------------------

def test_fob_supplier_pays_inland_and_exw_supplier_does_not():
    fob, exw = _sheet(), _sheet(supplier_price_basis="EXW")
    inland_fob = next(l for l in fob.lines if l.key == "inland_transport")
    inland_exw = next(l for l in exw.lines if l.key == "inland_transport")
    assert inland_fob.usd_per_mt == 0.0 and "inside FOB" in inland_fob.paid_by and "1,200 km" in inland_fob.note
    # Karnal → Mundra ~1,200 km × ₹3.5 + ₹250 handling ÷ 96 ≈ USD 46.35/MT
    assert inland_exw.usd_per_mt == pytest.approx((1200 * 3.5 + 250) / 96, abs=0.01)
    assert exw.fixed_usd_per_mt > fob.fixed_usd_per_mt


def test_every_portless_cost_is_on_the_sheet():
    sheet = _sheet()
    keys = {l.key for l in sheet.lines if l.paid_by == "PortLess"}
    assert {"customs_agent", "export_documents", "ocean_freight", "inspection", "marine_insurance",
            "escrow_fee", "contingency"} <= keys
    # 150 + 120 per shipment over 20 MT, + freight 142.5 + inspection 17.5
    assert sheet.fixed_usd_per_mt == pytest.approx(7.5 + 6.0 + 142.5 + 17.5, abs=0.01)
    assert sheet.pct_of_price == pytest.approx(0.5 + 1.0 + 0.5)
    assert any("freight is an estimate" in w for w in sheet.warnings)


def test_margin_maths_is_consistent():
    sheet = _sheet().model_dump(mode="json")
    low = trade_costs.min_buyer_price(sheet, 1010.0, 8.0)
    assert trade_costs.net_margin_pct(sheet, low, 1010.0) == pytest.approx(8.0, abs=1e-6)
    top = trade_costs.max_supplier_price(sheet, low, 8.0)
    assert top == pytest.approx(1010.0, abs=1e-6)


def test_last_runs_deal_is_shown_as_the_loss_maker_it_was():
    # UC-01 2026-11-10: buyer 1,225 CIF, supplier 1,010 FOB. Spread 215/MT − fixed 173.5/MT
    # − 2 % of 1,225 (insurance, escrow fee, contingency) = 17/MT → 1.4 % net, far below the 8 % floor.
    margin = trade_costs.net_margin_pct(_sheet().model_dump(mode="json"), 1225.0, 1010.0)
    assert margin == pytest.approx(1.39, abs=0.01)


def test_buyer_is_told_what_the_price_includes_but_no_amounts():
    basis = " ".join(trade_costs.buyer_price_basis(_sheet().model_dump(mode="json"), "sgs"))
    assert "ocean freight from the port of Mundra" in basis and "insurance" in basis and "SGS" in basis
    assert not any(ch.isdigit() for ch in basis)


# margin on the supplier's own price (fix a) -------------------------------------

async def test_agreement_is_judged_on_the_suppliers_price_not_our_counter():
    from agents.negotiation_agent.graph import orchestrator_node
    from tests.test_negotiation_agent import _orch_chat, _session, _state
    session = _session(round_no=4, current_bid=1225.0, current_ask=957.05, supplier_quote_price=1000.0,
                       supplier_last_quote=1010.0, buyer_last_offer=1225.0,
                       cost_sheet=_sheet().model_dump(mode="json"))
    state = _state(session, party="buyer", inbound_message="We accept your offer and wish to proceed.",
                   current_bid=1225.0, current_ask=957.05)
    with patch("agents.negotiation_agent.graph._chat", _orch_chat("CALL_BUYER_AGENT", 9.0)):
        result = await orchestrator_node(state)
    assert result["routing"] != "NEGOTIATION_AGREED"
    assert result["current_margin"] < 8.0


def test_buyer_band_floor_includes_percentage_costs():
    from agents.negotiation_agent.pricing import buyer_band
    plain = buyer_band(buyer_offer=1200, supplier_price=1010, costs=173.5, floor_pct=8, target_pct=12,
                       last_offer_to_buyer=None, round_no=0, max_rounds=5)
    full = buyer_band(buyer_offer=1200, supplier_price=1010, costs=173.5, floor_pct=8, target_pct=12,
                      last_offer_to_buyer=None, round_no=0, max_rounds=5, pct_costs=2.0)
    assert full.low > plain.low and full.recommended > plain.recommended


# the books (fix c) ------------------------------------------------------------------

async def test_ledger_counts_only_incurred_costs_and_real_profit(monkeypatch):
    from services.escrow_guard import service as eg
    sheet = _sheet().model_dump(mode="json")
    session = SimpleNamespace(cost_sheet=sheet, quantity_mt=20.0)
    monkeypatch.setattr("agents.negotiation_agent.repository.get_repository",
                        lambda: SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session)))
    orders = [SimpleNamespace(order_type="QC", status="COMPLETED", provider_payload={"fee_usd": 360.0}),
              SimpleNamespace(order_type="FREIGHT", status="BOOKED", provider_payload={})]
    monkeypatch.setattr("services.shield_link.service.list_deal_orders",
                        AsyncMock(return_value=SimpleNamespace(orders=orders)))
    monkeypatch.setattr("services.dealmaker.repository.get_repository",
                        lambda: SimpleNamespace(get_po_by_deal=AsyncMock(return_value=object())))
    costs, basis = await eg._portless_costs("D1", 24500.0)
    assert costs["inspection"] == 360.0 and basis["inspection"] == "actual"
    assert costs["ocean_freight"] == pytest.approx(142.5 * 20) and basis["ocean_freight"] == "estimate"
    assert costs["marine_insurance"] == pytest.approx(24500 * 0.005, abs=0.01)
    assert "customs_agent" not in costs          # not filed yet
    assert "escrow_fee" not in costs and "contingency" not in costs
    assert "inland_transport" not in costs       # FOB supplier: inside its price


def test_journal_books_the_costs_and_still_balances():
    from services.escrow_guard.journal import journal_from
    hold = SimpleNamespace(currency="USD", funded_amount=24500.0, amount=24500.0, status="FUNDED", shortfall=None,
                           funded_at=None, ext_ref=None, provider="tazapay", tranches=[], refunded_total=0.0,
                           deal_id="D1", escrow_id="E1", fee_pct=1.0, released_total=0.0, fees_total=0.0,
                           provider_payload={}, updated_at=None)
    ledger = SimpleNamespace(supplier_owed=None, payouts=[], outstanding=24500.0, issues=[],
                             portless_costs={"ocean_freight": 2850.0, "inspection": 360.0},
                             portless_costs_basis={"ocean_freight": "estimate", "inspection": "actual"})
    j = journal_from(hold, ledger, now=datetime.now(timezone.utc))
    assert j.balanced and j.balances["trade_costs"] == pytest.approx(3210.0)
    assert j.balances["service_payables"] == pytest.approx(-3210.0)


async def test_profit_breakdown_from_sale_to_net(monkeypatch):
    from services.deals import router as deals_router
    sheet = _sheet().model_dump(mode="json")
    session = SimpleNamespace(cost_sheet=sheet, quantity_mt=20.0, agreed_price=1225.0,
                              supplier_last_quote=910.0, supplier_quote_price=900.0)
    pi = SimpleNamespace(quantity_mt=20.0, agreed_price=1225.0, total_value=24500.0)
    po = SimpleNamespace(agreed_price=910.0, total_value=18200.0)
    monkeypatch.setattr(deals_router, "_org_deal", AsyncMock())
    monkeypatch.setattr("agents.negotiation_agent.repository.get_repository",
                        lambda: SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session)))
    monkeypatch.setattr("services.dealmaker.repository.get_repository",
                        lambda: SimpleNamespace(get_pi_by_deal=AsyncMock(return_value=pi),
                                                get_po_by_deal=AsyncMock(return_value=po)))
    ledger = SimpleNamespace(portless_costs={"ocean_freight": 2850.0, "inspection": 360.0},
                             portless_costs_basis={"ocean_freight": "estimate", "inspection": "actual"},
                             settled=True, fees_total=245.0, realized_margin=2350.0)
    monkeypatch.setattr("services.escrow_guard.service.get_deal_ledger", AsyncMock(return_value=ledger))
    out = await deals_router.deal_profit("D1", user=SimpleNamespace())
    assert out["gross_spread"] == 6300.0 and out["gross_spread_pct"] == pytest.approx(25.71, abs=0.01)
    status = {c["key"]: c["status"] for c in out["costs"]}
    assert status == {"customs_agent": "estimate", "export_documents": "estimate", "ocean_freight": "incurred",
                      "inspection": "actual", "marine_insurance": "estimate", "escrow_fee": "actual"}
    assert "contingency" not in status
    assert out["net_profit"] == pytest.approx(out["gross_spread"] - out["costs_total"])
    assert out["net_profit"] == pytest.approx(6300 - (150 + 120 + 2850 + 360 + 122.5 + 245))


# pre-trade market check ---------------------------------------------------------------

async def test_market_check_keeps_only_sourced_facts(monkeypatch):
    monkeypatch.setenv("MARKET_CHECK_ENABLED", "true")
    market_check.reset_for_tests()
    monkeypatch.setattr(market_check, "_mongo", lambda: None)
    heads = [market_check.Headline(title="India lifts curbs on non-basmati rice exports", url="https://news.example/1",
                                   source="Example News", published="2026-10-20"),
             market_check.Headline(title="Red Sea disruption pushes container rates up", url="https://news.example/2",
                                   published="2026-11-02")]
    monkeypatch.setattr(market_check, "search", AsyncMock(return_value=(heads, "google_news")))
    monkeypatch.setattr(market_check, "_judge", AsyncMock(return_value={
        "verdict": "caution", "summary": "Exports open; freight rising.", "reasons": ["policy open"],
        "risks": ["freight"],
        "facts": [{"fact": "India removed restrictions on non-basmati rice exports.", "headline": 0, "audience": ["buyer"]},
                  {"fact": "Invented fact", "headline": 9}],
    }))
    check = await market_check.for_deal("D1", "10063020", "AE")
    assert check.verdict == "caution" and len(check.facts) == 1
    assert check.facts[0].source_url == "https://news.example/1" and check.facts[0].published == "2026-10-20"
    again = await market_check.for_deal("D1", "10063020", "AE")
    assert again.checked_at == check.checked_at          # cached, not re-searched
    market_check.search.assert_awaited_once()


async def test_market_check_without_news_says_so(monkeypatch):
    monkeypatch.setenv("MARKET_CHECK_ENABLED", "true")
    market_check.reset_for_tests()
    monkeypatch.setattr(market_check, "_mongo", lambda: None)
    monkeypatch.setattr(market_check, "search", AsyncMock(return_value=([], None)))
    check = await market_check.for_deal("D1", "10063020", "AE", refresh=True)
    assert check.verdict == "unchecked" and "No current news" in check.summary


async def test_market_check_off_in_tests_by_default():
    assert await market_check.for_deal("D1", "10063020", "AE") is None


def test_google_news_feed_is_parsed(monkeypatch):
    import asyncio
    rss = ("<rss><channel><item><title>Rice export prices rise</title><link>https://n.example/a</link>"
           "<pubDate>Mon, 09 Nov 2026 08:00:00 GMT</pubDate><source url='x'>Agri Daily</source></item></channel></rss>")

    class Resp:
        status_code = 200
        text = rss

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return Resp()

    monkeypatch.setattr("httpx.AsyncClient", Client)
    rows = asyncio.new_event_loop().run_until_complete(market_check._google_news("rice"))
    assert rows[0].title == "Rice export prices rise" and rows[0].published == "2026-11-09" and rows[0].source == "Agri Daily"


def test_range_does_not_collapse_when_the_buyers_target_is_high():
    from agents.negotiation_agent.pricing import PriceBand, indicative_range
    band = PriceBand(1250.0, 1250.0, 1250.0, False, "opening: meet the buyer's own target")  # low = their target
    assert indicative_range(band) == (1250.0, 1250.0)                 # old behaviour: no range at all
    assert indicative_range(band, floor=1191.06) == (1225.0, 1250.0)  # halfway down to the real floor


async def test_buyer_draft_with_a_figure_above_our_ceiling_goes_to_a_person(monkeypatch):
    from agents.negotiation_agent import graph
    from tests.test_negotiation_agent import _session, _state
    session = _session(round_no=0, current_bid=1250.0, current_ask=900.0, supplier_quote_price=900.0,
                       cost_sheet=_sheet().model_dump(mode="json"))
    state = _state(session, party="init", current_bid=1250.0, current_ask=900.0)

    async def fake_chat_json(prompt, payload, caller, fallback):
        return {"counter_price_usd": payload["pricing"]["recommended_price_usd_per_mt"],
                "outbound_message": "Dear team,\n\nOur indicative price is USD 1,290–1,320 per MT CIF, which includes freight.",
                "negotiated_terms": {}, "reasoning": "copied a range"}

    monkeypatch.setattr(graph, "_chat_json", fake_chat_json)
    monkeypatch.setattr(graph, "_knowledge_context", AsyncMock(return_value=""))
    out = await graph.buyer_agent_node(state)
    assert "price_out_of_band" in out["buyer_draft_flags"]


# explaining every quoted price ----------------------------------------------------------

def test_price_without_basis_is_flagged():
    from agents.negotiation_agent.graph import _quotes_price_without_basis
    assert _quotes_price_without_basis("We can offer USD 1,290 per MT CIF.")
    assert not _quotes_price_without_basis("We can offer USD 1,290 per MT CIF, which includes freight and insurance.")
    assert not _quotes_price_without_basis("Thank you for your message.")


def test_supplier_basis_is_read_from_its_email():
    from agents.negotiation_agent.service import supplier_basis_from
    assert supplier_basis_from("Our price is INR 92,000 ex-works Karnal.") == "EXW"
    assert supplier_basis_from("USD 1,010 per MT FOB Chennai") == "FOB"
    assert supplier_basis_from("Thanks, noted.", "EXW") == "EXW"
