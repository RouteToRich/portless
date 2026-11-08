"""Trade-data connectors, Google Drive, the NATS event bus, the shared cache and knowledge memory."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from shared.bus import nats_bus as nb
from shared.bus.nats_bus import NatsJetStreamBus, _nak_delay, _normalize_url
from shared.cache import store as cache
from shared.connectors import demand_signals as ds
from shared.connectors import google_drive as gd
from shared.connectors import trade_intel as ti
from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import memory as mem

_REAL_CLIENT = httpx.AsyncClient


class _Http:
    def __init__(self):
        self.status, self.body, self.text = 200, {"ok": True}, None
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.body)

    def params(self, index=0):
        return dict(self.requests[index].url.params)

    def sent(self, index=0):
        return json.loads(self.requests[index].content)


@pytest.fixture
def http():
    handler = _Http()
    with patch("httpx.AsyncClient", side_effect=lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)):
        yield handler


@pytest.fixture
def keys(monkeypatch):
    for name in ("COMTRADE_SUBSCRIPTION_KEY", "VOLZA_API_KEY", "INDIAMART_API_KEY", "INDIAMART_MOBILE", "TRADEINDIA_API_KEY",
                 "CENSUS_API_KEY", "MACMAP_API_KEY", "TRADEMO_API_KEY", "IMPORTGENIUS_API_KEY", "ALIBABA_APP_KEY", "ALIBABA_APP_SECRET"):
        monkeypatch.setenv(name, "K")


class TestComtrade:
    async def test_quote_normalises_and_filters_on_india(self, keys, http):
        http.body = {"data": [{"reporterCode": 724, "reporterISO": "ESP", "cmdCode": "090422", "primaryValue": 5.0, "qty": 2}], "count": 9}
        result = await ti.ComtradeConnector().quote({"hs_code": "090422", "period": ["2022", "2023"]})
        assert (result["period"], result["total_records"], result["records"][0]["reporter_code"]) == ("2022,2023", 9, "724")
        params = http.params()
        assert params["partnerCode"] == "356" and params["flowCode"] == "M" and "reporterCode" not in params

    async def test_reporter_is_passed_when_given(self, keys, http):
        http.body = {"data": []}
        result = await ti.ComtradeConnector().quote({"hs_code": "1", "period": "2023", "reporter_code": "724", "freq": "M"})
        assert http.params()["reporterCode"] == "724" and "/C/M/HS" in str(http.requests[0].url) and result["total_records"] == 0

    async def test_status_growth(self, keys, http):
        http.body = {"data": [{"period": "2021", "primaryValue": 200.0}, {"period": "2020", "primaryValue": 100.0, "reporterDesc": "Spain"}]}
        result = await ti.ComtradeConnector().status("724:090422")
        assert result["yoy_growth_pct"] == 100.0 and [t["period"] for t in result["trend"]] == ["2020", "2021"] and result["reporter_name"] == "Spain"

    @pytest.mark.parametrize(("rows", "growth"), [
        ([], None), ([{"period": "2020", "primaryValue": 1.0}], None),
        ([{"period": "2020", "primaryValue": 0}, {"period": "2021", "primaryValue": 5.0}], None),
    ])
    async def test_status_without_a_base(self, keys, http, rows, growth):
        http.body = {"data": rows}
        assert (await ti.ComtradeConnector().status("724:1"))["yoy_growth_pct"] == growth

    async def test_bad_reference(self, keys):
        with pytest.raises(ValueError, match="reporter_m49"):
            await ti.ComtradeConnector().status("nocolon")


class TestVolza:
    async def test_quote(self, keys, http):
        http.body = {"shipments": [{"shipment_id": "1", "buyer_name": "B", "destination_country": "ES", "hsn_code": "09"}],
                     "api_pagination": {"total_records": 5, "has_next_page": True, "next_cursor": "c"}}
        result = await ti.VolzaConnector().quote({"hs_code": "09", "importer_country": "ES"})
        assert result["shipments"][0]["buyer_country"] == "ES" and result["pagination"]["next_cursor"] == "c"
        assert http.sent()["origin_country"] == "IN" and http.sent()["destination_country"] == "ES"
        assert http.requests[0].headers["authorization"] == "Bearer K"

    async def test_quote_other_exporter_and_empty(self, keys, http):
        http.body = {}
        result = await ti.VolzaConnector().quote({"hs_code": "09", "exporter_country": "VN"})
        assert http.sent()["origin_country"] == "VN" and "destination_country" not in http.sent()
        assert result["pagination"]["current_page"] == 1 and result["shipments"] == []

    async def test_status(self, keys, http):
        http.body = {"buyer_details": {"buyer_name": "B"}, "shipments": [{"x": 1}], "api_pagination": {"p": 1}}
        result = await ti.VolzaConnector().status("7")
        assert http.params()["volza_buyer_id"] == "7" and result["buyer_summary"] == {} and result["pagination"] == {"p": 1}


class TestIndiamart:
    async def test_quote_maps_keyword_and_caps_page_size(self, keys, http):
        http.body = {"Response": {"productList": [{"glusr_usr_id": "1", "comp_name": "C", "prod_name": "P", "min_price": "2.5", "max_price": "x"}]}, "total_count": 7}
        result = await ti.IndiamartConnector().quote({"hs_code": "090422", "page_size": 500, "state": "TN"})
        seller = result["sellers"][0]
        assert (result["total"], seller["company_id"], seller["min_price"], seller["max_price"]) == (7, "1", 2.5, None)
        assert http.params()["keyword"] == "ground pepper spices" and http.params()["page_size"] == "50" and http.params()["state"] == "TN"

    async def test_flat_list_and_keyword_override(self, keys, http):
        http.body = {"productList": [{"company_id": "2", "company_name": "D", "product_name": "Q", "listing_url": "u"}]}
        result = await ti.IndiamartConnector().quote({"hs_code": "1", "keyword": "salt"})
        assert http.params()["keyword"] == "salt" and result["total"] == 1 and result["sellers"][0]["listing_url"] == "u"

    async def test_book(self, keys, http):
        await ti.IndiamartConnector().book({"contact_id": "C", "message": "M"})
        assert http.sent()["buyer_name"] == "PortLess Trade Agent" and http.requests[0].headers["imapi-key"] == "K"

    async def test_status(self, keys, http):
        assert await ti.IndiamartConnector().status("1") == {"ok": True}
        assert http.params()["company_id"] == "1"


class TestTradeIndia:
    async def test_quote(self, keys, http):
        http.body = {"products": [{"id": 5, "company_name": "C", "name": "N", "price": "3", "moq": "1MT", "product_url": "u"}]}
        result = await ti.TradeIndiaConnector().quote({"hs_code": "090422", "limit": 99, "state": "KL"})
        seller = result["sellers"][0]
        assert (seller["company_id"], seller["contact_id"], seller["min_price"], seller["min_order_qty"], seller["listing_url"]) == ("TI-5", "TI-5-CTX", 3.0, "1MT", "u")
        assert http.params()["limit"] == "50" and http.params()["state"] == "KL" and result["total"] == 1

    async def test_book_and_status(self, keys, http):
        await ti.TradeIndiaConnector().book({"contact_id": "C", "message": "M"})
        assert http.sent()["contact_id"] == "C"
        assert await ti.TradeIndiaConnector().status("9") == {"ok": True}


class TestWits:
    PAYLOAD: ClassVar[dict] = {
        "dataSets": [{"series": {"0:0": {"observations": {"0": [1.5], "2": [2.13]}}}}],
        "structure": {"dimensions": {"observation": [{"values": [{"id": "2019"}, {"id": "2020"}, {"id": "2021"}]}]}},
    }

    def test_latest_observation(self):
        assert ti._parse_wits_sdmx_latest(self.PAYLOAD) == (2.13, "2021")

    @pytest.mark.parametrize("data", [{}, {"dataSets": [{}]}, {"dataSets": [{"series": {"0": {}}}]}])
    def test_no_data(self, data):
        assert ti._parse_wits_sdmx_latest(data) == (None, None)

    def test_year_unknown_when_periods_are_missing(self):
        assert ti._parse_wits_sdmx_latest({"dataSets": self.PAYLOAD["dataSets"]}) == (2.13, None)

    def test_empty_observation(self):
        assert ti._parse_wits_sdmx_latest({"dataSets": [{"series": {"0": {"observations": {"0": []}}}}]}) == (None, None)

    async def test_quote(self, http):
        http.body = self.PAYLOAD
        result = await ti.WitsTariffConnector().quote({"hs_code": "09041100", "reporter_iso2": "es"})
        assert (result["hs_code"], result["reporter_iso2"], result["applied_duty_pct"], result["year"]) == ("090411", "ES", 2.13, "2021")
        assert ".724.000.090411.reported" in str(http.requests[0].url)

    async def test_unmapped_market(self, http):
        assert (await ti.WitsTariffConnector().quote({"hs_code": "1", "reporter_iso2": "ZZ"}))["applied_duty_pct"] is None
        assert http.requests == []

    async def test_xml_error_means_unverified(self, http):
        http.text = "<error/>"
        assert (await ti.WitsTariffConnector().quote({"hs_code": "1", "reporter_iso2": "ES"}))["applied_duty_pct"] is None


class TestCensus:
    ROWS = (["CTY_CODE", "CTY_NAME", "GEN_VAL_MO", "GEN_CIF_MO", "I_COMMODITY"], ["5330", "INDIA", "100", "110", "090422"])

    async def test_quote(self, keys, http):
        http.body = list(self.ROWS)
        result = await ti.CensusTradeConnector().quote({"hs_code": "09042200", "time": "2026-01"})
        record = result["records"][0]
        assert (record["partner_name"], record["fob_value_usd"], record["primary_value_usd"], result["period"]) == ("INDIA", 100.0, 110.0, "2026-01")

    async def test_cif_missing_falls_back_to_fob(self, keys, http):
        http.body = [["CTY_NAME", "GEN_VAL_MO"], ["INDIA", "100"]]
        assert (await ti.CensusTradeConnector().quote({"hs_code": "1"}))["records"][0]["primary_value_usd"] == 100.0

    async def test_columns_absent(self, keys, http):
        http.body = [["OTHER"], ["x"]]
        record = (await ti.CensusTradeConnector().quote({"hs_code": "1"}))["records"][0]
        assert (record["partner_name"], record["fob_value_usd"], record["cif_value_usd"]) == ("", None, None)

    async def test_us_only(self, keys, http):
        assert (await ti.CensusTradeConnector().quote({"hs_code": "1", "market": "ES"}))["records"] == [] and http.requests == []

    @pytest.mark.parametrize("body", [{"a": 1}, [["only header"]]])
    async def test_empty_answers(self, keys, http, body):
        http.body = body
        assert (await ti.CensusTradeConnector().quote({"hs_code": "1"}))["total_records"] == 0

    async def test_not_json(self, keys, http):
        http.text = "error"
        assert (await ti.CensusTradeConnector().quote({"hs_code": "1"}))["total_records"] == 0


class TestAccess2Markets:
    @pytest.fixture
    def configured(self, monkeypatch):
        monkeypatch.setenv("ACCESS2MARKETS_API_URL", "https://x.test/{partner}/{reporter}/{product}")
        monkeypatch.setenv("ACCESS2MARKETS_API_KEY", "K")

    async def test_not_configured_falls_back(self, monkeypatch, http):
        monkeypatch.delenv("ACCESS2MARKETS_API_URL", raising=False)
        assert (await ti.Access2MarketsConnector().quote({"hs_code": "1"}))["applied_duty_pct"] is None and http.requests == []

    async def test_full_answer(self, configured, http):
        http.body = {"appliedDuty": "4.5%", "ntms": [{"code": "A"}, {"description": "B"}], "rulesOfOrigin": {"rule": "R"}}
        result = await ti.Access2MarketsConnector().quote({"hs_code": "09041100", "reporter_iso2": "es"})
        assert (result["applied_duty_pct"], result["ntb_flags"], result["rules_of_origin"]) == (4.5, ["A", "B"], "R")
        assert http.requests[0].url.path == "/IN/ES/090411" and http.requests[0].headers["authorization"] == "Bearer K"

    async def test_plain_text_measure_is_listed(self, configured, http):
        http.body = {"ntms": ["licence required"]}
        assert (await ti.Access2MarketsConnector().quote({"hs_code": "1"}))["ntb_flags"] == ["licence required"]

    async def test_duties_list_and_text_rules(self, configured, http):
        http.body = {"duties": [{"rate": 3}], "ntbs": "none", "roo": "wholly obtained"}
        result = await ti.Access2MarketsConnector().quote({"hs_code": "1"})
        assert (result["applied_duty_pct"], result["ntb_flags"], result["rules_of_origin"]) == (3.0, [], "wholly obtained")

    async def test_nothing_useful(self, configured, http):
        http.body = {"x": 1}
        assert (await ti.Access2MarketsConnector().quote({"hs_code": "1"}))["applied_duty_pct"] is None

    async def test_no_key_sends_no_auth(self, monkeypatch, http):
        monkeypatch.setenv("ACCESS2MARKETS_API_URL", "https://x.test/{product}")
        monkeypatch.delenv("ACCESS2MARKETS_API_KEY", raising=False)
        await ti.Access2MarketsConnector().quote({"hs_code": "1"})
        assert "authorization" not in http.requests[0].headers

    async def test_failure_falls_back(self, configured, http):
        http.status = 500
        assert (await ti.Access2MarketsConnector().quote({"hs_code": "1"}))["applied_duty_pct"] is None


class TestSmallConnectors:
    async def test_macmap(self, keys, http):
        assert await ti.MacmapConnector().quote({"hs_code": "1", "reporter_iso2": "ES", "partner_iso2": "IN"}) == {"ok": True}
        assert http.requests[0].headers["ocp-apim-subscription-key"] == "K"

    async def test_trademo(self, keys, http):
        await ti.TrademoConnector().quote({"hs_code": "1"})
        assert http.sent() == {"hs_code": "1", "buyer_country": "", "limit": 50}
        await ti.TrademoConnector().status("E")
        assert http.requests[1].url.path.endswith("/companies/E/risk-profile")

    async def test_importgenius(self, keys, http):
        await ti.ImportGeniusConnector().quote({"hs_code": "1"})
        assert "date_from" not in http.params()
        await ti.ImportGeniusConnector().quote({"hs_code": "1", "date_from": "2026-01-01"})
        assert http.params(1)["date_from"] == "2026-01-01"
        await ti.ImportGeniusConnector().status("E")
        assert http.requests[2].url.path.endswith("/companies/E/shipments")

    @pytest.mark.parametrize(("value", "expected"), [(None, None), ("x", None), ([], None), ("1.5", 1.5), (2, 2.0)])
    def test_safe_float(self, value, expected):
        assert ti._safe_float(value) == expected

    @pytest.mark.parametrize(("code", "default", "expected"), [
        ("290543", None, "glycerine"), ("290511", None, "glycerol alcohol"), ("999999", None, "999999"), ("999999", "", ""), (None, "d", "d"),
    ])
    def test_hs_keyword(self, code, default, expected):
        assert ti.hs_keyword(code, default) == expected

    READ_ONLY = (
        ti.ComtradeConnector, ti.VolzaConnector, ti.WitsTariffConnector, ti.CensusTradeConnector, ti.Access2MarketsConnector,
        ti.MacmapConnector, ti.TrademoConnector, ti.ImportGeniusConnector, ds.AlibabaRfqConnector, ds.GdeltDisruptionConnector, ds.EuTedConnector,
    )

    @pytest.mark.parametrize("cls", READ_ONLY)
    async def test_read_only_sources_cannot_book(self, keys, cls):
        with pytest.raises(NotImplementedError):
            await cls().book({})

    @pytest.mark.parametrize("cls", [*READ_ONLY, ti.IndiamartConnector, ti.TradeIndiaConnector])
    async def test_no_webhooks(self, keys, cls):
        with pytest.raises(NotImplementedError):
            await cls().webhook_verify({}, b"")

    @pytest.mark.parametrize("cls", [ti.WitsTariffConnector, ti.CensusTradeConnector, ti.Access2MarketsConnector, ti.MacmapConnector])
    async def test_no_status(self, keys, cls):
        with pytest.raises(NotImplementedError):
            await cls().status("x")


class TestAlibaba:
    def test_signature_is_sorted_hmac(self, keys):
        expected = hmac.new(b"K", b"a1b2", hashlib.sha256).hexdigest().upper()
        assert ds.AlibabaRfqConnector()._sign({"b": 2, "a": 1}) == expected

    async def test_quote(self, keys, http):
        http.body = {"result": {"data": [{"id": "R", "subject": "S", "countryName": "ES", "memberRank": 3}], "total": 4}}
        result = await ds.AlibabaRfqConnector().quote({"query": "pepper", "country": "ES", "category_id": 7, "page_size": 99})
        assert (result["total"], result["rfqs"][0]["rfq_id"], result["rfqs"][0]["verified_buyer"]) == (4, "R", True)
        params = http.params()
        assert (params["pageSize"], params["countryCode"], params["categoryId"]) == ("20", "ES", "7") and params["sign"]

    async def test_flat_answer(self, keys, http):
        http.body = {"buyOfferList": [{"buyOfferId": "B", "title": "T", "country": "DE", "publishTime": "p", "expiryDate": "e"}]}
        rfq = (await ds.AlibabaRfqConnector().quote({"query": "x"}))["rfqs"][0]
        assert (rfq["rfq_id"], rfq["title"], rfq["buyer_country"], rfq["posted_at"], rfq["verified_buyer"]) == ("B", "T", "DE", "p", False)

    async def test_status(self, keys, http):
        assert await ds.AlibabaRfqConnector().status("B") == {"ok": True}
        assert http.params()["buyOfferId"] == "B"


class TestGdelt:
    @pytest.mark.parametrize(("count", "strength", "detected"), [(0, "LOW", False), (2, "LOW", True), (3, "MEDIUM", True), (6, "MEDIUM", True), (7, "HIGH", True)])
    async def test_signal_strength(self, http, count, strength, detected):
        http.body = {"articles": [{"title": "T", "relevance": "0.5"}] * count}
        result = await ds.GdeltDisruptionConnector().quote({"origin_country": "Vietnam"})
        assert (result["signal_strength"], result["disruption_detected"], result["article_count"]) == (strength, detected, count)

    async def test_query(self, http):
        http.body = {}
        await ds.GdeltDisruptionConnector().quote({"origin_country": "Vietnam", "product_keyword": "pepper", "max_records": 5})
        query = http.params()
        assert '"Vietnam"' in query["query"] and '"pepper"' in query["query"] and query["maxrecords"] == "5"

    async def test_status(self, http):
        await ds.GdeltDisruptionConnector().status("Vietnam")
        assert http.params()["mode"] == "timelinevol"


class TestEuTed:
    async def test_quote(self, http):
        http.body = {"notices": [{"ND": "N", "TI": {"en": "Title"}, "AU_NAME": {"es": "Nombre"}, "AU_COUNTRY": "ES", "VALUE_COST": {"VALUE": 5, "CURRENCY": "USD"}}], "total": 3}
        result = await ds.EuTedConnector().quote({"query": "pepper", "country": "ES"})
        tender = result["tenders"][0]
        assert (result["total"], tender["title"], tender["buyer_name"], tender["contract_value"], tender["currency"]) == (3, "Title", "Nombre", 5, "USD")
        assert http.sent()["query"] == "pepper AND AU_COUNTRY:ES"

    async def test_sparse_notice(self, http):
        http.body = {"notices": [{}]}
        tender = (await ds.EuTedConnector().quote({"query": "x"}))["tenders"][0]
        assert (tender["title"], tender["buyer_name"], tender["contract_value"], tender["currency"]) == ("", "", None, "EUR")

    async def test_status(self, http):
        assert await ds.EuTedConnector().status("N1") == {"ok": True}


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

class TestDriveHelpers:
    @pytest.mark.parametrize(("uri", "expected"), [
        (None, None), ("", None), ("gdrive://ABC", "ABC"), ("gdrive://", None),
        ("https://drive.google.com/file/d/AB_c-1/view", "AB_c-1"), ("https://drive.google.com/open?id=XY", "XY"),
        ("file:///tmp/a.html", None),
    ])
    def test_file_id(self, uri, expected):
        assert gd.extract_drive_file_id(uri) == expected

    @pytest.mark.parametrize("missing", ["GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN"])
    def test_needs_all_credentials(self, monkeypatch, missing):
        for name in ("GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN"):
            monkeypatch.setenv(name, "x")
        assert gd.GoogleDriveConnector().is_configured() is True
        monkeypatch.delenv(missing)
        assert gd.GoogleDriveConnector().is_configured() is False

    def test_singleton(self, monkeypatch):
        monkeypatch.setattr(gd, "_connector", None)
        assert gd.get_connector() is gd.get_connector()

    def test_service_is_built_once(self):
        connector = gd.GoogleDriveConnector()
        with patch("googleapiclient.discovery.build", return_value="svc") as build:
            assert connector._get_service() == "svc" and connector._get_service() == "svc"
        build.assert_called_once()


class TestDriveOperations:
    @pytest.fixture
    def files(self):
        return MagicMock()

    @pytest.fixture
    def connector(self, files):
        connector = gd.GoogleDriveConnector()
        connector._service = MagicMock(files=MagicMock(return_value=files))
        return connector

    def test_existing_folder_is_reused(self, connector, files):
        files.list.return_value.execute.return_value = {"files": [{"id": "F1"}]}
        assert connector._find_or_create_folder("O'Neil", "P") == "F1"
        query = files.list.call_args.kwargs["q"]
        assert "O\\'Neil" in query and "'P' in parents" in query
        files.create.assert_not_called()

    def test_folder_is_created(self, connector, files):
        files.list.return_value.execute.return_value = {"files": []}
        files.create.return_value.execute.return_value = {"id": "F2"}
        assert connector._find_or_create_folder("D1", "P") == "F2"
        assert files.create.call_args.kwargs["body"]["parents"] == ["P"]

    def test_root_folder_has_no_parent(self, connector, files):
        files.list.return_value.execute.return_value = {"files": []}
        files.create.return_value.execute.return_value = {"id": "R"}
        assert connector._root_folder() == "R" and connector._root_folder() == "R"
        assert "parents" not in files.create.call_args.kwargs["body"]
        files.create.assert_called_once()

    def test_folder_failure(self, connector, files):
        files.list.side_effect = RuntimeError("quota")
        with pytest.raises(gd.GoogleDriveError, match="quota"):
            connector._find_or_create_folder("x", None)

    def test_deal_folder_is_cached(self, connector):
        with patch.object(connector, "_find_or_create_folder", return_value="DF") as find:
            connector._root_folder_id = "R"
            assert connector._deal_folder("D1") == "DF" and connector._deal_folder("D1") == "DF"
        find.assert_called_once_with("D1", "R")

    def test_upload_new(self, connector, files):
        connector._deal_folder_ids["D1"] = "DF"
        files.list.return_value.execute.return_value = {"files": []}
        files.create.return_value.execute.return_value = {"id": "X", "webViewLink": "L"}
        assert connector.upload_document("D1", "pi.html", "<p>") == {"file_id": "X", "web_view_link": "L"}

    def test_upload_overwrites_the_same_name(self, connector, files):
        connector._deal_folder_ids["D1"] = "DF"
        files.list.return_value.execute.return_value = {"files": [{"id": "OLD"}]}
        files.update.return_value.execute.return_value = {"id": "OLD"}
        assert connector.upload_document("D1", "pi.html", "<p>") == {"file_id": "OLD", "web_view_link": ""}
        files.create.assert_not_called()

    def test_upload_failure(self, connector, files):
        connector._deal_folder_ids["D1"] = "DF"
        files.list.side_effect = RuntimeError("down")
        with pytest.raises(gd.GoogleDriveError, match="pi.html"):
            connector.upload_document("D1", "pi.html", "<p>")

    @pytest.mark.parametrize(("mime", "expected"), [("text/html", "text/html"), (None, "application/octet-stream")])
    def test_download(self, connector, files, mime, expected):
        files.get.return_value.execute.return_value = {"mimeType": mime}

        class Downloader:
            def __init__(self, buffer, _request):
                buffer.write(b"data")

            def next_chunk(self):
                return None, True

        with patch("googleapiclient.http.MediaIoBaseDownload", Downloader):
            assert connector.download_file("X") == (b"data", expected)

    def test_download_failure(self, connector, files):
        files.get.side_effect = RuntimeError("gone")
        with pytest.raises(gd.GoogleDriveError, match="X"):
            connector.download_file("X")


# ---------------------------------------------------------------------------
# NATS JetStream
# ---------------------------------------------------------------------------

def _event(topic=EventTopic.DEAL_CREATED):
    return DomainEvent(topic=topic, deal_id="D1", payload={"a": 1})


def _msg(event=None, delivered=1, data=None):
    body = data if data is not None else (event or _event()).model_dump_json().encode()
    return MagicMock(data=body, metadata=SimpleNamespace(num_delivered=delivered), ack=AsyncMock(), nak=AsyncMock(), term=AsyncMock(), in_progress=AsyncMock())


@pytest.fixture
def bus():
    return NatsJetStreamBus(url="tls://host:4222", max_deliver=3, ack_wait=30.0, fetch_timeout=0.01)


class TestNatsHelpers:
    @pytest.mark.parametrize(("attempt", "delay"), [(0, 5.0), (1, 5.0), (2, 15.0), (4, 60.0), (99, 60.0)])
    def test_backoff(self, attempt, delay):
        assert _nak_delay(attempt) == delay

    @pytest.mark.parametrize(("url", "expected"), [
        ("tls://connect.ngs.global", "tls://connect.ngs.global:4222"), ("tls://h:1234", "tls://h:1234"), ("nothing", "nothing"),
    ])
    def test_port_is_made_explicit(self, url, expected):
        assert _normalize_url(url) == expected

    def test_names(self, bus):
        assert bus._subject(EventTopic.DEAL_CREATED) == f"portless.{EventTopic.DEAL_CREATED.value}"
        assert "." not in bus._durable(EventTopic.DEAL_CREATED)

    @pytest.mark.parametrize(("msg", "expected"), [(SimpleNamespace(metadata=SimpleNamespace(num_delivered="3")), 3), (object(), 1)])
    def test_delivery_attempt(self, msg, expected):
        assert NatsJetStreamBus._delivery_attempt(msg) == expected

    async def test_safe_swallows_broker_errors(self):
        await NatsJetStreamBus._safe(AsyncMock(side_effect=RuntimeError("gone")))


class TestNatsPublish:
    async def test_published_with_dedupe_header(self, bus):
        bus._js = MagicMock(publish=AsyncMock())
        event = _event()
        await bus.publish(event)
        call = bus._js.publish.await_args
        assert call.args[0] == bus._subject(event.topic) and call.kwargs["headers"] == {"Nats-Msg-Id": event.event_id}

    async def test_not_connected_runs_local_handlers(self, bus):
        handler = AsyncMock()
        bus.subscribe(EventTopic.DEAL_CREATED, handler)
        await bus.publish(_event())
        await asyncio.gather(*bus._local_tasks)
        handler.assert_awaited_once()

    async def test_broker_failure_never_raises(self, bus):
        bus._js = MagicMock(publish=AsyncMock(side_effect=RuntimeError("down")))
        await bus.publish(_event())

    async def test_local_handler_failure_is_contained(self):
        await NatsJetStreamBus._run_local(AsyncMock(side_effect=RuntimeError("x")), _event())


class TestNatsProcess:
    async def test_success_acks(self, bus):
        handler = AsyncMock()
        bus.subscribe(EventTopic.DEAL_CREATED, handler)
        msg = _msg()
        await bus._process(EventTopic.DEAL_CREATED, msg)
        msg.ack.assert_awaited_once()
        handler.assert_awaited_once()

    async def test_poison_message_is_terminated(self, bus):
        msg = _msg(data=b"{not json")
        await bus._process(EventTopic.DEAL_CREATED, msg)
        msg.term.assert_awaited_once()
        msg.ack.assert_not_awaited()

    async def test_failure_is_redelivered_with_backoff(self, bus):
        bus.subscribe(EventTopic.DEAL_CREATED, AsyncMock(side_effect=RuntimeError("x")))
        msg = _msg(delivered=2)
        await bus._process(EventTopic.DEAL_CREATED, msg)
        msg.nak.assert_awaited_once_with(delay=15.0)
        msg.ack.assert_not_awaited()

    async def test_last_attempt_is_dead_lettered(self, bus):
        bus.subscribe(EventTopic.DEAL_CREATED, AsyncMock(side_effect=RuntimeError("boom")))
        bus._js = MagicMock(publish=AsyncMock())
        msg = _msg(delivered=3)
        await bus._process(EventTopic.DEAL_CREATED, msg)
        msg.term.assert_awaited_once()
        sent = json.loads(bus._js.publish.await_args.args[1])
        assert sent["topic"] == EventTopic.DEAL_EXCEPTION.value and sent["payload"]["error"] == "boom"

    async def test_exception_events_never_create_more_exceptions(self, bus):
        bus.subscribe(EventTopic.DEAL_EXCEPTION, AsyncMock(side_effect=RuntimeError("x")))
        bus._js = MagicMock(publish=AsyncMock())
        await bus._process(EventTopic.DEAL_EXCEPTION, _msg(_event(EventTopic.DEAL_EXCEPTION), delivered=3))
        bus._js.publish.assert_not_awaited()

    async def test_dead_letter_failure_is_contained(self, bus):
        with patch.object(bus, "publish", new=AsyncMock(side_effect=RuntimeError("x"))):
            await bus._emit_dead_letter(EventTopic.DEAL_CREATED, _event(), RuntimeError("y"))

    @pytest.mark.parametrize("heartbeat_error", [None, RuntimeError("x")])
    async def test_long_handler_gets_heartbeats(self, bus, heartbeat_error):
        msg = _msg()
        msg.in_progress = AsyncMock(side_effect=heartbeat_error)
        bus._ack_wait = 0.003

        async def slow(_event):
            await asyncio.sleep(1.1)

        bus.subscribe(EventTopic.DEAL_CREATED, slow)
        await bus._run_handlers_with_heartbeat(EventTopic.DEAL_CREATED, _event(), msg)
        assert msg.in_progress.await_count >= 1


class TestNatsConsumeLoop:
    async def test_messages_are_processed_until_closing(self, bus):
        msg = _msg()

        async def fetch(*_a, **_k):
            bus._closing = True
            return [msg]

        with patch.object(bus, "_process", new=AsyncMock()) as process:
            await bus._consume_loop(EventTopic.DEAL_CREATED, MagicMock(fetch=fetch))
        process.assert_awaited_once()

    async def test_timeout_is_normal(self, bus):
        from nats.errors import TimeoutError as NatsTimeout
        calls = 0

        async def fetch(*_a, **_k):
            nonlocal calls
            calls += 1
            if calls == 2:
                bus._closing = True
                return []
            raise NatsTimeout

        await bus._consume_loop(EventTopic.DEAL_CREATED, MagicMock(fetch=fetch))
        assert calls == 2

    async def test_fetch_error_retries(self, bus):
        calls = 0

        async def fetch(*_a, **_k):
            nonlocal calls
            calls += 1
            if calls == 2:
                bus._closing = True
                return []
            raise RuntimeError("net")

        with patch.object(nb.asyncio, "sleep", new=AsyncMock()) as sleep:
            await bus._consume_loop(EventTopic.DEAL_CREATED, MagicMock(fetch=fetch))
        sleep.assert_awaited_once()

    async def test_fetch_error_while_closing_exits(self, bus):
        async def fetch(*_a, **_k):
            bus._closing = True
            raise RuntimeError("closed")

        await bus._consume_loop(EventTopic.DEAL_CREATED, MagicMock(fetch=fetch))

    async def test_cancellation_propagates(self, bus):
        with pytest.raises(asyncio.CancelledError):
            await bus._consume_loop(EventTopic.DEAL_CREATED, MagicMock(fetch=AsyncMock(side_effect=asyncio.CancelledError)))


class TestNatsConnect:
    async def test_connect_and_callbacks(self, bus):
        client = MagicMock(jetstream=MagicMock(return_value=MagicMock()))
        with patch("nats.connect", new=AsyncMock(return_value=client)) as connect, patch.object(bus, "_ensure_stream", new=AsyncMock()):
            await bus.connect()
        options = connect.await_args.kwargs
        assert options["servers"] == ["tls://host:4222"]
        await options["error_cb"](RuntimeError("e"))
        await options["disconnected_cb"]()
        await options["reconnected_cb"]()
        await options["closed_cb"]()

    async def test_stream_is_created_when_missing(self, bus):
        from nats.js.errors import NotFoundError
        bus._js = MagicMock(stream_info=AsyncMock(side_effect=NotFoundError), add_stream=AsyncMock())
        await bus._ensure_stream()
        kwargs = bus._js.add_stream.await_args.kwargs
        assert kwargs["subjects"] == ["portless.>"] and kwargs["max_bytes"] > 0

    async def test_missing_subject_is_added(self, bus):
        info = SimpleNamespace(config=SimpleNamespace(subjects=["other.>"]))
        bus._js = MagicMock(stream_info=AsyncMock(return_value=info), update_stream=AsyncMock())
        await bus._ensure_stream()
        assert info.config.subjects == ["other.>", "portless.>"]

    async def test_stream_already_right(self, bus):
        info = SimpleNamespace(config=SimpleNamespace(subjects=["portless.>"]))
        bus._js = MagicMock(stream_info=AsyncMock(return_value=info), update_stream=AsyncMock())
        await bus._ensure_stream()
        bus._js.update_stream.assert_not_awaited()

    async def test_one_durable_consumer_per_topic_then_close(self, bus):
        bus.subscribe(EventTopic.DEAL_CREATED, AsyncMock())
        bus.subscribe(EventTopic.DEAL_EXCEPTION, AsyncMock())
        bus._js = MagicMock(pull_subscribe=AsyncMock(return_value=MagicMock(fetch=AsyncMock(return_value=[]))))
        await bus.start_consumers()
        assert bus._js.pull_subscribe.await_count == 2 and len(bus._consumer_tasks) == 2
        bus._nc = MagicMock(drain=AsyncMock())
        await bus.close()
        assert bus._consumer_tasks == [] and bus._nc is None and bus._js is None

    async def test_close_survives_a_failed_drain(self, bus):
        bus._nc = MagicMock(drain=AsyncMock(side_effect=RuntimeError("x")))
        await bus.close()
        assert bus._nc is None

    async def test_close_without_connection(self, bus):
        await bus.close()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestCacheBackends:
    @pytest.fixture(params=["memory", "mongo"])
    def backend(self, request):
        return cache.InMemoryCacheBackend() if request.param == "memory" else cache.MongoCacheBackend(AsyncMongoMockClient()["c"]["cache"])

    async def test_round_trip(self, backend):
        await backend.set("n", "k", {"a": 1}, 60)
        assert await backend.get("n", "k") == {"a": 1}

    async def test_miss(self, backend):
        assert await backend.get("n", "x") is None

    async def test_expired_is_a_miss(self, backend):
        await backend.set("n", "k", 1, -1)
        assert await backend.get("n", "k") is None

    async def test_delete(self, backend):
        await backend.set("n", "k", 1, 60)
        await backend.delete("n", "k")
        assert await backend.get("n", "k") is None

    async def test_clear_namespace_only(self, backend):
        await backend.set("a", "k", 1, 60)
        await backend.set("b", "k", 2, 60)
        assert await backend.clear("a") == 1
        assert await backend.get("b", "k") == 2

    async def test_clear_all(self, backend):
        await backend.set("a", "k", 1, 60)
        await backend.set("b", "k", 2, 60)
        assert await backend.clear() == 2

    async def test_naive_expiry_is_utc(self):
        col = AsyncMongoMockClient()["c"]["cache"]
        await col.insert_one({"_id": "n:k", "payload": 1, "expires_at": (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)})
        assert await cache.MongoCacheBackend(col).get("n", "k") == 1


class TestCacheFacade:
    @pytest.fixture(autouse=True)
    def _reset(self):
        saved = cache._backend, cache._motor_client
        cache._backend, cache._motor_client = None, None
        yield
        cache._backend, cache._motor_client = saved

    def test_key_ignores_dict_order(self):
        assert cache.make_key({"a": 1, "b": 2}) == cache.make_key({"b": 2, "a": 1})
        assert cache.make_key("a") != cache.make_key("b")

    async def test_memory_without_uri_and_singleton(self):
        with patch.object(cache.cache_settings.__class__, "resolved_uri", return_value=""):
            first = await cache.get_backend()
            assert isinstance(first, cache.InMemoryCacheBackend) and await cache.init_cache() is first

    async def test_mongo_and_close(self):
        client = MagicMock()
        client.__getitem__.return_value = AsyncMongoMockClient()["c"]
        with patch.object(cache.cache_settings.__class__, "resolved_uri", return_value="mongodb://x"), patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=client):
            assert isinstance(await cache.init_cache(), cache.MongoCacheBackend)
        await cache.close_cache()
        client.close.assert_called_once()
        assert cache._backend is None

    async def test_connection_failure_uses_memory(self):
        with patch.object(cache.cache_settings.__class__, "resolved_uri", return_value="mongodb://x"), patch("motor.motor_asyncio.AsyncIOMotorClient", side_effect=RuntimeError("no")):
            assert isinstance(await cache.init_cache(), cache.InMemoryCacheBackend)
        assert cache._motor_client is None

    def test_reset_for_tests(self):
        backend = cache.InMemoryCacheBackend()
        cache.reset_for_tests(backend)
        assert cache._backend is backend
        cache.reset_for_tests()
        assert cache._backend is not backend

    @pytest.fixture
    def enabled(self, monkeypatch):
        monkeypatch.setattr(cache.cache_settings, "enabled", True)
        cache.reset_for_tests()

    async def test_get_and_set(self, enabled):
        await cache.cache_set("n", "k", 1, 60)
        assert await cache.cache_get("n", "k") == 1

    async def test_disabled(self, monkeypatch):
        monkeypatch.setattr(cache.cache_settings, "enabled", False)
        cache.reset_for_tests()
        await cache.cache_set("n", "k", 1, 60)
        assert await cache._backend.get("n", "k") is None and await cache.cache_get("n", "k") is None

    async def test_errors_are_swallowed(self, enabled):
        broken = MagicMock(get=AsyncMock(side_effect=RuntimeError), set=AsyncMock(side_effect=RuntimeError), clear=AsyncMock(side_effect=RuntimeError))
        cache.reset_for_tests(broken)
        assert await cache.cache_get("n", "k") is None
        await cache.cache_set("n", "k", 1, 60)
        assert await cache.cache_clear() == 0

    async def test_clear(self, enabled):
        await cache.cache_set("n", "k", 1, 60)
        assert await cache.cache_clear("n") == 1

    async def test_decorator_caches_truthy_results(self, enabled):
        source = AsyncMock(return_value={"v": 1})

        @cache.cached("d", 60)
        async def lookup(x):
            return await source(x)

        assert await lookup(1) == {"v": 1} and await lookup(1) == {"v": 1}
        source.assert_awaited_once()

    async def test_decorator_does_not_cache_empty(self, enabled):
        source = AsyncMock(return_value={})

        @cache.cached("d", 60, key_func=lambda x: f"k{x}")
        async def lookup(x):
            return await source(x)

        await lookup(1)
        await lookup(1)
        assert source.await_count == 2


# ---------------------------------------------------------------------------
# Knowledge memory
# ---------------------------------------------------------------------------

NEG_ID = "12345678-1234-1234-1234-123456789012"


class TestDealFromSubject:
    async def test_buyer_token(self):
        assert await mem._deal_from_subject("Hi [BUY:DEAL-1:C9]") == ("DEAL-1", "C9")

    async def test_rfq_token(self):
        with patch("services.sourcing.service.get_rfq", new=AsyncMock(return_value=SimpleNamespace(deal_id="D2"))):
            assert await mem._deal_from_subject("[RFQ:RFQ-1]") == ("D2", None)

    async def test_rfq_lookup_failure(self):
        with patch("services.sourcing.service.get_rfq", new=AsyncMock(side_effect=RuntimeError)):
            assert await mem._deal_from_subject("[RFQ:RFQ-1]") == (None, None)

    @pytest.mark.parametrize(("session", "expected"), [(SimpleNamespace(deal_id="D3"), ("D3", None)), (None, (None, None))])
    async def test_negotiation_token(self, session, expected):
        repo = MagicMock(get_session=AsyncMock(return_value=session))
        with patch("agents.negotiation_agent.repository.get_repository", return_value=repo):
            assert await mem._deal_from_subject(f"[NEG:{NEG_ID}]") == expected

    async def test_negotiation_lookup_failure(self):
        with patch("agents.negotiation_agent.repository.get_repository", side_effect=RuntimeError):
            assert await mem._deal_from_subject(f"[NEG:{NEG_ID}]") == (None, None)

    @pytest.mark.parametrize("subject", ["plain", "", None])
    async def test_no_token(self, subject):
        assert await mem._deal_from_subject(subject) == (None, None)


class TestMemoryWrites:
    @pytest.fixture
    def knowledge(self):
        with patch.object(mem, "knowledge") as fake:
            fake.enabled.return_value = True
            yield fake

    def test_slug(self):
        assert mem._slug("a b/c") == "a-b-c" and mem._slug(None) == "unknown" and mem._slug("///") == "unknown"

    async def test_deal_meta(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(hs_code="1520", market="ES"))):
            assert await mem._deal_meta("D") == {"deal_id": "D", "hs_code": "1520", "market": "ES"}
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError)):
            assert await mem._deal_meta("D") == {"deal_id": "D"}
        assert await mem._deal_meta(None) == {}

    async def test_email_is_tagged_with_the_counterparty(self, knowledge):
        with patch.object(mem, "_deal_meta", new=AsyncMock(return_value={"deal_id": "D", "hs_code": "1", "market": "ES"})):
            await mem.remember_email(direction="outbound", from_address="me@p.io", to_address="Ann@Acme.com", subject="[BUY:D:C1] Hi", body="b", party="buyer", message_id="<m1>")
        text = knowledge.remember.call_args.args[0]
        meta = knowledge.remember.call_args.kwargs["metadata"]
        assert "Counterparty role: buyer" in text and "Deal: D" in text
        assert (meta["counterparty_domain"], meta["counterparty_address"], meta["counterparty_id"]) == ("acme.com", "ann@acme.com", "C1")
        assert knowledge.remember.call_args.kwargs["path"].startswith("emails/D/")

    async def test_inbound_keeps_only_the_new_reply(self, knowledge):
        with patch("agents.negotiation_agent.guards._new_reply_text", return_value="just this"):
            await mem.remember_email(direction="inbound", from_address="a@b.c", to_address="me@p.io", subject="s", body="long quoted")
        text = knowledge.remember.call_args.args[0]
        assert text.endswith("just this") and knowledge.remember.call_args.kwargs["metadata"]["counterparty_address"] == "a@b.c"

    async def test_inbound_cleaner_failure_keeps_the_body(self, knowledge):
        with patch("agents.negotiation_agent.guards._new_reply_text", side_effect=RuntimeError):
            await mem.remember_email(direction="inbound", from_address="a@b.c", to_address="x", subject="s", body="whole")
        assert knowledge.remember.call_args.args[0].endswith("whole")

    async def test_disabled_writes_nothing(self, knowledge):
        knowledge.enabled.return_value = False
        await mem.remember_email(direction="outbound", from_address="a", to_address="b", subject="s", body="b")
        await mem.remember_deal_summary(SimpleNamespace(), "x")
        assert await mem.context_for(purpose="p") == ""
        knowledge.remember.assert_not_called()

    async def test_email_failure_is_contained(self, knowledge):
        knowledge.remember.side_effect = RuntimeError("down")
        await mem.remember_email(direction="outbound", from_address="a", to_address="b", subject="s", body="b")

    @pytest.mark.parametrize(("task", "queued"), [(object(), True), (None, False)])
    def test_note(self, knowledge, task, queued):
        knowledge.remember.return_value = task
        assert mem.remember_note(subject_type="buyer", subject_id="acme.com", text="late", author="ops", extra={"x": 1}) is queued
        meta = knowledge.remember.call_args.kwargs["metadata"]
        assert meta["kind"] == "note" and meta["x"] == 1
        assert "by ops" in knowledge.remember.call_args.args[0]

    def test_product(self, knowledge):
        mem.remember_product(SimpleNamespace(hs_code="1520", name="Glycerine", products=["a", "b"], markets=[], notes="n", grades=("g1",)))
        text = knowledge.remember.call_args.args[0]
        assert "Product: Glycerine (HS 1520)" in text and "a, b" in text and "Grades: g1" in text and "Target markets" not in text

    def test_product_failure_is_contained(self, knowledge):
        knowledge.remember.side_effect = RuntimeError
        mem.remember_product(SimpleNamespace(hs_code="1", name="x"))

    @staticmethod
    def _deal(**kw):
        party = lambda role, name: SimpleNamespace(role=role, legal_name=name, country="ES")
        base = {
            "deal_id": "D1", "hs_code": "1520", "market": "ES", "quantity_mt": 20, "status": "PAID",
            "parties": [party("buyer", "Buy Co"), party("supplier", "Sup Co")],
            "buyer_target_price_usd": 900.0, "buyer_price_source": "reply", "realized_margin_usd": 1000.0,
            "selected_pair": {"score": {"supplier_price": {"value": 700}, "margin_pct": 9, "probability": 0.6}},
            "status_history": [SimpleNamespace(status="MATCHING", reason=None), SimpleNamespace(status=SimpleNamespace(value="PAID"), reason="done")],
        }
        return SimpleNamespace(**{**base, **kw})

    async def test_deal_summary(self, knowledge):
        await mem.remember_deal_summary(self._deal(), "signed", reason="won")
        text = knowledge.remember.call_args.args[0]
        for piece in ("Buyer: Buy Co (ES)", "Supplier: Sup Co", "USD 900.00/MT (reply)", "margin 9%", "Realised margin: USD 1,000.00", "MATCHING → PAID (done)", "Reason: won"):
            assert piece in text
        assert knowledge.remember.call_args.kwargs["metadata"]["kind"] == "deal_summary"

    async def test_sparse_deal_summary(self, knowledge):
        deal = self._deal(parties=[], buyer_target_price_usd=None, realized_margin_usd=None, selected_pair=None, status_history=[], quantity_mt=None)
        await mem.remember_deal_summary(deal, "ended")
        text = knowledge.remember.call_args.args[0]
        assert "Buyer: not decided" in text and "Supplier: not decided" in text and "Path:" not in text and "? MT" in text

    async def test_summary_failure_is_contained(self, knowledge):
        await mem.remember_deal_summary(SimpleNamespace(deal_id="D"), "x")

    async def test_context_query(self, knowledge):
        knowledge.recall = AsyncMock(return_value=[{"t": 1}])
        knowledge.as_context.return_value = "CTX"
        assert await mem.context_for(purpose="draft", product="oil", hs_code="1520", market="ES", counterparties=["A", None], extra="more") == "CTX"
        assert knowledge.recall.await_args.args[0] == "draft oil HS 1520 market ES about A more"

    async def test_context_filter_scopes_results(self, knowledge):
        knowledge.recall = AsyncMock(return_value=[{"ok": 1}, {"ok": 0}, {"ok": 1}])
        knowledge.as_context.side_effect = lambda results, max_chars: len(results)
        assert await mem.context_for(purpose="p", top_k=1, accept=lambda r: r["ok"]) == 1
        assert knowledge.recall.await_args.kwargs["top_k"] == 3
