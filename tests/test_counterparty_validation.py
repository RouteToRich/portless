"""Scan-time buyer / supplier validation (services/trade_min/validation.py): the
Opportunity Radar can show only checked counterparties, a failed one never seeds
a deal, and a deal created from an opportunity no longer claims a supplier is
verified when nothing was checked."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from services.trade_min import directory, service as svc, validation
from services.trade_min.config import settings
from services.trade_min.schemas import (
    BuyerSignal,
    CounterpartyValidation,
    EnrichedOpportunity,
    SellerCandidate,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    directory.reset_directory()
    monkeypatch.setattr(settings, "trade_min_validation_enabled", True)
    monkeypatch.setattr(settings, "trade_min_validation_top_n", 5)
    monkeypatch.setattr(settings, "trade_min_validation_max_age_days", 30)
    yield
    directory.reset_directory()


class _Fake:
    def __init__(self, payload=None, book=None):
        self.payload = payload or {}
        self.book_payload = book
        self.calls: list[dict] = []
        self.book_calls: list[dict] = []

    async def quote(self, request):
        self.calls.append(request)
        return self.payload(request) if callable(self.payload) else dict(self.payload)

    async def book(self, request):
        self.book_calls.append(request)
        return dict(self.book_payload or {})


def _wire(monkeypatch, **fakes):
    def get(name):
        if name not in fakes:
            raise KeyError(name)  # e.g. API_SETU_KEY unset → registry unavailable
        return fakes[name]
    monkeypatch.setattr("shared.connectors.registry.get_connector", get)
    return fakes


def _screening(verdicts: dict[str, str]):
    return lambda req: {"results": [
        {"name": c["name"], "verdict": verdicts.get(c["name"], "clear"), "reason": f"r-{c['name']}"}
        for c in req["counterparties"]]}


def _buyer(name="Al Turab Foods", email="purchase@alturabfood.com", email_status="ok", fit=50, **kw):
    return BuyerSignal(buyer_name=name, buyer_country="AE", signal_type="web_search",
                       source="apify_company_search", urgency_tier=1, email=email,
                       email_status=email_status, fit_score=fit,
                       website=kw.pop("website", f"https://{name.split()[0].lower()}.ae"), **kw)


def _seller(name="Jashn Foods", email="info@jashnfoods.com", email_status="ok", **kw):
    return SellerCandidate(candidate_id=kw.pop("cid", name.lower().replace(" ", "")), company_name=name,
                           location_city="", location_state="", hs_code="100630",
                           source="apify_company_search", email=email, email_status=email_status,
                           website=kw.pop("website", f"https://{name.split()[0].lower()}.in"), **kw)


def _opp(buyers, sellers) -> EnrichedOpportunity:
    return EnrichedOpportunity(
        opportunity_id="OPP-1", hs_code="100630", market="AE", scanned_at=datetime.now(timezone.utc),
        demand_index=50, price_gap_pct=20, source="apify_export_markets", net_price_gap_pct=20,
        arbitrage_rank=60, confidence_score=20, buyer_signals=buyers, seller_candidates=sellers,
        price_benchmark_usd_per_mt=1000,
    )


def _v(status, **kw) -> CounterpartyValidation:
    return CounterpartyValidation(status=status, checked_at=datetime.now(timezone.utc), **kw)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

class TestDecide:
    def test_clear_and_deliverable_is_verified(self):
        v = validation.decide(CounterpartyValidation(sanctions="clear", email="ok"), role="buyer", country="AE")
        assert v.status == "verified" and v.reasons == []

    def test_catch_all_mail_is_verified_with_a_reason(self):
        v = validation.decide(CounterpartyValidation(sanctions="clear", email="risky"), role="buyer", country="AE")
        assert v.status == "verified" and "catch-all" in v.reasons[0]

    def test_sanctions_block_fails(self):
        v = validation.decide(CounterpartyValidation(sanctions="block", email="ok"), role="buyer", country="AE")
        assert v.status == "failed"

    @pytest.mark.parametrize("sanctions,email", [("review", "ok"), ("unchecked", "ok"),
                                                 ("clear", "invalid"), ("clear", "missing"),
                                                 ("clear", "unverified")])
    def test_anything_not_positively_checked_stays_unverified(self, sanctions, email):
        v = validation.decide(CounterpartyValidation(sanctions=sanctions, email=email), role="buyer", country="AE")
        assert v.status == "unverified" and v.reasons

    @pytest.mark.parametrize("field", ["gst", "iec"])
    def test_inactive_indian_registry_fails_a_supplier(self, field):
        v = validation.decide(CounterpartyValidation(sanctions="clear", email="ok", **{field: "inactive"}),
                              role="supplier", country="IN")
        assert v.status == "failed"

    def test_dgft_denied_fails(self):
        v = validation.decide(CounterpartyValidation(sanctions="clear", email="ok", dgft_denied="denied"),
                              role="supplier", country="IN")
        assert v.status == "failed"

    def test_unknown_gstin_is_a_reason_not_a_failure(self):
        v = validation.decide(CounterpartyValidation(sanctions="clear", email="ok", gst="missing"),
                              role="supplier", country="IN")
        assert v.status == "verified" and any("GSTIN" in r for r in v.reasons)

    def test_freshness(self):
        now = datetime.now(timezone.utc)
        assert validation.is_fresh(_v("verified", sanctions="clear"), now)
        assert not validation.is_fresh(_v("unverified"), now)  # nothing was screened
        old = CounterpartyValidation(status="verified", sanctions="clear", checked_at=now - timedelta(days=31))
        assert not validation.is_fresh(old, now)


# ---------------------------------------------------------------------------
# The scan check
# ---------------------------------------------------------------------------

class TestValidateScan:
    async def test_verified_failed_and_review(self, monkeypatch):
        fakes = _wire(monkeypatch, apify_counterparty_check=_Fake(_screening({"Bad Co": "block", "Maybe Co": "review"})),
                      apify_email_qualifier=_Fake({"emails": []}))
        good, bad, maybe = _buyer("Good Co"), _buyer("Bad Co"), _buyer("Maybe Co")
        counts = await validation.validate_scan([good, bad, maybe], [])
        assert (good.validation.status, bad.validation.status, maybe.validation.status) == \
            ("verified", "failed", "unverified")
        assert counts["verified"] == 1 and counts["failed"] == 1 and counts["unverified"] == 1
        assert len(fakes["apify_counterparty_check"].calls) == 1  # one batch for all
        assert fakes["apify_email_qualifier"].calls == []  # search already checked every address

    async def test_unchecked_addresses_go_to_the_qualifier(self, monkeypatch):
        fakes = _wire(monkeypatch, apify_counterparty_check=_Fake(_screening({})),
                      apify_email_qualifier=_Fake({"emails": [{"email": "a@x.ae", "status": "ok"},
                                                              {"email": "b@y.ae", "status": "invalid"}]}))
        a = _buyer("A Co", email="a@x.ae", email_status="unverified")
        b = _buyer("B Co", email="b@y.ae", email_status=None)
        await validation.validate_scan([a, b], [])
        assert fakes["apify_email_qualifier"].calls[0]["emails"] == ["a@x.ae", "b@y.ae"]
        assert a.validation.status == "verified"
        assert b.validation.status == "unverified" and b.validation.email == "invalid"

    async def test_without_an_apify_token_nothing_is_verified(self, monkeypatch):
        _wire(monkeypatch, apify_counterparty_check=_Fake({"results": [], "stub": True}),
              apify_email_qualifier=_Fake({"emails": [], "stub": True}))
        b = _buyer()
        await validation.validate_scan([b], [])
        assert b.validation.status == "unverified" and b.validation.sanctions == "unchecked"

    async def test_only_the_top_n_per_role_are_checked(self, monkeypatch):
        monkeypatch.setattr(settings, "trade_min_validation_top_n", 2)
        fakes = _wire(monkeypatch, apify_counterparty_check=_Fake(_screening({})))
        buyers = [_buyer(f"B{i} Co", fit=i * 10) for i in range(4)]
        await validation.validate_scan(buyers, [])
        screened = {c["name"] for c in fakes["apify_counterparty_check"].calls[0]["counterparties"]}
        assert screened == {"B3 Co", "B2 Co"}  # best fit first
        assert buyers[0].validation is None and buyers[1].validation is None

    async def test_a_fresh_stored_result_is_reused(self, monkeypatch):
        fakes = _wire(monkeypatch, apify_counterparty_check=_Fake(_screening({})))
        first = _buyer()
        await validation.validate_scan([first], [])
        await directory.record_scan("100630", "AE", [first], [], outcome="x")
        again = _buyer()
        counts = await validation.validate_scan([again], [])
        assert counts["reused"] == 1 and len(fakes["apify_counterparty_check"].calls) == 1
        assert again.validation.status == "verified"

    async def test_switched_off_checks_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "trade_min_validation_enabled", False)
        fakes = _wire(monkeypatch, apify_counterparty_check=_Fake(_screening({})))
        b = _buyer()
        await validation.validate_scan([b], [])
        assert b.validation is None and fakes["apify_counterparty_check"].calls == []

    async def test_indian_supplier_registries(self, monkeypatch):
        _wire(monkeypatch, apify_counterparty_check=_Fake(_screening({})),
              gstn=_Fake(lambda r: {"status": "CANCELLED" if r["gstin"].startswith("99") else "ACTIVE"}),
              dgft=_Fake({"status": "ACTIVE"}, book={"on_denied_list": False, "note": "Stub — cache the list"}))
        ok = _seller("Ok Foods", gstin="27AAAAA0000A1Z5", iec="0123456789")
        cancelled = _seller("Gone Foods", gstin="99AAAAA0000A1Z5")
        no_number = _seller("Plain Foods")
        await validation.validate_scan([], [ok, cancelled, no_number])
        assert ok.validation.status == "verified"
        assert (ok.validation.gst, ok.validation.iec) == ("active", "active")
        assert ok.validation.dgft_denied == "unchecked"  # the stub is no answer
        assert cancelled.validation.status == "failed"
        assert no_number.validation.status == "verified" and no_number.validation.gst == "missing"

    async def test_registry_without_a_key_is_unchecked_not_failed(self, monkeypatch):
        _wire(monkeypatch, apify_counterparty_check=_Fake(_screening({})))  # no gstn connector
        s = _seller(gstin="27AAAAA0000A1Z5")
        await validation.validate_scan([], [s])
        assert s.validation.gst == "unchecked" and s.validation.status == "verified"

    async def test_a_crashing_connector_never_breaks_the_scan(self, monkeypatch):
        class _Boom:
            async def quote(self, request):
                raise RuntimeError("down")
        _wire(monkeypatch, apify_counterparty_check=_Boom(), apify_email_qualifier=_Boom())
        b = _buyer(email_status=None)
        await validation.validate_scan([b], [])
        assert b.validation.status == "unverified"


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------

class TestDirectory:
    async def test_validation_is_stored_and_kept_by_a_scan_that_checked_nothing(self):
        checked = _buyer(validation=_v("verified", sanctions="clear", email="ok"))
        await directory.record_scan("100630", "AE", [checked], [], outcome="a")
        await directory.record_scan("100630", "AE", [_buyer()], [], outcome="b")
        rec = (await directory.list_counterparties("buyer"))[0]
        assert rec.validation.status == "verified" and rec.times_seen == 2

    async def test_list_filters_by_validation(self):
        await directory.record_scan("100630", "AE", [
            _buyer("Good Co", validation=_v("verified")),
            _buyer("Bad Co", validation=_v("failed")),
            _buyer("New Co"),
        ], [], outcome="x")
        names = lambda rows: {r.name for r in rows}  # noqa: E731
        assert names(await directory.list_counterparties("buyer", validation="verified")) == {"Good Co"}
        assert names(await directory.list_counterparties("buyer", validation="failed")) == {"Bad Co"}
        assert names(await directory.list_counterparties("buyer", validation="unverified")) == {"New Co"}

    def test_routes(self, monkeypatch):
        from services.trade_min.main import create_app
        # A directory record keeps no e-mail status of its own, so the qualifier runs.
        _wire(monkeypatch, apify_counterparty_check=_Fake(_screening({})),
              apify_email_qualifier=_Fake(lambda r: {"emails": [{"email": e, "status": "ok"} for e in r["emails"]]}))
        with TestClient(create_app()) as client:  # records through the client's own loop
            buyers = [_buyer("Good Co", validation=_v("verified")), _buyer("New Co")]
            client.portal.call(lambda: directory.record_scan("100630", "AE", buyers, [], outcome="x"))
            rows = client.get("/trade-min/buyers", params={"validation": "verified"}).json()
            assert [r["name"] for r in rows] == ["Good Co"]
            new_id = next(r["id"] for r in client.get("/trade-min/buyers").json() if r["name"] == "New Co")
            out = client.post(f"/trade-min/buyers/{new_id}/validate")
            assert out.status_code == 200 and out.json()["validation"]["status"] == "verified"
            assert client.post(f"/trade-min/suppliers/{new_id}/validate").status_code == 404
            rows = client.get("/trade-min/buyers", params={"validation": "verified"}).json()
            assert {r["name"] for r in rows} == {"Good Co", "New Co"}


# ---------------------------------------------------------------------------
# Radar view and deal seed
# ---------------------------------------------------------------------------

class TestRadarAndSeed:
    def _opp(self):
        return _opp(
            [_buyer("Good Co", validation=_v("verified")), _buyer("Bad Co", validation=_v("failed")),
             _buyer("New Co")],
            [_seller("Good Foods", validation=_v("verified"), estimated_fob_price_usd=800),
             _seller("Bad Foods", validation=_v("failed"), estimated_fob_price_usd=700),
             _seller("New Foods", estimated_fob_price_usd=750)],
        )

    def test_verified_view_hides_the_rest_and_counts_them(self):
        view = svc._with_counterparties(self._opp(), "verified")
        assert [b.buyer_name for b in view.buyer_signals] == ["Good Co"]
        assert [s.company_name for s in view.seller_candidates] == ["Good Foods"]
        assert view.counterparty_counts == {"buyers": {"verified": 1, "unverified": 1, "failed": 1},
                                            "suppliers": {"verified": 1, "unverified": 1, "failed": 1}}
        assert all(p.buyer_name == "Good Co" for p in view.pair_matches)

    def test_all_view_keeps_everyone(self):
        view = svc._with_counterparties(self._opp(), "all")
        assert len(view.buyer_signals) == 3 and view.counterparty_filter == "all"

    def test_seed_drops_failed_and_puts_verified_first(self):
        seed = svc._build_match_seed(self._opp())
        assert [s["company_name"] for s in seed.top_sellers] == ["Good Foods", "New Foods"]
        assert seed.top_sellers[0]["validation"]["status"] == "verified"
        assert [b["buyer_name"] for b in seed.top_buyers] == ["Good Co", "New Co"]


class TestDealSeeding:
    async def test_a_failed_check_fails_the_gatekeeper(self):
        from services.deals.service import _score_seller
        seller = {"supplier_id": "s1", "company_name": "Gone Foods", "fob_price_usd": 700,
                  "validation": {"status": "failed", "gst": "inactive", "reasons": ["GST registration is not active"]}}
        r = await _score_seller("DEAL-1", seller, 1000)
        assert r.gatekeeper_passed is False and r.decision == "DISCARD"
        assert r.verification == "failed" and r.verification_reasons

    async def test_an_unchecked_supplier_is_scored_but_not_called_verified(self):
        from services.deals.service import _score_seller
        r = await _score_seller("DEAL-1", {"supplier_id": "s1", "company_name": "X", "fob_price_usd": 700}, 1000)
        assert r.gatekeeper_passed is True and r.verification == "unverified"

    def test_party_says_only_what_was_checked(self):
        from services.deals.service import _party_check_fields
        assert _party_check_fields(None, "a@b.c") == {
            "email_status": "unverified", "sanctions_status": "unchecked", "verification_status": "unverified"}
        f = _party_check_fields(_v("verified", sanctions="clear", email="ok"), "a@b.c")
        assert f["verification_status"] == "scan_verified" and f["sanctions_status"] == "clear"
        assert f["email_status"] == "ok" and f["sanctions_checked_at"] is not None
        f = _party_check_fields(_v("unverified", sanctions="review", email="invalid"), "a@b.c")
        assert f["verification_status"] == "unverified" and f["email_status"] == "invalid"

    def test_a_failed_buyer_is_never_an_outreach_candidate(self):
        from services.deals.service import _buyer_candidates_from_seed
        out = _buyer_candidates_from_seed({"market": "AE", "top_buyers": [
            {"buyer_name": "Bad Co", "validation": {"status": "failed"}},
            {"buyer_name": "Good Co", "validation": {"status": "verified"}},
            {"buyer_name": "New Co"},
        ]})
        assert [(c.legal_name, c.validation_status) for c in out] == [("Good Co", "verified"), ("New Co", "unverified")]
