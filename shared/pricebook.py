"""Price book — what PortLess knows about prices per HS code, from every source.

Buyers and suppliers rarely state prices up front, so a prospect cannot be
matched on stated prices alone. The price book collects every price signal the
platform ever sees and turns it into an *estimate with uncertainty* per side:

- **supplier side** (USD/MT, FOB India): supplier quotes (RFQ replies), supplier
  offers entered by operators, and the price agreed with the supplier;
- **buyer side** (USD/MT, CIF destination): buyer target prices, counters to our
  indicative offers, accepted offers, and agreed buyer prices.

Public anchors are the prior: UN Comtrade unit values the scan already fetches —
what the destination market pays per MT (buyer side) and India's own export
unit value (supplier side; derived from the market benchmark and the measured
corridor gap). With no private observation the estimate is the prior with a
±15 % band and confidence ``prior``; every observation pulls it toward what
real counterparties said, recent ones more (90-day half-life), the same grade
and the same market more.

Storage: Mongo ``price_observations`` (the app's single database) when
``MONGO_URI`` is set, else in memory.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Side = Literal["supplier", "buyer"]
Kind = Literal["supplier_quote", "supplier_offer", "supplier_agreed",
               "buyer_target", "buyer_counter", "buyer_accepted", "buyer_agreed", "indicative_offer"]

_COLLECTION = "price_observations"
_HALF_LIFE_DAYS = 90.0
_PRIOR_WEIGHT = 1.5
_PRIOR_SPREAD = 0.15
_OTHER_MARKET_WEIGHT = 0.35
_OTHER_GRADE_WEIGHT = 0.5
_MIN_SIGMA_PCT = 0.03


class PriceObservation(BaseModel):
    obs_id: str = Field(default_factory=lambda: f"PX-{uuid4().hex[:12].upper()}")
    hs_code: str
    side: Side
    kind: Kind
    price_usd_per_mt: float
    """Supplier side: FOB India. Buyer side: CIF destination (converted when the
    counterparty quoted another incoterm)."""
    market: str | None = None
    grade: str | None = None
    incoterm: str | None = None
    quantity_mt: float | None = None
    deal_id: str | None = None
    counterparty_id: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    note: str | None = None


class PriceEstimate(BaseModel):
    hs_code: str
    side: Side
    market: str | None = None
    grade: str | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    sigma: float | None = None
    """Spread used for probabilities (IQR / 1.349, at least 3 % of the median)."""
    n_observations: int = 0
    prior: float | None = None
    prior_source: str | None = None
    confidence: Literal["none", "prior", "low", "medium", "high"] = "none"


_memory: list[PriceObservation] = []


def reset_for_tests() -> None:
    _memory.clear()


def _mongo_collection():
    if os.getenv("PRICEBOOK_MONGO", "true").strip().lower() in ("0", "false", "no", "off"):
        return None
    try:
        from shared.config import settings
        if not settings.mongo_uri:
            return None
        import motor.motor_asyncio as motor  # type: ignore[import]
        client: Any = motor.AsyncIOMotorClient(settings.mongo_uri)
        return client[settings.mongo_db_name][_COLLECTION]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Price book: Mongo unavailable (%s) — in-memory only", exc)
        return None


async def record(obs: PriceObservation) -> PriceObservation | None:
    """Store one observation. Never raises — a lost observation must not break a flow."""
    if not obs.price_usd_per_mt or obs.price_usd_per_mt <= 0 or not math.isfinite(obs.price_usd_per_mt):
        return None
    obs.hs_code = str(obs.hs_code)
    obs.market = obs.market.upper() if obs.market else None
    col = _mongo_collection()
    if col is not None:
        try:
            await col.insert_one(obs.model_dump(mode="python"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Price book: could not persist %s: %s", obs.obs_id, exc)
            _memory.append(obs)
    else:
        _memory.append(obs)
    logger.info("Price book: %s %s HS %s %s $%.2f/MT (%s)", obs.side, obs.kind, obs.hs_code,
                obs.market or "", obs.price_usd_per_mt, obs.deal_id or "-")
    return obs


async def observations(hs_code: str, side: Side | None = None, *, limit: int = 500) -> list[PriceObservation]:
    query: dict[str, Any] = {"hs_code": str(hs_code)}
    if side:
        query["side"] = side
    col = _mongo_collection()
    rows: list[PriceObservation] = []
    if col is not None:
        try:
            async for doc in col.find(query).sort("observed_at", -1).limit(limit):
                doc.pop("_id", None)
                rows.append(PriceObservation(**doc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Price book: read failed for HS %s: %s", hs_code, exc)
    rows += [o for o in _memory if o.hs_code == str(hs_code) and (side is None or o.side == side)]
    seen: set[str] = set()
    out = []
    for o in sorted(rows, key=lambda o: o.observed_at, reverse=True):
        if o.obs_id not in seen:
            seen.add(o.obs_id)
            out.append(o)
    return out[:limit]


def _grade_tokens(grade: str | None) -> set[str]:
    import re
    return {t for t in re.findall(r"[a-z0-9.]+", (grade or "").lower()) if len(t) >= 2}


def _weighted_quantile(points: list[tuple[float, float]], q: float) -> float:
    pts = sorted(points)
    total = sum(w for _, w in pts)
    acc = 0.0
    for value, w in pts:
        acc += w
        if acc >= q * total:
            return value
    return pts[-1][0]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def estimate_from(
    obs: list[PriceObservation],
    *,
    hs_code: str,
    side: Side,
    market: str | None = None,
    grade: str | None = None,
    prior: float | None = None,
    prior_source: str | None = None,
    now: datetime | None = None,
) -> PriceEstimate:
    """Pure: the estimate for one side from observations + an optional prior."""
    now = now or datetime.now(timezone.utc)
    want = _grade_tokens(grade)
    points: list[tuple[float, float]] = []
    n = 0
    for o in obs:
        if o.side != side or o.kind == "indicative_offer":
            continue  # our own offers are not market evidence
        age_days = max(0.0, (now - _aware(o.observed_at)).total_seconds() / 86400.0)
        w = 0.5 ** (age_days / _HALF_LIFE_DAYS)
        if side == "buyer" and market and o.market and o.market != market.upper():
            w *= _OTHER_MARKET_WEIGHT
        have = _grade_tokens(o.grade)
        if want and have and not (want & have):
            w *= _OTHER_GRADE_WEIGHT
        if o.kind in ("buyer_agreed", "supplier_agreed", "buyer_accepted"):
            w *= 1.5  # a price someone actually committed to
        points.append((o.price_usd_per_mt, w))
        n += 1
    if prior and prior > 0:
        for f in (1 - _PRIOR_SPREAD, 1.0, 1 + _PRIOR_SPREAD):
            points.append((prior * f, _PRIOR_WEIGHT / 3))
    est = PriceEstimate(hs_code=hs_code, side=side, market=market, grade=grade, n_observations=n,
                        prior=prior, prior_source=prior_source if prior else None)
    if not points:
        return est
    est.median = round(_weighted_quantile(points, 0.5), 2)
    est.p25 = round(_weighted_quantile(points, 0.25), 2)
    est.p75 = round(_weighted_quantile(points, 0.75), 2)
    est.sigma = round(max((est.p75 - est.p25) / 1.349, est.median * _MIN_SIGMA_PCT), 2)
    est.confidence = "high" if n >= 5 else "medium" if n >= 2 else "low" if n == 1 else "prior"
    return est


async def public_priors(hs_code: str, market: str | None) -> dict[str, Any]:
    """Comtrade anchors from the newest scan of this corridor: buyer side = what the
    market pays per MT; supplier side = India's export unit value."""
    out: dict[str, Any] = {"buyer": None, "supplier": None, "source": None, "tariff_pct": None}
    try:
        from services.trade_min.repository import get_repository as _trade_repo  # type: ignore[import]
        repo = await _trade_repo()
        opps = await repo.list_by_hs_market(hs_code, market) if market else []
        if not opps:
            return out
        opp = max(opps, key=lambda o: o.scanned_at)
        bench = getattr(opp, "price_benchmark_usd_per_mt", None)
        gap = getattr(opp, "price_gap_pct", None)
        if bench:
            out["buyer"] = round(float(bench), 2)
            out["source"] = getattr(opp, "price_benchmark_source", None) or "comtrade"
            if gap is not None and float(gap) > -90:
                out["supplier"] = round(float(bench) / (1 + float(gap) / 100.0), 2)
        tariff = getattr(opp, "tariff_data", None)
        if tariff is not None:
            out["tariff_pct"] = getattr(tariff, "applied_duty_pct", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Price book: no public prior for %s×%s: %s", hs_code, market, exc)
    return out


async def estimate(hs_code: str, side: Side, *, market: str | None = None, grade: str | None = None,
                   priors: dict[str, Any] | None = None) -> PriceEstimate:
    priors = priors if priors is not None else await public_priors(hs_code, market)
    obs = await observations(hs_code, side)
    return estimate_from(obs, hs_code=hs_code, side=side, market=market, grade=grade,
                         prior=priors.get(side), prior_source=priors.get("source"))
