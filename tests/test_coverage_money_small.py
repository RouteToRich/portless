"""Money modules to 100 % (part 1): pricing edges, chargebacks, escrow interest, currency."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient


class _Broken:
    def find(self, *a, **k):
        raise RuntimeError("store down")


# ── agents/negotiation_agent/pricing.py ────────────────────────────────────

def test_supplier_band_final_round_and_ladder_refusal():
    from agents.negotiation_agent.pricing import concession_ladder, supplier_band
    band = supplier_band(supplier_quote=500.0, ceiling=480.0, last_offer_to_supplier=450.0, round_no=4, max_rounds=5)
    assert band.final_offer is True and band.recommended == 480.0  # last round: our ceiling (below their quote)
    with pytest.raises(ValueError):
        concession_ladder(ask=0, walk_away=400)


# ── shared/orchestration/chargebacks.py ────────────────────────────────────

async def test_chargebacks_in_mongo_and_store_failures():
    from shared.orchestration import chargebacks as cb
    col = AsyncMongoMockClient()["t"]["supplier_chargebacks"]
    cb.set_collection_for_tests(col)
    try:
        assert await cb.record("D", "reinspection", 0, reason="none") is None
        row = await cb.record("D", "reinspection", 300, reason="QC failed", ref="QC-2", by="ops")
        assert (await cb.record("D", "reinspection", 300, reason="again", ref="QC-2")) == row  # same ref → once
        assert (await cb.record("D", "reinspection", 50, reason="second", ref="QC-3"))["ref"] == "QC-3"
        assert await cb.total("D") == 350.0
        cb.set_collection_for_tests(_Broken())
        assert await cb.listed("D") == []
    finally:
        cb.set_collection_for_tests(None)
    cb._resolved = False  # noqa: SLF001
    with patch("shared.mongo.collection", side_effect=RuntimeError("no mongo")):
        assert cb._collection() is None  # noqa: SLF001
    cb.set_collection_for_tests(None)


async def test_reinspection_cost_sources():
    from shared.orchestration import chargebacks as cb
    assert await cb.reinspection_cost("D", SimpleNamespace(provider_payload={"total_usd": 410})) == 410.0
    session = SimpleNamespace(cost_sheet={"quantity_mt": 50, "inspection_phases": 2,
                                          "lines": [{"key": "inspection", "usd_per_mt": 12.0}]})
    repo = SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session))
    with patch("agents.negotiation_agent.repository.get_repository", return_value=repo):
        assert await cb.reinspection_cost("D") == 300.0
    with patch("agents.negotiation_agent.repository.get_repository", side_effect=RuntimeError("x")):
        assert await cb.reinspection_cost("D") > 0  # the setting


# ── shared/orchestration/escrow_interest.py ────────────────────────────────

async def test_escrow_interest_refusals_mongo_and_failures():
    from shared.orchestration import escrow_interest as ei
    with pytest.raises(ValueError, match="number"):
        await ei.record("D", "abc", period=None, ref="S1", belongs_to="portless", by="ops")
    col = AsyncMongoMockClient()["t"]["escrow_interest"]
    with patch.object(ei, "_collection", return_value=col), \
            patch("shared.audit_log.record", side_effect=RuntimeError("audit down")):
        await ei.record("D", 12.5, period="2026-10", ref="ST-1", belongs_to="portless", by="ops")
        assert await ei.income("D") == 12.5
    with patch.object(ei, "_collection", return_value=_Broken()):
        assert await ei.listed("D") == []
    with patch("shared.mongo.collection", side_effect=RuntimeError("no mongo")):
        assert ei._collection() is None


# ── shared/currency.py ─────────────────────────────────────────────────────

async def test_currency_fallbacks():
    from shared import currency
    with patch("shared.fx_rates.has_rate", side_effect=RuntimeError("x")):
        assert currency.party_currency("DE") == "USD"
    with patch("shared.fx_rates.has_rate", return_value=False):
        assert currency.party_currency("DE") == "USD"
    assert currency.party_currency("ZZ") == "USD"
    assert currency.party_currency("IN") == "INR"
    with patch("shared.fx_rates.has_rate", return_value=True):
        assert currency.party_currency("DE") == "EUR"
    with patch("shared.fx_rates.cached", side_effect=RuntimeError("x")):
        assert currency.fallback_rate("INR") > 0
        with patch("agents.negotiation_agent.config.settings", new=SimpleNamespace()):
            assert currency.fallback_rate("INR") == 96.0
        assert currency.fallback_rate("EUR") is None
    with patch("shared.fx_rates.cached", return_value={"rate": 0.9}):
        assert currency.fallback_rate("EUR") == 0.9
    assert await currency.usd_rate("USD") == 1.0
    with patch("agents.negotiation_agent.market_intel.get_usd_inr_rate", new=AsyncMock(return_value=88.0)):
        assert await currency.usd_rate("INR") == 88.0
    with patch("agents.negotiation_agent.market_intel.get_usd_inr_rate", side_effect=RuntimeError("x")), \
            patch("shared.fx_rates.cached", return_value={"rate": 90.0}):
        assert await currency.usd_rate("INR") == 90.0
    with patch("shared.fx_rates.usable", new=AsyncMock(return_value={"rate": 0.92})):
        assert await currency.usd_rate("EUR") == 0.92
    with patch("shared.fx_rates.usable", new=AsyncMock(return_value=None)):
        assert await currency.usd_rate("EUR") is None
    with patch("shared.fx_rates.usable", side_effect=RuntimeError("x")):
        assert await currency.usd_rate("EUR") is None


async def test_rate_info_sources():
    from shared import currency
    with patch("agents.negotiation_agent.market_intel.fetch_usd_inr_rate", new=AsyncMock(return_value=87.5)):
        assert (await currency.rate_info("INR"))["source"] == "live"
    with patch("agents.negotiation_agent.market_intel.fetch_usd_inr_rate", side_effect=RuntimeError("x")), \
            patch("shared.fx_rates.usable", new=AsyncMock(return_value={"rate": 88.0, "source": "ecb", "as_of": "d"})):
        assert (await currency.rate_info("INR"))["source"] == "reference"
    with patch("shared.fx_rates.usable", side_effect=RuntimeError("x")), \
            patch("shared.fx_rates.cached", return_value=None):
        assert (await currency.rate_info("EUR")) == {"currency": "EUR", "rate": None, "source": "none"}


def test_money_and_quoted_currency():
    from shared import currency
    assert currency.money("n/a") == "USD n/a"
    assert currency.money(10) == "USD 10.00"
    assert currency.money(10, "INR", 88.0) == "INR 880 (USD 10.00)"
    assert currency.rate_note("USD", None) == "" and "INR figures" in currency.rate_note("INR", 88.0)
    assert currency.quoted_currency(None) is None
    assert currency.quoted_currency("Rs 4500 per quintal") == "INR"
    assert currency.quoted_currency("USD 900 or INR 75,000") is None
    assert currency.quoted_currency("USD 900 FOB") == "USD"
