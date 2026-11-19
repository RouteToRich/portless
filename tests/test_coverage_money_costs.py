"""Money modules to 100 % (part 3): the trade cost sheet."""
from __future__ import annotations

from unittest.mock import patch

from shared.orchestration import trade_costs as tc


def _build(**kw):
    base = dict(quantity_mt=50, market="AE", freight_usd_per_mt=None, freight_is_quote=False, qc_usd_per_mt=None,
                qc_is_quote=False, supplier_address=None)
    base.update(kw)
    return tc.build(**base)


def test_settings_and_locating_the_supplier(monkeypatch):
    monkeypatch.setenv("TRADE_COST_CONTINGENCY_PCT", "abc")
    assert tc._f("TRADE_COST_CONTINGENCY_PCT", 0.5) == 0.5
    assert tc.locate_supplier("Plot 4, PIN 550001").state is None  # PIN prefix 55 is in no state's range
    assert tc.locate_supplier("an unknown place").state is None
    assert tc.locate_supplier("Factory in Gujarat").basis == "state"


def test_exw_to_fob():
    assert tc.exw_to_fob(300.0, "FOB") == 300.0
    assert tc.exw_to_fob(300.0, "EXW", port_km=0) > 300.0
    assert tc.exw_to_fob(300.0, "EXW") > tc.exw_to_fob(300.0, "EXW", port_km=0)


def test_build_with_every_optional_line(monkeypatch):
    monkeypatch.setenv("TRADE_COST_CONTINGENCY_PCT", "0")
    sheet = _build(inspection_types=["DUPRO", "PSI"], supplier_currency="INR", incentive_pct=2.0, hs_code="070310")
    keys = {line.key for line in sheet.lines}
    assert {"value_added_services", "fx_buffer", "export_incentive"} <= keys and "contingency" not in keys
    insp = next(line for line in sheet.lines if line.key == "inspection")
    assert "DUPRO + PSI" in insp.label and sheet.inspection_phases == 2
    assert any("ocean freight is an estimate" in w for w in sheet.warnings)
    monkeypatch.setenv("FX_BUFFER_PCT", "0")
    assert "fx_buffer" not in {line.key for line in _build(supplier_currency="INR").lines}


def test_build_survives_a_broken_catalogue():
    with patch("shared.catalogue.container_for", side_effect=RuntimeError("x")):
        sheet = _build(hs_code="070310")
    assert sheet.container_plan["mt_per_20"] == 20.0


def test_sheet_arithmetic_on_dicts_models_and_none():
    sheet = _build()
    assert tc._nums(None) == (0.0, 0.0)
    assert tc._nums(sheet) == (sheet.fixed_usd_per_mt, sheet.pct_of_price)
    assert tc.net_margin_pct(sheet, 0, 300) == 0.0
    assert tc.cost_usd({"lines": [{"key": "x", "paid_by": "PortLess", "usd_per_mt": 0, "pct_of_price": 0}]}, 400, 10) == {}
    assert tc._route_of(sheet) == sheet.supplier_route.model_dump()
    assert tc._route_of(None) == {}


def test_container_plan_is_used_for_origin_handling():
    sheet = _build(quantity_mt=60, supplier_price_basis="EXW", hs_code=None)
    handling = next(line for line in sheet.lines if line.key == "origin_handling")
    plan = sheet.container_plan
    assert handling.note == f"{plan['containers_20'] + plan['containers_40']} container(s)"
