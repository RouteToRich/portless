"""Apify spend controls shared by every actor connector (shared/connectors/apify.py).

**Daily budget** (``APIFY_MAX_RUNS_PER_DAY`` and ``APIFY_MAX_USD_PER_DAY``; 0 = unlimited).
Every actor run costs money; a scan over a large catalogue, the enrichment of every new deal
and the carrier-tracking poll all start runs. The caps are a circuit breaker so a
mis-configured schedule cannot burn the account in a night. Runs and ``usageTotalUsd`` are
counted per UTC day in the ``platform_costs`` day document of ``shared/cost_meter.py``
(``apify_runs`` / ``apify_usd`` / ``apify_by_actor.<key>``), so every instance and every
restart sees the same count. A run is reserved atomically before it starts (``$inc``, then the
check; undone when over a cap). Without Mongo — or when the store does not answer — the process
memory here is the counter. ``_runs_by_day`` / ``_usd_by_day`` mirror what was last seen.

**Result cache** per actor input. Identical calls (the same search line for two corridors,
the same company re-checked in the next scan) are served from the last SUCCEEDED run for a
TTL per actor: Mongo ``apify_result_cache`` (memory without Mongo). Off with
``APIFY_CACHE_ENABLED=false`` (pinned off in tests/conftest.py). A TTL of 0 hours (carrier
tracking) never caches. ``APIFY_CACHE_HOURS_<ACTOR_KEY>`` overrides one actor.

**Carrier tracking pause.** After the first ``source_refused`` of a UTC day the tracking actor is
not run again that day (each run is paid and the plan refuses it anyway) unless
``APIFY_CARRIER_TRACKING_RETRY_REFUSED=true`` — set it once the actor can run (A.27).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_runs_by_day: dict[str, int] = {}
_usd_by_day: dict[str, float] = {}
_by_actor_day: dict[str, dict[str, dict[str, float]]] = {}
_cache_memory: dict[str, dict[str, Any]] = {}
_cache_stats: dict[str, dict[str, int]] = {}
_tracking_refused_day: dict[str, str] = {}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).date().isoformat()


# ---------------------------------------------------------------------------
# Daily budget
# ---------------------------------------------------------------------------

def max_runs_per_day() -> int:
    return max(0, int(_env_float("APIFY_MAX_RUNS_PER_DAY", 0)))


def max_usd_per_day() -> float:
    return max(0.0, _env_float("APIFY_MAX_USD_PER_DAY", 0.0))


def runs_today() -> int:
    return _runs_by_day.get(day_key(), 0)


def usd_today() -> float:
    return round(_usd_by_day.get(day_key(), 0.0), 4)


def over_cap(runs_before: int, usd: float) -> str | None:
    """Why another run may not start, or None."""
    cap = max_runs_per_day()
    if cap > 0 and runs_before >= cap:
        return f"daily run budget exhausted ({runs_before}/{cap} runs today, APIFY_MAX_RUNS_PER_DAY)"
    usd_cap = max_usd_per_day()
    if usd_cap > 0 and usd >= usd_cap:
        return f"daily spend cap reached ({usd:.2f}/{usd_cap:.2f} USD today, APIFY_MAX_USD_PER_DAY)"
    return None


def run_budget_left() -> bool:
    return over_cap(runs_today(), usd_today()) is None


def _forget_old_days(key: str) -> None:
    for book in (_runs_by_day, _usd_by_day, _by_actor_day):
        for stale in [k for k in book if k != key]:
            book.pop(stale, None)


def _actor_book(key: str, actor: str) -> dict[str, float]:
    return _by_actor_day.setdefault(key, {}).setdefault(actor, {"runs": 0, "usd": 0.0})


def count_run(actor: str = "other") -> None:
    """Count one run in process memory."""
    key = day_key()
    _forget_old_days(key)
    _runs_by_day[key] = _runs_by_day.get(key, 0) + 1
    _actor_book(key, actor)["runs"] += 1


def _usage_collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("platform_costs")
    except Exception:  # noqa: BLE001
        return None


def _cost_memory(**amounts: float) -> None:
    """Keep shared/cost_meter's in-process day row in step (its report reads it without Mongo)."""
    try:
        from shared import cost_meter
        cost_meter.add_memory_only(**amounts)
    except Exception:  # noqa: BLE001
        pass


