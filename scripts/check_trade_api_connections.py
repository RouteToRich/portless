"""Connectivity check for all third-party APIs used by the Trade-Min module.

Run from the repo root:
    python scripts/check_trade_api_connections.py

Requires: httpx, python-dotenv  (both in pyproject.toml dependencies)
Optional: motor or pymongo  (for the MongoDB ping)

Exit code 0 if all *configured* APIs are reachable and authenticated.
Exit code 1 if any configured API is unreachable or returns an auth error.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Force UTF-8 output on Windows so box-drawing/tick characters render correctly.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Load the repository-root .env.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ENV_FILE = _HERE.parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE, override=False)

# ---------------------------------------------------------------------------
# ANSI colours (disabled automatically on non-TTY, e.g. CI logs)
# ---------------------------------------------------------------------------
_IS_TTY = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text

GREEN  = lambda t: _c("32", t)  # noqa: E731
YELLOW = lambda t: _c("33", t)  # noqa: E731
RED    = lambda t: _c("31", t)  # noqa: E731
CYAN   = lambda t: _c("36", t)  # noqa: E731
BOLD   = lambda t: _c("1",  t)  # noqa: E731
DIM    = lambda t: _c("2",  t)  # noqa: E731


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    status: str        # "ok" | "auth_error" | "unreachable" | "skipped" | "error"
    http_status: int | None
    latency_ms: float | None
    detail: str


_PLACEHOLDER_SUFFIXES = ("_here", "_key_here", "_secret_here", "_token_here")

def _is_placeholder(value: str) -> bool:
    v = value.lower()
    return not value or any(v.endswith(s) for s in _PLACEHOLDER_SUFFIXES) or v.startswith("your_")


def _env(key: str) -> str | None:
    v = os.environ.get(key, "")
    return None if _is_placeholder(v) else v


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, float]:
    t0 = time.perf_counter()
    r = await client.get(url, params=params, headers=headers)
    ms = (time.perf_counter() - t0) * 1000
    return r.status_code, ms


async def _post(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, float]:
    t0 = time.perf_counter()
    r = await client.post(url, json=json, headers=headers)
    ms = (time.perf_counter() - t0) * 1000
    return r.status_code, ms


async def _head_reachable(client: httpx.AsyncClient, url: str) -> tuple[int, float]:
    t0 = time.perf_counter()
    r = await client.head(url)
    ms = (time.perf_counter() - t0) * 1000
    return r.status_code, ms


def _exc_detail(exc: Exception) -> str:
    msg = str(exc)
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _result(name: str, code: int | None, ms: float | None, detail: str = "") -> CheckResult:
    if code is None:
        return CheckResult(name, "unreachable", None, ms, detail)
    if code in range(200, 300):
        return CheckResult(name, "ok", code, ms, detail or "connected")
    if code in (400, 422):
        # Server responded — request format wrong but auth probably OK
        return CheckResult(name, "ok", code, ms, detail or f"HTTP {code} (reachable, check params)")
    if code in (401, 403):
        return CheckResult(name, "auth_error", code, ms, detail or f"HTTP {code} — check API key")
    if code == 429:
        return CheckResult(name, "ok", code, ms, detail or "HTTP 429 rate-limited (reachable)")
    return CheckResult(name, "error", code, ms, detail or f"HTTP {code}")


# ---------------------------------------------------------------------------
# Individual API checks
# ---------------------------------------------------------------------------

async def check_comtrade(client: httpx.AsyncClient) -> CheckResult:
    """UN Comtrade — subscription-key query param."""
    key = _env("COMTRADE_SUBSCRIPTION_KEY")
    if not key:
        return CheckResult("UN Comtrade", "skipped", None, None, "COMTRADE_SUBSCRIPTION_KEY not set")

    try:
        code, ms = await _get(
            client,
            "https://comtradeapi.un.org/data/v1/get/C/A/HS",
            params={
                "cmdCode": "100190",   # Wheat — safe, common HS code
                "period": "2023",
                "flowCode": "M",
                "maxrecords": 1,
                "subscription-key": key,
            },
        )
        return _result("UN Comtrade", code, ms)
    except Exception as exc:
        return CheckResult("UN Comtrade", "unreachable", None, None, _exc_detail(exc))


async def check_volza(client: httpx.AsyncClient) -> CheckResult:
    """Volza — Bearer token; HEAD to base URL (no free ping endpoint documented)."""
    key = _env("VOLZA_API_KEY")
    if not key:
        return CheckResult("Volza", "skipped", None, None, "VOLZA_API_KEY not set")

    try:
        code, ms = await _head_reachable(client, "https://backend.volza.com")
        note = "HEAD reachable (auth not verified — no free ping endpoint)"
        if code in range(200, 500):
            return CheckResult("Volza", "ok", code, ms, note)
        return _result("Volza", code, ms, note)
    except Exception as exc:
        return CheckResult("Volza", "unreachable", None, None, _exc_detail(exc))


async def check_trademo(client: httpx.AsyncClient) -> CheckResult:
    """Trademo Intel — X-API-Key header; minimal buyers/search POST."""
    key = _env("TRADEMO_API_KEY")
    if not key:
        return CheckResult("Trademo Intel", "skipped", None, None, "TRADEMO_API_KEY not set")

    try:
        code, ms = await _post(
            client,
            "https://platform.trademo.com/api/v1/buyers/search",
            json={"hs_code": "100190", "buyer_country": "ES", "limit": 1},
            headers={"X-API-Key": key, "Accept": "application/json"},
        )
        return _result("Trademo Intel", code, ms)
    except Exception as exc:
        return CheckResult("Trademo Intel", "unreachable", None, None, _exc_detail(exc))


async def check_importgenius(client: httpx.AsyncClient) -> CheckResult:
    """ImportGenius — Bearer token; manifests/importers GET with limit=1."""
    key = _env("IMPORTGENIUS_API_KEY")
    if not key:
        return CheckResult("ImportGenius", "skipped", None, None, "IMPORTGENIUS_API_KEY not set")

    try:
        code, ms = await _get(
            client,
            "https://api.importgenius.com/v2/manifests/importers",
            params={"hs_code": "100190", "country": "US", "limit": 1},
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        return _result("ImportGenius", code, ms)
    except Exception as exc:
        return CheckResult("ImportGenius", "unreachable", None, None, _exc_detail(exc))


async def check_alibaba(client: httpx.AsyncClient) -> CheckResult:
    """Alibaba RFQ — HMAC-SHA256 signed request."""
    app_key = _env("ALIBABA_APP_KEY")
    app_secret = _env("ALIBABA_APP_SECRET")
    if not app_key or not app_secret:
        missing = ", ".join(
            k for k, v in [("ALIBABA_APP_KEY", app_key), ("ALIBABA_APP_SECRET", app_secret)] if not v
        )
        return CheckResult("Alibaba RFQ", "skipped", None, None, f"{missing} not set")

    params: dict = {
        "appKey":    app_key,
        "timestamp": str(int(time.time() * 1000)),
        "keywords":  "wheat",
        "pageIndex": "1",
        "pageSize":  "1",
    }
    sorted_payload = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    params["sign"] = hmac.new(
        app_secret.encode(), sorted_payload.encode(), hashlib.sha256
    ).hexdigest().upper()

    try:
        code, ms = await _get(
            client,
            "https://gw.api.alibaba.com/openapi/param2/1/aliinternational.trade.leads.search",
            params=params,
        )
        return _result("Alibaba RFQ", code, ms)
    except Exception as exc:
        return CheckResult("Alibaba RFQ", "unreachable", None, None, _exc_detail(exc))


async def check_gdelt(client: httpx.AsyncClient) -> CheckResult:
    """GDELT Project — fully open, no credentials required."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=24)).strftime("%Y%m%d%H%M%S")
    end = now.strftime("%Y%m%d%H%M%S")
    try:
        code, ms = await _get(
            client,
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query":         "trade disruption",
                "mode":          "artlist",
                "maxrecords":    "1",
                "format":        "json",
                "startdatetime": start,
                "enddatetime":   end,
            },
        )
        return _result("GDELT (open)", code, ms)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return CheckResult("GDELT (open)", "unreachable", None, None, detail)


