"""Carrier shipment lifecycle (Maersk): booking → containers → VGM → B/L → voyage → delivery.

``sync_shipment(tpo)`` reads the carrier and applies what changed — it is the
one path for the booking-status webhook (re-read, never trusted alone), the
deal watcher's poll (``FREIGHT_POLL_MINUTES``) and ``POST
/shield-link/deals/{id}/track``:

  booking     RECEIVED → CONFIRMED (containers, vessel, voyage, planned ETD / ETA,
              transport document reference) · REJECTED / CANCELLED → deal.exception
  VGM         containers confirmed + the inspector's loading record → a
              ``SUBMIT_VGM`` gate (verified gross mass = loaded cargo per box + the
              box's tare, method 2); APPROVE submits it to the carrier
  B/L         carrier draft (DRAFT) → an ``APPROVE_TRANSPORT_DOCUMENT`` gate; APPROVE
              approves it; ISSUED → ``freight.transport_document_issued`` (the B/L
              number is proof for the export tranche)
  tracking    DCSA events → timeline on the order; ACT DEPA → DEPARTED,
              ARRI at the port of discharge → ARRIVED (deal → CLEARED), GTOT at
              destination / DROP → DELIVERED (opens the buyer's acceptance window)

Every change refreshes the deal's freight status line (``freight_status``).
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from shared.domain.events import EventTopic

from .repository import get_repository
from .schemas import ThirdPartyOrderRecord

logger = logging.getLogger(__name__)

CARRIER_PROVIDERS = ("maersk",)
_DEFAULT_TARE_KG = {"2": 2230.0, "4": 3750.0}  # ISO size digit → typical tare (20' / 40')
_MILESTONE_TO_DEAL = {"DEPARTED": "DEPARTED", "ARRIVED": "ARRIVED", "DISCHARGED": "ARRIVED", "DELIVERED": "DELIVERED"}


def _now() -> datetime:
    return datetime.now(UTC)


def poll_minutes() -> float:
    try:
        return float(os.getenv("FREIGHT_POLL_MINUTES", "60") or 60)
    except ValueError:
        return 60.0


def is_carrier(tpo: ThirdPartyOrderRecord) -> bool:
    return tpo.order_type == "FREIGHT" and tpo.provider in CARRIER_PROVIDERS


def _connector(tpo: ThirdPartyOrderRecord):
    from shared.connectors.shields import get_freight_connector
    return get_freight_connector(tpo.provider)


def _tare(iso: str | None, tare: Any) -> float:
    if isinstance(tare, dict) and tare.get("value"):
        value = float(tare["value"])
        return value * 0.45359237 if str(tare.get("unit", "KGM")).upper() == "LBR" else value
    return _DEFAULT_TARE_KG.get(str(iso or "4")[:1], 3750.0)


def _containers(booking: dict[str, Any], events: list[dict[str, Any]], previous: list[dict[str, Any]],
                seals: dict[str, str]) -> list[dict[str, Any]]:
    by_ref: dict[str, dict[str, Any]] = {c["equipment_reference"]: dict(c) for c in previous if c.get("equipment_reference")}
    for eq in booking.get("confirmedEquipments") or booking.get("equipments") or []:
        ref = eq.get("equipmentReference")
        if not ref:
            continue
        box = by_ref.setdefault(ref, {"equipment_reference": ref})
        box["iso_code"] = eq.get("ISOEquipmentCode") or box.get("iso_code")
        box["tare_kg"] = _tare(box["iso_code"], eq.get("tareWeight"))
    for e in events:
        ref = e.get("equipment_reference")
        if not ref:
            continue
        box = by_ref.setdefault(ref, {"equipment_reference": ref, "tare_kg": _tare(None, None)})
        box.update({"last_event": e.get("code"), "last_event_at": e.get("at"),
                    "location": (e.get("location") or {}).get("name") or (e.get("location") or {}).get("unlocode")})
    for ref, seal in seals.items():
        if ref in by_ref:
            by_ref[ref]["seal"] = seal
    return list(by_ref.values())


async def _loading(deal_id: str):
    try:
        from shared.orchestration import compliance
        return (await compliance.get_record(deal_id)).loading
    except Exception:  # noqa: BLE001
        return None


async def sync_shipment(tpo: ThirdPartyOrderRecord, *, source: str = "poll") -> dict[str, Any]:
    """Read booking + events + B/L from the carrier and apply what changed. Never raises."""
    from shared.connectors.maersk import normalize_events
    from shared.connectors.payments import ConnectorError
    from shared.orchestration import freight_status, trade_brief

    from . import service as sl

    if not is_carrier(tpo) or not tpo.ext_ref:
        return {"status": "not_a_carrier_booking", "deal_id": tpo.deal_id}
    connector = _connector(tpo)
    repo = get_repository()
    try:
        booking = await connector.status(tpo.ext_ref)
        raw_events = await connector.events(booking_ref=tpo.ext_ref)
    except ConnectorError as exc:
        logger.warning("Freight: %s read failed for deal %s: %s", tpo.provider, tpo.deal_id, exc)
        await repo.save(tpo.model_copy(update={"last_polled_at": _now(), "updated_at": _now()}))
        return {"status": "error", "deal_id": tpo.deal_id, "error": str(exc)}
    if booking.get("stub"):
        return {"status": "stub", "deal_id": tpo.deal_id}

    events = normalize_events(raw_events, origin=trade_brief.unlocode(tpo.origin_port),
                              destination=trade_brief.unlocode(tpo.destination_port))
    loading = await _loading(tpo.deal_id)
    seals = dict(zip(loading.container_numbers, loading.seal_numbers, strict=False)) if loading else {}
    plan = (booking.get("transportPlan") or [{}])[0] or {}
    tdr = booking.get("transportDocumentReference") or tpo.transport_document_reference
    status = booking.get("bookingStatus") or tpo.booking_status
    document: dict[str, Any] | None = None
    if tdr and status not in ("REJECTED", "CANCELLED"):
        try:
            document = await connector.transport_document(tdr)
        except ConnectorError:
            document = None  # draft not produced yet
    ebl_status = (document or {}).get("transportDocumentStatus") or tpo.ebl_status
    containers = _containers(booking, events, tpo.containers, seals)
    vessel_event = next((e for e in reversed(events) if e.get("vessel")), {})
    update: dict[str, Any] = {
        "booking_status": status,
        "carrier": booking.get("carrierCode") or tpo.carrier or "MAEU",
        "transport_document_reference": tdr,
        "ebl_status": ebl_status,
        "ebl_issued_at": tpo.ebl_issued_at or (_now() if ebl_status == "ISSUED" else None),
        "containers": containers,
        "container_number": tpo.container_number or (containers[0]["equipment_reference"] if containers else None),
        "tracking_events": events,
        "vessel": plan.get("vesselName") or vessel_event.get("vessel") or tpo.vessel,
        "voyage": plan.get("carrierExportVoyageNumber") or vessel_event.get("voyage") or tpo.voyage,
        "etd": (plan.get("plannedDepartureDate") or "")[:10] or tpo.etd,
        "eta": (plan.get("plannedArrivalDate") or "")[:10] or tpo.eta,
        "last_polled_at": _now(),
        "updated_at": _now(),
    }
    if status in ("REJECTED", "CANCELLED") and tpo.status not in ("FAILED", "CANCELLED"):
        update["status"] = "CANCELLED" if status == "CANCELLED" else "FAILED"
    updated = tpo.model_copy(update=update)
    await repo.save(updated)

    if status != tpo.booking_status:
        await sl._emit(EventTopic.FREIGHT_BOOKING_UPDATED, tpo.deal_id, {
            "tpo_id": tpo.tpo_id, "provider": tpo.provider, "ext_ref": tpo.ext_ref, "booking_status": status,
            "previous_status": tpo.booking_status, "vessel": updated.vessel, "voyage": updated.voyage,
            "etd": updated.etd, "eta": updated.eta, "containers": [c["equipment_reference"] for c in containers],
            "source": source,
        })
        if status in ("REJECTED", "CANCELLED"):
            await sl._emit(EventTopic.DEAL_EXCEPTION, tpo.deal_id, {
                "stage": "FREIGHT_BOOKING", "category": "FREIGHT_DELAY",
                "error": f"{tpo.provider.title()} {status.lower()} booking {tpo.ext_ref} — rebook or choose another sailing",
                "action_required": "rebook_freight", "tpo_id": tpo.tpo_id,
            })
    if ebl_status == "ISSUED" and tpo.ebl_status != "ISSUED":
        await sl._emit(EventTopic.TRANSPORT_DOCUMENT_ISSUED, tpo.deal_id, {
            "tpo_id": tpo.tpo_id, "provider": tpo.provider, "transport_document_reference": tdr,
            "bl_number": tdr, "booking_ref": tpo.ext_ref, "vessel": updated.vessel, "voyage": updated.voyage,
            "containers": [c["equipment_reference"] for c in containers],
            "seal_numbers": [c.get("seal") for c in containers if c.get("seal")],
        })

    gates = []
    if status == "CONFIRMED" and tdr and not updated.vgm_status:
        gates.append(await _gate_vgm(updated, loading))
    if ebl_status == "DRAFT":
        gates.append(await _gate_bl(updated))

    applied = None
    milestones = [e["milestone"] for e in events if e.get("milestone") in _MILESTONE_TO_DEAL]
    if milestones:
        latest = _MILESTONE_TO_DEAL[milestones[-1]]
        last = next(e for e in reversed(events) if e.get("milestone") and _MILESTONE_TO_DEAL.get(e["milestone"]) == latest)
        order = sl._MILESTONE_ORDER
        if latest in order and (tpo.last_milestone not in order or order.index(latest) > order.index(tpo.last_milestone)):
            fresh = await repo.get(tpo.tpo_id) or updated
            applied = await sl._apply_milestone(
                fresh, latest, location=(last.get("location") or {}).get("name"), timestamp=last.get("at"),
                notes=f"{tpo.provider.title()} {last.get('code')} event", source="tracking" if source == "poll" else source,
                container_number=last.get("equipment_reference"),
            )
    await freight_status.sync(tpo.deal_id)
    return {"status": "ok", "deal_id": tpo.deal_id, "booking_status": status, "ebl_status": ebl_status,
            "events": len(events), "milestone": (applied or {}).get("milestone"), "gates": [g for g in gates if g]}


def vgm_lines(tpo: ThirdPartyOrderRecord, loading: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Per-container verified gross mass (method 2: cargo + tare) and any warnings."""
    warnings: list[str] = []
    boxes = [c for c in tpo.containers if c.get("equipment_reference")]
    if not boxes:
        return [], ["the carrier has not confirmed any container yet"]
    loaded_mt = float(getattr(loading, "loaded_quantity_mt", 0) or 0) if loading else 0.0
    if loaded_mt <= 0:
        return [], ["no loaded quantity on record — the inspector's supervised loading record is needed"]
    inspector = set(getattr(loading, "container_numbers", []) or [])
    carrier = {c["equipment_reference"] for c in boxes}
    if inspector and inspector != carrier:
        warnings.append(f"inspector recorded {', '.join(sorted(inspector))}; carrier holds {', '.join(sorted(carrier))}")
    per_box_kg = loaded_mt * 1000 / len(boxes)
    return [{
        "equipment_reference": c["equipment_reference"],
        "cargo_kg": round(per_box_kg, 1),
        "tare_kg": round(float(c.get("tare_kg") or _tare(c.get("iso_code"), None)), 1),
        "verified_gross_mass_kg": round(per_box_kg + float(c.get("tare_kg") or _tare(c.get("iso_code"), None)), 1),
        "method": "METHOD2",
    } for c in boxes], warnings


