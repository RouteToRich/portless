"""Export proceeds realised per Shipping Bill (DEF-17; FEMA / RBI: the full export value must
be realised within the time limit and reported against the Shipping Bill in EDPMS).

Per deal and per Shipping Bill (ICEGATE filing or a manual CHA record with its date):

* invoice value — the PI total (the escrow principal);
* realised — what the escrow has released to PortLess so far (gross of provider fees: the
  buyer paid it; the fee is a cost);
* deadline — Shipping Bill date + ``REALISATION_DAYS`` (270, the RBI's nine months);
* status — ``realised`` · ``open`` · ``due_soon`` (within ``REALISATION_WARN_DAYS``, 30) ·
  ``overdue``. A Shipping Bill without a date counts from the filing record.

``GET /deals/{id}/realisation``, ``GET /deals/realisation/open`` (and the digest) list what is
not realised yet, most urgent first — the AD bank's EDPMS follows the same Shipping Bills.
Never raises.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)
_TOL = 0.01


def _days(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _sb_date(order: Any) -> date | None:
    payload = getattr(order, "provider_payload", None) or {}
    for key in ("shipping_bill_date", "let_export_order_date"):
        try:
            if payload.get(key):
                return date.fromisoformat(str(payload[key])[:10])
        except ValueError:
            continue
    created = getattr(order, "created_at", None)
    return created.date() if isinstance(created, datetime) else None


def assess(invoice: float, realised: float, sb_date: date | None, today: date) -> dict[str, Any]:
    """Pure: the realisation status of one Shipping Bill."""
    outstanding = round(max(0.0, invoice - realised), 2)
    deadline = sb_date + timedelta(days=_days("REALISATION_DAYS", 270)) if sb_date else None
    if outstanding <= _TOL:
        status = "realised"
    elif deadline is None:
        status = "open"
    elif today > deadline:
        status = "overdue"
    elif (deadline - today).days <= _days("REALISATION_WARN_DAYS", 30):
        status = "due_soon"
    else:
        status = "open"
    return {"invoice_value": round(invoice, 2), "realised": round(realised, 2), "outstanding": outstanding,
            "deadline": deadline.isoformat() if deadline else None,
            "days_left": (deadline - today).days if deadline else None, "status": status}


async def for_deal(deal_id: str, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date()
    out: dict[str, Any] = {"deal_id": deal_id, "shipping_bills": []}
    try:
        from services.shield_link.service import list_deal_orders  # type: ignore[import]
        orders = [o for o in (await list_deal_orders(deal_id)).orders
                  if str(o.order_type).upper() == "CHA" and o.status == "COMPLETED"]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Realisation: orders of %s unavailable: %s", deal_id, exc)
        orders = []
    invoice, realised, currency = 0.0, 0.0, "USD"
    try:
        from services.escrow_guard.repository import get_repository as eg_repo  # type: ignore[import]
        hold = await eg_repo().get_hold_by_deal(deal_id)
        if hold is not None:
            invoice, realised, currency = float(hold.amount), float(hold.released_total or 0.0), hold.currency
    except Exception as exc:  # noqa: BLE001
        logger.debug("Realisation: escrow of %s unavailable: %s", deal_id, exc)
    for o in orders:
        payload = getattr(o, "provider_payload", None) or {}
        if payload.get("stub"):
            continue  # an ICEGATE stub filing is not a real Shipping Bill
        sb_day = _sb_date(o)
        row = {"shipping_bill_number": o.ext_ref or payload.get("shipping_bill_number"), "currency": currency,
               "shipping_bill_date": sb_day.isoformat() if sb_day else None, "filed_by": o.provider,
               **assess(invoice, realised, sb_day, today)}
        out["shipping_bills"].append(row)
    return out


async def open_items(today: date | None = None) -> list[dict[str, Any]]:
    """Every Shipping Bill not fully realised, most urgent first."""
    rows: list[dict[str, Any]] = []
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=2000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Realisation: deals unavailable: %s", exc)
        return rows
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status not in ("IN_TRANSIT", "CLEARED", "PAID", "DISPUTE", "REFUNDED"):
            continue
        for sb in (await for_deal(d.deal_id, today))["shipping_bills"]:
            if sb["status"] != "realised":
                rows.append({"deal_id": d.deal_id, **sb})
    order = {"overdue": 0, "due_soon": 1, "open": 2}
    return sorted(rows, key=lambda r: (order.get(r["status"], 3), r["days_left"] if r["days_left"] is not None else 9999))
