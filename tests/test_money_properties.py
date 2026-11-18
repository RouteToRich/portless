"""Property-based tests (Hypothesis) for the money arithmetic: for any amount, quantity, fee
and price the invariants hold — tranches add up to the principal, the holdback never exceeds
what is owed, the minimum offer keeps the margin floor, the negotiation band and the
concession ladder never go below the floor."""
from __future__ import annotations

from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from agents.negotiation_agent.pricing import buyer_band, concession_ladder
from services.escrow_guard import service as eg

money = st.floats(min_value=0.01, max_value=50_000_000, allow_nan=False, allow_infinity=False)
price = st.floats(min_value=10.0, max_value=20_000.0, allow_nan=False, allow_infinity=False)
pct = st.floats(min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False)

SETTINGS = settings(max_examples=300, deadline=None)


@SETTINGS
@given(amount=money)
def test_tranches_add_up_to_the_principal_to_the_cent(amount):
    tranches = eg._build_tranches(amount, None)
    assert abs(sum(t.amount for t in tranches) - round(amount, 2)) < 0.005
    assert all(t.amount >= 0 for t in tranches)
    assert abs(sum(t.pct for t in tranches) - 1.0) < 1e-9


@SETTINGS
@given(amount=money, shares=st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=6))
def test_any_schedule_adds_up(amount, shares):
    from services.escrow_guard.schemas import TrancheInput
    total = sum(shares)
    pcts = [s / total for s in shares]
    schedule = [TrancheInput(tranche_id=f"T{i}", trigger="PO_ISSUED", pct=p) for i, p in enumerate(pcts, 1)]
    tranches = eg._build_tranches(amount, schedule)
    assert abs(sum(t.amount for t in tranches) - round(amount, 2)) < 0.005


@SETTINGS
@given(owed=money, released=st.booleans())
def test_the_holdback_never_exceeds_what_is_owed(owed, released):
    tranches = eg._build_tranches(owed * 1.2, None)
    if released:
        tranches = [t.model_copy(update={"status": "RELEASED"}) for t in tranches]
    hold = SimpleNamespace(tranches=tranches)
    held = eg._retained_until_acceptance(hold, owed)
    assert 0.0 <= held <= owed + 0.005
    if released:
        assert held == 0.0


@SETTINGS
@given(fob=price, qty=st.floats(min_value=1, max_value=5000), floor=pct, freight=st.floats(min_value=0, max_value=500))
def test_the_minimum_offer_keeps_the_margin_floor(fob, qty, floor, freight):
    from shared.orchestration.landed_cost import landed
    lc = landed(fob, "AE", qty, incoterm="CIF", margin_floor_pct=floor, freight_usd_per_mt=freight)
    margin = lc.min_offer_usd_per_mt - lc.cost_usd_per_mt
    assert margin >= lc.min_offer_usd_per_mt * floor / 100.0 - 0.02
    assert lc.min_offer_usd_per_mt >= lc.cost_usd_per_mt - 0.01


@SETTINGS
@given(supplier=price, costs=st.floats(min_value=0, max_value=500), floor=st.floats(min_value=0, max_value=15),
       extra=st.floats(min_value=0, max_value=10), buyer=price,
       last=st.one_of(st.none(), price), round_no=st.integers(min_value=0, max_value=6))
def test_the_buyer_band_never_goes_below_the_walk_away_price(supplier, costs, floor, extra, buyer, last, round_no):
    band = buyer_band(buyer_offer=buyer, supplier_price=supplier, costs=costs, floor_pct=floor,
                      target_pct=floor + extra, last_offer_to_buyer=last, round_no=round_no, max_rounds=6)
    walk = (supplier + costs) / max(1.0 - floor / 100.0, 0.01)
    assert band.low >= walk - 0.01
    assert band.low <= band.recommended <= band.high


@SETTINGS
@given(walk=price, gap=st.floats(min_value=0, max_value=5000), counter=st.one_of(st.none(), price),
       moves=st.sampled_from([2, 3]))
def test_the_concession_ladder_descends_and_stops_at_the_floor(walk, gap, counter, moves):
    ask = walk + gap
    out = concession_ladder(ask=ask, walk_away=walk, counter=counter, moves=moves)
    prices = [s["price"] for s in out["steps"]]
    assert all(p >= walk - 0.005 for p in prices)
    assert all(a >= b for a, b in zip([out["ask"], *prices], prices))
    assert all(s["concede_usd"] >= 0 for s in out["steps"])


@SETTINGS
@given(usd=st.floats(min_value=0, max_value=10_000_000), rate=st.floats(min_value=50, max_value=150))
def test_the_local_figure_is_the_usd_figure_times_the_rate(usd, rate):
    from shared.currency import money as fmt
    text = fmt(usd, "INR", rate)
    local = int(text.split(" ")[1].replace(",", ""))
    assert abs(local - usd * rate) <= 0.5 + 1e-6 * usd * rate
    assert f"USD {usd:,.2f}" in text
