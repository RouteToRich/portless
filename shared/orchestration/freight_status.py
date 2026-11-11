"""The deal's freight status — one line for the Deal Board, from the freight orders.

  NOT_BOOKED        QC passed, no booking yet           BOOKING_FAILED     the request never got through
  BOOKING_REQUESTED sent, carrier not confirmed         BOOKING_REJECTED   rejected / cancelled by the carrier
  BOOKING_CONFIRMED space + containers confirmed        DOCS_PENDING       VGM or the B/L draft waits for PortLess
  BL_ISSUED         bill of lading issued               LOADED             gated in / loaded on board
  DEPARTED · ARRIVED · DISCHARGED · DELIVERED          the voyage, from the carrier's events

``compute()`` derives it from the orders alone; ``sync()`` stores it with an
atomic field update. Called by shield-link on every booking / tracking change.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from shared.domain.enums import FreightStatus

logger = logging.getLogger(__name__)

LABELS: dict[FreightStatus, str] = {
    FreightStatus.NOT_BOOKED: "Not booked",
    FreightStatus.BOOKING_FAILED: "Booking failed",
    FreightStatus.BOOKING_REQUESTED: "Booking requested",
    FreightStatus.BOOKING_REJECTED: "Booking rejected",
    FreightStatus.BOOKING_CONFIRMED: "Booking confirmed",
    FreightStatus.DOCS_PENDING: "VGM / B/L pending",
    FreightStatus.BL_ISSUED: "B/L issued",
    FreightStatus.LOADED: "Loaded",
    FreightStatus.DEPARTED: "Sailed",
    FreightStatus.ARRIVED: "Arrived",
    FreightStatus.DISCHARGED: "Discharged",
    FreightStatus.DELIVERED: "Delivered",
}

_AFTER_QC = {"QC_PASSED", "IN_TRANSIT", "CLEARED", "PAID"}
_EVENT_STATUS = {"GATE_IN": FreightStatus.LOADED, "LOADED": FreightStatus.LOADED, "DEPARTED": FreightStatus.DEPARTED,
                 "ARRIVED": FreightStatus.ARRIVED, "DISCHARGED": FreightStatus.DISCHARGED,
                 "DELIVERED": FreightStatus.DELIVERED}
_RANK = list(FreightStatus)


def _val(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


def compute(deal_status: Any, orders: list[Any]) -> tuple[FreightStatus | None, str | None]:
    status = _val(deal_status)
    freight = [o for o in orders if _val(getattr(o, "order_type", "")) == "FREIGHT"]
    if not freight:
        return (FreightStatus.NOT_BOOKED, "QC passed — waiting for the freight booking") if status == "QC_PASSED" else (None, None)
    live = [o for o in freight if o.status not in ("FAILED", "CANCELLED")]
    o = live[-1] if live else freight[-1]
    who = str(o.provider or "carrier").title()
    ref = f" {o.ext_ref}" if o.ext_ref else ""
    if not live:
        if o.booking_status in ("REJECTED", "CANCELLED"):
            return FreightStatus.BOOKING_REJECTED, f"{who}{ref} {o.booking_status.lower()} — rebook"
        return FreightStatus.BOOKING_FAILED, f"{who} booking did not go through — fix the cause and book again"
    voyage = " ".join(x for x in (o.vessel, o.voyage) if x)
    boxes = ", ".join(c.get("equipment_reference") for c in (o.containers or []) if c.get("equipment_reference")) or o.container_number
    tail = " · ".join(x for x in (voyage, f"ETA {o.eta}" if o.eta else "", boxes or "") if x)
    detail = f"{who}{ref}" + (f" · {tail}" if tail else "")

    from_events = [_EVENT_STATUS[e["milestone"]] for e in (o.tracking_events or []) if e.get("milestone") in _EVENT_STATUS]
    best = max(from_events, key=_RANK.index) if from_events else None
    if o.last_milestone in ("ARRIVED", "CLEARED") and (best is None or _RANK.index(best) < _RANK.index(FreightStatus.ARRIVED)):
        best = FreightStatus.ARRIVED
    if o.last_milestone == "DELIVERED" or o.delivered_at:
        best = FreightStatus.DELIVERED
    if o.last_milestone == "DEPARTED" and best is None:
        best = FreightStatus.DEPARTED
    if best is not None and _RANK.index(best) >= _RANK.index(FreightStatus.DEPARTED):
        return best, detail
    if o.booking_status in ("REJECTED", "CANCELLED"):
        return FreightStatus.BOOKING_REJECTED, f"{who}{ref} {o.booking_status.lower()} — rebook"
    if o.ebl_status in ("ISSUED", "SURRENDERED"):
        return (best or FreightStatus.BL_ISSUED), f"{detail} · B/L {o.transport_document_reference} issued"
    pending = []
    if o.vgm_status == "GATED":
        pending.append("VGM awaiting approval")
    if o.vgm_status == "FAILED":
        pending.append("VGM refused")
    if o.ebl_status == "DRAFT":
        pending.append("B/L draft awaiting approval")
    if pending:
        return FreightStatus.DOCS_PENDING, f"{detail} · {'; '.join(pending)}"
    if best is not None:
        return best, detail
    if o.booking_status == "CONFIRMED" or (o.booking_status is None and o.status in ("BOOKED", "IN_PROGRESS", "COMPLETED")):
        return FreightStatus.BOOKING_CONFIRMED, detail
    return FreightStatus.BOOKING_REQUESTED, f"{detail} · waiting for {who} to confirm"


async def sync(deal_id: str | None) -> FreightStatus | None:
    """Recompute and store the deal's freight status when it changed. Never raises."""
    if not deal_id:
        return None
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        from services.shield_link.repository import (
            get_repository as sl_repo,  # type: ignore[import]
        )
        deal = await deals_repo().get(deal_id)
        if deal is None:
            return None
        status, detail = compute(deal.status, await sl_repo().list_by_deal(deal_id))
        if status != deal.freight_status or detail != deal.freight_status_detail:
            await deals_repo().set_fields(deal_id, {
                "freight_status": status.value if status else None, "freight_status_detail": detail,
                "freight_status_at": datetime.now(UTC),
            })
        return status
    except Exception as exc:  # noqa: BLE001
        logger.warning("FreightStatus: sync failed for deal %s: %s", deal_id, exc)
        return None
