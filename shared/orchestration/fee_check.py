"""Fees actually charged vs the fees PortLess priced in.

Every price PortLess quotes assumes an escrow fee (``LANDED_ESCROW_FEE_PCT`` / the deal's cost
sheet), a supplier-payout fee (``PAYOUT_FEE_PCT``, default 0) and no bank charges. The real
figures come from the provider and bank statements: staff record them per deal
(``POST /escrow-guard/deals/{id}/fees``: kind ``escrow`` / ``payout`` / ``bank``, amount, statement
reference). ``check(deal_id)`` compares, per kind, the actual fee as a % of the money it applied
to (released escrow / payouts) with the assumption; above it by more than
``FEE_TOLERANCE_PCT`` (0.25 points) is a finding — the deal's margin is smaller than shown. Before
anything is recorded the escrow provider's own quoted fee % (on the escrow) is the actual figure.

``GET /escrow-guard/deals/{id}/fees`` shows one deal, ``GET /escrow-guard/fees/over`` every deal
above its assumption; the daily digest lists them.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

KINDS = ("escrow", "payout", "bank")
_memory: list[dict[str, Any]] = []
_col: Any = None
_resolved = False


def _collection() -> Any:
    global _col, _resolved
    if _resolved:
        return _col
    _resolved = True
    try:
        from shared.config import settings
        if settings.mongo_uri:
            import motor.motor_asyncio as motor  # type: ignore[import]
            _col = motor.AsyncIOMotorClient(settings.mongo_uri)[settings.mongo_db_name]["deal_fees"]
    except Exception as exc:  # noqa: BLE001
        logger.debug("FeeCheck: Mongo unavailable: %s", exc)
    return _col


def set_collection_for_tests(col: Any) -> None:
    global _col, _resolved
    _col, _resolved = col, True
    _memory.clear()


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


async def record(deal_id: str, kind: str, amount_usd: float, *, ref: str | None, by: str) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if amount_usd < 0:
        raise ValueError("amount must be positive")
    row = {"fee_id": f"FEE-{uuid4().hex[:10].upper()}", "deal_id": deal_id, "kind": kind,
           "amount_usd": round(float(amount_usd), 2), "ref": ref, "by": by,
           "at": datetime.now(timezone.utc).isoformat()}
    col = _collection()
    if col is None:
        _memory.append(row)
    else:
        await col.insert_one({"_id": row["fee_id"], **row})
    return row


async def recorded(deal_id: str) -> list[dict[str, Any]]:
    col = _collection()
    if col is None:
        return [r for r in _memory if r["deal_id"] == deal_id]
    out = []
    async for d in col.find({"deal_id": deal_id}):
        d.pop("_id", None)
        out.append(d)
    return out


async def _assumed(deal_id: str) -> dict[str, float]:
    escrow = None
    try:
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
        session = await neg_repo().get_session_by_deal(deal_id)
        for line in (getattr(getattr(session, "cost_sheet", None), "lines", None) or []):
            if getattr(line, "key", "") == "escrow_fee":
                escrow = float(getattr(line, "pct_of_price", 0) or 0) or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("FeeCheck: cost sheet of %s unavailable: %s", deal_id, exc)
    if escrow is None:
        from shared.orchestration.landed_cost import escrow_fee_pct
        escrow = escrow_fee_pct()
    return {"escrow": escrow, "payout": _num("PAYOUT_FEE_PCT", 0.0), "bank": 0.0}


async def check(deal_id: str) -> dict[str, Any]:
    from services.escrow_guard.service import get_deal_ledger  # type: ignore[import]
    ledger = await get_deal_ledger(deal_id)
    base = {"escrow": float(getattr(ledger, "released_total", 0) or 0),
            "payout": float(getattr(ledger, "payouts_total", 0) or 0),
            "bank": float(getattr(ledger, "released_total", 0) or 0)}
    assumed = await _assumed(deal_id)
    rows = await recorded(deal_id)
    tolerance = _num("FEE_TOLERANCE_PCT", 0.25)
    lines, over_usd = [], 0.0
    for kind in KINDS:
        amount = round(sum(r["amount_usd"] for r in rows if r["kind"] == kind), 2)
        source = "statement" if amount else None
        if not amount and kind == "escrow" and getattr(ledger, "fees_total", 0):
            amount, source = round(float(ledger.fees_total), 2), "provider quote"
        actual_pct = round(amount / base[kind] * 100, 3) if base[kind] and amount else None
        # A fee nobody assumed (bank charges, a payout fee priced at 0) is over at any amount;
        # an assumed percentage gets the tolerance for rounding.
        over = actual_pct is not None and actual_pct > assumed[kind] + (tolerance if assumed[kind] > 0 else 0.0)
        extra = round(amount - base[kind] * assumed[kind] / 100, 2) if over else 0.0
        over_usd += extra
        lines.append({"kind": kind, "assumed_pct": assumed[kind], "actual_pct": actual_pct, "actual_usd": amount,
                      "applies_to_usd": round(base[kind], 2), "source": source, "over": over, "over_usd": extra})
    return {"deal_id": deal_id, "lines": lines, "over": any(l["over"] for l in lines),
            "over_usd": round(over_usd, 2), "recorded": rows}


async def over_all() -> list[dict[str, Any]]:
    """Every deal with an escrow ledger whose fees run above the assumption."""
    from services.escrow_guard.service import list_deal_ledgers  # type: ignore[import]
    out = []
    for led in await list_deal_ledgers():
        try:
            c = await check(led.deal_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("FeeCheck: %s skipped: %s", led.deal_id, exc)
            continue
        if c["over"]:
            out.append({"deal_id": led.deal_id, "over_usd": c["over_usd"],
                        "kinds": [l["kind"] for l in c["lines"] if l["over"]]})
    return sorted(out, key=lambda r: -r["over_usd"])
