"""Tests for the Match Engine: deterministic scoring + HTTP surface (LLD 4.3)."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from services.match_engine import scoring
from services.match_engine.config import DEFAULT_CONFIG
from services.match_engine.repository import repository


@pytest.fixture(autouse=True)
def _clean_repository() -> None:
    repository.clear()
    yield
    repository.clear()


@pytest.fixture
def client() -> TestClient:
    module = importlib.import_module("services.match_engine.main")
    return TestClient(module.app)


def _base_payload(**overrides: object) -> dict:
    payload = {
        "deal_id": "DEAL-1",
        "supplier_id": "SUP-1",
        "buyer_target_price": 100.0,
        "supplier_quote_price": 75.0,  # 25% spread -> arbitrage 100
        "currency": "USD",
        "verification": {"gst_verified": True, "iec_active": True, "dgft_denied": False},
        "supplier_to_port_km": 0.0,  # proximity 100
        "business_age_years": 10.0,  # age 100
        "udyam_registered": True,  # udyam 100
        "annual_shipment_count": 120,  # frequency 100
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Pure scoring functions
# --------------------------------------------------------------------------- #
def test_gatekeeper_passes_when_all_clear() -> None:
    assert scoring.gatekeeper_multiplier(True, True, False) == 1


@pytest.mark.parametrize(
    "gst, iec, denied",
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_gatekeeper_fails_on_any_violation(gst: bool, iec: bool, denied: bool) -> None:
    assert scoring.gatekeeper_multiplier(gst, iec, denied) == 0


def test_arbitrage_saturates_at_configured_spread() -> None:
    assert scoring.arbitrage_score(100.0, 75.0) == 100.0  # 25% spread
    assert scoring.arbitrage_score(100.0, 90.0) == pytest.approx(40.0)  # 10% spread


def test_arbitrage_zero_when_quote_not_below_target() -> None:
    assert scoring.arbitrage_score(100.0, 100.0) == 0.0
    assert scoring.arbitrage_score(100.0, 120.0) == 0.0


def test_proximity_linear_decay() -> None:
    assert scoring.proximity_score(0.0) == 100.0
    assert scoring.proximity_score(300.0) == pytest.approx(50.0)  # half of 600 km
    assert scoring.proximity_score(600.0) == 0.0
    assert scoring.proximity_score(900.0) == 0.0


def test_reliability_combines_signals() -> None:
    assert scoring.reliability_score(10.0, True, 120) == 100.0
    # age 100*0.4 + udyam 0*0.2 + freq 0*0.4 = 40
    assert scoring.reliability_score(10.0, False, 0) == pytest.approx(40.0)


def test_normalize_weights_sums_to_one() -> None:
    w = scoring.normalize_weights(1.0, 1.0, 2.0)
    assert sum(w) == pytest.approx(1.0)
    assert w == pytest.approx((0.25, 0.25, 0.5))


def test_normalize_weights_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        scoring.normalize_weights(0.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        scoring.normalize_weights(-1.0, 1.0, 1.0)


def test_composite_zero_when_gatekeeper_fails() -> None:
    cms = scoring.composite_match_score(0, 100.0, 100.0, 100.0, (0.5, 0.3, 0.2))
    assert cms == 0.0


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_perfect_supplier_is_sourced(client: TestClient) -> None:
    response = client.post("/match-engine/score", json=_base_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["composite_score"] == 100.0
    assert body["gatekeeper_passed"] is True
    assert body["decision"] == "SOURCE"
    assert body["threshold"] == DEFAULT_CONFIG.cms_threshold
    assert body["match_id"].startswith("MATCH-")


def test_gatekeeper_failure_is_discarded(client: TestClient) -> None:
    payload = _base_payload(
        verification={"gst_verified": False, "iec_active": True, "dgft_denied": False}
    )
    body = client.post("/match-engine/score", json=payload).json()
    assert body["composite_score"] == 0.0
    assert body["gatekeeper_passed"] is False
    assert body["decision"] == "DISCARD"


def test_weak_supplier_below_threshold_is_discarded(client: TestClient) -> None:
    payload = _base_payload(
        supplier_quote_price=90.0,  # 10% spread -> arbitrage 40
        supplier_to_port_km=300.0,  # proximity 50
        business_age_years=2.0,
        udyam_registered=False,
        annual_shipment_count=12,
    )
    body = client.post("/match-engine/score", json=payload).json()
    assert body["composite_score"] < DEFAULT_CONFIG.cms_threshold
    assert body["decision"] == "DISCARD"


def test_get_latest_score_for_deal(client: TestClient) -> None:
    client.post("/match-engine/score", json=_base_payload())
    response = client.get("/match-engine/scores/DEAL-1")
    assert response.status_code == 200
    assert response.json()["deal_id"] == "DEAL-1"


def test_get_score_unknown_deal_returns_404(client: TestClient) -> None:
    assert client.get("/match-engine/scores/UNKNOWN").status_code == 404


def test_list_scores(client: TestClient) -> None:
    client.post("/match-engine/score", json=_base_payload())
    client.post("/match-engine/score", json=_base_payload(deal_id="DEAL-2"))
    body = client.get("/match-engine/scores").json()
    assert len(body) == 2
