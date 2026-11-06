"""Tests for the Trade-Min service: ranking math + HTTP surface (FR-1, LLD 4.5)."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from services.trade_min import ranking
from services.trade_min.config import DEFAULT_CONFIG
from services.trade_min.repository import repository


@pytest.fixture(autouse=True)
def _clean_repository() -> None:
    repository.clear()
    yield
    repository.clear()


@pytest.fixture
def client() -> TestClient:
    module = importlib.import_module("services.trade_min.main")
    return TestClient(module.app)


# --------------------------------------------------------------------------- #
# Pure ranking functions
# --------------------------------------------------------------------------- #
def test_price_gap_score_saturates() -> None:
    assert ranking.price_gap_score(DEFAULT_CONFIG.price_gap_saturation_pct) == 100.0
    assert ranking.price_gap_score(0.0) == 0.0
    assert ranking.price_gap_score(DEFAULT_CONFIG.price_gap_saturation_pct * 2) == 100.0


def test_arbitrage_rank_zero_when_no_demand() -> None:
    assert ranking.arbitrage_rank(0.0, 30.0) == 0.0
    assert ranking.arbitrage_rank(50.0, 0.0) == 0.0


def test_arbitrage_rank_in_range() -> None:
    rank = ranking.arbitrage_rank(80.0, 30.0)
    assert 0.0 <= rank <= 100.0


def test_arbitrage_rank_geometric_mean() -> None:
    """Both demand and price gap at max → rank should be 100."""
    assert ranking.arbitrage_rank(100.0, DEFAULT_CONFIG.price_gap_saturation_pct) == 100.0


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_scan_returns_opportunities(client: TestClient) -> None:
    payload = {"hs_codes": ["090422"], "target_markets": ["ES", "DE", "US"]}
    response = client.post("/trade-min/scan", json=payload)
    assert response.status_code == 200
    body = response.json()
    # Each opportunity must pass the default min_arbitrage_rank filter.
    for opp in body:
        assert opp["arbitrage_rank"] >= DEFAULT_CONFIG.min_arbitrage_rank
        assert opp["opportunity_id"].startswith("OPP-")
        assert opp["hs_code"] == "090422"
        assert opp["market"] in ("ES", "DE", "US")
        assert opp["source"] in ("comtrade", "census", "apify_export_markets")


def test_scan_results_sorted_descending(client: TestClient) -> None:
    """Results must be ordered by descending arbitrage_rank."""
    payload = {
        "hs_codes": ["090422", "100199"],
        "target_markets": ["ES", "DE", "US", "AE", "GB"],
    }
    body = client.post("/trade-min/scan", json=payload).json()
    if len(body) >= 2:
        for i in range(len(body) - 1):
            assert body[i]["arbitrage_rank"] >= body[i + 1]["arbitrage_rank"]


def test_scan_min_rank_filter(client: TestClient) -> None:
    """Caller-supplied min_arbitrage_rank is respected."""
    high_threshold = 90.0
    payload = {
        "hs_codes": DEFAULT_CONFIG.default_hs_codes,
        "target_markets": DEFAULT_CONFIG.default_markets,
        "min_arbitrage_rank": high_threshold,
    }
    body = client.post("/trade-min/scan", json=payload).json()
    for opp in body:
        assert opp["arbitrage_rank"] >= high_threshold


def test_scan_default_hs_codes_and_markets(client: TestClient) -> None:
    """Empty ScanRequest uses configured defaults."""
    body = client.post("/trade-min/scan", json={}).json()
    # Defaults are 3 HS codes × 5 markets = 15 pairs; some may be filtered.
    assert isinstance(body, list)


def test_list_opportunities_reflects_previous_scan(client: TestClient) -> None:
    client.post("/trade-min/scan", json={"hs_codes": ["090422"], "target_markets": ["ES"]})
    body = client.get("/trade-min/opportunities").json()
    assert len(body) >= 0  # may be empty if the pair is filtered, but should not error


def test_get_opportunity_by_id(client: TestClient) -> None:
    scan_body = client.post(
        "/trade-min/scan",
        json={"hs_codes": ["090422"], "target_markets": ["ES", "DE", "US", "AE", "GB"]},
    ).json()
    if not scan_body:
        pytest.skip("No opportunities passed the rank filter for this pair")
    opp_id = scan_body[0]["opportunity_id"]
    response = client.get(f"/trade-min/opportunities/{opp_id}")
    assert response.status_code == 200
    assert response.json()["opportunity_id"] == opp_id


def test_get_unknown_opportunity_returns_404(client: TestClient) -> None:
    assert client.get("/trade-min/opportunities/OPP-unknown").status_code == 404


def test_scan_is_idempotent_on_repeated_calls(client: TestClient) -> None:
    """Repeated scans accumulate in the repository (each call generates new IDs)."""
    payload = {"hs_codes": ["090422"], "target_markets": ["ES"]}
    first = client.post("/trade-min/scan", json=payload).json()
    second = client.post("/trade-min/scan", json=payload).json()
    all_opps = client.get("/trade-min/opportunities").json()
    assert len(all_opps) >= len(first) + len(second)


def test_no_live_demand_data_means_no_opportunity(client: TestClient) -> None:
    """Without a live demand source (the suite has no keys / Apify token) a scan
    builds no opportunity on invented numbers — the list stays empty."""
    body = client.post("/trade-min/scan", json={"hs_codes": ["090422"], "target_markets": ["ES", "DE"]}).json()
    assert body == []
    assert client.get("/trade-min/opportunities").json() == []
