"""Reference exchange rates (todo: a rate source before adding a country; an official USD/INR
rate that updates the fallback automatically).

Where a rate comes from, in order:

1. a **staff-recorded reference rate** (``POST /deals/fx/reference``: currency, units per USD,
   source such as "RBI reference rate" or "HDFC card rate", audited) — Mongo ``fx_reference_rates``;
2. the **daily refresh** from a published reference feed (``FX_REFERENCE_URL``, default the ECB
   rates through frankfurter.app — no key, includes INR / EUR / GBP; not AED / SAR) — run once a day
   by the deal watcher while ``FX_REFERENCE_AUTO_REFRESH`` is on (pinned off in the test suite);
3. a static ``FX_RATE_<CCY>`` setting (e.g. ``FX_RATE_AED=3.6725`` — the dirham is pegged).

A rate older than ``FX_REFERENCE_MAX_AGE_DAYS`` (7) is not used, except a pegged one set by
setting. ``shared.currency`` uses these: a country is written to in its own currency only once
a usable rate exists, and the USD/INR fallback is the latest reference rate rather than the
static ``NEGOTIATION_FALLBACK_USD_INR_RATE``. A rate outside the plausible band per currency is
refused. Never raises on read.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Plausible units per USD — a typo (9.6 for 96) or a feed glitch is refused.
BANDS: dict[str, tuple[float, float]] = {
    "INR": (60.0, 130.0), "AED": (3.0, 4.5), "SAR": (3.0, 4.5), "EUR": (0.6, 1.4), "GBP": (0.5, 1.2),
    "QAR": (3.0, 4.5), "OMR": (0.3, 0.5), "KWD": (0.25, 0.4), "BHD": (0.3, 0.5), "SGD": (1.0, 2.0),
    "MYR": (3.0, 6.0), "JPY": (80.0, 250.0), "CNY": (5.0, 9.0), "AUD": (1.0, 2.2), "CAD": (1.0, 1.8),
}

_cache: dict[str, dict[str, Any]] = {}
_memory: list[dict[str, Any]] = []
_last_refresh: dict[str, datetime] = {}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("fx_reference_rates")
    except Exception:  # noqa: BLE001
        return None


def plausible(currency: str, rate: float) -> bool:
    lo, hi = BANDS.get(currency.upper(), (1e-6, 1e6))
    return lo <= rate <= hi


def _fresh(row: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        at = datetime.fromisoformat(str(row["as_of"]))
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return False
    return now - at <= timedelta(days=_f("FX_REFERENCE_MAX_AGE_DAYS", 7))


def _setting(currency: str) -> dict[str, Any] | None:
    raw = os.getenv(f"FX_RATE_{currency.upper()}", "").strip()
    try:
        rate = float(raw) if raw else None
    except ValueError:
        rate = None
    if rate and plausible(currency, rate):
        return {"currency": currency.upper(), "rate": rate, "source": f"setting FX_RATE_{currency.upper()}",
                "as_of": None, "kind": "setting"}
    return None


def cached(currency: str) -> dict[str, Any] | None:
    """Sync read for the e-mail templates: a fresh reference rate, else the static setting."""
    cur = currency.upper()
    row = _cache.get(cur)
    if row and _fresh(row):
        return row
    return _setting(cur)


def has_rate(currency: str) -> bool:
    return currency.upper() == "USD" or cached(currency) is not None


async def record(currency: str, rate: float, *, source: str, by: str, as_of: datetime | None = None,
                 kind: str = "staff") -> dict[str, Any]:
    cur = (currency or "").strip().upper()
    if len(cur) != 3 or cur == "USD":
        raise ValueError("currency must be a 3-letter code other than USD")
    try:
        value = float(rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("rate must be a number of units per USD") from exc
    if not plausible(cur, value):
        lo, hi = BANDS[cur]
        raise ValueError(f"{value} {cur} per USD is outside the plausible band {lo}–{hi}")
    if not (source or "").strip():
        raise ValueError("say where the rate comes from (e.g. RBI reference rate)")
    row = {"currency": cur, "rate": value, "source": source.strip(), "kind": kind, "recorded_by": by,
           "as_of": (as_of or datetime.now(timezone.utc)).isoformat()}
    col = _collection()
    if col is None:
        _memory.append(row)
    else:
        await col.insert_one(dict(row))
    _cache[cur] = row
    if kind == "staff":
        try:
            from shared import audit_log
            await audit_log.record("fx_reference_rate", by, cur, {"rate": value, "source": row["source"]})
        except Exception:  # noqa: BLE001
            pass
    return row


async def latest(currency: str) -> dict[str, Any] | None:
    """The newest recorded rate (fresh or not) — loads it into the sync cache."""
    cur = currency.upper()
    row = None
    col = _collection()
    if col is None:
        rows = [r for r in _memory if r["currency"] == cur]
        row = max(rows, key=lambda r: r["as_of"]) if rows else None
    else:
        try:
            async for d in col.find({"currency": cur}).sort("as_of", -1).limit(1):
                d.pop("_id", None)
                row = d
        except Exception as exc:  # noqa: BLE001
            logger.warning("FxRates: %s unreadable: %s", cur, exc)
    if row:
        _cache[cur] = row
    return row


async def usable(currency: str) -> dict[str, Any] | None:
    """A fresh reference rate, else the static setting, else None."""
    row = await latest(currency)
    if row and _fresh(row):
        return row
    return _setting(currency)


async def history(currency: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    col = _collection()
    q = {"currency": currency.upper()} if currency else {}
    if col is None:
        rows = [r for r in _memory if not currency or r["currency"] == currency.upper()]
        return sorted(rows, key=lambda r: r["as_of"], reverse=True)[:limit]
    out = []
    try:
        async for d in col.find(q).sort("as_of", -1).limit(limit):
            d.pop("_id", None)
            out.append(d)
    except Exception as exc:  # noqa: BLE001
        logger.warning("FxRates: history unreadable: %s", exc)
    return out


async def warm() -> None:
    """Load the newest rate of every known currency into the sync cache (startup / refresh)."""
    for cur in BANDS:
        await latest(cur)


async def refresh_from_source(*, transport: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """Pull the published reference rates once (``FX_REFERENCE_URL``) and record the plausible ones."""
    url = os.getenv("FX_REFERENCE_URL", "https://api.frankfurter.app/latest?from=USD").strip()
    if not url:
        return {"ok": False, "reason": "FX_REFERENCE_URL is empty", "recorded": []}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            r = await client.get(url)
            r.raise_for_status()
            body = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("FxRates: reference feed unreachable: %s", exc)
        return {"ok": False, "reason": f"feed unreachable: {exc}", "recorded": []}
    rates = body.get("rates") if isinstance(body, dict) else None
    if not isinstance(rates, dict):
        return {"ok": False, "reason": "feed answered without rates", "recorded": []}
    recorded, refused = [], []
    for cur, value in rates.items():
        cur = str(cur).upper()
        if cur not in BANDS:
            continue
        try:
            await record(cur, float(value), source=f"reference feed {body.get('date') or ''}".strip(),
                         by="system:fx-refresh", kind="feed", as_of=now)
            recorded.append(cur)
        except (TypeError, ValueError):
            refused.append(cur)
    _last_refresh["at"] = now or datetime.now(timezone.utc)
    return {"ok": True, "recorded": recorded, "refused": refused}


def auto_refresh_enabled() -> bool:
    return os.getenv("FX_REFERENCE_AUTO_REFRESH", "true").strip().lower() in ("1", "true", "yes", "on")


async def daily_refresh(now: datetime | None = None) -> dict[str, Any] | None:
    """Called by the deal watcher every tick: refreshes at most once per 24 h."""
    if not auto_refresh_enabled():
        return None
    now = now or datetime.now(timezone.utc)
    last = _last_refresh.get("at")
    if last and now - last < timedelta(hours=24):
        return None
    _last_refresh["at"] = now
    return await refresh_from_source(now=now)


def reset_for_tests() -> None:
    _cache.clear()
    _memory.clear()
    _last_refresh.clear()
