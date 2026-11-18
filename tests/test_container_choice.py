"""Cheapest containers per tonne without wasting space (todo: freight APIs and container choice)."""
from __future__ import annotations

import pytest

from shared.orchestration import container_choice as cc


def test_weight_limited_cargo_gains_nothing_from_a_high_cube():
    dry = cc.EQUIPMENT["dry"]
    assert cc.tonnes_per_box(dry[0], 1.4) == 23.57      # 33 m³ / 1.4 — volume caps a 20' of bagged rice
    assert cc.tonnes_per_box(dry[2], 1.4) == 26.5        # a 40' HC is weight-limited at its payload
    assert cc.tonnes_per_box(dry[2], 4.0) == 19.0        # light cargo: 76 m³ / 4 — volume-limited


def test_cheapest_mix_and_fill():
    options = [{"code": "20DRY", "tonnes": 20.0, "usd": 1000.0}, {"code": "40HC", "tonnes": 26.5, "usd": 1400.0}]
    plan = cc.plan_mix(40.0, options)
    assert plan["boxes"] == {"20DRY": 2} and plan["usd"] == 2000.0 and plan["fill_pct"] == 100.0
    plan = cc.plan_mix(26.0, options)
    assert plan["boxes"] == {"40HC": 1} and plan["usd_per_mt"] == pytest.approx(53.85, abs=0.01)
    small = cc.plan_mix(8.0, options)
    assert small["boxes"] == {"20DRY": 1} and small["fill_pct"] == 40.0 and small["fill_suggestion_mt"] == 12.0
    assert cc.plan_mix(0, options)["boxes"] == {}


def test_light_cargo_goes_in_high_cubes():
    # dried chilli: 4 m³ / t → 20' carries 8.25 t, 40' HC 19 t
    options = [{"code": "20DRY", "tonnes": 8.25, "usd": 1000.0}, {"code": "40HC", "tonnes": 19.0, "usd": 1400.0}]
    plan = cc.plan_mix(38.0, options)
    assert plan["boxes"] == {"40HC": 2} and plan["usd"] == 2800.0


async def test_choose_uses_lane_quotes_per_size(monkeypatch):
    from services.trade_min import freight_lanes as fl
    fl.reset_for_tests()
    await fl.save(fl.LaneRate(market="AE", container_type="dry", usd_per_container=900.0, container_mt=20.0, provider="staff"))
    await fl.save(fl.LaneRate(market="AE", container_type="hc40", usd_per_container=1100.0, container_mt=26.5, provider="staff"))
    plan = await cc.choose("AE", None, 52.0)
    assert plan["quoted"] == ["20DRY", "40HC"] and plan["estimated"] == ["40DRY"]
    assert plan["boxes"] == {"40HC": 2} and plan["usd"] == 2200.0
    fl.reset_for_tests()
