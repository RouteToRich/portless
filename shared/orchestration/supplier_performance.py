"""Supplier performance from real deals (todo: supplier performance; supplier track record to rank
manual and scanned suppliers together).

Per supplier (joined by the same keys as the directory — website / e-mail domain, else name —
``services.trade_min.relationships.keys_for``), over every deal it was the supplier party of:

* ``deals`` / ``completed`` (PAID) / ``cancelled`` (CANCELLED / REFUNDED after the PO);
* ``qc_inspections`` / ``qc_passed`` / ``qc_failed`` / ``qc_pass_rate`` — the pre-shipment
  inspection results of its QC orders (re-inspections count);
* ``on_time_ready`` / ``late_ready`` — the goods-ready date it promised (supplier commitment) vs
  the date the inspector found the goods ready (QC ``inspection_date``), beyond
  ``SUPPLIER_LATE_GRACE_DAYS`` (2);
* ``disputes`` — deals that went to DISPUTE;
* ``price_kept`` / ``requoted_up`` — its confirmed price vs the price it first quoted.

The directory score adds it (``relationships.score_of``), the company profile shows it, and
``GET /trade-min/suppliers/{id}/performance`` returns it. Cached as a computed view
(``shared.view_cache``, recomputed after any deal write). Never raises.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _grace() -> float:
    try:
        return float(os.getenv("SUPPLIER_LATE_GRACE_DAYS", "2"))
    except ValueError:
        return 2.0


def _day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _status(deal: Any) -> str:
    return str(getattr(deal.status, "value", deal.status))


def _supplier_party(deal: Any) -> Any:
    for p in getattr(deal, "parties", None) or []:
        role = str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower()
        if role == "supplier":
            return p
    return None


def _first_quote(deal: Any, party: Any) -> float | None:
    from services.trade_min.relationships import keys_for  # type: ignore[import]
    mine = keys_for(getattr(party, "website", None), getattr(party, "email", None), name=getattr(party, "legal_name", None))
    quotes = []
    for o in getattr(deal, "supplier_offers", None) or []:
        keys = keys_for(getattr(o, "email", None), name=getattr(o, "company_name", None))
        if keys & mine and getattr(o, "fob_price_usd_per_mt", None) and getattr(o, "price_basis", "quoted") == "quoted":
            quotes.append((getattr(o, "received_at", None) or datetime.min, float(o.fob_price_usd_per_mt)))
    return min(quotes, key=lambda q: str(q[0]))[1] if quotes else None


def deal_facts(deal: Any, qc_orders: list[Any], commitment: Any) -> dict[str, Any]:
    """Pure: what one deal says about its supplier."""
    st = _status(deal)
    history = {str(getattr(h.status, "value", h.status)) for h in (getattr(deal, "status_history", None) or [])}
    psi = [o for o in qc_orders if getattr(o, "order_type", "QC") == "QC"
           and (getattr(o, "inspection_type", "PSI") or "PSI") == "PSI" and getattr(o, "qc_result", None) in ("PASS", "FAIL")]
    facts: dict[str, Any] = {
        "deal_id": deal.deal_id, "status": st,
        "qc_passed": sum(1 for o in psi if o.qc_result == "PASS"),
        "qc_failed": sum(1 for o in psi if o.qc_result == "FAIL"),
        "dispute": "DISPUTE" in history or st == "DISPUTE",
        "completed": st == "PAID",
        "cancelled": st in ("CANCELLED", "REFUNDED") and bool(history & {"PO_ISSUED", "SUPPLIER_DISPATCH_READY"}),
        "ready": None, "price": None,
    }
    promised = _day(getattr(commitment, "qc_ready_date", None)) if commitment else None
    inspected = [d for d in (_day(getattr(o, "inspection_date", None)) for o in psi) if d]
    if promised and inspected:
        late_days = (min(inspected) - promised).days
        facts["ready"] = "late" if late_days > _grace() else "on_time"
        facts["late_days"] = late_days
    confirmed = getattr(commitment, "confirmed_price", None) if commitment else None
    party = _supplier_party(deal)
    quoted = _first_quote(deal, party) if party is not None else None
    if confirmed and quoted:
        facts["price"] = "requoted_up" if confirmed > quoted * 1.005 else "kept"
        facts["price_change_pct"] = round((confirmed - quoted) / quoted * 100.0, 1)
    return facts


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(r["qc_passed"] for r in rows)
    failed = sum(r["qc_failed"] for r in rows)
    out = {
        "deals": len(rows), "completed": sum(1 for r in rows if r["completed"]),
        "cancelled": sum(1 for r in rows if r["cancelled"]),
        "qc_inspections": passed + failed, "qc_passed": passed, "qc_failed": failed,
        "qc_pass_rate": round(passed / (passed + failed), 2) if passed + failed else None,
        "on_time_ready": sum(1 for r in rows if r["ready"] == "on_time"),
        "late_ready": sum(1 for r in rows if r["ready"] == "late"),
        "disputes": sum(1 for r in rows if r["dispute"]),
        "price_kept": sum(1 for r in rows if r["price"] == "kept"),
        "requoted_up": sum(1 for r in rows if r["price"] == "requoted_up"),
        "deal_ids": [r["deal_id"] for r in rows],
    }
    return out


async def compute() -> dict[str, dict[str, Any]]:
    """Every supplier key → its performance summary (one shared dict per company)."""
    from services.trade_min.relationships import keys_for  # type: ignore[import]
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        deals = await deals_repo().list_active(limit=5000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("SupplierPerformance: deals unavailable: %s", exc)
        return {}
    by_company: dict[frozenset[str], list[dict[str, Any]]] = {}
    for deal in deals:
        party = _supplier_party(deal)
        if party is None:
            continue
        keys = keys_for(getattr(party, "website", None), getattr(party, "email", None), name=getattr(party, "legal_name", None))
        if not keys:
            continue
        orders, commitment = [], None
        try:
            from services.shield_link.repository import get_repository as sl_repo  # type: ignore[import]
            orders = [o for o in await sl_repo().list_by_deal(deal.deal_id) if o.order_type == "QC"]
        except Exception:  # noqa: BLE001
            pass
        try:
            from shared.orchestration import supplier_commitment
            commitment = await supplier_commitment.get(deal.deal_id)
        except Exception:  # noqa: BLE001
            pass
        facts = deal_facts(deal, orders, commitment)
        group = next((g for g in by_company if g & keys), None)
        if group is None:
            by_company[frozenset(keys)] = [facts]
        else:
            rows = by_company.pop(group)
            by_company[group | keys] = rows + [facts]
    out: dict[str, dict[str, Any]] = {}
    for keys, rows in by_company.items():
        summary = summarise(rows)
        for k in keys:
            out[k] = summary
    return out


async def cached() -> dict[str, dict[str, Any]]:
    from shared import view_cache
    return await view_cache.get_or_compute("supplier_performance", compute)


async def for_record(record: Any) -> dict[str, Any] | None:
    from services.trade_min.relationships import record_keys  # type: ignore[import]
    perf = await cached()
    return next((perf[k] for k in record_keys(record) if k in perf), None)
