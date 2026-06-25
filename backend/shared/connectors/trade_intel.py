"""Trade-intelligence connectors (global demand & buyer discovery).

See Appendix 8.1 of the master specification. All adapters implement the uniform
``Connector`` interface.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from .base import Connector


class ComtradeConnector(Connector):
    """UN Comtrade: global trade volumes, HS-code flow data, and YoY demand growth.

    Used by Trade-Min Agent to detect import surges — e.g. "Turmeric imports in
    Poland grew 40 % YoY" — and to identify which countries are buying the most
    of a specific HS code from India, before the platform commits to a deal.

    API documentation : https://comtradeapi.un.org
    Developer portal  : https://comtradedeveloper.un.org
    Authentication    : ``subscription-key`` query-string parameter
                        HOW TO GET KEY (free tier, no payment required):
                          1. Create account at https://comtradeplus.un.org
                          2. Visit https://comtradedeveloper.un.org
                          3. Products → Free APIs → Subscribe (auto-approved)
                          4. Key available at /profile → Show keys
                        Premium subscribers: key auto-issued on developer portal login;
                          email comtrade@un.org if not received within 1 hour.
    Env var           : COMTRADE_SUBSCRIPTION_KEY
    Pricing           : Free  — 500 API calls/day, up to 100K records/call.
                        Premium — unlimited calls, up to 250K records/call.
    """

    name = "comtrade"
    _BASE = "https://comtradeapi.un.org"
    _INDIA_M49 = "356"   # M49 numeric code for India (default exporter/partner)
    _WORLD_M49 = "0"     # Partner code 0 = all countries

    def __init__(self) -> None:
        self._key = os.environ["COMTRADE_SUBSCRIPTION_KEY"]

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Detect which countries are importing an HS code from India (demand-surge scan).

        Calls ``GET /data/v1/get/C/{freq}/HS`` with ``flowCode=M`` and
        ``partnerCode=356`` (India) so every row returned is one reporter country
        importing the HS code from India.

        Expected request keys:
            hs_code        (str)            – 6-digit HS code, e.g. ``"090422"``
            period         (str|list[str])  – year(s), e.g. ``"2023"`` or ``["2022","2023"]``
            reporter_code  (str)            – M49 reporter; omit for all countries (default)
            freq           (str)            – ``"A"`` annual (default) or ``"M"`` monthly
            flow_code      (str)            – ``"M"`` imports (default) or ``"X"`` exports
            partner_code   (str)            – M49 partner, default ``"356"`` (India)
            max_records    (int)            – max records returned, default 500

        Returns a normalised dict ready for the UI demand-surge table::

            {
              "flow":          "M",
              "period":        "2023",
              "hs_code":       "090422",
              "total_records": 42,
              "records": [
                {
                  "reporter_code":     "724",
                  "reporter_iso3":     "ESP",
                  "reporter_name":     "Spain",
                  "partner_code":      "356",
                  "partner_iso3":      "IND",
                  "partner_name":      "India",
                  "period":            "2023",
                  "hs_code":           "090422",
                  "product_desc":      "Pepper, crushed or ground",
                  "flow":              "M",
                  "qty_kg":            16000.0,
                  "net_weight_kg":     16000.0,
                  "fob_value_usd":     44000.0,
                  "cif_value_usd":     46500.0,
                  "primary_value_usd": 46500.0
                },
                ...
              ]
            }
        """
        period = request.get("period", "2023")
        period_str: str = ",".join(period) if isinstance(period, list) else period

        freq = request.get("freq", "A")
        flow_code = request.get("flow_code", "M")
        partner_code = request.get("partner_code", self._INDIA_M49)
        reporter_code = request.get("reporter_code", self._WORLD_M49)
        max_records = request.get("max_records", 500)

        params: dict[str, Any] = {
            "cmdCode": request["hs_code"],
            "period": period_str,
            "flowCode": flow_code,
            "partnerCode": partner_code,
            "maxrecords": max_records,
            "subscription-key": self._key,
        }
        if reporter_code and reporter_code != self._WORLD_M49:
            params["reporterCode"] = reporter_code

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/data/v1/get/C/{freq}/HS",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        raw: list[dict[str, Any]] = data.get("data", [])

        normalised = [
            {
                "reporter_code":     str(r.get("reporterCode", "")),
                "reporter_iso3":     r.get("reporterISO", ""),
                "reporter_name":     r.get("reporterDesc", ""),
                "partner_code":      str(r.get("partnerCode", "")),
                "partner_iso3":      r.get("partnerISO", ""),
                "partner_name":      r.get("partnerDesc", ""),
                "period":            r.get("period", ""),
                "hs_code":           r.get("cmdCode", ""),
                "product_desc":      r.get("cmdDesc", ""),
                "flow":              r.get("flowCode", ""),
                "qty_kg":            r.get("qty"),
                "net_weight_kg":     r.get("netWgt"),
                "fob_value_usd":     r.get("fobvalue"),
                "cif_value_usd":     r.get("cifvalue"),
                "primary_value_usd": r.get("primaryValue"),
            }
            for r in raw
        ]

        return {
            "flow":          flow_code,
            "period":        period_str,
            "hs_code":       request["hs_code"],
            "total_records": data.get("count", len(normalised)),
            "records":       normalised,
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("UN Comtrade is a read-only data source.")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Fetch a 5-year YoY import trend for a specific reporter–HS code pair.

        Used by the Trade-Min Agent to calculate demand-growth velocity before
        scoring an arbitrage opportunity.

        ext_ref: Encoded as ``"<reporter_m49>:<hs_code>"`` e.g. ``"724:090422"``
                 (Spain importing HS 090422 from India over the last 5 years).

        Returns a time-series dict suited for a trend chart on the UI::

            {
              "reporter_code":  "724",
              "reporter_name":  "Spain",
              "hs_code":        "090422",
              "partner":        "India",
              "yoy_growth_pct": 9.8,
              "trend": [
                {"period": "2019", "primary_value_usd": 41000.0, "net_weight_kg": 14000.0, ...},
                {"period": "2020", "primary_value_usd": 43000.0, "net_weight_kg": 15000.0, ...},
                ...
              ]
            }
        """
        try:
            reporter_code, hs_code = ext_ref.split(":", 1)
        except ValueError as exc:
            raise ValueError(
                "ext_ref must be '<reporter_m49>:<hs_code>', e.g. '724:090422'"
            ) from exc

        from datetime import date as _date
        current_year = _date.today().year
        period_str = ",".join(str(y) for y in range(current_year - 5, current_year))

        params: dict[str, Any] = {
            "cmdCode":          hs_code,
            "reporterCode":     reporter_code,
            "partnerCode":      self._INDIA_M49,
            "period":           period_str,
            "flowCode":         "M",
            "maxrecords":       500,
            "subscription-key": self._key,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/data/v1/get/C/A/HS",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        raw = sorted(data.get("data", []), key=lambda r: r.get("period", ""))

        trend = [
            {
                "period":            r.get("period", ""),
                "primary_value_usd": r.get("primaryValue"),
                "net_weight_kg":     r.get("netWgt"),
                "fob_value_usd":     r.get("fobvalue"),
                "cif_value_usd":     r.get("cifvalue"),
            }
            for r in raw
        ]

        yoy_growth_pct: float | None = None
        if len(trend) >= 2:
            v_last = trend[-1].get("primary_value_usd") or 0
            v_prev = trend[-2].get("primary_value_usd") or 0
            if v_prev:
                yoy_growth_pct = round((v_last - v_prev) / v_prev * 100, 2)

        return {
            "reporter_code":  reporter_code,
            "reporter_name":  raw[0].get("reporterDesc", "") if raw else "",
            "hs_code":        hs_code,
            "partner":        "India",
            "yoy_growth_pct": yoy_growth_pct,
            "trend":          trend,
        }

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("UN Comtrade does not issue webhooks.")


class VolzaConnector(Connector):
    """Volza: Bill of Lading and customs shipping manifests for 80+ countries.

    Used by Trade-Min Agent to extract the corporate names of active buyers who
    are already importing a specific HS code from India, so the platform can
    send warm, verified outreach instead of cold prospecting.

    API documentation : https://docs.volza.com
    Base URL          : https://backend.volza.com
    Authentication    : Bearer token
                        → Sign in to app.volza.com → API Settings → Generate Key
                          OR email helpdesk@volza.com for enterprise access.
    Env var           : VOLZA_API_KEY
    Pricing           : $0.10 per search + from $0.02 per shipment record returned.
    Rate limit        : 50 req/min (Standard) / 150 req/min (Custom plan).
    """

    name = "volza"
    _BASE = "https://backend.volza.com"

    def __init__(self) -> None:
        self._key = os.environ["VOLZA_API_KEY"]

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Search global shipment manifests for active importers of an HS code.

        Calls the **Global Trade Shipments API** (POST /api/v1/global-shipments).

        Expected request keys:
            hs_code          (str) – 6-digit HS/HSN code, e.g. ``"090422"``
            start_date       (str) – ISO-8601 date, e.g. ``"2024-01-01"``
            end_date         (str) – ISO-8601 date, e.g. ``"2024-12-31"``
            importer_country (str) – destination country ISO-2, e.g. ``"ES"`` (optional)
            exporter_country (str) – origin country ISO-2, default ``"IN"`` (optional)
            page             (int) – page number, default 1
            page_size        (int) – records per page (max 1 000), default 50

        Returns a normalised dict ready for the UI opportunities table::

            {
              "shipments": [
                {
                  "shipment_id":          "76456564",
                  "shipment_date":        "2025-07-27T00:00:00",
                  "buyer_name":           "Iberia Spices SL",
                  "buyer_country":        "Spain",
                  "supplier_name":        "Guntur Agri Products Ltd",
                  "product_description":  "Ground red pepper",
                  "hs_code":              "090422",
                  "value_usd":            45000.0,
                  "unit_price_usd":       2.8,
                  "gross_weight_kg":      16000.0,
                  "quantity_unit":        "KGS",
                  "origin_country":       "India",
                  "origin_port":          "Chennai",
                  "destination_port":     "Valencia",
                  "shipment_mode":        "FCL"
                },
                ...
              ],
              "pagination": {
                "total_records": 3200,
                "current_page":  1,
                "page_size":     50,
                "has_next_page": true,
                "next_cursor":   "eyJTb3J0VmFsdWUi..."
              }
            }
        """
        payload: dict[str, Any] = {
            "hsn_code": request["hs_code"],
            "start_date": request.get("start_date", "2024-01-01"),
            "end_date": request.get("end_date", "2024-12-31"),
            "page": request.get("page", 1),
            "page_size": request.get("page_size", 50),
        }
        if "importer_country" in request:
            payload["destination_country"] = request["importer_country"]
        if "exporter_country" in request:
            payload["origin_country"] = request["exporter_country"]
        else:
            payload["origin_country"] = "IN"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._BASE}/api/v1/global-shipments",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        raw_shipments: list[dict[str, Any]] = data.get("shipments", [])
        pagination: dict[str, Any] = data.get("api_pagination", {})

        normalised = [
            {
                "shipment_id": s.get("shipment_id"),
                "shipment_date": s.get("shipment_date"),
                "buyer_name": s.get("buyer_name"),
                "buyer_country": s.get("destination_country"),
                "supplier_name": s.get("supplier_name"),
                "product_description": s.get("product_description"),
                "hs_code": s.get("hsn_code"),
                "value_usd": s.get("value_usd"),
                "unit_price_usd": s.get("unit_price_usd"),
                "gross_weight_kg": s.get("gross_weight_kg"),
                "quantity_unit": s.get("quantity_unit"),
                "origin_country": s.get("origin_country"),
                "origin_port": s.get("origin_port"),
                "destination_port": s.get("destination_port"),
                "shipment_mode": s.get("shipment_mode"),
            }
            for s in raw_shipments
        ]

        return {
            "shipments": normalised,
            "pagination": {
                "total_records": pagination.get("total_records", 0),
                "current_page": pagination.get("current_page", 1),
                "page_size": pagination.get("page_size", 50),
                "has_next_page": pagination.get("has_next_page", False),
                "next_cursor": pagination.get("next_cursor"),
            },
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Volza is a read-only data source; booking is not applicable.")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Fetch the enriched buyer profile and KPIs for a given Volza Company ID.

        Calls the **Buyer Insights API** (GET /api/v1/buyer-insights).

        ext_ref: The ``volza_buyer_id`` integer (as a string) found inside a
                 ``quote()`` shipment record.

        Returns a dict ready for the opportunity detail / buyer profile page::

            {
              "buyer_details": {
                "buyer_name":     "Iberia Spices SL",
                "volza_buyer_id": 7839201,
                "buyer_address":  "Calle Gran Via 12, Madrid, Spain",
                "buyer_country":  "Spain",
                "total_contacts": 5
              },
              "buyer_summary": {
                "shipment_count":          420,
                "total_value_usd":         3800000,
                "hsn_code_count":          12,
                "supplier_count":          38,
                "origin_country_count":    7,
                "origin_port_count":       9,
                "destination_port_count":  3
              },
              "summary_insights": {
                "top_10_hsn_codes":         [...],
                "top_10_suppliers":         [...],
                "top_10_origin_countries":  [...],
                "top_10_origin_ports":      [...],
                "top_10_destination_ports": [...]
              },
              "shipments":  [...],
              "pagination": {...}
            }
        """
        params: dict[str, Any] = {
            "volza_buyer_id": int(ext_ref),
            "sort_by": "shipment_date",
            "sort_order": "DESC",
            "page_size": 25,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/api/v1/buyer-insights",
                headers=self._headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "buyer_details": data.get("buyer_details") or {},
            "buyer_summary": data.get("buyer_summary") or {},
            "summary_insights": data.get("summary_insights") or {},
            "shipments": data.get("shipments") or [],
            "pagination": data.get("api_pagination") or {},
        }

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("Volza does not issue webhooks.")


class IndiamartConnector(Connector):
    """IndiaMART: India's largest wholesale B2B directory for seller discovery.

    Used by Trade-Min Agent (Phase 4) to find verified Indian manufacturers
    who supply a specific HS code and can receive automated RFQs.

    API documentation : https://seller.indiamart.com/lmsrest/
    Base URL          : https://seller.indiamart.com/lmsrest/api/v1
    Authentication    : IMAPI-KEY header + registered mobile number
                        HOW TO GET ACCESS:
                          1. Register as a seller at seller.indiamart.com
                          2. Navigate to Settings → API Settings
                          3. Generate API key (auto-approved for verified sellers)
    Env vars          : INDIAMART_API_KEY, INDIAMART_MOBILE
    Pricing           : Included in seller subscription (Gold/Platinum plan).
    Rate limit        : 1 000 calls/day on standard plan.
    """

    name = "indiamart"
    _BASE = "https://seller.indiamart.com/lmsrest/api/v1"

    def __init__(self) -> None:
        self._key = os.environ["INDIAMART_API_KEY"]
        self._mobile = os.environ["INDIAMART_MOBILE"]

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "IMAPI-KEY": self._key,
            "Accept": "application/json",
        }

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Search IndiaMART product catalog for verified suppliers of an HS code.

        Maps the HS code to a keyword for IndiaMART's product-keyword search.
        Returns seller candidates with company details, pricing, and capacity.

        Expected request keys:
            hs_code      (str) – 6-digit HS code, e.g. ``"090422"``
            keyword      (str) – product keyword override (optional)
            state        (str) – filter by Indian state, e.g. ``"Tamil Nadu"`` (optional)
            page         (int) – page number, default 1
            page_size    (int) – results per page (max 50), default 20

        Returns a normalised dict ready for seller-candidate assembly::

            {
              "total": 142,
              "sellers": [
                {
                  "company_id":    "IM-78932",
                  "company_name":  "Guntur Agri Products Ltd",
                  "city":          "Guntur",
                  "state":         "Andhra Pradesh",
                  "product_name":  "Ground Red Pepper",
                  "hs_code":       "090422",
                  "min_price":     2.5,
                  "max_price":     3.2,
                  "price_unit":    "USD/kg",
                  "min_order_qty": "500 KG",
                  "annual_capacity_mt": 480.0,
                  "listing_url":   "https://www.indiamart.com/...",
                  "contact_id":    "78932-SELLER"
                },
                ...
              ]
            }
        """
        keyword = request.get("keyword") or _HS_TO_KEYWORD.get(
            request["hs_code"][:4], request["hs_code"]
        )
        params: dict[str, Any] = {
            "mobile_no": self._mobile,
            "api_key":   self._key,
            "keyword":   keyword,
            "page":      request.get("page", 1),
            "page_size": min(request.get("page_size", 20), 50),
        }
        if "state" in request:
            params["state"] = request["state"]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/product/catalog/search",
                headers=self._headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        raw: list[dict[str, Any]] = (
            data.get("Response", {}).get("productList", [])
            or data.get("productList", [])
            or []
        )

        sellers = [
            {
                "company_id":         p.get("glusr_usr_id") or p.get("company_id", ""),
                "company_name":       p.get("comp_name") or p.get("company_name", ""),
                "city":               p.get("city", ""),
                "state":              p.get("state", ""),
                "product_name":       p.get("prod_name") or p.get("product_name", ""),
                "hs_code":            request["hs_code"],
                "min_price":          _safe_float(p.get("min_price")),
                "max_price":          _safe_float(p.get("max_price")),
                "price_unit":         p.get("price_unit", "USD/kg"),
                "min_order_qty":      p.get("min_order_qty", ""),
                "annual_capacity_mt": _safe_float(p.get("annual_capacity")),
                "listing_url":        p.get("canonical_url") or p.get("listing_url", ""),
                "contact_id":         p.get("contact_id", ""),
            }
            for p in raw
        ]

        return {
            "total":   data.get("total_count", len(sellers)),
            "sellers": sellers,
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Dispatch an automated RFQ to a seller on IndiaMART.

        Expected request keys:
            contact_id   (str) – seller contact ID from ``quote()``
            message      (str) – RFQ message text
            quantity     (str) – requested quantity, e.g. ``"500 KG"``
            buyer_name   (str) – name of the prospective buyer
            buyer_email  (str) – buyer's contact email
        """
        payload: dict[str, Any] = {
            "mobile_no":   self._mobile,
            "api_key":     self._key,
            "contact_id":  request["contact_id"],
            "message":     request["message"],
            "quantity":    request.get("quantity", ""),
            "buyer_name":  request.get("buyer_name", "PortLess Trade Agent"),
            "buyer_email": request.get("buyer_email", ""),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._BASE}/rfq/send",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Retrieve company profile + product listings for an IndiaMART company ID.

        ext_ref: IndiaMART company ID (``glusr_usr_id``) from ``quote()``.
        """
        params: dict[str, Any] = {
            "mobile_no":  self._mobile,
            "api_key":    self._key,
            "company_id": ext_ref,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/company/profile",
                headers=self._headers,
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("IndiaMART does not issue outbound webhooks.")


class TradeIndiaConnector(Connector):
    """TradeIndia: fallback wholesale directory for Indian supplier discovery.

    Used by Trade-Min Agent (Phase 4) as a secondary supplier source when
    IndiaMART yields insufficient quotes or for alternative price benchmarking.

    API documentation : https://www.tradeindia.com/api-docs/
    Base URL          : https://api.tradeindia.com/v1
    Authentication    : Bearer token
                        HOW TO GET ACCESS:
                          1. Register a company account at tradeindia.com
                          2. Navigate to My Account → API Access → Generate Token
                          3. Token valid for 90 days; refresh via /auth/refresh
    Env var           : TRADEINDIA_API_KEY
    Pricing           : Included in paid membership (Gold/Platinum plan).
    Rate limit        : 500 calls/day on standard plan.
    """

    name = "tradeindia"
    _BASE = "https://api.tradeindia.com/v1"

    def __init__(self) -> None:
        self._key = os.environ["TRADEINDIA_API_KEY"]

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        }

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Search TradeIndia product listings for verified Indian suppliers.

        Expected request keys:
            hs_code      (str) – 6-digit HS code
            keyword      (str) – product keyword override (optional)
            state        (str) – filter by Indian state (optional)
            page         (int) – page number, default 1
            limit        (int) – results per page (max 50), default 20

        Returns normalised seller candidates in the same schema as
        ``IndiamartConnector.quote()`` for drop-in interchangeability::

            {
              "total": 87,
              "sellers": [
                {
                  "company_id":    "TI-44210",
                  "company_name":  "Kerala Spice Exports Pvt Ltd",
                  "city":          "Kochi",
                  "state":         "Kerala",
                  "product_name":  "Black Pepper Ground",
                  "hs_code":       "090422",
                  "min_price":     2.3,
                  "max_price":     2.9,
                  "price_unit":    "USD/kg",
                  "min_order_qty": "1 MT",
                  "annual_capacity_mt": 600.0,
                  "listing_url":   "https://www.tradeindia.com/...",
                  "contact_id":    "TI-44210-CTX"
                },
                ...
              ]
            }
        """
        keyword = request.get("keyword") or _HS_TO_KEYWORD.get(
            request["hs_code"][:4], request["hs_code"]
        )
        params: dict[str, Any] = {
            "keyword": keyword,
            "page":    request.get("page", 1),
            "limit":   min(request.get("limit", 20), 50),
            "country": "IN",
        }
        if "state" in request:
            params["state"] = request["state"]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/products/search",
                headers=self._headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        raw: list[dict[str, Any]] = data.get("products", [])

        sellers = [
            {
                "company_id":         f"TI-{p.get('company_id', p.get('id', ''))}",
                "company_name":       p.get("company_name", ""),
                "city":               p.get("city", ""),
                "state":              p.get("state", ""),
                "product_name":       p.get("product_name") or p.get("name", ""),
                "hs_code":            request["hs_code"],
                "min_price":          _safe_float(p.get("min_price") or p.get("price")),
                "max_price":          _safe_float(p.get("max_price")),
                "price_unit":         p.get("price_unit", "USD/kg"),
                "min_order_qty":      p.get("min_order") or p.get("moq", ""),
                "annual_capacity_mt": _safe_float(p.get("annual_capacity")),
                "listing_url":        p.get("url") or p.get("product_url", ""),
                "contact_id":         f"TI-{p.get('company_id', p.get('id', ''))}-CTX",
            }
            for p in raw
        ]

        return {
            "total":   data.get("total", len(sellers)),
            "sellers": sellers,
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Dispatch an RFQ to a supplier via TradeIndia.

        Expected request keys:
            contact_id   (str) – seller contact ID from ``quote()``
            message      (str) – RFQ message text
            quantity     (str) – requested quantity
            buyer_name   (str) – name of the prospective buyer
            buyer_email  (str) – buyer's contact email
        """
        payload: dict[str, Any] = {
            "contact_id":  request["contact_id"],
            "message":     request["message"],
            "quantity":    request.get("quantity", ""),
            "buyer_name":  request.get("buyer_name", "PortLess Trade Agent"),
            "buyer_email": request.get("buyer_email", ""),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._BASE}/rfq/submit",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Fetch company profile and all product listings for a TradeIndia company.

        ext_ref: TradeIndia company ID (numeric, as a string) from ``quote()``.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/companies/{ext_ref}",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("TradeIndia does not issue outbound webhooks.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> float | None:
    """Convert a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Maps 4-digit HS prefix → product keyword for IndiaMART / TradeIndia search.
# Extend this table as new HS codes are added to the default scan list.
_HS_TO_KEYWORD: dict[str, str] = {
    "0904": "ground pepper spices",
    "0902": "tea leaves",
    "0910": "mixed spices cardamom",
    "1001": "wheat grain",
    "2905": "glycerol alcohol",
    "0901": "coffee beans",
    "0306": "shrimp prawn seafood",
    "6109": "cotton t-shirts garments",
    "8517": "mobile phone electronics",
    "2709": "crude petroleum oil",
    "7108": "gold bullion precious metal",
    "3004": "pharmaceutical medicines",
    "8471": "computers laptops",
    "6203": "mens suits trousers garments",
}


class MacmapConnector(Connector):
    """ITC Market Access Map: import tariffs, trade barriers, and rules of origin.

    Used by Trade-Min Agent to verify target-country duty rates before committing
    to a deal: CIF Cost + Tariff must remain competitive.

    API reference : https://api.macmap.org/v2
    Registration  : https://macmap.org/tools/macmap-api
    Env var       : MACMAP_API_KEY  (Ocp-Apim-Subscription-Key)
    """

    name = "macmap"
    _BASE = "https://api.macmap.org/v2"

    def __init__(self) -> None:
        self._key = os.environ["MACMAP_API_KEY"]

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Fetch applied tariff rates and non-tariff barriers for an HS code.

        Expected request keys:
            hs_code        (str) – 6-digit HS code, e.g. "090422"
            reporter_iso2  (str) – importing country ISO-2, e.g. "ES"
            partner_iso2   (str) – exporting country ISO-2, e.g. "IN"

        Returns applied MFN / preferential duty rates, tariff quotas, and NTBs.
        """
        params = {
            "reporter": request["reporter_iso2"],
            "partner": request["partner_iso2"],
            "product": request["hs_code"],
            "lang": "EN",
        }
        headers = {"Ocp-Apim-Subscription-Key": self._key}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/tariffs/applied",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("MacMap is a read-only data source.")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError("MacMap is a read-only data source.")

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("MacMap does not issue webhooks.")


class TrademoConnector(Connector):
    """Trademo Intel: supply chain records, buyer risk profiling, trade frequencies.

    Used by Trade-Min Agent to identify active, high-volume Western buyers and
    assess their supply-chain risk before issuing a Proforma Invoice.

    API reference : https://docs.trademo.com
    Env var       : TRADEMO_API_KEY
    """

    name = "trademo"
    _BASE = "https://platform.trademo.com/api/v1"

    def __init__(self) -> None:
        self._key = os.environ["TRADEMO_API_KEY"]

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Search for high-volume buyers by HS code and importing country.

        Expected request keys:
            hs_code        (str) – 6-digit HS code
            buyer_country  (str) – ISO-2 country of importing nation
            limit          (int) – max records returned, default 50

        Returns list of company records with trade frequency and risk scores.
        """
        headers = {"X-API-Key": self._key, "Accept": "application/json"}
        payload: dict[str, Any] = {
            "hs_code": request["hs_code"],
            "buyer_country": request.get("buyer_country", ""),
            "limit": request.get("limit", 50),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._BASE}/buyers/search",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Trademo is a read-only data source.")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Fetch the full risk profile for a company by its Trademo entity ID.

        ext_ref: Trademo entity identifier returned inside a ``quote()`` response.
        """
        headers = {"X-API-Key": self._key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/companies/{ext_ref}/risk-profile",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("Trademo does not issue webhooks.")


class ImportGeniusConnector(Connector):
    """ImportGenius / Panjiva: US and Latin-American customs shipping manifests.

    Used by Trade-Min Agent to spotlight high-volume importers who repeatedly
    buy bulk categories from India (BoL / US Customs manifest data).

    API reference : Enterprise REST API — importgenius.com / panjiva.com
    Env var       : IMPORTGENIUS_API_KEY
    """

    name = "importgenius"
    _BASE = "https://api.importgenius.com/v2"

    def __init__(self) -> None:
        self._key = os.environ["IMPORTGENIUS_API_KEY"]

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Search US/LatAm customs manifests for high-volume importers of an HS code.

        Expected request keys:
            hs_code    (str) – 6-digit HS code
            country    (str) – destination country ISO-2, default "US"
            date_from  (str) – ISO-8601 date, default 12 months ago
            limit      (int) – max results, default 100

        Returns list of importer records with shipment count and net weight.
        """
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        params: dict[str, Any] = {
            "hs_code": request["hs_code"],
            "country": request.get("country", "US"),
            "limit": request.get("limit", 100),
        }
        if "date_from" in request:
            params["date_from"] = request["date_from"]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/manifests/importers",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("ImportGenius is a read-only data source.")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Retrieve full shipment history for an importer by ImportGenius entity ID.

        ext_ref: entity ID returned inside a ``quote()`` response.
        """
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/companies/{ext_ref}/shipments",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("ImportGenius does not issue webhooks.")
