"""Sea freight per lane for the radar's profit, instead of one estimate per region.

A lane is India's usual port of loading → the destination country, per container type
(dry 20' / reefer 20'). Its rate comes from, best first:

* a **forwarder quote** cached for ``LANE_QUOTE_MAX_AGE_DAYS`` (default 14): the quote
  connector of ``FREIGHT_QUOTE_PROVIDER`` (Cogoport / Freightos ``quote()``), asked at
  scan time only when that provider's key is really configured — a placeholder key is
  never sent;
* a **rate staff entered** from a quote they received (``PUT /trade-min/freight-lanes``);
* the **regional estimate** (``landed_cost.freight_per_mt``), × ``REEFER_FREIGHT_FACTOR``
  (default 1.6) for reefer boxes.

Stored in Mongo ``freight_lane_rates`` next to the opportunities, else in memory.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_COLLECTION = "freight_lane_rates"
_memory: dict[str, "LaneRate"] = {}
ORIGIN_PORT = "Nhava Sheva, IN"

_QUOTE_KEYS = {"cogoport": "COGOPORT_API_KEY", "freightos": "FREIGHTOS_API_KEY"}


class LaneRate(BaseModel):
    market: str
    container_type: str = "dry"
    origin_port: str = ORIGIN_PORT
    usd_per_container: float = Field(gt=0)
    container_mt: float = Field(default=20.0, gt=0)
    provider: str = "staff"
    quoted_at: datetime | None = None
    note: str | None = None

    @property
    def usd_per_mt(self) -> float:
        return round(self.usd_per_container / self.container_mt, 2)


_history: list[dict] = []
_HISTORY = "freight_lane_history"


def reset_for_tests() -> None:
    _memory.clear()
    _history.clear()


def _key(market: str, container_type: str) -> str:
    return f"{(market or '').upper()}:{(container_type or 'dry').lower()}"


def max_age() -> timedelta:
    try:
        return timedelta(days=float(os.getenv("LANE_QUOTE_MAX_AGE_DAYS", "") or 14))
    except ValueError:
        return timedelta(days=14)


def reefer_factor() -> float:
    try:
        return float(os.getenv("REEFER_FREIGHT_FACTOR", "") or 1.6)
    except ValueError:
        return 1.6


async def _collection():
    try:
        from .repository import MongoOpportunityRepository, get_repository
        repo = await get_repository()
        if isinstance(repo, MongoOpportunityRepository):
            return repo._col.database[_COLLECTION]  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Freight lanes: no Mongo collection: %s", exc)
    return None


async def save(rate: LaneRate) -> LaneRate:
    rate.market = rate.market.upper()
    rate.quoted_at = rate.quoted_at or datetime.now(timezone.utc)
    _memory[_key(rate.market, rate.container_type)] = rate
    point = {"lane": _key(rate.market, rate.container_type), "usd_per_container": rate.usd_per_container,
             "provider": rate.provider, "quoted_at": _aware(rate.quoted_at).isoformat()}
    _history.append(point)
    col = await _collection()
    if col is not None:
        doc = rate.model_dump(mode="json")
        doc["_id"] = _key(rate.market, rate.container_type)
        await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        try:
            await col.database[_HISTORY].insert_one(dict(point))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Freight lanes: history not stored: %s", exc)
    return rate


async def history(market: str, container_type: str = "dry", days: int = 180) -> list[dict]:
    """Every rate seen on the lane in the last ``days`` (quotes, staff entries, bookings)."""
    want = _key(market, container_type)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    col = await _collection()
    if col is not None:
        try:
            return await col.database[_HISTORY].find({"lane": want, "quoted_at": {"$gte": since}}).to_list(length=2000)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Freight lanes: history unreadable: %s", exc)
    return [p for p in _history if p["lane"] == want and p["quoted_at"] >= since]


def deviation_pct() -> float:
    try:
        return float(os.getenv("LANE_QUOTE_DEVIATION_PCT", "") or 20.0)
    except ValueError:
        return 20.0


async def check_quote(market: str, usd_per_container: float, container_type: str = "dry") -> dict:
    """A single freight quote against the lane's own rate index (median of the last 180 days of
    rates seen — todo 13.6 "freight rate index per lane"): ``in_line`` / ``above_market`` /
    ``below_market`` beyond ``LANE_QUOTE_DEVIATION_PCT`` (20), or ``no_index`` with fewer than
    ``LANE_INDEX_MIN_POINTS`` (3) points."""
    import statistics
    pts = [float(p["usd_per_container"]) for p in await history(market, container_type) if p.get("usd_per_container")]
    try:
        need = int(os.getenv("LANE_INDEX_MIN_POINTS", "3") or 3)
    except ValueError:
        need = 3
    out = {"market": (market or "").upper(), "container_type": container_type, "quote_usd": usd_per_container,
           "points": len(pts)}
    if len(pts) < need:
        return {**out, "index_usd": None, "deviation_pct": None, "verdict": "no_index"}
    index = statistics.median(pts)
    dev = round((usd_per_container - index) / index * 100.0, 1)
    verdict = "above_market" if dev > deviation_pct() else "below_market" if dev < -deviation_pct() else "in_line"
    return {**out, "index_usd": round(index, 2), "deviation_pct": dev, "verdict": verdict}


async def list_rates() -> list[LaneRate]:
    col = await _collection()
    if col is not None:
        try:
            docs = await col.find({}).to_list(length=1000)
            out = []
            for d in docs:
                d.pop("_id", None)
                out.append(LaneRate.model_validate(d))
            return out
        except Exception as exc:  # noqa: BLE001
            logger.debug("Freight lanes: read failed: %s", exc)
    return list(_memory.values())


async def lane(market: str, container_type: str = "dry") -> LaneRate | None:
    """The lane's rate when it is recent enough, else None."""
    want = _key(market, container_type)
    for r in await list_rates():
        if _key(r.market, r.container_type) == want:
            if r.quoted_at and datetime.now(timezone.utc) - _aware(r.quoted_at) > max_age():
                return None
            return r
    return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def provider() -> str | None:
    """The quote provider whose key is really configured, else None."""
    from shared.integration_mode import configured
    name = (os.getenv("FREIGHT_QUOTE_PROVIDER", "") or os.getenv("SHIELD_LINK_FREIGHT_QUOTE_PROVIDER", "")
            or "cogoport").strip().lower()
    key = _QUOTE_KEYS.get(name)
    return name if key and configured(key) else None