async def reserve_run(actor: str) -> str | None:
    """Count one run for today before it starts; returns the refusal reason when a cap is reached."""
    key = day_key()
    _forget_old_days(key)
    col = _usage_collection()
    if col is not None:
        try:
            from pymongo import ReturnDocument  # type: ignore[import]
            inc = {"apify_runs": 1, f"apify_by_actor.{actor}.runs": 1}
            doc = await col.find_one_and_update(
                {"_id": key}, {"$inc": inc, "$set": {"day": key}},
                upsert=True, return_document=ReturnDocument.AFTER,
            ) or {}
            runs = int(doc.get("apify_runs") or 0)
            usd = float(doc.get("apify_usd") or 0.0)
            refusal = over_cap(runs - 1, usd)
            _usd_by_day[key] = usd
            if refusal:
                await col.update_one({"_id": key}, {"$inc": {k: -1 for k in inc}})
                _runs_by_day[key] = runs - 1
                return refusal
            _runs_by_day[key] = runs
            _actor_book(key, actor)["runs"] += 1
            _cost_memory(apify_runs=1)
            return None
        except Exception as exc:  # noqa: BLE001 - the counter store never stops the business
            logger.warning("Apify: usage store unreachable (%s) — counting in this process", exc)
    refusal = over_cap(runs_today(), usd_today())
    if refusal:
        return refusal
    count_run(actor)
    _cost_memory(apify_runs=1)
    return None


async def release_run(actor: str) -> None:
    """Undo a reservation whose run never started (start refused by Apify)."""
    key = day_key()
    _runs_by_day[key] = max(0, _runs_by_day.get(key, 0) - 1)
    book = _actor_book(key, actor)
    book["runs"] = max(0, book["runs"] - 1)
    _cost_memory(apify_runs=-1)
    col = _usage_collection()
    if col is not None:
        try:
            await col.update_one({"_id": key}, {"$inc": {"apify_runs": -1, f"apify_by_actor.{actor}.runs": -1}})
        except Exception:  # noqa: BLE001
            pass


async def record_usd(actor: str, usd: float) -> None:
    """Add a finished run's ``usageTotalUsd`` to today's spend."""
    if not usd:
        return
    key = day_key()
    _usd_by_day[key] = round(_usd_by_day.get(key, 0.0) + usd, 6)
    book = _actor_book(key, actor)
    book["usd"] = round(book["usd"] + usd, 6)
    _cost_memory(apify_usd=usd)
    col = _usage_collection()
    if col is not None:
        try:
            await col.update_one({"_id": key}, {"$inc": {"apify_usd": usd, f"apify_by_actor.{actor}.usd": usd},
                                                "$set": {"day": key}}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Apify: spend not stored: %s", exc)


async def usage_today() -> dict[str, Any]:
    """Today's runs / spend, in total and per actor, against the caps (digest, /health/costs)."""
    key = day_key()
    runs, usd = runs_today(), usd_today()
    by_actor = {a: {"runs": int(v["runs"]), "usd": round(v["usd"], 4)}
                for a, v in (_by_actor_day.get(key) or {}).items()}
    col = _usage_collection()
    if col is not None:
        try:
            doc = await col.find_one({"_id": key}) or {}
            if doc:
                runs = int(doc.get("apify_runs") or 0)
                usd = round(float(doc.get("apify_usd") or 0.0), 4)
                stored = doc.get("apify_by_actor") or {}
                by_actor = {a: {"runs": int((v or {}).get("runs") or 0),
                                "usd": round(float((v or {}).get("usd") or 0.0), 4)} for a, v in stored.items()}
        except Exception:  # noqa: BLE001
            pass
    return {"runs_today": runs, "usd_today": usd, "max_runs_per_day": max_runs_per_day(),
            "max_usd_per_day": max_usd_per_day(), "by_actor": by_actor,
            "budget_left": over_cap(runs, usd) is None}


# ---------------------------------------------------------------------------
# Result cache
# ---------------------------------------------------------------------------

DEFAULT_CACHE_HOURS: dict[str, float] = {
    "COMPANY_CONTACT_FINDER": 14 * 24,
    "EXPORT_MARKET_FINDER": 30 * 24,
    "EMAIL_LIST_QUALIFIER": 7 * 24,
    "CARRIER_TRACKING": 0,
}


def cache_enabled() -> bool:
    return _env_flag("APIFY_CACHE_ENABLED", True)


def cache_hours(actor_key: str) -> float:
    override = os.getenv(f"APIFY_CACHE_HOURS_{actor_key}")
    if override not in (None, ""):
        try:
            return max(0.0, float(override))
        except ValueError:
            pass
    if actor_key == "COUNTERPARTY_CHECK":
        return max(0.0, _env_float("TRADE_MIN_VALIDATION_MAX_AGE_DAYS", 30)) * 24.0
    return DEFAULT_CACHE_HOURS.get(actor_key, _env_float("APIFY_CACHE_HOURS", 24.0))


def caches(actor_key: str) -> bool:
    return cache_enabled() and cache_hours(actor_key) > 0


def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in sorted(value.items()) if k not in ("webhookUrl", "webhookSecret")}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.lower().split())
    return value


