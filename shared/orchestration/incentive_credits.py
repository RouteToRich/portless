"""RoDTEP / drawback actually credited vs what was counted (todo: "RoDTEP / drawback actually
credited").

The radar and the cost sheet count a confirmed export incentive as income (``trade_min.incentives``,
``eligible_confirmed``). Nothing checked that the money arrived: RoDTEP is credited as an
e-scrip in the ICEGATE ledger per Shipping Bill, drawback straight to the bank. Staff record each
credit from ICEGATE (``POST /deals/{id}/incentive-credits``: Shipping Bill, kind, amount, date —
audited) and ``check(deal)`` compares it with the amount counted:

    counted = incentive % of FOB × the FOB value (escrow principal less freight and insurance
              from the cost sheet when known, else the principal) — the same rule as the cost sheet

``status``: ``not_counted`` (no confirmed rate), ``awaiting`` (counted, nothing credited yet),
``short`` (credited below counted by more than ``INCENTIVE_CREDIT_TOLERANCE_PCT`` 5 %), ``ok``.
``short_all()`` feeds the digest and ``GET /deals/incentive-credits/short``.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

KINDS = ("rodtep", "rosctl", "drawback")
_memory: list[dict[str, Any]] = []


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("incentive_credits")
    except Exception:  # noqa: BLE001
        return None


def tolerance_pct() -> float:
    try:
        return max(0.0, float(os.getenv("INCENTIVE_CREDIT_TOLERANCE_PCT", "5") or 5))
    except ValueError:
        return 5.0


async def record(deal_id: str, *, shipping_bill: str, kind: str, amount_usd: float,
                 credited_on: str | None, by: str) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not (shipping_bill or "").strip():
        raise ValueError("the Shipping Bill number is required")
    if amount_usd is None or amount_usd < 0:
        raise ValueError("amount_usd must be zero or more")
    if credited_on:
        try:
            date.fromisoformat(credited_on)
        except ValueError as exc:
            raise ValueError("credited_on must be an ISO date") from exc
    row = {"credit_id": f"INC-{uuid4().hex[:10].upper()}", "deal_id": deal_id, "shipping_bill": shipping_bill.strip(),
           "kind": kind, "amount_usd": round(float(amount_usd), 2), "credited_on": credited_on, "by": by,
           "at": datetime.now(timezone.utc).isoformat()}
    col = _collection()
    if col is None:
        _memory.append(row)
    else:
        await col.insert_one({"_id": row["credit_id"], **row})
    return row


async def recorded(deal_id: str) -> list[dict[str, Any]]:
    col = _collection()
    if col is None:
        return [r for r in _memory if r["deal_id"] == deal_id]
    out = []
    try:
        async for d in col.find({"deal_id": deal_id}):
            d.pop("_id", None)
            out.append(d)
    except Exception as exc:  # noqa: BLE001
        logger.warning("IncentiveCredits: %s unreadable: %s", deal_id, exc)
    return out


async def _fob_value(deal: Any) -> float:
    """FOB value of the shipment: escrow principal (else PI value) less freight + insurance."""
    value = 0.0
    try:
        from services.escrow_guard.repository import get_repository as eg_repo  # type: ignore[import]
        hold = await eg_repo().get_hold_by_deal(deal.deal_id)
        if hold is not None:
            value = float(hold.amount or 0.0)
    except Exception:  # noqa: BLE001
        value = 0.0
    if not value:
        value = float(getattr(deal, "buyer_target_price_usd", 0) or 0) * float(getattr(deal, "quantity_mt", 0) or 0)
    qty = float(getattr(deal, "quantity_mt", 0) or 0)
    try:
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
        session = await neg_repo().get_session_by_deal(deal.deal_id)
        lines = getattr(getattr(session, "cost_sheet", None), "lines", None) or []
        freight = sum(float(getattr(line, "usd_per_mt", 0) or 0) for line in lines if getattr(line, "key", "") == "ocean_freight")
        ins = sum(float(getattr(line, "pct_of_price", 0) or 0) for line in lines if getattr(line, "key", "") == "marine_insurance")
        value = value / (1 + ins / 100.0) - freight * qty
    except Exception:  # noqa: BLE001
        pass
    return max(0.0, value)


def assess(counted_usd: float | None, credited_usd: float, tolerance: float) -> str:
    """Pure: the status of one deal's credits."""
    if not counted_usd:
        return "not_counted"
    if credited_usd <= 0:
        return "awaiting"
    if credited_usd < counted_usd * (1 - tolerance / 100.0):
        return "short"
    return "ok"


async def check(deal_id: str) -> dict[str, Any]:
    from services.deals.repository import get_repository  # type: ignore[import]
    deal = await get_repository().get(deal_id)
    if deal is None:
        raise LookupError(f"deal {deal_id} not found")
    from services.trade_min import incentives
    entry = await incentives.entry_for(getattr(deal, "hs_code", "") or "")
    fob = await _fob_value(deal)
    pct = 0.0
    if entry is not None and entry.eligible_confirmed:
        pct = incentives.total_pct(entry)
    counted = round(fob * pct / 100.0, 2) if pct else None
    rows = await recorded(deal_id)
    credited = round(sum(r["amount_usd"] for r in rows), 2)
    status = assess(counted, credited, tolerance_pct())
    return {"deal_id": deal_id, "fob_value_usd": round(fob, 2), "incentive_pct": pct or None,
            "counted_usd": counted, "credited_usd": credited,
            "short_usd": round(max(0.0, (counted or 0) - credited), 2) if status == "short" else 0.0,
            "status": status, "credits": rows}


async def short_all() -> list[dict[str, Any]]:
    """Every deal with a recorded credit below what was counted (digest)."""
    ids = sorted({r["deal_id"] for r in _memory})
    col = _collection()
    if col is not None:
        try:
            ids = sorted(set(await col.distinct("deal_id")))
        except Exception:  # noqa: BLE001
            pass
    out = []
    for deal_id in ids:
        try:
            res = await check(deal_id)
        except Exception:  # noqa: BLE001
            continue
        if res["status"] == "short":
            out.append({k: res[k] for k in ("deal_id", "counted_usd", "credited_usd", "short_usd")})
    return out


def reset_for_tests() -> None:
    _memory.clear()
