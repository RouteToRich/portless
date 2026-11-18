"""Amounts charged back to the supplier: deducted from what PortLess owes it.

Today one kind: a **re-inspection** after the supplier's own failed QC (review board B-17).
The first inspection is in the price; a second one, booked because the lot failed, is the
supplier's cost (``SUPPLIER_REINSPECTION_CHARGE``, default on). The rework notice says so,
and the amount — the inspection order's own fee, else the cost sheet's estimate for one
inspection — is recorded here.

Everything that decides how much the supplier is paid reads ``total(deal_id)``: the escrow
ledger's ``supplier_owed`` / ``supplier_balance``, the payout cap, the PAID rule and the
continuations' payout gates use PO value − chargebacks. The journal posts each one as a
recovery (Dr supplier_payable, Cr trade_costs).

Mongo collection ``supplier_chargebacks`` (memory without Mongo). Reads never raise.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

KINDS = ("reinspection",)
_memory: list[dict[str, Any]] = []
_col: Any = None
_resolved = False


def _collection() -> Any:
    global _col, _resolved
    if _resolved:
        return _col
    _resolved = True
    try:
        from shared.mongo import collection
        _col = collection("supplier_chargebacks")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Chargebacks: Mongo unavailable: %s", exc)
    return _col


def set_collection_for_tests(col: Any) -> None:
    global _col, _resolved
    _col, _resolved = col, True
    _memory.clear()


def reinspection_charged() -> bool:
    return os.getenv("SUPPLIER_REINSPECTION_CHARGE", "true").strip().lower() in ("1", "true", "yes", "on")


async def record(deal_id: str, kind: str, amount_usd: float, *, reason: str, ref: str | None = None,
                 by: str = "system") -> dict[str, Any] | None:
    """Record one chargeback; the same (deal, kind, ref) is recorded once."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    amount = round(float(amount_usd or 0.0), 2)
    if amount <= 0:
        return None
    for r in await listed(deal_id):
        if r["kind"] == kind and ref and r.get("ref") == ref:
            return r
    row = {"chargeback_id": f"CB-{uuid4().hex[:10].upper()}", "deal_id": deal_id, "kind": kind,
           "amount_usd": amount, "reason": reason, "ref": ref, "by": by,
           "at": datetime.now(timezone.utc).isoformat()}
    col = _collection()
    if col is None:
        _memory.append(row)
    else:
        await col.insert_one({"_id": row["chargeback_id"], **row})
    logger.warning("Chargebacks: %s USD %.2f charged to the supplier of %s (%s)", kind, amount, deal_id, reason)
    return row


async def listed(deal_id: str) -> list[dict[str, Any]]:
    try:
        col = _collection()
        if col is None:
            return [r for r in _memory if r["deal_id"] == deal_id]
        out = []
        async for d in col.find({"deal_id": deal_id}):
            d.pop("_id", None)
            out.append(d)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chargebacks: %s unreadable: %s", deal_id, exc)
        return []


async def total(deal_id: str) -> float:
    return round(sum(float(r["amount_usd"]) for r in await listed(deal_id)), 2)


async def net_owed(deal_id: str, owed: float | None) -> float | None:
    """What the supplier is owed after chargebacks (None stays None)."""
    if owed is None:
        return None
    return round(max(0.0, float(owed) - await total(deal_id)), 2)


async def reinspection_cost(deal_id: str, booking: Any = None) -> float:
    """The re-inspection's cost: the provider order's fee when it reports one, else the cost
    sheet's estimate for one inspection, else the LANDED_INSPECTION_USD_PER_SHIPMENT setting."""
    payload = getattr(booking, "provider_payload", None) or {}
    for key in ("fee_usd", "total_usd"):
        if isinstance(payload.get(key), (int, float)) and payload[key] > 0:
            return round(float(payload[key]), 2)
    try:
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
        session = await neg_repo().get_session_by_deal(deal_id)
        sheet = getattr(session, "cost_sheet", None) or {}
        line = next((ln for ln in sheet.get("lines") or [] if ln.get("key") == "inspection"), None)
        if line and sheet.get("quantity_mt"):
            phases = max(1, int(sheet.get("inspection_phases") or 1))
            return round(float(line.get("usd_per_mt") or 0.0) * float(sheet["quantity_mt"]) / phases, 2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Chargebacks: cost sheet of %s unavailable: %s", deal_id, exc)
    from shared.orchestration import landed_cost
    return round(landed_cost.inspection_usd_per_shipment(), 2)
