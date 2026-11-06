"""PortLess-owned Apify actors (shared/connectors/apify.py) + their Trade-Min wiring.

No network: the connector's ``run()`` is monkeypatched with canned dataset rows
(the shapes each actor's .actor/dataset_schema.json documents), and the HTTP
run/poll/dataset flow is exercised against an httpx MockTransport.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.connectors import apify as ap
from shared.connectors.registry import get_connector


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

class TestConfig:
    def test_actor_ref_defaults_to_owner_slug(self, monkeypatch):
        monkeypatch.delenv("APIFY_ACTOR_US_IMPORTER_FINDER", raising=False)
        monkeypatch.setenv("APIFY_ACTOR_OWNER", "rich_minds")
        assert ap.resolve_actor_ref("US_IMPORTER_FINDER", "us-importer-finder-ai") == "rich_minds~us-importer-finder-ai"

    def test_actor_ref_overrides(self, monkeypatch):
        monkeypatch.setenv("APIFY_ACTOR_OWNER", "rich_minds")
        monkeypatch.setenv("APIFY_ACTOR_ALIBABA_RFQ_LEADS", "other-slug")
        assert ap.resolve_actor_ref("ALIBABA_RFQ_LEADS", "x") == "rich_minds~other-slug"
        monkeypatch.setenv("APIFY_ACTOR_ALIBABA_RFQ_LEADS", "someone/their-actor")
        assert ap.resolve_actor_ref("ALIBABA_RFQ_LEADS", "x") == "someone~their-actor"
        monkeypatch.setenv("APIFY_ACTOR_ALIBABA_RFQ_LEADS", "Fn26zPbcUPRREJ45V")
        assert ap.resolve_actor_ref("ALIBABA_RFQ_LEADS", "x") == "Fn26zPbcUPRREJ45V"

    def test_no_token_means_stub(self):
        # conftest pins APIFY_TOKEN="" and disables auto-discovery for the suite
        assert ap.apify_enabled() is False
        assert all(not a["live"] for a in ap.describe_actors()["actors"])

    def test_every_actor_registered(self):
        names = {cls.name for cls in ap.APIFY_CONNECTORS}
        for name in names:
            assert isinstance(get_connector(name), ap.ApifyActorConnector)

    def test_iso_helpers(self):
        assert ap.iso2("USA") == "US" and ap.iso2("de") == "DE" and ap.iso3("US") == "USA"
        assert ap.iso2(None) is None


# ---------------------------------------------------------------------------
# Stub mode: every quote() returns a well-formed empty result, never raises
# ---------------------------------------------------------------------------

class TestStubMode:
    @pytest.mark.asyncio
    async def test_each_connector_returns_empty_shape(self):
        cases = [
            (ap.ApifyUsImporterConnector(), {"hs_code": "090422"}, "buyers"),
            (ap.ApifyAlibabaRfqConnector(), {"products": ["pepper"], "country": "DE"}, "rfqs"),
            (ap.ApifyExportMarketConnector(), {"hs_code": "090422", "markets": ["DE"]}, "markets"),
            (ap.ApifyIndiaSupplierConnector(), {"hs_code": "090422", "keyword": "pepper"}, "sellers"),
            (ap.ApifyContactFinderConnector(), {"companies": [{"name": "Acme", "country": "DE"}]}, "contacts"),
            (ap.ApifyCounterpartyCheckConnector(), {"counterparties": [{"name": "Acme", "country": "DE"}]}, "results"),
            (ap.ApifyEmailQualifierConnector(), {"emails": ["a@b.com"]}, "emails"),
        ]
        for conn, req, key in cases:
            out = await conn.quote(req)
            assert out[key] == [] and out["stub"] is True and out["total"] == 0, conn.name

    @pytest.mark.asyncio
    async def test_missing_required_input_raises(self):
        with pytest.raises(ValueError):
            await ap.ApifyUsImporterConnector().quote({})
        with pytest.raises(ValueError):
            await ap.ApifyContactFinderConnector().quote({"companies": []})


# ---------------------------------------------------------------------------
# Row normalisation (dataset rows → PortLess shapes)
# ---------------------------------------------------------------------------

def _rows(conn, rows):
    """Patch run() on this instance to return canned rows and mark it configured."""
    conn._token = "test"  # noqa: SLF001
    conn.run = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    return conn


class TestNormalisers:
    @pytest.mark.asyncio
    async def test_us_importers(self):
        conn = _rows(ap.ApifyUsImporterConnector(), [{
            "itemId": "iy:acme", "name": "Acme Foods Inc", "website": "https://acme.example",
            "phone": "+1 555 0100", "weightKg12m": 48000, "shipments12m": 9, "lastArrival": "2026-09-01",
            "estValueUsd12m": 120000.0, "hsCodes": ["090422"], "score": 82, "fitReason": "buys pepper",
            "pitchAngle": "You already import from Vietnam.", "contactRoute": "https://acme.example",
            "buysFromOrigin": False, "originSharePct": 0.0, "flags": ["active"],
        }, {"name": None}])
        out = await conn.quote({"hs_code": "090422", "unit_value_usd_per_kg": 2.5})
        assert out["total"] == 1
        b = out["buyers"][0]
        assert b["buyer_country"] == "US" and b["annual_volume_mt"] == 48.0
        assert b["unit_price_usd"] == 2500.0 and b["price_unit"] == "USD/MT"
        assert b["fit_score"] == 82 and b["website"] == "https://acme.example"
        sent = conn.run.await_args.args[0]
        assert sent["hsCodes"] == ["090422"] and sent["unitValueUsdPerKg"] == 2.5
        assert sent["dedupeAcrossRuns"] is False and sent["originCountry"] == "IN"

    @pytest.mark.asyncio
    async def test_alibaba_rfq_price_units_and_country_filter(self):
        conn = _rows(ap.ApifyAlibabaRfqConnector(), [
            {"rfqId": "r1", "subjectEnglish": "Need 20 tons pepper", "buyerCountryCode": "DE",
             "quantity": 20, "quantityUnit": "tons", "targetPrice": 3100, "targetPriceCurrency": "USD",
             "score": 77, "quoteUrl": "https://alibaba/quote/r1", "postedAt": "2026-10-20T09:00:00Z"},
            {"rfqId": "r2", "subject": "pepper", "buyerCountryCode": "FR", "quantity": 500,
             "quantityUnit": "kg", "targetPrice": 3.2, "targetPriceCurrency": "USD", "score": 60},
            {"rfqId": "r3", "subject": "pepper", "buyerCountryCode": "DE", "quantity": 10,
             "quantityUnit": "pieces", "targetPrice": 5, "targetPriceCurrency": "EUR", "score": 40},
        ])
        out = await conn.quote({"products": ["black pepper"], "country": "DE"})
        ids = [r["rfq_id"] for r in out["rfqs"]]
        assert ids == ["r1", "r3"]  # FR filtered out
        assert out["rfqs"][0]["unit_price_usd"] == 3100.0 and out["rfqs"][0]["price_unit"] == "USD/MT"
        assert out["rfqs"][1]["unit_price_usd"] is None  # EUR / pieces → no USD/MT price
        assert conn.run.await_args.args[0]["buyerCountries"] == ["DE"]

    @pytest.mark.asyncio
    async def test_export_markets(self):
        conn = _rows(ap.ApifyExportMarketConnector(), [{
            "marketCountryCode": "DEU", "marketCountry": "Germany", "hsCode": "090422",
            "latestPeriod": "2025", "importValueUsd": 42_000_000, "growth1yPct": 8.5,
            "exporterSharePct": 21.0, "unitValueUsdPerKg": 4.2, "exporterWorldUnitValueUsdPerKg": 3.5,
            "score": 71, "flags": ["fast_growing"],
        }])
        out = await conn.quote({"hs_code": "090422", "markets": ["DE"]})
        m = out["markets"][0]
        assert m["market"] == "DE" and m["unit_value_usd_per_mt"] == 4200.0
        assert m["exporter_world_unit_value_usd_per_mt"] == 3500.0
        # The deployed actor's markets enum is ISO-2 (ISO-3 → HTTP 400 invalid-input).
        assert conn.run.await_args.args[0]["markets"] == ["DE"]
        assert conn.run.await_args.args[0]["enableAi"] is False

    @pytest.mark.asyncio
    async def test_india_suppliers_tolerant_mapping(self):
        conn = _rows(ap.ApifyIndiaSupplierConnector(), [{
            "itemId": "s1", "companyName": "Kerala Spice Co", "city": "Kochi", "state": "Kerala",
            "minPrice": 3.1, "maxPrice": 3.4, "priceUnit": "USD/kg", "bestEmail": "sales@kerala.example",
            "phones": [{"number": "+91 98 000"}], "gstin": "32AAAAA0000A1Z5", "score": 70,
        }])
        out = await conn.quote({"hs_code": "090422"})
        s = out["sellers"][0]
        assert s["company_name"] == "Kerala Spice Co" and s["email"] == "sales@kerala.example"
        assert s["phone"] == "+91 98 000" and s["gstin"] == "32AAAAA0000A1Z5" and s["price_unit"] == "USD/kg"

    @pytest.mark.asyncio
    async def test_contact_finder(self):
        conn = _rows(ap.ApifyContactFinderConnector(), [
            {"inputName": "Acme Foods Inc", "name": "Acme Foods", "inputCountry": "US", "website": "https://acme.example",
             "domain": "acme.example", "websiteConfidence": 0.9, "bestEmail": "purchasing@acme.example",
             "bestEmailRole": "purchasing", "bestEmailStatus": "ok", "phones": [{"number": "+1 555"}],
             "vatId": None, "generatedText": "Hi there", "score": 80},
            {"inputName": "Ghost GmbH", "notFound": True, "notFoundReason": "no_website"},
        ])
        out = await conn.quote({"companies": [{"name": "Acme Foods Inc", "country": "US"}, {"name": "Ghost GmbH", "country": "DE"}]})
        c0, c1 = out["contacts"]
        assert c0["email"] == "purchasing@acme.example" and c0["email_status"] == "ok" and c0["phone"] == "+1 555"
        assert c1["not_found"] is True
        assert conn.run.await_args.args[0]["companies"] == ["Acme Foods Inc, US", "Ghost GmbH, DE"]
        assert conn.run.await_args.args[0]["includeNotFound"] is True

    @pytest.mark.asyncio
    async def test_company_search_buyers(self):
        # Rows shaped like the live company-contact-finder-ai search output (2026-11-06).
        conn = _rows(ap.ApifyCompanySearchConnector(), [
            {"name": "Best Wholesale Rice Markets in Dubai", "domain": "alturabfood.com", "inputCountry": "AE",
             "website": "https://alturabfood.com/", "bestEmail": "purchase@alturabfood.com", "bestEmailStatus": "ok",
             "phones": [{"number": "+971 4 000"}], "foundBy": "search: basmati rice importers, AE", "score": 80},
            {"name": "Jashn Foods: Basmati Rice Manufacturers, Suppliers", "domain": "jashnfoods.com",
             "inputCountry": "AE", "website": "https://www.jashnfoods.com/", "bestEmail": "info@jashnfoods.com"},
            {"name": "Rice Exporters from India", "legalName": "Rice Master Global Ltd", "domain": "ricemasterglobal.com",
             "bestEmail": "info@ricemasterglobal.com"},
            {"name": "Top Rice Exporters in India", "domain": "exportmandi.com", "bestEmail": "x@y.com"},   # directory
            {"name": "All India Rice Exporters Association", "domain": "rice-assoc.example", "bestEmail": "a@b.c"},
            {"name": "No Contact Co", "domain": "nocontact.example"},                                        # no e-mail/phone
            {"name": "Ghost", "notFound": True, "notFoundReason": "site_unreachable"},
            {"name": "Dup", "domain": "alturabfood.com", "bestEmail": "info@alturabfood.com"},              # duplicate domain
        ])
        out = await conn.quote({"keyword": "basmati rice", "countries": ["AE"], "role": "buyer", "max_results": 4})
        names = [c["company_name"] for c in out["companies"]]
        assert names == ["Alturabfood", "Jashn Foods", "Rice Master Global Ltd"]
        first = out["companies"][0]
        assert first["email"] == "purchase@alturabfood.com" and first["phone"] == "+971 4 000"
        assert first["role"] == "buyer" and first["query"] == "basmati rice importers, AE"
        sent = conn.run.await_args.args[0]
        assert sent["sourceMode"] == "live" and sent["maxSearchResults"] == 4
        assert sent["searchQuery"] == "basmati rice importers, AE\nbasmati rice wholesale distributors, AE"
        assert sent["preferRoles"][0] == "purchasing"

    @pytest.mark.parametrize("row,expected", [
        # real names the live search returned on 2026-11-06
        ({"legal_name": "88 Motta white Rice 20 KG", "domain": "meridianpf.com"}, "Meridianpf"),
        ({"legal_name": "21, Warsan 1, Dubai, UAE, PO BOX 455012 Arham Foodstuff Trading L.L.C"},
         "Arham Foodstuff Trading L.L.C"),
        ({"name": "Welcome To Indiafeast Millers Private Limited"}, "Indiafeast Millers Private Limited"),
        ({"legal_name": "Rice Master Global Ltd"}, "Rice Master Global Ltd"),
        ({"name": "Best Wholesale Rice Markets in Dubai", "domain": "alturabfood.com"}, "Alturabfood"),
        ({"name": "Agro Factory", "domain": "agro-factory.com"}, "Agro Factory"),
    ])
    def test_company_name_cleanup(self, row, expected):
        assert ap._company_name(row) == expected

    @pytest.mark.asyncio
    async def test_company_search_suppliers_query(self):
        conn = _rows(ap.ApifyCompanySearchConnector(), [])
        await conn.quote({"keyword": "basmati rice", "role": "supplier"})
        sent = conn.run.await_args.args[0]
        assert sent["searchQuery"] == "basmati rice exporter, IN\nbasmati rice manufacturer, IN"
        assert sent["preferRoles"][0] == "export"

    @pytest.mark.asyncio
    async def test_company_search_requires_queries(self):
        with pytest.raises(ValueError):
            await ap.ApifyCompanySearchConnector().quote({"role": "buyer"})

    @pytest.mark.asyncio
    async def test_counterparty_check_verdicts(self):
        conn = _rows(ap.ApifyCounterpartyCheckConnector(), [
            {"name": "Clean Co", "country": "DE", "verdict": "clear", "listsUnavailable": [], "lei": "5493001KJTIIGC8Y1R12",
             "vatNumber": "DE123", "vatValid": True},
            {"name": "Maybe Co", "country": "AE", "verdict": "clear", "listsUnavailable": ["UN"]},
            {"name": "Bad Co", "country": "IR", "verdict": "block", "verdictReason": "OFAC SDN hit",
             "sanctionsHits": [{"list": "OFAC_SDN", "entryName": "BAD CO", "score": 97}]},
        ])
        out = await conn.quote({"counterparties": [{"name": "Clean Co", "country": "DE", "vat_number": "DE123"}]})
        v = {r["name"]: r["verdict"] for r in out["results"]}
        assert v == {"Clean Co": "clear", "Maybe Co": "review", "Bad Co": "block"}
        assert out["results"][2]["hits"][0]["list"] == "OFAC_SDN"
        assert out["results"][0]["vat_valid"] is True
        sent = conn.run.await_args.args[0]
        assert sent["itemsList"][0]["vatNumber"] == "DE123" and sent["includeNoMatch"] is True

    @pytest.mark.asyncio
    async def test_email_qualifier_marks_missing_as_invalid(self):
        conn = _rows(ap.ApifyEmailQualifierConnector(), [
            {"email": "ok@acme.example", "verifierScore": 80, "mxFound": True},
            {"email": "risky@acme.example", "verifierScore": 70, "isCatchAll": True},
        ])
        out = await conn.quote({"emails": ["ok@acme.example", "RISKY@acme.example", "gone@acme.example"]})
        st = {r["email"]: r["status"] for r in out["emails"]}
        assert st == {"ok@acme.example": "ok", "risky@acme.example": "risky", "gone@acme.example": "invalid"}
        assert conn.run.await_args.args[0]["enableAi"] is False


# ---------------------------------------------------------------------------
# HTTP run flow (start → poll → dataset) against a mock transport
# ---------------------------------------------------------------------------

class TestRunFlow:
    def _client_factory(self, handler):
        real_client = httpx.AsyncClient  # captured before the module attribute is patched

        def factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), base_url="https://api.apify.com")
        return factory

    @pytest.mark.asyncio
    async def test_start_poll_and_read_dataset(self, monkeypatch):
        monkeypatch.setenv("APIFY_TOKEN", "tok")
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            assert request.headers["authorization"] == "Bearer tok"
            if request.method == "POST" and request.url.path.endswith("/runs"):
                return httpx.Response(201, json={"data": {"id": "run1", "status": "RUNNING"}})
            if request.url.path == "/v2/actor-runs/run1":
                return httpx.Response(200, json={"data": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1"}})
            if request.url.path == "/v2/datasets/ds1/items":
                return httpx.Response(200, json=[{"name": "A"}, {"name": "B"}, "junk"])
            return httpx.Response(404)

        monkeypatch.setattr(ap.httpx, "AsyncClient", self._client_factory(handler))
        conn = ap.ApifyUsImporterConnector()
        rows = await conn.run({"hsCodes": ["090422"]}, max_items=5)
        assert rows == [{"name": "A"}, {"name": "B"}]
        assert calls[0].startswith("POST /v2/acts/") and "run1" in calls[1] and calls[-1].endswith("/items")

    @pytest.mark.asyncio
    async def test_failed_run_raises(self, monkeypatch):
        monkeypatch.setenv("APIFY_TOKEN", "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"data": {"id": "run2", "status": "FAILED", "statusMessage": "boom"}})

        monkeypatch.setattr(ap.httpx, "AsyncClient", self._client_factory(handler))
        with pytest.raises(ap.ApifyError):
            await ap.ApifyExportMarketConnector().run({"hsCodes": ["1"]})

    @pytest.mark.asyncio
    async def test_webhook_signature(self, monkeypatch):
        monkeypatch.setenv("APIFY_WEBHOOK_SECRET", "s3cret")
        conn = ap.ApifyContactFinderConnector()
        body = json.dumps({"itemId": "x"}).encode()
        sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert await conn.webhook_verify({"x-signature": f"sha256={sig}"}, body) is True
        assert await conn.webhook_verify({"x-signature": "sha256=nope"}, body) is False


# ---------------------------------------------------------------------------
# Trade-Min wiring
# ---------------------------------------------------------------------------

class TestTradeMinWiring:
    def test_export_market_extraction(self):
        from services.trade_min.service import _extract_export_market, _pick_market_row
        row = {"market": "DE", "score": 64, "import_value_usd": 1e7, "unit_value_usd_per_mt": 4200.0,
               "exporter_world_unit_value_usd_per_mt": 3500.0, "latest_period": "2025", "growth_1y_pct": 8.0,
               "exporter_share_pct": 20.0}
        assert _pick_market_row({"markets": [row]}, "de") is row
        assert _pick_market_row({"markets": [row]}, "FR") is None
        score, signals, benchmark, gap = _extract_export_market(row, "090422", "DE", datetime.now(timezone.utc))
        assert score == 64 and benchmark == 4200.0 and gap == 20.0
        assert signals[0].source == "apify_export_markets" and "pays $4,200/MT" in signals[0].raw_summary

    def test_seller_price_normalised_to_per_mt(self):
        from services.trade_min.service import _normalise_price_per_mt, _seller_from_raw
        assert _normalise_price_per_mt(3.2, "USD/kg") == 3200.0
        assert _normalise_price_per_mt(1200.0, "USD/MT") == 1200.0
        assert _normalise_price_per_mt(None, "USD/kg") is None
        cand = _seller_from_raw({"company_id": "s1", "company_name": "X", "state": "Kerala", "min_price": 3.0,
                                 "max_price": 3.4, "price_unit": "USD/kg", "email": "a@x.in"},
                                "apify_india_suppliers", "090422")
        assert cand.estimated_fob_price_usd == 3200.0 and cand.email == "a@x.in" and cand.price_unit == "USD/MT"

    def test_match_seed_price_precedence(self):
        from services.trade_min.schemas import BuyerSignal, EnrichedOpportunity, SellerCandidate
        from services.trade_min.service import _build_match_seed
        now = datetime.now(timezone.utc)
        base = dict(opportunity_id="OPP-1", hs_code="090422", market="DE", scanned_at=now, demand_index=50.0,
                    price_gap_pct=20.0, source="comtrade", net_price_gap_pct=15.0, arbitrage_rank=60.0,
                    confidence_score=55.0)
        sig_kg = BuyerSignal(buyer_name="K", buyer_country="DE", signal_type="rfq", source="apify_alibaba_rfq",
                             urgency_tier=3, unit_price_usd=3.0, price_unit="USD/kg")
        sig_mt = BuyerSignal(buyer_name="M", buyer_country="DE", signal_type="manifest", source="apify_us_importers",
                             urgency_tier=2, unit_price_usd=3300.0, price_unit="USD/MT", fit_score=90,
                             website="https://m.example")
        opp = EnrichedOpportunity(**base, buyer_signals=[sig_kg, sig_mt], price_benchmark_usd_per_mt=4200.0,
                                  price_benchmark_source="apify_export_markets",
                                  seller_candidates=[SellerCandidate(candidate_id="s", company_name="S", location_city="",
                                                                     location_state="", hs_code="090422",
                                                                     source="apify_india_suppliers", email="s@x.in")])
        seed = _build_match_seed(opp)
        assert seed.buyer_target_price_usd == 3300.0 and seed.buyer_price_source == "buyer_signals"
        assert seed.top_buyers[0]["buyer_name"] == "M" and seed.top_sellers[0]["email"] == "s@x.in"
        opp2 = EnrichedOpportunity(**base, buyer_signals=[sig_kg], price_benchmark_usd_per_mt=4200.0,
                                   price_benchmark_source="apify_export_markets")
        seed2 = _build_match_seed(opp2)
        assert seed2.buyer_target_price_usd == 4200.0 and seed2.buyer_price_source == "apify_export_markets"

    def test_sources_endpoint_shape(self):
        from services.trade_min.service import describe_sources
        d = describe_sources()
        names = {s["name"] for s in d["sources"]}
        assert {"apify_us_importers", "apify_alibaba_rfq", "apify_export_markets", "apify_india_suppliers",
                "apify_contact_finder", "apify_company_search", "apify_counterparty_check",
                "apify_email_qualifier", "apify_carrier_tracking"} <= names
        assert d["apify"]["token_present"] is False
        assert all(s["mode"] in ("live", "stub", "open") for s in d["sources"])
        by_name = {s["name"]: s for s in d["sources"]}
        # contact finder and company search share one actor but stay separate entries
        assert by_name["apify_contact_finder"]["actor"] == by_name["apify_company_search"]["actor"]
        assert "not used" in by_name["apify_us_importers"]["detail"]

    def test_sources_mark_blocked_actors_stub_even_with_token(self, monkeypatch):
        from services.trade_min.service import describe_sources
        monkeypatch.setenv("APIFY_TOKEN", "tok")
        by_name = {s["name"]: s for s in describe_sources()["sources"]}
        assert by_name["apify_company_search"]["mode"] == "live"
        for blocked in ("apify_us_importers", "apify_alibaba_rfq", "apify_india_suppliers"):
            assert by_name[blocked]["mode"] == "stub"

    @pytest.mark.asyncio
    async def test_phase2_buyers_come_from_company_search(self):
        from services.trade_min import service as svc
        calls: dict[str, dict] = {}

        async def fake_quote(name, req):
            calls[name] = req
            if name == "apify_company_search":
                return {"companies": [{"company_name": "Al Turab Foods", "country": "AE", "domain": "alturabfood.com",
                                       "website": "https://alturabfood.com/", "email": "purchase@alturabfood.com",
                                       "phone": "+971 4 000", "fit_score": 80}]}
            return None

        with patch.object(svc, "_safe_quote", fake_quote):
            res = await svc._phase2_live_signals("100630", "AE", svc._Phase1Result(), svc.DEFAULT_CONFIG)
        assert "apify_us_importers" not in calls and "apify_alibaba_rfq" not in calls
        assert calls["apify_company_search"] == {"keyword": svc._keyword_for("100630"), "countries": ["AE"],
                                                 "role": "buyer", "max_results": 5}
        b = res.buyer_signals[0]
        assert (b.buyer_name, b.email, b.source, b.signal_type) == (
            "Al Turab Foods", "purchase@alturabfood.com", "apify_company_search", "web_search")

    @pytest.mark.asyncio
    async def test_no_web_search_for_a_bare_hs_number(self, monkeypatch):
        from services.trade_min import service as svc
        calls: list[str] = []

        async def fake_quote(name, req):
            calls.append(name)
            return None

        monkeypatch.setattr(svc, "_keyword_for", lambda hs: hs)   # no catalogue / map entry
        with patch.object(svc, "_safe_quote", fake_quote):
            await svc._phase2_live_signals("999999", "AE", svc._Phase1Result(), svc.DEFAULT_CONFIG)
            await svc._phase4_seller_discovery("999999", svc.DEFAULT_CONFIG)
        assert "apify_company_search" not in calls
        assert svc._web_searchable("rice") and not svc._web_searchable("100630")

    @pytest.mark.asyncio
    async def test_phase4_suppliers_come_from_company_search(self):
        from services.trade_min import service as svc
        calls: dict[str, dict] = {}

        async def fake_quote(name, req):
            calls[name] = req
            if name == "apify_company_search":
                return {"companies": [{"company_name": "Jashn Foods", "domain": "jashnfoods.com",
                                       "website": "https://www.jashnfoods.com/", "email": "info@jashnfoods.com"}]}
            return None

        with patch.object(svc, "_safe_quote", fake_quote):
            sellers = await svc._phase4_seller_discovery("100630", svc.DEFAULT_CONFIG)
        assert "apify_india_suppliers" not in calls
        assert calls["apify_company_search"]["role"] == "supplier"
        assert (sellers[0].company_name, sellers[0].email, sellers[0].source) == (
            "Jashn Foods", "info@jashnfoods.com", "apify_company_search")
