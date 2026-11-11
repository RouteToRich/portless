"""The deal's QC status — one value the console and the workflow read.

``DealRecord.qc_status`` (``shared.domain.enums.QcStatus``) says where the
pre-shipment inspection stands, from the PO to the report:

  AWAITING_PO_ACK      PO sent, the supplier has not acknowledged it
  AWAITING_READY_DATE  acknowledged; the supplier was asked when the goods are ready
  NOT_BOOKED           nothing holds the booking back, but no inspection is booked
  BOOKING_FAILED       the provider refused the booking / the request never reached it
  REQUESTED            booked; the provider has not confirmed yet
  SCHEDULED            the provider confirmed the date / inspector
  INSPECTED            the inspection happened; report not out yet
  OVERDUE              inspection day + 1 day + QC_REPORT_GRACE_HOURS passed without a report
  RESULT_PENDING       the report leaves the decision to the client — a person decides
  REINSPECTION         a re-inspection is booked after a failed one
  PASSED               report PASS
  FAILED               report FAIL (deal QC_FAILED → DISPUTE)

``compute()`` derives it from its sources only — the deal status, the supplier
commitment (PO acknowledgement, goods-ready date) and the deal's QC orders —
so it can always be recomputed and never drifts. ``sync()`` writes it onto the
deal with an atomic field update (never a full save, so a concurrent status
change is never overwritten). Called wherever one of those sources changes:
deal transitions, QC booking, every provider update, the PO acknowledgement /
goods-ready date, and the deal watcher's sweep (for OVERDUE).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.domain.enums import QcStatus

logger = logging.getLogger(__name__)

LABELS: dict[QcStatus, str] = {
    QcStatus.AWAITING_PO_ACK: "Awaiting PO acknowledgement",
    QcStatus.AWAITING_READY_DATE: "Awaiting goods-ready date",
    QcStatus.NOT_BOOKED: "Not booked",
    QcStatus.BOOKING_FAILED: "Booking failed",
    QcStatus.REQUESTED: "Booking requested",
    QcStatus.SCHEDULED: "Scheduled",
    QcStatus.INSPECTED: "Inspected — report due",
    QcStatus.OVERDUE: "Report overdue",
    QcStatus.RESULT_PENDING: "Decision needed",
    QcStatus.REINSPECTION: "Re-inspection booked",
    QcStatus.PASSED: "Passed",
    QcStatus.FAILED: "Failed",
}

_AFTER_QC = {"QC_PASSED", "IN_TRANSIT", "CLEARED", "PAID"}
_ENDED = {"REFUNDED", "CANCELLED"}


def _val(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _who(o: Any) -> str:
    return str(o.provider or "").replace("_", " ").upper()


def compute(deal_status: Any, commitment: Any, orders: list[Any],
            now: datetime | None = None) -> tuple[QcStatus | None, str | None]:
    """(status, one-line detail) from the deal status, the supplier commitment
    and the deal's QC orders (oldest first). None before the PO."""
    from shared.orchestration import qc_schedule
    status = _val(deal_status)
    now = now or datetime.now(timezone.utc)
    qc = sorted([o for o in orders if _val(getattr(o, "order_type", "QC")) == "QC"
                 and (getattr(o, "inspection_type", None) or "PSI") == "PSI"],
                key=lambda o: _aware(o.created_at) or now)
    live = [o for o in qc if o.status != "CANCELLED"]
    latest = live[-1] if live else None
    failed = [o for o in qc if o.qc_result == "FAIL"]
    passed = [o for o in qc if o.qc_result == "PASS"]

    if status in ("QC_FAILED", "DISPUTE"):
        o = failed[-1] if failed else latest
        attempts = f" (inspection {len(failed)})" if len(failed) > 1 else ""
        return QcStatus.FAILED, (f"{_who(o)} report FAIL{attempts} — re-inspect, refund or cancel" if o
                                 else "Inspection failed — re-inspect, refund or cancel")
    if status in _AFTER_QC:
        o = passed[-1] if passed else latest
        return QcStatus.PASSED, (f"{_who(o)} report PASS" + (f" · {o.provider_order_number}" if o and o.provider_order_number else "")
                                 if o else "Inspection passed")
    if status in _ENDED:
        if failed:
            return QcStatus.FAILED, f"{_who(failed[-1])} report FAIL — deal {status.lower()}"
        if passed:
            return QcStatus.PASSED, f"{_who(passed[-1])} report PASS"
        return None, None
    if status not in ("PO_ISSUED", "SUPPLIER_DISPATCH_READY"):
        return None, None

    if latest is None or latest.status == "FAILED" and not latest.qc_result:
        if latest is not None:
            return QcStatus.BOOKING_FAILED, f"{_who(latest)} booking was not placed — fix the cause and book again"
        if commitment is not None and getattr(commitment, "po_ack", None) == "awaiting":
            return QcStatus.AWAITING_PO_ACK, "Waiting for the supplier to acknowledge the PO and name its goods-ready date"
        if commitment is not None and getattr(commitment, "qc_ready_status", None) == "awaiting":
            asked = _aware(getattr(commitment, "qc_ready_asked_at", None))
            return QcStatus.AWAITING_READY_DATE, ("Supplier asked when the goods are ready"
                                                  + (f" on {asked.date().isoformat()}" if asked else ""))
        return QcStatus.NOT_BOOKED, "No inspection booked yet"

    who = _who(latest)
    ref = f" · {latest.provider_order_number}" if latest.provider_order_number else ""
    when = latest.inspection_date
    if latest.qc_result == "PENDING":
        return QcStatus.RESULT_PENDING, f"{who} report needs a PASS / FAIL decision{ref}"
    if latest.qc_result in ("PASS", "FAIL"):  # deal not moved yet
        return (QcStatus.PASSED if latest.qc_result == "PASS" else QcStatus.FAILED), f"{who} report {latest.qc_result}{ref}"
    day = qc_schedule.to_date(when)
    if day is not None:
        due = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
            hours=24 + qc_schedule.report_grace_hours())
        if now >= due:
            return QcStatus.OVERDUE, f"{who} inspection on {when} — no report yet{ref}"
    if latest.provider_status == "INSPECTED":
        return QcStatus.INSPECTED, f"{who} inspected on {when} — report due{ref}"
    reinspect = bool(failed)
    if latest.provider_status == "CONFIRMED":
        return ((QcStatus.REINSPECTION if reinspect else QcStatus.SCHEDULED),
                f"{who} {'re-inspection' if reinspect else 'inspection'} confirmed for {when}{ref}")
    return ((QcStatus.REINSPECTION if reinspect else QcStatus.REQUESTED),
            f"{who} {'re-inspection' if reinspect else 'inspection'} requested for {when}{ref}")


