"""Maersk API simulator — the request / response shapes the connector uses.

  POST /customer-identity/oauth/v2/access_token   form client_credentials → {access_token, expires_in}
  POST /dcsa/bkg/v2/bookings                      DCSA BookingRequest → 201 Booking (carrierBookingReference, bookingStatus RECEIVED)
  GET|PATCH|DELETE /dcsa/bkg/v2/bookings/{ref}
  GET  /track-and-trace-private/events?carrierBookingReference=…   DCSA events
  GET  /dcsa/ebl/v3/transport-documents/{tdr}      TransportDocument (DRAFT → APPROVED → ISSUED)
  PATCH /dcsa/ebl/v3/transport-documents/{tdr}/approve
  POST|GET /export-shipments/vgm/v1/shipments/{tdr}/vgm

Every call needs ``Consumer-Key`` and the bearer token. Sandbox controls play
Maersk's operations: ``/_sandbox/bookings/{ref}/confirm`` (equipment, vessel,
transport document reference, draft B/L), ``/_sandbox/bookings/{ref}/advance``
(the next container event), ``/_sandbox/transport-documents/{tdr}/issue``, and
``auto`` (confirm at once; issue the B/L once approved and the VGM is in).
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter(prefix="/maersk")

# The container journey, in order: (eventType, code field, code, at = origin | destination).
JOURNEY = [
    ("EQUIPMENT", "equipmentEventTypeCode", "GTIN", "origin"),
    ("EQUIPMENT", "equipmentEventTypeCode", "LOAD", "origin"),
    ("TRANSPORT", "transportEventTypeCode", "DEPA", "origin"),
    ("TRANSPORT", "transportEventTypeCode", "ARRI", "destination"),
    ("EQUIPMENT", "equipmentEventTypeCode", "DISC", "destination"),
    ("EQUIPMENT", "equipmentEventTypeCode", "GTOT", "destination"),
]


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.tokens: set[str] = set()
        self.bookings: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.vgm: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.auto: bool = True


STATE = _State()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(status: int, message: str) -> HTTPException:
    return HTTPException(status, detail={"errors": [{"errorCode": str(status), "message": message}]})


def _auth(request: Request) -> None:
    if not request.headers.get("consumer-key"):
        raise _err(401, "Consumer-Key header missing")
    bearer = request.headers.get("authorization", "")
    if not bearer.lower().startswith("bearer ") or bearer.split(" ", 1)[1] not in STATE.tokens:
        raise _err(401, "invalid or expired access token")


async def _body(request: Request) -> dict[str, Any]:
    _auth(request)
    body = (await request.json()) if (await request.body()) else {}
    STATE.requests.append({"method": request.method, "path": request.url.path, "body": body})
    return body


def _booking(ref: str) -> dict[str, Any]:
    booking = STATE.bookings.get(ref)
    if booking is None:
        raise _err(404, f"booking {ref} not found")
    return booking


@router.post("/customer-identity/oauth/v2/access_token")
async def token(request: Request) -> dict[str, Any]:
    form = await request.form()
    if form.get("grant_type") != "client_credentials" or not form.get("client_id") or not form.get("client_secret"):
        raise _err(400, "grant_type=client_credentials with client_id and client_secret required")
    if not request.headers.get("consumer-key"):
        raise _err(401, "Consumer-Key header missing")
    value = uuid4().hex
    STATE.tokens.add(value)
    return {"access_token": value, "token_type": "Bearer", "expires_in": 7200}


@router.post("/dcsa/bkg/v2/bookings", status_code=201)
async def create_booking(request: Request) -> dict[str, Any]:
    body = await _body(request)
    for field in ("carrierServiceCode", "receiptTypeAtOrigin", "deliveryTypeAtDestination",
                  "cargoMovementTypeAtOrigin", "cargoMovementTypeAtDestination", "requestedEquipments"):
        if not body.get(field):
            raise _err(400, f"{field} is required")
    for eq in body["requestedEquipments"]:
        if not eq.get("ISOEquipmentCode") or int(eq.get("units") or 0) < 1:
            raise _err(400, "requestedEquipments[] needs ISOEquipmentCode and units >= 1")
    ref = f"{random.randint(230_000_000, 269_999_999)}"
    booking = {**body, "carrierBookingReference": ref, "bookingStatus": "RECEIVED", "bookingRequestDateTime": _now()}
    STATE.bookings[ref] = booking
    STATE.events[ref] = []
    if STATE.auto:
        _confirm(ref)
    return booking


@router.get("/dcsa/bkg/v2/bookings/{ref}")
async def get_booking(ref: str, request: Request) -> dict[str, Any]:
    _auth(request)
    return _booking(ref)


@router.patch("/dcsa/bkg/v2/bookings/{ref}")
async def amend_booking(ref: str, request: Request) -> dict[str, Any]:
    body = await _body(request)
    booking = _booking(ref)
    booking.update({k: v for k, v in body.items() if k not in ("carrierBookingReference", "bookingStatus")})
    booking["bookingStatus"] = "PENDING_UPDATE"
    return booking


@router.delete("/dcsa/bkg/v2/bookings/{ref}", status_code=204)
async def cancel_booking(ref: str, request: Request):
    _auth(request)
    booking = _booking(ref)
    if any(e.get("transportEventTypeCode") == "DEPA" for e in STATE.events.get(ref, [])):
        raise _err(409, "the vessel has departed — the booking can no longer be cancelled")
    booking["bookingStatus"] = "CANCELLED"
    return Response(status_code=204)


@router.get("/track-and-trace-private/events")
async def events(request: Request) -> Any:
    _auth(request)
    ref = request.query_params.get("carrierBookingReference")
    tdr = request.query_params.get("transportDocumentReference")
    equipment = request.query_params.get("equipmentReference")
    if not (ref or tdr or equipment):
        raise _err(400, "carrierBookingReference, transportDocumentReference or equipmentReference is required")
    if tdr and not ref:
        ref = next((r for r, b in STATE.bookings.items() if b.get("transportDocumentReference") == tdr), None)
    found = list(STATE.events.get(ref or "", []))
    if equipment:
        found = [e for e in (found or [e for evs in STATE.events.values() for e in evs])
                 if e.get("equipmentReference") in (None, equipment)]
    return found


@router.get("/dcsa/ebl/v3/transport-documents/{tdr}")
async def get_document(tdr: str, request: Request) -> dict[str, Any]:
    _auth(request)
    doc = STATE.documents.get(tdr)
    if doc is None:
        raise _err(404, f"transport document {tdr} not found")
    return doc


@router.patch("/dcsa/ebl/v3/transport-documents/{tdr}/approve")
async def approve_document(tdr: str, request: Request) -> dict[str, Any]:
    await _body(request)
    doc = STATE.documents.get(tdr)
    if doc is None:
        raise _err(404, f"transport document {tdr} not found")
    if doc["transportDocumentStatus"] != "DRAFT":
        raise _err(409, f"only a DRAFT can be approved (status {doc['transportDocumentStatus']})")
    doc["transportDocumentStatus"] = "APPROVED"
    if STATE.auto and tdr in STATE.vgm:
        _issue(tdr)
    return doc


@router.post("/export-shipments/vgm/v1/shipments/{tdr}/vgm", status_code=202)
async def submit_vgm(tdr: str, request: Request) -> dict[str, Any]:
    body = await _body(request)
    if tdr not in STATE.documents:
        raise _err(400, f"unknown transport document {tdr}")
    containers = body.get("containers") or []
    if not containers:
        raise _err(400, "containers is required")
    booking = next(b for b in STATE.bookings.values() if b.get("transportDocumentReference") == tdr)
    known = {e["equipmentReference"] for e in booking.get("confirmedEquipments", [])}
    for c in containers:
        for field in ("equipmentReference", "verifiedGrossMass", "weighingMethod", "responsibleParty"):
            if c.get(field) in (None, "", {}):
                raise _err(400, f"containers[].{field} is required")
        if c["weighingMethod"] not in ("METHOD1", "METHOD2"):
            raise _err(400, "weighingMethod must be METHOD1 or METHOD2")
        if c["equipmentReference"] not in known:
            raise _err(400, f"container {c['equipmentReference']} is not on this booking")
        if float(c["verifiedGrossMass"]) <= 0:
            raise _err(400, "verifiedGrossMass must be positive")
    STATE.vgm[tdr] = {**body, "receivedDateTime": _now()}
    if STATE.auto and STATE.documents[tdr]["transportDocumentStatus"] == "APPROVED":
        _issue(tdr)
    return {"status": "ACCEPTED"}


@router.get("/export-shipments/vgm/v1/shipments/{tdr}/vgm")
async def get_vgm(tdr: str, request: Request) -> dict[str, Any]:
    _auth(request)
    if tdr not in STATE.vgm:
        raise _err(404, "no VGM for this transport document")
    return STATE.vgm[tdr]


# ---------------------------------------------------------------------------
# Sandbox controls — Maersk's operations team, played
# ---------------------------------------------------------------------------

def _container_no() -> str:
    return f"MSKU{random.randint(1_000_000, 9_999_999)}"


def _confirm(ref: str) -> dict[str, Any]:
    booking = _booking(ref)
    if booking["bookingStatus"] in ("CONFIRMED", "CANCELLED", "REJECTED"):
        return booking
    equipment = []
    for eq in booking.get("requestedEquipments", []):
        allocated = list(eq.get("equipmentReferences") or [])
        for n in range(int(eq.get("units") or 1)):
            equipment.append({"equipmentReference": allocated[n] if n < len(allocated) else _container_no(),
                              "ISOEquipmentCode": eq.get("ISOEquipmentCode"),
                              "tareWeight": {"value": 3750 if str(eq.get("ISOEquipmentCode", "")).startswith("4") else 2230,
                                             "unit": "KGM"}})
    departs = datetime.now(UTC) + timedelta(days=6)
    booking.update({
        "bookingStatus": "CONFIRMED",
        "confirmedEquipments": equipment,
        "transportDocumentReference": ref,
        "transportPlan": [{"transportPlanStage": "MNC", "modeOfTransport": "VESSEL", "vesselName": "MAERSK SANDBOX",
                           "vesselIMONumber": "9778791", "carrierExportVoyageNumber": f"{random.randint(100, 999)}W",
                           "plannedDepartureDate": departs.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "plannedArrivalDate": (departs + timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")}],
    })
    STATE.documents[ref] = {
        "transportDocumentReference": ref, "transportDocumentTypeCode": "BOL", "transportDocumentStatus": "DRAFT",
        "carrierCode": "MAEU", "cargoMovementTypeAtOrigin": "FCL", "cargoMovementTypeAtDestination": "FCL",
        "shipperParty": {"partyName": "PortLess"}, "placeOfIssue": {"UNLocationCode": None},
    }
    return booking


def _issue(tdr: str) -> dict[str, Any]:
    doc = STATE.documents[tdr]
    doc["transportDocumentStatus"] = "ISSUED"
    doc["issueDate"] = datetime.now(UTC).date().isoformat()
    return doc


def _locode(booking: dict[str, Any], kind: str) -> str | None:
    for loc in booking.get("shipmentLocations", []):
        if loc.get("locationTypeCode") == kind:
            return (loc.get("location") or {}).get("UNLocationCode")
    return None


@router.post("/_sandbox/bookings/{ref}/confirm")
async def sandbox_confirm(ref: str) -> dict[str, Any]:
    return _confirm(ref)


@router.post("/_sandbox/bookings/{ref}/reject")
async def sandbox_reject(ref: str) -> dict[str, Any]:
    booking = _booking(ref)
    booking["bookingStatus"] = "REJECTED"
    return booking


@router.post("/_sandbox/bookings/{ref}/advance")
async def sandbox_advance(ref: str, request: Request) -> dict[str, Any]:
    """Append the next ACT event of the journey (``{"to": "DISC"}`` runs up to that code)."""
    body = (await request.json()) if (await request.body()) else {}
    booking = _booking(ref)
    if booking["bookingStatus"] != "CONFIRMED":
        raise _err(409, "only a confirmed booking moves")
    done = [e for e in STATE.events[ref] if e["eventClassifierCode"] == "ACT"]
    codes_done = [e.get("equipmentEventTypeCode") or e.get("transportEventTypeCode") for e in done]
    target = body.get("to")
    plan = booking["transportPlan"][0]
    added = []
    for event_type, field, code, where in JOURNEY:
        if code in codes_done:
            continue
        unloc = _locode(booking, "POL" if where == "origin" else "POD")
        boxes = [e["equipmentReference"] for e in booking["confirmedEquipments"]] if event_type == "EQUIPMENT" else [None]
        for box in boxes:
            event = {"eventID": uuid4().hex, "eventType": event_type, "eventClassifierCode": "ACT", field: code,
                     "eventDateTime": _now(), "eventCreatedDateTime": _now(),
                     "eventLocation": {"UNLocationCode": unloc, "locationName": unloc},
                     "transportCall": {"modeOfTransport": "VESSEL", "carrierVoyageNumber": plan["carrierExportVoyageNumber"],
                                       "vessel": {"vesselName": plan["vesselName"], "vesselIMONumber": plan["vesselIMONumber"]},
                                       "location": {"UNLocationCode": unloc}},
                     "documentReferences": [{"type": "BKG", "value": ref}]}
            if box:
                event["equipmentReference"] = box
            STATE.events[ref].append(event)
            added.append(event)
        if not target or code == target:
            break
    return {"added": len(added), "events": STATE.events[ref]}


@router.post("/_sandbox/transport-documents/{tdr}/issue")
async def sandbox_issue(tdr: str) -> dict[str, Any]:
    if tdr not in STATE.documents:
        raise _err(404, f"transport document {tdr} not found")
    return _issue(tdr)


@router.post("/_sandbox/config")
async def sandbox_config(request: Request) -> dict[str, Any]:
    body = (await request.json()) if (await request.body()) else {}
    if "auto" in body:
        STATE.auto = bool(body["auto"])
    return {"auto": STATE.auto}