def responsible_party() -> dict[str, str]:
    from shared import company_profile
    return {
        "party_name": company_profile.legal_name(),
        "signatory": os.getenv("PORTLESS_SENDER_NAME", "").strip() or os.getenv("PORTLESS_DESK_NAME", "Trade Desk"),
        "contact_email": os.getenv("PORTLESS_CONTACT_EMAIL", "").strip(),
    }


async def _gate_vgm(tpo: ThirdPartyOrderRecord, loading: Any) -> str | None:
    lines, warnings = vgm_lines(tpo, loading)
    if not lines:
        logger.info("Freight: VGM for deal %s not ready — %s", tpo.deal_id, "; ".join(warnings))
        return None
    from shared.orchestration.continuations import _create_gate
    total = sum(x["verified_gross_mass_kg"] for x in lines)
    await _create_gate(
        tpo.deal_id, "SUBMIT_VGM",
        summary=(f"Submit the verified gross mass to {tpo.provider.title()} for B/L {tpo.transport_document_reference}: "
                 + "; ".join(f"{x['equipment_reference']} {x['verified_gross_mass_kg']:,.0f} kg "
                             f"(cargo {x['cargo_kg']:,.0f} + tare {x['tare_kg']:,.0f})" for x in lines)
                 + (f" — check: {'; '.join(warnings)}" if warnings else "")),
        financial_impact_usd=None,
        callback_payload={"deal_id": tpo.deal_id, "tpo_id": tpo.tpo_id, "containers": lines,
                          "total_kg": round(total, 1), "warnings": warnings},
        idempotency_key=f"auto-vgm-gate-{tpo.deal_id}-{tpo.transport_document_reference}",
        risk_flags=["solas_declaration"] + (["container_numbers_differ"] if warnings else []),
    )
    await get_repository().save(tpo.model_copy(update={"vgm_status": "GATED", "updated_at": _now()}))
    return "SUBMIT_VGM"


