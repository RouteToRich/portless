"""National trade statistics next to UN Comtrade (todo 17.1): monthly imports with value AND
quantity, so a market whose Comtrade line has a value but no weight still gets a unit price.

- ``UkHmrcTradeConnector`` (``uk_hmrc``) — HMRC Overseas Trade Statistics, uktradeinfo.com OData
  API (free, no key): UK imports by CN8 commodity and partner country per month, value in GBP and
  net mass in kg. Phase 1 source for market ``GB``.
- ``EurostatComextConnector`` (``eurostat_comext``) — Eurostat Comext dissemination API (free, no
  key; JSON-stat): EU member-state imports by CN8 / HS6 product and partner per month, value in EUR
  and quantity in 100 kg. Phase 1 source for EU markets (DE, NL, FR, IT, ES …).

Both return one shape::

    {"source", "market", "hs_code", "months": ["2026-07", ...], "data_period": "2026-09",
     "value_usd", "quantity_mt", "unit_value_usd_per_mt",               # all origins
     "india_value_usd", "india_quantity_mt", "india_unit_value_usd_per_mt",  # partner India
     "currency", "fx_rate", "records": n}

Values are converted to USD with the reference rate (``shared/fx_rates.py``: staff / ECB feed /
``FX_RATE_GBP`` / ``FX_RATE_EUR``); without a rate the USD figures are None and the native ones
stay (``value_native``). Request / field names follow the providers' public documentation and
have not been run against the live APIs yet — verify on live data before relying on a price.
Network calls are skipped with ``NATIONAL_TRADE_SOURCES_ENABLED=false`` (pinned in tests). Never
raises on a provider error: an empty result (``records: 0``) with ``error``.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

import httpx

from .base import Connector

logger = logging.getLogger(__name__)

EU27 = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
    "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
})


def enabled() -> bool:
    return os.getenv("NATIONAL_TRADE_SOURCES_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def source_for(market: str | None) -> str | None:
    """The national connector for a market, or None."""
    m = (market or "").upper()
    if m in ("GB", "UK"):
        return "uk_hmrc"
    if m in EU27:
        return "eurostat_comext"
    return None


def _months(n: int, today: date | None = None, lag_months: int = 2) -> list[str]:
    """The last ``n`` settled months (YYYY-MM), newest last; statistics lag ~2 months."""
    d = (today or date.today()).replace(day=1)
    for _ in range(lag_months):
        d = (d - timedelta(days=1)).replace(day=1)
    out = []
    for _ in range(max(1, n)):
        out.append(f"{d.year}-{d.month:02d}")
        d = (d - timedelta(days=1)).replace(day=1)
    return list(reversed(out))


def _usd(amount: float | None, currency: str) -> tuple[float | None, float | None]:
    if amount is None:
        return None, None
    try:
        from shared import fx_rates
        row = fx_rates.cached(currency)
    except Exception:  # noqa: BLE001
        row = None
    rate = float(row["rate"]) if row and row.get("rate") else None
    return (round(amount / rate, 2) if rate else None), rate


def summarise(rows: list[dict[str, Any]], *, source: str, market: str, hs_code: str, currency: str,
              months: list[str]) -> dict[str, Any]:
    """rows: ``{"month", "partner" (ISO-2), "value" (native currency), "kg"}`` → the common shape."""
    def total(sel: list[dict[str, Any]], key: str) -> float:
        return sum(float(r.get(key) or 0.0) for r in sel)

    india = [r for r in rows if str(r.get("partner") or "").upper() == "IN"]
    value, kg = total(rows, "value"), total(rows, "kg")
    in_value, in_kg = total(india, "value"), total(india, "kg")
    value_usd, rate = _usd(value or None, currency)
    in_value_usd, _ = _usd(in_value or None, currency)
    qty_mt = round(kg / 1000.0, 3) if kg else None
    in_qty_mt = round(in_kg / 1000.0, 3) if in_kg else None
    seen = sorted({r["month"] for r in rows if r.get("month")})
    return {
        "source": source, "market": market, "hs_code": hs_code, "months": months,
        "data_period": seen[-1] if seen else None, "currency": currency, "fx_rate": rate,
        "value_native": round(value, 2) if value else None,
        "value_usd": value_usd, "quantity_mt": qty_mt,
        "unit_value_usd_per_mt": round(value_usd / qty_mt, 2) if value_usd and qty_mt else None,
        "india_value_usd": in_value_usd, "india_quantity_mt": in_qty_mt,
        "india_unit_value_usd_per_mt": round(in_value_usd / in_qty_mt, 2) if in_value_usd and in_qty_mt else None,
        "records": len(rows),
    }


def _empty(source: str, market: str, hs_code: str, **extra: Any) -> dict[str, Any]:
    return {"source": source, "market": market, "hs_code": hs_code, "records": 0, "months": [],
            "value_usd": None, "quantity_mt": None, "unit_value_usd_per_mt": None,
            "india_unit_value_usd_per_mt": None, **extra}


class UkHmrcTradeConnector(Connector):
    """HMRC Overseas Trade Statistics (uktradeinfo.com) — UK imports by CN8 and partner, monthly.

    API documentation : https://www.uktradeinfo.com/api-documentation/
    Base URL          : https://api.uktradeinfo.com  (OData v4, entity ``OTS``)
    Authentication    : none (free, open data)
    Fields            : ``MonthId`` (YYYYMM), ``FlowTypeId`` (1 EU imports, 3 non-EU imports),
                        ``CommodityId`` (CN8 as integer), ``CountryId`` → ``Country/CountryCodeAlpha``,
                        ``Value`` (GBP), ``NetMass`` (kg).
    """

    name = "uk_hmrc"

    def __init__(self, transport: Any = None) -> None:
        self._base = os.getenv("UK_HMRC_BASE_URL", "https://api.uktradeinfo.com").rstrip("/")
        self._transport = transport

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        hs = "".join(ch for ch in str(request.get("hs_code") or "") if ch.isdigit())[:8]
        market = "GB"
        if len(hs) < 6:
            raise ValueError("uk_hmrc: hs_code (6 or 8 digits) required")
        months = _months(int(request.get("months") or 12))
        if not enabled():
            return _empty(self.name, market, hs, disabled=True)
        lo = int(hs.ljust(8, "0"))
        hi = int(hs.ljust(8, "9"))
        start, end = months[0].replace("-", ""), months[-1].replace("-", "")
        params = {
            "$filter": (f"CommodityId ge {lo} and CommodityId le {hi} and MonthId ge {start} and MonthId le {end} "
                        "and (FlowTypeId eq 1 or FlowTypeId eq 3)"),
            "$select": "MonthId,CountryId,Value,NetMass",
            "$expand": "Country($select=CountryCodeAlpha)",
        }
        rows: list[dict[str, Any]] = []
        url: str | None = f"{self._base}/OTS"
        try:
            async with httpx.AsyncClient(timeout=40, transport=self._transport) as client:
                pages = 0
                while url and pages < 20:
                    resp = await client.get(url, params=params if pages == 0 else None)
                    if resp.status_code != 200:
                        return _empty(self.name, market, hs, error=f"HTTP {resp.status_code}")
                    data = resp.json()
                    for r in data.get("value") or []:
                        month = str(r.get("MonthId") or "")
                        country = (r.get("Country") or {}).get("CountryCodeAlpha") or r.get("CountryCodeAlpha")
                        rows.append({"month": f"{month[:4]}-{month[4:6]}" if len(month) >= 6 else None,
                                     "partner": country, "value": r.get("Value"), "kg": r.get("NetMass")})
                    url = data.get("@odata.nextLink")
                    pages += 1
        except Exception as exc:  # noqa: BLE001 - a statistics outage never breaks a scan
            logger.warning("UK HMRC: query failed for %s: %s", hs, exc)
            return _empty(self.name, market, hs, error=str(exc))
        return summarise(rows, source=self.name, market=market, hs_code=hs, currency="GBP", months=months)

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "not_supported"}

    async def status(self, ext_ref: str) -> dict[str, Any]:
        return {"status": "not_supported"}

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        return False


class EurostatComextConnector(Connector):
    """Eurostat Comext — EU imports by product and partner, monthly (JSON-stat 2.0).

    API documentation : https://wikis.ec.europa.eu/display/EUROSTATHELP/API+-+Getting+started+with+statistics+API
    Base URL          : https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/DS-045409
    Authentication    : none (free, open data)
    Dimensions        : ``reporter`` (ISO-2), ``partner`` (ISO-2 / ``EXT_EU27_2020``), ``product``
                        (CN8 / HS6), ``flow`` (1 = imports), ``indicators`` (``VALUE_IN_EUROS``,
                        ``QUANTITY_IN_100KG``), ``freq`` = M, ``time`` (``2026-07``).
    """

    name = "eurostat_comext"

    def __init__(self, transport: Any = None) -> None:
        self._base = os.getenv(
            "EUROSTAT_COMEXT_URL",
            "https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/DS-045409").rstrip("/")
        self._transport = transport

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        hs = "".join(ch for ch in str(request.get("hs_code") or "") if ch.isdigit())[:8]
        market = str(request.get("market") or "").upper()
        if len(hs) < 6:
            raise ValueError("eurostat_comext: hs_code (6 or 8 digits) required")
        if market not in EU27:
            return _empty(self.name, market, hs, error="not an EU market")
        months = _months(int(request.get("months") or 12))
        if not enabled():
            return _empty(self.name, market, hs, disabled=True)
        params: list[tuple[str, str]] = [
            ("reporter", market), ("partner", "IN"), ("partner", "EXT_EU27_2020"), ("product", hs),
            ("flow", "1"), ("freq", "M"), ("indicators", "VALUE_IN_EUROS"), ("indicators", "QUANTITY_IN_100KG"),
            *[("time", m) for m in months],
        ]
        try:
            async with httpx.AsyncClient(timeout=40, transport=self._transport) as client:
                resp = await client.get(self._base, params=params)
            if resp.status_code != 200:
                return _empty(self.name, market, hs, error=f"HTTP {resp.status_code}")
            rows = self.rows_from_jsonstat(resp.json())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Eurostat Comext: query failed for %s×%s: %s", hs, market, exc)
            return _empty(self.name, market, hs, error=str(exc))
        # All origins = the extra-EU aggregate; India is one partner inside it.
        all_rows = [r for r in rows if r["partner"] == "EXT_EU27_2020"]
        india = [r for r in rows if r["partner"] == "IN"]
        out = summarise(all_rows, source=self.name, market=market, hs_code=hs, currency="EUR", months=months)
        in_sum = summarise(india, source=self.name, market=market, hs_code=hs, currency="EUR", months=months)
        out.update({k: in_sum[k.replace("india_", "", 1)] for k in
                    ("india_value_usd", "india_quantity_mt", "india_unit_value_usd_per_mt")})
        out["records"] = len(rows)
        return out

    @staticmethod
    def rows_from_jsonstat(data: dict[str, Any]) -> list[dict[str, Any]]:
        """JSON-stat 2.0 cube → ``{"month", "partner", "value" (EUR), "kg"}`` rows."""
        ids: list[str] = list(data.get("id") or [])
        sizes: list[int] = list(data.get("size") or [])
        dims = data.get("dimension") or {}
        values = data.get("value") or {}
        if not ids or not sizes or not values:
            return []
        index: dict[str, list[str]] = {}
        for d in ids:
            cat = ((dims.get(d) or {}).get("category") or {}).get("index") or {}
            if isinstance(cat, list):
                index[d] = list(cat)
            else:
                index[d] = [k for k, _ in sorted(cat.items(), key=lambda kv: kv[1])]
        cells: dict[tuple[str, str], dict[str, float]] = {}
        items = values.items() if isinstance(values, dict) else enumerate(values)
        for flat, v in items:
            if v is None:
                continue
            n = int(flat)
            coords: dict[str, str] = {}
            for d, size in zip(reversed(ids), reversed(sizes)):
                coords[d] = index[d][n % size] if index.get(d) and n % size < len(index[d]) else ""
                n //= size
            key = (coords.get("time", ""), coords.get("partner", ""))
            cell = cells.setdefault(key, {})
            ind = coords.get("indicators", "")
            if ind == "VALUE_IN_EUROS":
                cell["value"] = cell.get("value", 0.0) + float(v)
            elif ind == "QUANTITY_IN_100KG":
                cell["kg"] = cell.get("kg", 0.0) + float(v) * 100.0
        return [{"month": t, "partner": p, "value": c.get("value"), "kg": c.get("kg")} for (t, p), c in cells.items()]

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "not_supported"}

    async def status(self, ext_ref: str) -> dict[str, Any]:
        return {"status": "not_supported"}

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        return False
