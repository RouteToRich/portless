"""Property-based tests (Hypothesis) for the text readers whose output decides money or dates:
USD prices in e-mails, the supplier's goods-ready date, the facility address with its PIN."""
from __future__ import annotations

from datetime import date, timedelta

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from agents.negotiation_agent.guards import extract_usd_prices
from shared.orchestration.qc_schedule import parse_ready_date
from shared.orchestration.supplier_address import parse as parse_address

SETTINGS = settings(max_examples=300, deadline=None)
cents = st.integers(min_value=1000, max_value=9_999_999).map(lambda c: c / 100)  # USD 10.00 … 99,999.99
TODAY = date(2026, 11, 18)


@SETTINGS
@given(price=cents, fmt=st.sampled_from(["USD {p:,.2f}", "USD {p:.2f}", "${p:,.2f}", "US$ {p:,.2f}",
                                         "{p:,.2f} USD", "{p:.2f} dollars", "usd {p:,.2f}"]),
       before=st.sampled_from(["", "We can offer ", "Our best price is ", "Price: "]),
       after=st.sampled_from(["", " per MT CIF Hamburg.", " FOB, valid 7 days", " /MT"]))
def test_a_usd_price_written_any_usual_way_is_read_exactly(price, fmt, before, after):
    text = before + fmt.format(p=price) + after
    assert price in extract_usd_prices(text)


@SETTINGS
@given(a=cents, b=cents)
def test_two_prices_are_both_read_in_order(a, b):
    assert extract_usd_prices(f"Last year USD {a:,.2f}, now USD {b:,.2f} per MT") == [a, b]


@SETTINGS
@given(text=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200))
def test_text_without_a_currency_never_yields_a_price(text):
    assume(not any(t in text.lower() for t in ("usd", "$", "dollar", "us$")))
    assert extract_usd_prices(text) == []


@SETTINGS
@given(days=st.integers(min_value=1, max_value=170),
       fmt=st.sampled_from(["%Y-%m-%d", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y", "%B %d, %Y"]),
       lead=st.sampled_from(["Goods ready on ", "The lot will be packed by ", "Ready: ", ""]))
def test_a_ready_date_within_six_months_is_read_exactly(days, fmt, lead):
    d = TODAY + timedelta(days=days)
    assert parse_ready_date(lead + d.strftime(fmt), on=TODAY) == d


@SETTINGS
@given(days=st.integers(min_value=200, max_value=2000))
def test_a_date_beyond_six_months_or_in_the_past_is_not_taken(days):
    far = TODAY + timedelta(days=days)
    past = TODAY - timedelta(days=days)
    assert parse_ready_date(f"ready on {far:%Y-%m-%d}", on=TODAY) is None
    assert parse_ready_date(f"ready on {past:%Y-%m-%d}", on=TODAY) is None


@SETTINGS
@given(plot=st.integers(min_value=1, max_value=999), pin=st.integers(min_value=110001, max_value=855999),
       town=st.sampled_from(["Thanjavur", "Kochi", "Indore", "Guntur", "Nashik"]))
def test_a_labelled_facility_address_keeps_its_pin(plot, pin, town):
    found = parse_address(f"Factory address: Plot {plot}, Industrial Estate, {town} {pin}")
    assert found is not None and str(pin) in found and town in found
