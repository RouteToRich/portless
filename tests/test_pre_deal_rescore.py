"""Pre-deal rescore (services/trade_min/rescore.py) and the pair-match inputs it fills:
the radar's pair scores are improved with real, labelled data before a deal starts,
and nothing the counterparties must give (firm price, volumes) is invented."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from services.trade_min import directory, pair_match as pm, rescore
from services.trade_min import repository as trade_repo
from services.trade_min.config import settings
from services.trade_min.schemas import (
    BuyerSignal,
    CounterpartyValidation,
    EnrichedOpportunity,
    SellerCandidate,
)

TODAY = date(2026, 11, 13)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    trade_repo._repository = trade_repo.InMemoryFallbackRepository()
    directory.reset_directory()
    monkeypatch.setattr(settings, "trade_min_validation_enabled", True)
    yield
    trade_repo._repository = None
    directory.reset_directory()


def _buyer(**kw) -> BuyerSignal:
    base = dict(buyer_name="Biova GmbH", buyer_country="DE", signal_type="web_search",
                source="apify_company_search", urgency_tier=1, fit_score=94, email="einkauf@biova.de",
                email_status="ok", phone="+49 30 1", website="https://biova.de", hs_codes=["090422"])
    base.update(kw)
    return BuyerSignal(**base)


def _seller(**kw) -> SellerCandidate:
    base = dict(candidate_id="anufoods.in", company_name="Anufoods", location_city="", location_state="",
                hs_code="090422", source="apify_company_search", email="info@anufoods.in", email_status="ok",
                phone="+91 1", website="https://anufoods.in", fit_score=92)
    base.update(kw)
    return SellerCandidate(**base)


def _opp(buyers, sellers, benchmark=3620.0) -> EnrichedOpportunity:
    return EnrichedOpportunity(
        opportunity_id="OPP-R", hs_code="090422", market="DE", scanned_at=datetime.now(UTC),
        demand_index=53, price_gap_pct=92.3, source="apify_export_markets", net_price_gap_pct=92.3,
        arbitrage_rank=70, confidence_score=20, buyer_signals=buyers, seller_candidates=sellers,
        price_benchmark_usd_per_mt=benchmark,
    )


def _v(status, **kw):
    return CounterpartyValidation(status=status, checked_at=datetime.now(UTC), **kw)


def _factor(p, key):
    return next(f for f in p.factors if f.key == key)


# ---------------------------------------------------------------------------
# Pair-match inputs
# ---------------------------------------------------------------------------

class TestPairInputs:
    def test_web_search_buyer_does_not_confirm_the_product(self):
        p = pm.score_pair(_buyer(), 0, _seller(), _opp([], []), today=TODAY)
        assert _factor(p, "product_fit").score == 85
        assert "unknown" in _factor(p, "product_fit").reason

    def test_a_buyer_from_import_records_still_confirms_it(self):
        p = pm.score_pair(_buyer(signal_type="manifest"), 0, _seller(), _opp([], []), today=TODAY)
        assert _factor(p, "product_fit").score == 100

    def test_the_check_raises_trust_and_quality(self):
        plain = pm.score_pair(_buyer(), 0, _seller(), _opp([], []), today=TODAY)
        checked = pm.score_pair(_buyer(validation=_v("verified")), 0,
                                _seller(gstin="27AAAAA0000A1Z5", validation=_v("verified", gst="active")),
                                _opp([], []), today=TODAY)
        assert _factor(checked, "supplier_trust").score > _factor(plain, "supplier_trust").score
        assert "GSTIN active" in _factor(checked, "supplier_trust").reason
        assert _factor(checked, "buyer_quality").score > _factor(plain, "buyer_quality").score
        assert any("counterparty check" in x for x in plain.improve)
        assert not any("counterparty check" in x for x in checked.improve)

    def test_a_failed_check_blocks_the_pair(self):
        p = pm.score_pair(_buyer(), 0, _seller(validation=_v("failed")), _opp([], []), today=TODAY)
        assert p.grade == "Blocked" and "Supplier failed the counterparty check" in p.blockers

    def test_an_estimated_supplier_price_is_labelled_and_trusted_less(self):
        quoted = pm.score_pair(_buyer(), 0, _seller(estimated_fob_price_usd=1900), _opp([], []), today=TODAY)
        est = pm.score_pair(_buyer(), 0, _seller(fob_estimate_usd_per_mt=1900, fob_estimate_basis="price book"),
                            _opp([], []), today=TODAY)
        assert est.margin_pct == quoted.margin_pct
        assert "estimated" in _factor(est, "margin").reason
        assert _factor(est, "price_confidence").score < _factor(quoted, "price_confidence").score
        assert any("estimated supplier price" in x for x in est.improve)


# ---------------------------------------------------------------------------
# The rescore
# ---------------------------------------------------------------------------

class _Fake:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def quote(self, request):
        self.calls.append(request)
        return self.payload(request) if callable(self.payload) else dict(self.payload)

    async def book(self, request):
        return {"on_denied_list": False, "note": "Stub"}


def _wire(monkeypatch, **fakes):
    def get(name):
        if name not in fakes:
            raise KeyError(name)
        return fakes[name]
    monkeypatch.setattr("shared.connectors.registry.get_connector", get)
    return fakes


def _estimate(median):
    async def fake(hs_code, side, **kw):
        from shared.pricebook import PriceEstimate
        return PriceEstimate(hs_code=hs_code, side=side, median=median, n_observations=0)
    return fake


async def _save(opp):
    repo = await trade_repo.get_repository()
    await repo.save(opp)


class TestRescore:
    async def test_fills_the_gaps_and_raises_the_score(self, monkeypatch):
        fakes = _wire(
            monkeypatch,
            apify_contact_finder=_Fake({"contacts": [{"input_name": "Anufoods", "address": "Plot 4, Kochi 682001",
                                                      "vat_id": "32AAAAA0000A1Z5"}]}),
            apify_counterparty_check=_Fake(lambda r: {"results": [{"name": c["name"], "verdict": "clear"}
                                                                  for c in r["counterparties"]]}),
            gstn=_Fake({"status": "ACTIVE"}),
        )
        monkeypatch.setattr("shared.pricebook.estimate", _estimate(1880.0))
        await _save(_opp([_buyer()], [_seller()]))

        opp = await rescore.rescore("OPP-R")

        [row] = opp.rescore["pairs"]
        assert row["after"]["score"] > row["before"]["score"]
        s = opp.seller_candidates[0]
        assert s.gstin == "32AAAAA0000A1Z5" and s.address.startswith("Plot 4")
        assert s.supplier_to_port_km == 120  # Kerala → Kochi, from the PIN code
        assert s.fob_estimate_usd_per_mt == 1880.0 and s.estimated_fob_price_usd is None  # never a quote
        assert s.validation.status == "verified" and s.validation.gst == "active"
        assert opp.buyer_signals[0].validation.status == "verified"
        assert any("GSTIN" in c for c in opp.rescore["changes"])
        assert any("Get the supplier's FOB quote" in x for x in row["still_needed"])
        assert opp.rescored_at is not None
        assert fakes["apify_contact_finder"].calls[0]["companies"][0]["name"] == "Anufoods"
        stored = await (await trade_repo.get_repository()).get("OPP-R")
        assert stored.rescore["pairs"][0]["after"] == row["after"]

    async def test_without_actors_nothing_is_invented(self, monkeypatch):
        _wire(monkeypatch,
              apify_contact_finder=_Fake({"contacts": [], "stub": True}),
              apify_counterparty_check=_Fake({"results": [], "stub": True}))
        monkeypatch.setattr("shared.pricebook.estimate", _estimate(None))
        await _save(_opp([_buyer()], [_seller()]))
        opp = await rescore.rescore("OPP-R")
        s = opp.seller_candidates[0]
        assert s.gstin is None and s.fob_estimate_usd_per_mt is None and s.supplier_to_port_km is None
        assert s.validation.status == "unverified"

    async def test_unknown_opportunity(self):
        assert await rescore.rescore("NOPE") is None

    def test_recently_rescored(self):
        opp = _opp([], [])
        assert not rescore.recently_rescored(opp)
        opp.rescored_at = datetime.now(UTC) - timedelta(minutes=5)
        assert rescore.recently_rescored(opp)
        opp.rescored_at = datetime.now(UTC) - timedelta(hours=2)
        assert not rescore.recently_rescored(opp)

    def test_route(self, monkeypatch):
        from services.trade_min.main import create_app
        _wire(monkeypatch, apify_counterparty_check=_Fake({"results": [], "stub": True}))
        monkeypatch.setattr("shared.pricebook.estimate", _estimate(None))
        with TestClient(create_app()) as client:
            client.portal.call(lambda: _save(_opp([_buyer()], [_seller()])))
            r = client.post("/trade-min/opportunities/OPP-R/rescore")
            assert r.status_code == 200 and r.json()["rescore"]["pairs"]
            assert client.post("/trade-min/opportunities/NOPE/rescore").status_code == 404


class TestDealCreation:
    async def test_runs_once_then_reuses(self, monkeypatch):
        from services.deals import service as deals
        calls = []

        async def fake_rescore(opportunity_id, **kw):
            calls.append(opportunity_id)
        monkeypatch.setattr(rescore, "rescore", fake_rescore)
        opp = _opp([_buyer()], [_seller()])
        await _save(opp)
        await deals._rescore_opportunity("OPP-R")
        assert calls == ["OPP-R"]
        opp.rescored_at = datetime.now(UTC)
        await deals._rescore_opportunity("OPP-R")
        assert calls == ["OPP-R"]  # rescored in the last hour

    async def test_switched_off(self, monkeypatch):
        from services.deals import service as deals
        monkeypatch.setenv("DEAL_RESCORE_BEFORE_CREATE", "false")
        calls = []

        async def fake_rescore(opportunity_id, **kw):
            calls.append(opportunity_id)
        monkeypatch.setattr(rescore, "rescore", fake_rescore)
        await _save(_opp([], []))
        await deals._rescore_opportunity("OPP-R")
        assert calls == []