def phase_lines(plan: list[str], orders: list[Any]) -> list[dict[str, Any]]:
    """One line per planned inspection phase (DUPRO / PSI / PEO) for the console."""
    from shared.orchestration import inspection_plan
    out = []
    for phase in plan:
        mine = [o for o in orders if _val(getattr(o, "order_type", "")) == "QC"
                and (getattr(o, "inspection_type", None) or "PSI") == phase and o.status != "CANCELLED"]
        o = mine[-1] if mine else None
        if o is None:
            state = "PLANNED"
        elif o.qc_result in ("PASS", "FAIL"):
            state = "PASSED" if o.qc_result == "PASS" else "FAILED"
        elif o.qc_result == "PENDING":
            state = "DECISION_NEEDED"
        elif o.status == "FAILED":
            state = "BOOKING_FAILED"
        else:
            state = {"CONFIRMED": "SCHEDULED", "INSPECTED": "INSPECTED"}.get(o.provider_status or "", "BOOKED")
        out.append({
            "type": phase, "label": inspection_plan.label(phase), "status": state,
            "provider": o.provider if o else None, "inspection_date": o.inspection_date if o else None,
            "result": o.qc_result if o else None, "order_number": (o.provider_order_number or o.ext_ref) if o else None,
            "tpo_id": o.tpo_id if o else None,
        })
    return out


async def sync(deal_id: str | None) -> QcStatus | None:
    """Recompute the deal's QC status and store it when it changed. Never raises."""
    if not deal_id:
        return None
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        from services.shield_link.repository import get_repository as sl_repo  # type: ignore[import]
        from shared.orchestration import supplier_commitment
        deal = await deals_repo().get(deal_id)
        if deal is None:
            return None
        orders = await sl_repo().list_by_deal(deal_id)
        commitment = await supplier_commitment.get(deal_id)
        status, detail = compute(deal.status, commitment, orders)
        from shared.orchestration import inspection_plan
        phases = phase_lines(inspection_plan.normalise(getattr(deal, "inspection_types", None)
                                                       or inspection_plan.default_types()), orders)
        fields: dict[str, Any] = {}
        if status != deal.qc_status or detail != deal.qc_status_detail:
            fields.update({"qc_status": status, "qc_status_detail": detail, "qc_status_at": datetime.now(timezone.utc)})
        if phases != (getattr(deal, "inspections", None) or []):
            fields["inspections"] = phases
        if fields:
            await deals_repo().set_fields(deal_id, fields)
            logger.info("QcStatus: deal %s → %s (%s)", deal_id, getattr(status, "value", status), detail)
        return status
    except Exception as exc:  # noqa: BLE001 — a status line never breaks the workflow
        logger.warning("QcStatus: sync failed for deal %s: %s", deal_id, exc)
        return None