def cache_key(actor_ref: str, actor_input: dict[str, Any], limit: int) -> str:
    raw = json.dumps({"a": actor_ref, "i": _normalise(actor_input), "l": limit}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("apify_result_cache")
    except Exception:  # noqa: BLE001
        return None


def note_cache(actor_key: str, kind: str) -> None:
    book = _cache_stats.setdefault(actor_key, {"hits": 0, "misses": 0})
    book[kind] = book.get(kind, 0) + 1


def cache_stats(actor_key: str) -> dict[str, Any]:
    book = _cache_stats.get(actor_key) or {"hits": 0, "misses": 0}
    total = book["hits"] + book["misses"]
    return {"hits": book["hits"], "misses": book["misses"],
            "hit_rate": round(book["hits"] / total, 3) if total else None,
            "ttl_hours": cache_hours(actor_key), "enabled": caches(actor_key)}


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def cache_get(key: str) -> dict[str, Any] | None:
    entry = _cache_memory.get(key)
    if entry is None:
        col = _cache_collection()
        if col is not None:
            try:
                entry = await col.find_one({"_id": key})
            except Exception as exc:  # noqa: BLE001
                logger.debug("Apify: cache unreadable: %s", exc)
    if not entry:
        return None
    expires = _aware(entry.get("expires_at"))
    if expires is None or expires <= datetime.now(timezone.utc):
        return None
    return entry


async def cache_put(actor_key: str, key: str, rows: list[dict[str, Any]], run: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    entry = {
        "_id": key, "actor_key": actor_key, "rows": rows, "created_at": now,
        "expires_at": now + timedelta(hours=cache_hours(actor_key)),
        "run": {k: run.get(k) for k in ("id", "status", "defaultKeyValueStoreId", "defaultDatasetId")},
    }
    _cache_memory[key] = entry
    col = _cache_collection()
    if col is not None:
        try:
            await col.replace_one({"_id": key}, entry, upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Apify: cache not stored: %s", exc)


# ---------------------------------------------------------------------------
# Carrier tracking pause after source_refused
# ---------------------------------------------------------------------------

def tracking_paused() -> bool:
    if _env_flag("APIFY_CARRIER_TRACKING_RETRY_REFUSED", False):
        return False
    return _tracking_refused_day.get("day") == day_key()


def note_tracking_refused(detail: str) -> None:
    _tracking_refused_day.update({"day": day_key(), "detail": detail})


def tracking_refused_detail() -> str:
    return _tracking_refused_day.get("detail", "")


def reset_for_tests() -> None:
    for book in (_runs_by_day, _usd_by_day, _by_actor_day, _cache_memory, _cache_stats, _tracking_refused_day):
        book.clear()