async def check_eu_ted(client: httpx.AsyncClient) -> CheckResult:
    """EU TED — fully open, no credentials required."""
    try:
        code, ms = await _post(
            client,
            "https://ted.europa.eu/api/v3.0/notices/search",
            json={
                "query":  "wheat food supply",
                "scope":  "ACTIVE",
                "fields": ["ND"],
                "page":   1,
                "limit":  1,
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        return _result("EU TED (open)", code, ms)
    except Exception as exc:
        return CheckResult("EU TED (open)", "unreachable", None, None, _exc_detail(exc))


async def check_macmap(client: httpx.AsyncClient) -> CheckResult:
    """ITC MacMap — Ocp-Apim-Subscription-Key header."""
    key = _env("MACMAP_API_KEY")
    if not key:
        return CheckResult("ITC MacMap", "skipped", None, None, "MACMAP_API_KEY not set")

    try:
        code, ms = await _get(
            client,
            "https://api.macmap.org/v2/tariffs/applied",
            params={"reporter": "ES", "partner": "IN", "product": "100190", "lang": "EN"},
            headers={"Ocp-Apim-Subscription-Key": key},
        )
        return _result("ITC MacMap", code, ms)
    except Exception as exc:
        return CheckResult("ITC MacMap", "unreachable", None, None, _exc_detail(exc))


async def check_indiamart(client: httpx.AsyncClient) -> CheckResult:
    """IndiaMART — IMAPI-KEY header + mobile number query params."""
    key = _env("INDIAMART_API_KEY")
    mobile = _env("INDIAMART_MOBILE")
    if not key or not mobile:
        missing = ", ".join(
            k for k, v in [("INDIAMART_API_KEY", key), ("INDIAMART_MOBILE", mobile)] if not v
        )
        return CheckResult("IndiaMART", "skipped", None, None, f"{missing} not set")

    try:
        code, ms = await _get(
            client,
            "https://seller.indiamart.com/lmsrest/api/v1/product/catalog/search",
            params={"mobile_no": mobile, "api_key": key, "keyword": "wheat", "page": 1, "page_size": 1},
            headers={"IMAPI-KEY": key, "Accept": "application/json"},
        )
        return _result("IndiaMART", code, ms)
    except Exception as exc:
        return CheckResult("IndiaMART", "unreachable", None, None, _exc_detail(exc))


async def check_tradeindia(client: httpx.AsyncClient) -> CheckResult:
    """TradeIndia — Bearer token."""
    key = _env("TRADEINDIA_API_KEY")
    if not key:
        return CheckResult("TradeIndia", "skipped", None, None, "TRADEINDIA_API_KEY not set")

    try:
        code, ms = await _get(
            client,
            "https://api.tradeindia.com/v1/products",
            params={"keyword": "wheat", "page": 1, "limit": 1, "country": "IN"},
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        return _result("TradeIndia", code, ms)
    except Exception as exc:
        return CheckResult("TradeIndia", "unreachable", None, None, _exc_detail(exc))


async def check_mongodb() -> CheckResult:
    """Trade-Min MongoDB (Atlas) — motor ping."""
    uri = _env("TRADE_MONGO_URI")
    if not uri:
        return CheckResult("MongoDB (trade)", "skipped", None, None, "TRADE_MONGO_URI not set")

    t0 = time.perf_counter()
    try:
        from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[import]
        cli = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        await cli.admin.command("ping")
        ms = (time.perf_counter() - t0) * 1000
        cli.close()
        return CheckResult("MongoDB (trade)", "ok", None, ms, "ping OK")
    except ImportError:
        pass  # Try pymongo sync fallback
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return CheckResult("MongoDB (trade)", "unreachable", None, ms, _exc_detail(exc))

    try:
        import pymongo  # type: ignore[import]
        cli = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
        cli.admin.command("ping")
        ms = (time.perf_counter() - t0) * 1000
        cli.close()
        return CheckResult("MongoDB (trade)", "ok", None, ms, "ping OK (pymongo)")
    except ImportError:
        return CheckResult(
            "MongoDB (trade)", "skipped", None, None,
            "motor/pymongo not installed — install to verify"
        )
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return CheckResult("MongoDB (trade)", "unreachable", None, ms, _exc_detail(exc))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def run_all() -> list[CheckResult]:
    timeout = httpx.Timeout(connect=8.0, read=20.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        results = await asyncio.gather(
            check_comtrade(client),
            check_volza(client),
            check_trademo(client),
            check_importgenius(client),
            check_alibaba(client),
            check_gdelt(client),
            check_eu_ted(client),
            check_macmap(client),
            check_indiamart(client),
            check_tradeindia(client),
            check_mongodb(),
            return_exceptions=False,
        )
    return list(results)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
_STATUS_SYMBOL = {
    "ok":          GREEN("✓ OK"),
    "auth_error":  RED("✗ AUTH"),
    "unreachable": RED("✗ DOWN"),
    "error":       YELLOW("! ERR"),
    "skipped":     DIM("– SKIP"),
}

_PHASE_MAP = {
    "UN Comtrade":    "Phase 1",
    "Volza":          "Phase 1",
    "Trademo Intel":  "Phase 1",
    "ImportGenius":   "Phase 1",
    "Alibaba RFQ":    "Phase 2",
    "GDELT (open)":   "Phase 2",
    "EU TED (open)":  "Phase 2",
    "ITC MacMap":     "Phase 3",
    "IndiaMART":      "Phase 4",
    "TradeIndia":     "Phase 4",
    "MongoDB (trade)":"Persist",
}


def render(results: list[CheckResult]) -> None:
    w_name   = max(len(r.name) for r in results) + 2
    w_phase  = 9
    w_status = 10
    w_lat    = 10
    sep      = "─" * (w_name + w_phase + w_status + w_lat + 20)

    print()
    print(BOLD("Trade-Min API Connection Check"))
    print(DIM(f"  Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))
    print(sep)
    print(
        BOLD(f"  {'API':<{w_name}} {'Phase':<{w_phase}} {'Status':<{w_status}} {'Latency':<{w_lat}}  Detail")
    )
    print(sep)

    counts: dict[str, int] = {"ok": 0, "auth_error": 0, "unreachable": 0, "error": 0, "skipped": 0}
    for r in results:
        symbol  = _STATUS_SYMBOL.get(r.status, r.status)
        phase   = _PHASE_MAP.get(r.name, "")
        latency = f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "—"
        http    = f"[{r.http_status}] " if r.http_status else ""
        print(f"  {r.name:<{w_name}} {phase:<{w_phase}} {symbol:<{w_status}} {latency:<{w_lat}}  {DIM(http)}{r.detail}")
        counts[r.status] = counts.get(r.status, 0) + 1

    print(sep)
    summary_parts = [
        GREEN(f"{counts['ok']} OK"),
        RED(f"{counts['auth_error']} AUTH-ERR") if counts["auth_error"] else None,
        RED(f"{counts['unreachable']} DOWN") if counts["unreachable"] else None,
        YELLOW(f"{counts['error']} ERR") if counts["error"] else None,
        DIM(f"{counts['skipped']} skipped"),
    ]
    print("  " + "  |  ".join(p for p in summary_parts if p))
    print()


def main() -> int:
    results = asyncio.run(run_all())
    render(results)
    failed = [r for r in results if r.status in ("auth_error", "unreachable", "error")]
    if failed:
        print(RED("  One or more configured APIs failed connectivity check."))
        print()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