async def _gate_bl(tpo: ThirdPartyOrderRecord) -> str | None:
    from shared.orchestration.continuations import _create_gate
    await _create_gate(
        tpo.deal_id, "APPROVE_TRANSPORT_DOCUMENT",
        summary=(f"{tpo.provider.title()} draft bill of lading {tpo.transport_document_reference} is ready "
                 f"({tpo.vessel or 'vessel tbc'} {tpo.voyage or ''}, {len(tpo.containers)} container(s)) — "
                 "check shipper, consignee, cargo and seals, then approve so the carrier issues it"),
        financial_impact_usd=None,
        callback_payload={"deal_id": tpo.deal_id, "tpo_id": tpo.tpo_id,
                          "transport_document_reference": tpo.transport_document_reference},
        idempotency_key=f"auto-bl-gate-{tpo.deal_id}-{tpo.transport_document_reference}",
        risk_flags=["legal_document"],
    )
    return "APPROVE_TRANSPORT_DOCUMENT"


async def submit_vgm(tpo_id: str, containers: list[dict[str, Any]]) -> dict[str, Any]:
    """SUBMIT_VGM approved → send the declaration to the carrier, then re-read."""
    from shared.connectors.payments import ConnectorError

    from . import service as sl

    repo = get_repository()
    tpo = await repo.get(tpo_id)
    if tpo is None or not tpo.transport_document_reference:
        raise sl.TpoNotFoundError(tpo_id)
    await sl._assert_platform_running(f"VGM submission for deal {tpo.deal_id}")
    try:
        result = await _connector(tpo).submit_vgm(tpo.transport_document_reference, containers, responsible_party())
    except ConnectorError as exc:
        await repo.save(tpo.model_copy(update={"vgm_status": "FAILED", "updated_at": _now()}))
        raise sl.ConnectorCallError(f"VGM submission failed for deal {tpo.deal_id}: {exc}") from exc
    await repo.save(tpo.model_copy(update={
        "vgm_status": "SUBMITTED", "vgm_submitted_at": _now(), "updated_at": _now(),
        "provider_payload": {**tpo.provider_payload, "vgm": result},
    }))
    return await sync_shipment(await repo.get(tpo_id) or tpo, source="vgm")


