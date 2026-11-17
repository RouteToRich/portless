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


def reset_for_tests() -> None:
    _memory.clear()


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
    col = await _collection()
    if col is not None:
        doc = rate.model_dump(mode="json")
        doc["_id"] = _key(rate.market, rate.container_type)
        await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    return rate


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
            "container_type": "20RF" if container_type == "reefer" else "20GP", "containers": 1,
        })
    except Exception as exc:  # noqa: BLE001 - the regional estimate stays
        logger.info("Freight lanes: %s quote for %s failed: %s", name, market, exc)
        return None
    rate = _rate_from(quote or {})
    if not rate or (quote or {}).get("stub"):
        return None
    return await save(LaneRate(market=market, container_type=container_type, usd_per_container=rate,
                               container_mt=container_mt, provider=name))


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
