"""Live demand-signal connectors: active RFQs, supply disruptions, and tender alerts.

These connectors target Level 3 and Level 4 buying-intent signals — identifying
buyers who are searching for a product RIGHT NOW, rather than relying on
historical shipment manifests alone.

    Level 3 — Active Intent  : AlibabaRfqConnector, EuTedConnector
    Level 4 — Forced Intent  : GdeltDisruptionConnector

Pipeline position (Trade-Min Agent):

    ComtradeConnector.quote()      → which COUNTRIES have rising demand
           ↓
    VolzaConnector.quote()         → which COMPANIES in those countries bought before
           ↓
    ReorderWindowEngine.compute()  → which of those companies is DUE to buy again
           ↓
    AlibabaRfqConnector.quote()    → which companies posted an RFQ RIGHT NOW
    GdeltDisruptionConnector.quote()→ which origin countries face disruption (forced demand)
    EuTedConnector.quote()         → which EU institutions issued a tender RIGHT NOW
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

import httpx

from .base import Connector


# ---------------------------------------------------------------------------
# AlibabaRfqConnector
# ---------------------------------------------------------------------------

class AlibabaRfqConnector(Connector):
    """Alibaba International B2B Platform: active buyer RFQ and leads feed.

    Polls buy-offers (RFQs) posted by international buyers on alibaba.com,
    filtered by product keyword or category.  A live RFQ is the strongest
    possible buying-intent signal — the buyer is actively sourcing RIGHT NOW.

    API documentation : https://developer.alibaba.com
    Base URL          : https://gw.api.alibaba.com/openapi/param2/1
    Authentication    : App Key + HMAC-SHA256 request signature
                        HOW TO GET ACCESS:
                          1. Visit https://developer.alibaba.com
                          2. Register as a developer → Create an Application
                          3. Apply for the "International Trade" API product
                          4. App Key and App Secret in the app dashboard
    Env vars          : ALIBABA_APP_KEY, ALIBABA_APP_SECRET
    """

    name = "alibaba_rfq"
    _BASE = "https://gw.api.alibaba.com/openapi/param2/1"

    def __init__(self) -> None:
        self._app_key = os.environ["ALIBABA_APP_KEY"]
        self._app_secret = os.environ["ALIBABA_APP_SECRET"]

    def _sign(self, params: dict[str, Any]) -> str:
        """Generate HMAC-SHA256 request signature for Alibaba Open Platform.

        Algorithm:
          1. Sort all params alphabetically by key.
          2. Concatenate as ``key1value1key2value2...`` (no separators).
          3. HMAC-SHA256 of the concatenated string using ``app_secret`` as key.
          4. Return uppercase hex digest.
        """
        sorted_payload = "".join(f"{k}{v}" for k, v in sorted(params.items()))
        return hmac.new(
            self._app_secret.encode(),
            sorted_payload.encode(),
            hashlib.sha256,
        ).hexdigest().upper()

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Search active buyer RFQs (buy-offers) on Alibaba International.

        Calls ``aliinternational.trade.leads.search`` to find buyers actively
        posting sourcing requests for a product category right now.

        Expected request keys:
            query        (str) – product keyword, e.g. ``"ground pepper spices"``
            category_id  (str) – Alibaba category ID (optional, narrows results)
            country      (str) – buyer country ISO-2, e.g. ``"ES"`` (optional)
            page         (int) – page number, default 1
            page_size    (int) – results per page (max 20), default 20

        Returns a normalised dict ready for the UI active-RFQ feed::

            {
              "total": 38,
              "rfqs": [
                {
                  "rfq_id":         "BFO-20260610-12345",
                  "title":          "Need 10 MT ground red pepper, Q3 delivery",
                  "buyer_country":  "Spain",
                  "quantity":       "10 Metric Ton",
                  "posted_at":      "2026-06-18T09:30:00Z",
                  "expiry_date":    "2026-07-18T00:00:00Z",
                  "sourcing_type":  "regular",
                  "verified_buyer": true
                },
                ...
              ]
            }
        """
        params: dict[str, Any] = {
            "appKey":    self._app_key,
            "timestamp": str(int(time.time() * 1000)),
            "keywords":  request["query"],
            "pageIndex": str(request.get("page", 1)),
            "pageSize":  str(min(request.get("page_size", 20), 20)),
        }
        if "country" in request:
            params["countryCode"] = request["country"]
        if "category_id" in request:
            params["categoryId"] = str(request["category_id"])

        params["sign"] = self._sign(params)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/aliinternational.trade.leads.search",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        result = data.get("result", data)
        raw_rfqs: list[dict[str, Any]] = (
            result.get("data") or result.get("buyOfferList") or []
        )

        normalised = [
            {
                "rfq_id":         r.get("id") or r.get("buyOfferId", ""),
                "title":          r.get("subject") or r.get("title", ""),
                "buyer_country":  r.get("countryName") or r.get("country", ""),
                "quantity":       r.get("quantity", ""),
                "posted_at":      r.get("gmtCreate") or r.get("publishTime", ""),
                "expiry_date":    r.get("gmtExpire") or r.get("expiryDate", ""),
                "sourcing_type":  r.get("sourcingType", ""),
                "verified_buyer": bool(r.get("memberRank") or r.get("verified", False)),
            }
            for r in raw_rfqs
        ]

        return {
            "total": result.get("total", len(normalised)),
            "rfqs":  normalised,
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "AlibabaRfqConnector is read-only; respond to RFQs via the platform UI."
        )

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Fetch full details of a single RFQ by its Alibaba buy-offer ID.

        ext_ref: Alibaba buy-offer ID returned inside a ``quote()`` response.
        """
        params: dict[str, Any] = {
            "appKey":     self._app_key,
            "timestamp":  str(int(time.time() * 1000)),
            "buyOfferId": ext_ref,
        }
        params["sign"] = self._sign(params)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/aliinternational.trade.leads.get",
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("AlibabaRfqConnector does not support inbound webhooks.")


# ---------------------------------------------------------------------------
# GdeltDisruptionConnector
# ---------------------------------------------------------------------------

class GdeltDisruptionConnector(Connector):
    """GDELT Project: real-time global news monitoring for supply-chain disruption.

    Detects Level 4 forced-intent signals.  When a buyer's usual supply country
    faces an export ban, port strike, or geopolitical disruption, they MUST find
    an alternative supplier immediately — creating a high-urgency window for the
    Trade-Min Agent to match India-sourced goods to the displaced demand.

    API documentation : https://api.gdeltproject.org/api/v2/doc/doc
    Authentication    : None — fully open, no key required.
    Rate limit        : ~1 request/second (be polite; no hard cap published).
    """

    name = "gdelt_disruption"
    _BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

    # Disruption signals the agent monitors for.
    _DISRUPTION_TERMS = (
        "export ban OR export restriction OR port strike OR supply shortage "
        "OR trade sanction OR embargo OR logistics disruption OR supply chain disruption"
    )

    def __init__(self) -> None:
        pass  # No credentials required.

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Scan global news for supply-disruption signals in a competitor origin country.

        When a competing origin country (e.g. Vietnam for pepper) faces a crisis,
        buyers who previously sourced from there are now emergency-sourcing — the
        highest-urgency trade window available to PortLess.

        Expected request keys:
            origin_country   (str) – competitor country to monitor, e.g. ``"Vietnam"``
            product_keyword  (str) – narrows search scope, e.g. ``"pepper"`` (optional)
            hours_lookback   (int) – hours of news to scan, default 72
            max_records      (int) – max articles returned, default 25

        Returns disruption signals ready for the UI alert panel::

            {
              "disruption_detected": true,
              "signal_strength":     "HIGH",
              "origin_country":      "Vietnam",
              "article_count":       7,
              "query_window_hours":  72,
              "articles": [
                {
                  "title":        "Vietnam Imposes Emergency Export Quota on Pepper",
                  "url":          "https://...",
                  "source":       "reuters.com",
                  "published_at": "2026-06-19T14:22:00Z",
                  "language":     "English",
                  "relevance":    0.92
                },
                ...
              ]
            }
        """
        from datetime import datetime, timedelta, timezone

        hours: int = request.get("hours_lookback", 72)
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=hours)

        query_parts = [f'"{request["origin_country"]}"', self._DISRUPTION_TERMS]
        if "product_keyword" in request:
            query_parts.append(f'"{request["product_keyword"]}"')

        params: dict[str, str] = {
            "query":         " ".join(query_parts),
            "mode":          "artlist",
            "maxrecords":    str(request.get("max_records", 25)),
            "format":        "json",
            "startdatetime": start_dt.strftime("%Y%m%d%H%M%S"),
            "enddatetime":   end_dt.strftime("%Y%m%d%H%M%S"),
            "sort":          "DateDesc",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(self._BASE, params=params)
            resp.raise_for_status()
            data = resp.json()

        raw_articles: list[dict[str, Any]] = data.get("articles", [])

        articles = [
            {
                "title":        a.get("title", ""),
                "url":          a.get("url", ""),
                "source":       a.get("domain", ""),
                "published_at": a.get("seendate", ""),
                "language":     a.get("language", ""),
                "relevance":    round(float(a.get("relevance", 0)), 3),
            }
            for a in raw_articles
        ]

        count = len(articles)
        signal_strength = "LOW" if count < 3 else "MEDIUM" if count < 7 else "HIGH"

        return {
            "disruption_detected": count > 0,
            "signal_strength":     signal_strength,
            "origin_country":      request["origin_country"],
            "article_count":       count,
            "query_window_hours":  hours,
            "articles":            articles,
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("GDELT is a read-only news feed.")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Fetch a 7-day disruption-volume timeline for a country.

        ext_ref: country name to monitor, e.g. ``"Vietnam"``.
        Returns a GDELT ``timelinevol`` series usable in a sparkline chart.
        """
        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=7)

        params: dict[str, str] = {
            "query":         f'"{ext_ref}" {self._DISRUPTION_TERMS}',
            "mode":          "timelinevol",
            "format":        "json",
            "startdatetime": start_dt.strftime("%Y%m%d%H%M%S"),
            "enddatetime":   end_dt.strftime("%Y%m%d%H%M%S"),
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(self._BASE, params=params)
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("GDELT does not issue webhooks.")


# ---------------------------------------------------------------------------
# EuTedConnector
# ---------------------------------------------------------------------------

class EuTedConnector(Connector):
    """EU TED (Tenders Electronic Daily): European institutional procurement notices.

    Detects Level 3 active-intent signals from EU government bodies, hospitals,
    universities, and state enterprises that issue formal procurement tenders for
    bulk food commodities, textiles, and other HS-code categories.
    These buyers have defined budgets, published quantities, and hard deadlines —
    the most commercially predictable of all buying-intent signals.

    API documentation : https://ted.europa.eu/api/v3.0/
    Authentication    : None — fully open, no key required.
    """

    name = "eu_ted"
    _BASE = "https://ted.europa.eu/api/v3.0"

    def __init__(self) -> None:
        pass  # No credentials required.

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Search active EU procurement tenders for a product keyword.

        Expected request keys:
            query    (str) – product keyword, e.g. ``"pepper spices food"``
            country  (str) – buyer country ISO-2, e.g. ``"ES"`` (optional)
            page     (int) – page number, default 1
            limit    (int) – results per page, default 25

        Returns normalised tender notices ready for the UI active-tenders panel::

            {
              "total": 12,
              "tenders": [
                {
                  "notice_id":      "2026/S 117-123456",
                  "title":          "Supply of dried spices for hospital network",
                  "buyer_name":     "Ministerio de Salud",
                  "buyer_country":  "ES",
                  "contract_value": 480000.0,
                  "currency":       "EUR",
                  "deadline":       "2026-07-15",
                  "published_at":   "2026-06-10",
                  "cpv_codes":      ["15871200", "15872400"],
                  "notice_url":     "https://ted.europa.eu/udl?uri=TED:NOTICE:123456-2026"
                },
                ...
              ]
            }
        """
        query = request["query"]
        if "country" in request:
            query += f" AND AU_COUNTRY:{request['country']}"

        payload: dict[str, Any] = {
            "query": query,
            "scope": "ACTIVE",
            "fields": [
                "ND", "TI", "AU_NAME", "AU_COUNTRY",
                "VALUE_COST", "DT_DATE_FOR_SUBMISSION",
                "DATE_PUB", "CPV", "URI_DOC",
            ],
            "page":  request.get("page", 1),
            "limit": request.get("limit", 25),
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._BASE}/notices/search",
                json=payload,
                headers={
                    "Accept":       "application/json",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        raw_notices: list[dict[str, Any]] = data.get("notices", [])

        tenders = [
            {
                "notice_id":      n.get("ND", ""),
                "title":          (
                    n.get("TI", {}).get("en")
                    or next(iter(n.get("TI", {}).values()), "")
                ),
                "buyer_name":     (
                    n.get("AU_NAME", {}).get("en")
                    or next(iter(n.get("AU_NAME", {}).values()), "")
                ),
                "buyer_country":  n.get("AU_COUNTRY", ""),
                "contract_value": (n.get("VALUE_COST") or {}).get("VALUE"),
                "currency":       (n.get("VALUE_COST") or {}).get("CURRENCY", "EUR"),
                "deadline":       n.get("DT_DATE_FOR_SUBMISSION", ""),
                "published_at":   n.get("DATE_PUB", ""),
                "cpv_codes":      n.get("CPV", []),
                "notice_url":     n.get("URI_DOC", ""),
            }
            for n in raw_notices
        ]

        return {
            "total":   data.get("total", len(tenders)),
            "tenders": tenders,
        }

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("EU TED is a read-only procurement portal.")

    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Fetch full details of a single tender notice by TED notice ID.

        ext_ref: TED notice number, e.g. ``"2026/S 117-123456"``.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._BASE}/notices/{ext_ref}",
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError("EU TED does not issue webhooks.")