async def approve_transport_document(tpo_id: str) -> dict[str, Any]:
    """APPROVE_TRANSPORT_DOCUMENT approved → approve the carrier's draft, then re-read."""
    from shared.connectors.payments import ConnectorError

    from . import service as sl

    repo = get_repository()
    tpo = await repo.get(tpo_id)
    if tpo is None or not tpo.transport_document_reference:
        raise sl.TpoNotFoundError(tpo_id)
    await sl._assert_platform_running(f"B/L approval for deal {tpo.deal_id}")
    try:
        await _connector(tpo).approve_transport_document(tpo.transport_document_reference)
    except ConnectorError as exc:
        raise sl.ConnectorCallError(f"B/L approval failed for deal {tpo.deal_id}: {exc}") from exc
    await repo.save(tpo.model_copy(update={"ebl_status": "APPROVED", "updated_at": _now()}))
    return await sync_shipment(await repo.get(tpo_id) or tpo, source="bl_approval")


async def sync_deal(deal_id: str, *, force: bool = True) -> dict[str, Any]:
    """Re-read the deal's carrier booking (``force`` ignores FREIGHT_POLL_MINUTES)."""
    orders = await get_repository().list_by_deal(deal_id)
    tpo = next((o for o in reversed(orders) if is_carrier(o) and o.status not in ("FAILED", "CANCELLED")), None)
    if tpo is None:
        return {"status": "no_booking", "deal_id": deal_id}
    if not force and tpo.last_polled_at is not None:
        last = tpo.last_polled_at if tpo.last_polled_at.tzinfo else tpo.last_polled_at.replace(tzinfo=UTC)
        if (_now() - last).total_seconds() < poll_minutes() * 60:
            return {"status": "not_due", "deal_id": deal_id}
    return await sync_shipment(tpo)