def _rate_from(quote: dict) -> float | None:
    for k in ("rate_usd", "total_usd", "price_usd", "amount_usd"):
        try:
            v = float(quote.get(k) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    rates = quote.get("rates") or quote.get("data") or []
    if isinstance(rates, list):
        vals = []
        for r in rates:
            if isinstance(r, dict):
                for k in ("rate_usd", "total_usd", "price", "amount"):
                    try:
                        v = float(r.get(k) or 0)
                    except (TypeError, ValueError):
                        v = 0.0
                    if v > 0:
                        vals.append(v)
                        break
        return min(vals) if vals else None
    return None


async def refresh(market: str, hs_code: str, container_type: str = "dry", container_mt: float = 20.0) -> LaneRate | None:
    """Ask the quote provider for this lane when it is configured and no recent quote exists."""
    if await lane(market, container_type):
        return None
    name = provider()
    if not name:
        return None
    try:
        from shared.connectors.registry import get_connector
        conn = get_connector(name)
        quote = await conn.quote({
            "origin_port": ORIGIN_PORT, "destination_country": market.upper(), "hs_code": hs_code,
            "container_type": EQUIPMENT_CODES.get(container_type, "20GP"), "containers": 1,
            **({"load_type": "LCL", "weight_tonnes": container_mt} if container_type == "lcl" else {}),
        })
    except Exception as exc:  # noqa: BLE001 - the regional estimate stays
        logger.info("Freight lanes: %s quote for %s failed: %s", name, market, exc)
        return None
    rate = _rate_from(quote or {})
    if not rate or (quote or {}).get("stub"):
        return None
    return await save(LaneRate(market=market, container_type=container_type, usd_per_container=rate,
                               container_mt=container_mt, provider=name))


# Lane key → the equipment code the forwarder APIs use (ISO 6346 size-type groups).
EQUIPMENT_CODES = {"dry": "20GP", "dry40": "40GP", "hc40": "40HC", "reefer": "20RF", "reefer40": "40RH", "lcl": "LCL"}
_PAYLOAD_T = {"dry": 25.0, "dry40": 26.5, "hc40": 26.5, "reefer": 21.0, "reefer40": 26.0, "lcl": 1.0}


async def refresh_all(market: str, hs_code: str, kind: str = "dry", lot_mt: float | None = None) -> list[LaneRate]:
    """Quote every container size for the lane (todo: freight APIs per container type), so the
    container choice compares real 20' / 40' / 40' HC (or reefer) prices — and LCL for a small
    lot. Only when the quote provider is really configured; a lane quoted recently is skipped."""
    keys = ["reefer", "reefer40"] if kind == "reefer" else ["dry", "dry40", "hc40"]
    if kind != "reefer" and lot_mt is not None and lot_mt <= 12:
        keys.append("lcl")
    saved = []
    for key in keys:
        rate = await refresh(market, hs_code, key, _PAYLOAD_T[key])
        if rate is not None:
            saved.append(rate)
    return saved


async def freight_per_mt(market: str, container_type: str = "dry") -> tuple[float, str]:
    """(USD per MT, basis) for the profit: lane quote, else regional estimate."""
    r = await lane(market, container_type)
    if r is not None:
        when = _aware(r.quoted_at).date().isoformat() if r.quoted_at else "unknown date"
        return r.usd_per_mt, f"lane_quote ({r.provider}, {when})"
    from shared.orchestration import landed_cost as lc
    base = lc.freight_per_mt(market)
    if container_type == "reefer":
        return round(base * reefer_factor(), 2), "region_estimate (reefer)"
    return base, "region_estimate"
