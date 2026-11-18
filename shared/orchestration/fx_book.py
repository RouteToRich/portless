"""Exchange rates actually used on a deal, and the FX gain or loss they make.

PortLess sells in USD and buys from Indian suppliers in INR. The books (journal, ledger) are
in USD, so the supplier payable is fixed in INR at the rate the PO was priced at, and every
INR payout made later at a different bank rate leaves a gain (the rupee weakened: fewer
dollars clear the same rupees) or a loss.

Staff record the rates from the bank advice (``POST /escrow-guard/deals/{id}/fx``):

  kind ``po``       the INR-per-USD rate the supplier's INR price was converted at for the PO
  kind ``payout``   the bank rate of one supplier payout (``ref`` = payout id)
  kind ``receipt``  the bank rate at which a released tranche was converted (``ref`` = tranche id);
                    kept for the e-BRC / bank reconciliation, no entry (the books are in USD)

``gain_loss(payout_usd, payout_rate, po_rate)``: the payout's rupees valued at the PO rate,
minus the dollars actually spent. The journal posts it to ``fx_gain_loss`` (credit = gain).
The latest record per (kind, ref) wins. Never raises on read.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

KINDS = ("po", "payout", "receipt")
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
        _col = collection("deal_fx_rates")
    except Exception as exc:  # noqa: BLE001
        logger.debug("FxBook: Mongo unavailable: %s", exc)
    return _col


def set_collection_for_tests(col: Any) -> None:
    global _col, _resolved
    _col, _resolved = col, True
    _memory.clear()


async def record(deal_id: str, kind: str, rate_inr_per_usd: float, *, ref: str | None = None,
                 by: str, note: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not rate_inr_per_usd or rate_inr_per_usd <= 0:
        raise ValueError("rate must be a positive number of INR per USD")
    if kind in ("payout", "receipt") and not ref:
        raise ValueError(f"a {kind} rate needs the {'payout' if kind == 'payout' else 'tranche'} id as ref")
    row = {"fx_id": f"FX-{uuid4().hex[:10].upper()}", "deal_id": deal_id, "kind": kind,
           "rate": round(float(rate_inr_per_usd), 4), "ref": ref if kind != "po" else None, "by": by,
           "note": note, "at": datetime.now(timezone.utc).isoformat()}
    col = _collection()
    if col is None:
        _memory.append(row)
    else:
        await col.insert_one({"_id": row["fx_id"], **row})
    return row


async def recorded(deal_id: str) -> list[dict[str, Any]]:
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
        logger.warning("FxBook: rates of %s unreadable: %s", deal_id, exc)
        return []


def rates_from(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """{"po": rate | None, "payout": {payout_id: rate}, "receipt": {tranche_id: rate}} — latest wins."""
    out: dict[str, Any] = {"po": None, "payout": {}, "receipt": {}}
    for r in sorted(rows, key=lambda r: str(r.get("at") or "")):
        if r["kind"] == "po":
            out["po"] = float(r["rate"])
        elif r.get("ref"):
            out[r["kind"]][r["ref"]] = float(r["rate"])
    return out


async def rates(deal_id: str) -> dict[str, Any]:
    return rates_from(await recorded(deal_id))


def gain_loss(payout_usd: float, payout_rate: float | None, po_rate: float | None) -> float:
    """USD gain (+) or loss (−) of one payout; 0 without both rates."""
    if not payout_rate or not po_rate or not payout_usd:
        return 0.0
    rupees = payout_usd * payout_rate
    return round(rupees / po_rate - payout_usd, 2)


async def summary(deal_id: str, payouts: list[Any] | None = None) -> dict[str, Any]:
    """Rates on file and the gain / loss per completed payout (for the deal page)."""
    r = await rates(deal_id)
    if payouts is None:
        try:
            from services.escrow_guard.service import get_deal_ledger  # type: ignore[import]
            payouts = list((await get_deal_ledger(deal_id)).payouts or [])
        except Exception:  # noqa: BLE001
            payouts = []
    lines = []
    for p in payouts:
        if getattr(p, "status", None) != "COMPLETED":
            continue
        rate = r["payout"].get(p.payout_id)
        lines.append({"payout_id": p.payout_id, "amount_usd": p.amount, "rate": rate,
                      "inr": round(p.amount * rate, 2) if rate else None,
                      "gain_loss_usd": gain_loss(p.amount, rate, r["po"])})
    missing = []
    if r["po"] is None:
        missing.append("the PO rate (kind po)")
    missing += [f"the bank rate of payout {ln['payout_id']}" for ln in lines if ln["rate"] is None]
    return {"deal_id": deal_id, "po_rate": r["po"], "receipt_rates": r["receipt"], "payouts": lines,
            "gain_loss_usd": round(sum(ln["gain_loss_usd"] for ln in lines), 2), "missing": missing}