async def shipment_view(deal_id: str) -> dict[str, Any]:
    """Everything the console's shipment card shows (``GET /shield-link/deals/{id}/shipment``)."""
    from shared.orchestration import freight_status

    orders = await get_repository().list_by_deal(deal_id)
    freight = [o for o in orders if o.order_type == "FREIGHT"]
    tpo = next((o for o in reversed(freight) if o.status not in ("FAILED", "CANCELLED")), freight[-1] if freight else None)
    status, detail = freight_status.compute(None, freight)
    if tpo is None:
        return {"deal_id": deal_id, "booking": None, "freight_status": status, "freight_status_detail": detail}
    return {
        "deal_id": deal_id,
        "freight_status": status, "freight_status_detail": detail,
        "booking": {
            "tpo_id": tpo.tpo_id, "provider": tpo.provider, "carrier": tpo.carrier, "booking_reference": tpo.ext_ref,
            "booking_status": tpo.booking_status, "status": tpo.status, "origin_port": tpo.origin_port,
            "destination_port": tpo.destination_port, "vessel": tpo.vessel, "voyage": tpo.voyage, "etd": tpo.etd,
            "eta": tpo.eta, "transport_document_reference": tpo.transport_document_reference, "ebl_status": tpo.ebl_status,
            "ebl_issued_at": tpo.ebl_issued_at, "vgm_status": tpo.vgm_status, "vgm_submitted_at": tpo.vgm_submitted_at,
            "last_milestone": tpo.last_milestone, "last_milestone_at": tpo.last_milestone_at,
            "delivered_at": tpo.delivered_at, "last_polled_at": tpo.last_polled_at, "note": tpo.note,
        },
        "containers": tpo.containers,
        "events": tpo.tracking_events,
    }
