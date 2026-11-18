"""Supplier price dips and spikes from the price book (shared/orchestration/price_alerts.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from shared.orchestration import price_alerts as pa

NOW = datetime.now(UTC)


def _obs(days_ago, price):
    return SimpleNamespace(price_usd_per_mt=price, observed_at=NOW - timedelta(days=days_ago))


def test_a_dip_and_a_spike():
    base = [_obs(d, 800.0) for d in (20, 30, 40)]
    dip = pa.compare(base + [_obs(d, 720.0) for d in (1, 3, 5)], NOW)
    assert dip["kind"] == "dip" and dip["change_pct"] == -10.0 and "lock the PO" in dip["advice"]
    spike = pa.compare(base + [_obs(d, 880.0) for d in (1, 3, 5)], NOW)
    assert spike["kind"] == "spike" and spike["change_pct"] == 10.0


def test_small_moves_and_thin_data_are_quiet(monkeypatch):
    base = [_obs(d, 800.0) for d in (20, 30, 40)]
    assert pa.compare(base + [_obs(d, 780.0) for d in (1, 3, 5)], NOW) is None    # -2.5 %
    assert pa.compare(base + [_obs(1, 600.0), _obs(2, 600.0)], NOW) is None       # 2 recent points
    assert pa.compare([_obs(d, 800.0) for d in (100, 120, 130)] + [_obs(1, 600.0)] * 3, NOW) is None  # base too old
    monkeypatch.setenv("PRICE_ALERT_PCT", "2")
    assert pa.compare(base + [_obs(d, 780.0) for d in (1, 3, 5)], NOW)["kind"] == "dip"


async def test_alerts_per_product_and_in_the_digest(monkeypatch):
    monkeypatch.setattr(pa, "_hs_codes", AsyncMock(return_value=["090422", "100630"]))
    data = {"090422": [_obs(d, 800.0) for d in (20, 30, 40)] + [_obs(d, 700.0) for d in (1, 2, 3)],
            "100630": [_obs(d, 500.0) for d in (20, 30, 40, 1, 2, 3)]}
    monkeypatch.setattr("shared.pricebook.observations", AsyncMock(side_effect=lambda hs, side: data[hs]))
    alerts = await pa.alerts(NOW)
    assert [a["hs_code"] for a in alerts] == ["090422"]
    from shared.orchestration import digest
    text = digest.render_digest_text({"generated_at": NOW.isoformat(), "price_alerts": alerts})
    assert "PRICE ALERTS (1):" in text and "HS 090422 supplier FOB -12.5% (800 → 700 USD/MT)" in text
