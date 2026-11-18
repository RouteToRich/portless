"""Quantity against supply on the prospect desk: the chosen supplier's capacity / minimum vs the
buyer's quantity, pointing to a second supplier (shared/orchestration/prospect_desk.supply_gap)."""
from __future__ import annotations

from types import SimpleNamespace

from shared.orchestration.prospect_desk import supply_gap


def _buyer(qty, days=None):
    return SimpleNamespace(candidate_id="C1", status="interested",
                           requirement=SimpleNamespace(quantity_mt=qty, delivery_by_days=days))


def _offer(sid, price, cap=None, moq=None, qty=None, name=None):
    return SimpleNamespace(supplier_id=sid, company_name=name or sid, fob_price_usd_per_mt=price,
                           capacity_mt_per_month=cap, moq_mt=moq, quantity_mt=qty)


DEAL = SimpleNamespace(quantity_mt=50.0)


def test_capacity_short_points_to_second_supplier():
    offers = [_offer("S1", 900, cap=20, name="Shree"), _offer("S2", 930, cap=40, name="Ganesh"), _offer("S3", 990)]
    gap = supply_gap(DEAL, [_buyer(60, 30)], offers)
    assert gap["gap_mt"] == 40.0 and gap["kind"] == "capacity"
    assert "Shree can deliver about 20 MT in 30 days" in gap["note"] and "Ganesh (up to 40 MT at 930 FOB)" in gap["note"]


def test_longer_window_covers_it():
    assert supply_gap(DEAL, [_buyer(60, 90)], [_offer("S1", 900, cap=20)]) is None  # 3 months × 20


def test_offered_quantity_caps_capacity_and_no_alternative():
    gap = supply_gap(DEAL, [_buyer(50)], [_offer("S1", 900, cap=100, qty=30, name="Shree")])
    assert gap["gap_mt"] == 20.0 and "ask Shree for its maximum" in gap["note"]


def test_moq_above_buyer_quantity():
    gap = supply_gap(DEAL, [_buyer(10)], [_offer("S1", 900, moq=25, name="Shree"), _offer("S2", 950, moq=5, name="Ganesh")])
    assert gap["kind"] == "moq" and "offer the buyer 25 MT or use Ganesh" in gap["note"]


def test_nothing_stated_or_no_offers():
    assert supply_gap(DEAL, [_buyer(60)], [_offer("S1", 900)]) is None
    assert supply_gap(DEAL, [_buyer(60)], []) is None


def test_desk_row_carries_the_action():
    from datetime import datetime, timezone

    from shared.orchestration.prospect_desk import summarise
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    deal = SimpleNamespace(deal_id="D1", hs_code="0904", market="AE", status="MATCHING", created_at=now, updated_at=now,
                           quantity_mt=50.0, buyer_candidates=[_buyer(60, 30)],
                           supplier_offers=[_offer("S1", 900, cap=20, name="Shree"), _offer("S2", 930, cap=40, name="Ganesh")],
                           radar_estimate={})
    for o in deal.supplier_offers:
        o.received_at = now
    row = summarise(deal, None, now=now)
    assert row.supply_gap_mt == 40.0 and any(a.kind == "second_supplier" for a in row.actions)
