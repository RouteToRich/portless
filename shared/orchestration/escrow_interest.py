"""Interest / float on buyer money held in escrow (todo: record it if the provider pays any).

Neither Tazapay nor Escrow.com promises interest today; if a provider statement or the bank
credits any to PortLess, staff record it per deal (``POST /escrow-guard/deals/{id}/interest``:
amount, period, statement reference, who it belongs to — audited). ``belongs_to``:

* ``portless`` — income: it raises the deal's realised margin and posts
  Dr portless_cash / Cr interest_income in the journal;
* ``buyer`` — owed back to the buyer (the PI says so or the provider pays it for the buyer):
  posts Dr portless_cash / Cr buyer_deposits and never touches the margin.

Mongo ``escrow_interest`` (in memory without Mongo). Never raises on read.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_memory: list[dict[str, Any]] = []
BELONGS_TO = ("portless", "buyer")


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("escrow_interest")
    except Exception:  # noqa: BLE001
        return None


async def record(deal_id: str, amount_usd: float, *, period: str | None, ref: str | None,
                 belongs_to: str, by: str) -> dict[str, Any]:
    if belongs_to not in BELONGS_TO:
        raise ValueError(f"belongs_to must be one of {', '.join(BELONGS_TO)}")
    try:
        amount = round(float(amount_usd), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("amount must be a number") from exc
    if amount <= 0:
        raise ValueError("amount must be above 0")
    if not (ref or "").strip():
        raise ValueError("the statement reference is required")
    row = {"interest_id": f"INT-{uuid4().hex[:10].upper()}", "deal_id": deal_id, "amount_usd": amount,
           "period": period, "ref": ref.strip(), "belongs_to": belongs_to, "by": by,
           "at": datetime.now(timezone.utc).isoformat()}
    col = _collection()
    if col is None:
        _memory.append(row)
    else:
        await col.insert_one({"_id": row["interest_id"], **row})
    try:
        from shared import audit_log
        await audit_log.record("escrow_interest", by, deal_id, {"amount_usd": amount, "belongs_to": belongs_to, "ref": row["ref"]})
    except Exception:  # noqa: BLE001
        pass
    return row


async def listed(deal_id: str) -> list[dict[str, Any]]:
    col = _collection()
    if col is None:
        return [r for r in _memory if r["deal_id"] == deal_id]
    out = []
    try:
        async for d in col.find({"deal_id": deal_id}):
            d.pop("_id", None)
            out.append(d)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EscrowInterest: %s unreadable: %s", deal_id, exc)
    return out


async def income(deal_id: str) -> float:
    """Interest that is PortLess's own (raises the realised margin)."""
    return round(sum(r["amount_usd"] for r in await listed(deal_id) if r["belongs_to"] == "portless"), 2)


def reset_for_tests() -> None:
    _memory.clear()
