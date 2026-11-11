"""Maersk — ocean booking, track & trace, electronic bill of lading, VGM.

Contract (Maersk developer portal APIs, DCSA-aligned; paths and schemas from the
OpenAPI definitions mirrored at artifacts.apis.io/maersk-line — Maersk's own
portal pages render client-side and could not be read, so every path is
overridable by environment variable):

  Auth      ``Consumer-Key: <MAERSK_CONSUMER_KEY>`` on every call, plus an OAuth2
            client-credentials bearer from
            ``POST https://api.maersk.com/customer-identity/oauth/v2/access_token``
            (form ``grant_type=client_credentials``, ``client_id`` = consumer key,
            ``client_secret``) — cached until shortly before it expires.
  Booking   ``POST /dcsa/bkg/v2/bookings`` (DCSA Booking 2.0 BookingRequest:
            carrierServiceCode, receiptTypeAtOrigin, deliveryTypeAtDestination,
            cargoMovementTypeAtOrigin/AtDestination, requestedEquipments[],
            commodities[]) → ``carrierBookingReference`` + ``bookingStatus``
            (RECEIVED / PENDING_UPDATE / UPDATE_CONFIRMED / CONFIRMED / REJECTED /
            CANCELLED / COMPLETED); ``GET`` / ``PATCH`` / ``DELETE`` …/{carrierBookingReference}.
  Tracking  ``GET /track-and-trace-private/events?carrierBookingReference=…``
            (DCSA events: eventType TRANSPORT / EQUIPMENT / SHIPMENT,
            eventClassifierCode PLN / EST / ACT, transportEventTypeCode ARRI / DEPA,
            equipmentEventTypeCode GTIN / LOAD / DISC / GTOT / STUF / STRP / PICK / DROP …).
  eBL       ``GET /dcsa/ebl/v3/transport-documents/{transportDocumentReference}``
            (transportDocumentStatus DRAFT / APPROVED / ISSUED / SURRENDERED / VOIDED),
            ``PATCH …/{transportDocumentReference}/approve`` (shipper approval of the draft).
  VGM       ``POST /export-shipments/vgm/v1/shipments/{transportDocumentReference}/vgm``
            ``{"transportDocumentReference", "containers": [{equipmentReference,
            verifiedGrossMass, massUnit, weighingMethod METHOD1|METHOD2, weighingDate,
            responsibleParty {partyName, signatory, contactEmail}}]}`` → 202; ``GET`` reads it back.
  Webhook   booking-status webhooks exist on the portal; no signature scheme is
            published — PortLess registers ``…/carrier/maersk/webhook?token=…``
            and always re-reads the booking before acting.

``INTEGRATION_MODE=live`` → real HTTP (a placeholder key is sent and refused);
``sandbox`` → ``CARRIER_SANDBOX_PROVIDERS=maersk`` sends the same requests to the
simulator ``services/carrier_sandbox`` (in process or ``CARRIER_SANDBOX_URL``),
otherwise a one-line stub (pytest default).
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from shared.integration_mode import sandbox as stub_mode

from .base import Connector
from .payments import ConnectorError

logger = logging.getLogger(__name__)

_TIMEOUT = 25.0
_TOKENS: dict[str, tuple[str, float]] = {}

# DCSA event → PortLess shipment milestone (applied only for ACT events).
DEPARTURE_CODES = {"DEPA"}
ARRIVAL_CODES = {"ARRI"}


def _env(name: str, default: str) -> str:
    return (os.getenv(name, "") or "").strip() or default


def sandbox_providers() -> set[str]:
    raw = os.getenv("CARRIER_SANDBOX_PROVIDERS", "").strip().lower()
    if raw in ("all", "*", "true", "1", "yes"):
        return {"maersk"}
    return {p.strip() for p in raw.split(",") if p.strip() == "maersk"}


def in_simulator(provider: str = "maersk") -> bool:
    return stub_mode() and provider in sandbox_providers()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MaerskConnector(Connector):
    """Maersk Ocean Booking + Track & Trace + eBL + VGM."""

    name = "maersk"
    display_name = "Maersk"

    def __init__(self) -> None:
        self._consumer_key = os.getenv("MAERSK_CONSUMER_KEY", "")
        self._client_secret = os.getenv("MAERSK_CLIENT_SECRET", "")
        self._webhook_secret = os.getenv("MAERSK_WEBHOOK_SECRET", "")
        self._base_url = _env("MAERSK_BASE_URL", "https://api.maersk.com").rstrip("/")
        self._token_url = _env("MAERSK_TOKEN_URL", f"{self._base_url}/customer-identity/oauth/v2/access_token")
        self._paths = {
            "bookings": _env("MAERSK_BOOKING_PATH", "/dcsa/bkg/v2/bookings"),
            "events": _env("MAERSK_EVENTS_PATH", "/track-and-trace-private/events"),
            "documents": _env("MAERSK_EBL_PATH", "/dcsa/ebl/v3/transport-documents"),
            "vgm": _env("MAERSK_VGM_PATH", "/export-shipments/vgm/v1/shipments/{tdr}/vgm"),
        }

    # -- plumbing -----------------------------------------------------------

    @property
    def stubbed(self) -> bool:
        return stub_mode() and not in_simulator(self.name)

    def _client(self) -> httpx.AsyncClient:
        if in_simulator(self.name):
            url = os.getenv("CARRIER_SANDBOX_URL", "").strip().rstrip("/")
            if url:
                return httpx.AsyncClient(base_url=f"{url}/maersk", timeout=_TIMEOUT)
            from services.carrier_sandbox.main import app as sandbox_app
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox_app),
                                     base_url="http://carrier-sandbox.local/maersk", timeout=_TIMEOUT)
        return httpx.AsyncClient(base_url=self._base_url, timeout=_TIMEOUT)

    def _key(self) -> str:
        return self._consumer_key or ("sandbox-consumer-key" if in_simulator(self.name) else "")

    async def _token(self, client: httpx.AsyncClient) -> str:
        cache_key = f"{self._key()}@{self._token_url}"
        cached = _TOKENS.get(cache_key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        token_url = "/customer-identity/oauth/v2/access_token" if in_simulator(self.name) else self._token_url
        resp = await client.post(token_url, data={
            "grant_type": "client_credentials",
            "client_id": self._key(),
            "client_secret": self._client_secret or ("sandbox-secret" if in_simulator(self.name) else ""),
        }, headers={"Consumer-Key": self._key()})
        if resp.status_code not in range(200, 300):
            raise ConnectorError(f"Maersk token request failed: HTTP {resp.status_code} — {resp.text[:300]}")
        body = resp.json()
        token = body.get("access_token") or ""
        if not token:
            raise ConnectorError("Maersk token response carried no access_token")
        _TOKENS[cache_key] = (token, time.time() + float(body.get("expires_in") or 3600))
        return token

    async def _call(self, method: str, path: str, *, json_body: Any = None,
                    params: dict[str, Any] | None = None) -> httpx.Response:
        async with self._client() as client:
            for attempt in (1, 2):
                token = await self._token(client)
                resp = await client.request(method, path, json=json_body, params=params, headers={
                    "Consumer-Key": self._key(), "Authorization": f"Bearer {token}", "Accept": "application/json",
                })
                if resp.status_code == 401 and attempt == 1:
                    _TOKENS.clear()
                    continue
                return resp
        raise ConnectorError("Maersk call failed")  # pragma: no cover

    @staticmethod
    def _ok(resp: httpx.Response, what: str) -> Any:
        if resp.status_code not in range(200, 300):
            raise ConnectorError(f"Maersk {what} failed: HTTP {resp.status_code} — {resp.text[:300]}")
        return resp.json() if resp.content else {}

    # -- quote ---------------------------------------------------------------

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Maersk rates come from the contract / Product Offers API, not a booking
        call; ``MAERSK_RATE_USD_PER_CONTAINER`` gives the contracted rate."""
        rate = os.getenv("MAERSK_RATE_USD_PER_CONTAINER", "").strip()
        return {"provider": "maersk", "rate_usd": float(rate) if rate else 0.0,
                "container_type": request.get("container_type") or _env("MAERSK_DEFAULT_EQUIPMENT", "42GP"),
                "source": "contract_rate" if rate else "no_rate_configured", "stub": not rate}

    # -- booking ---------------------------------------------------------------

    def build_booking(self, request: dict[str, Any]) -> dict[str, Any]:
        """PortLess booking request → DCSA Booking 2.0 BookingRequest."""
        qty = float(request.get("quantity_mt") or 0)
        equipment = request.get("container_type") or _env("MAERSK_DEFAULT_EQUIPMENT", "42GP")
        per_box = float(os.getenv("MAERSK_MT_PER_CONTAINER", "") or 25.0)
        units = int(request.get("containers") or max(1, -(-int(qty * 1000) // int(per_box * 1000)) if qty else 1))
        brief = request.get("brief") or {}
        body: dict[str, Any] = {
            "carrierServiceCode": request.get("carrier_service_code") or _env("MAERSK_CARRIER_SERVICE_CODE", ""),
            "receiptTypeAtOrigin": "CY",
            "deliveryTypeAtDestination": "CY",
            "cargoMovementTypeAtOrigin": "FCL",
            "cargoMovementTypeAtDestination": "FCL",
            "serviceContractReference": request.get("service_contract") or _env("MAERSK_SERVICE_CONTRACT", ""),
            "requestedEquipments": [{k: v for k, v in {
                "ISOEquipmentCode": equipment, "units": max(units, len(request.get("container_numbers") or [])),
                "isShipperOwned": False,
                # DCSA 2.0: containers already allocated (released empty to the factory and
                # stuffed under the inspector's supervision).
                "equipmentReferences": list(request.get("container_numbers") or []) or None,
            }.items() if v is not None}],
            "commodities": [{
                "commodityType": (request.get("commodity") or brief.get("product") or f"HS {request.get('hs_code', '')}")[:550],
                "HSCodes": [str(request.get("hs_code") or "")[:10]] if request.get("hs_code") else [],
                "cargoGrossWeight": round(qty * 1000, 3),
                "cargoGrossWeightUnit": "KGM",
            }],
            "partyContactDetails": [c for c in request.get("contacts") or [] if c.get("name") or c.get("email")],
            # DCSA Booking 2.0 fields beyond the mirrored minimum — the ports and our references.
            "shipmentLocations": [loc for loc in (
                {"locationTypeCode": "POL", "location": {"UNLocationCode": request.get("origin_unlocode"),
                                                         "locationName": request.get("origin_port")}}
                if request.get("origin_port") or request.get("origin_unlocode") else None,
                {"locationTypeCode": "POD", "location": {"UNLocationCode": request.get("destination_unlocode"),
                                                         "locationName": request.get("destination_port")}}
                if request.get("destination_port") or request.get("destination_unlocode") else None,
            ) if loc],
            "references": [r for r in (
                {"type": "PO", "value": request.get("po_number")} if request.get("po_number") else None,
                {"type": "CR", "value": request.get("deal_id")} if request.get("deal_id") else None,
            ) if r],
            "expectedDepartureDate": request.get("expected_departure_date"),
            "remarks": (request.get("note") or "")[:1000] or None,
        }
        customer = os.getenv("MAERSK_CUSTOMER_CODE", "").strip()
        if customer:
            body["bookingParty"] = {"partyCode": customer}
        return {k: v for k, v in body.items() if v not in (None, "", [])}

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.stubbed:
            ref = f"MSK{uuid4().hex[:9].upper()}"
            return {"ext_ref": ref, "provider": "maersk", "booking_status": "CONFIRMED", "carrier": "MAEU",
                    "etd": (datetime.now(UTC) + timedelta(days=5)).date().isoformat(),
                    "eta": (datetime.now(UTC) + timedelta(days=25)).date().isoformat(), "stub": True}
        body = self.build_booking(request)
        if not body.get("carrierServiceCode"):
            raise ConnectorError("Maersk booking needs a carrierServiceCode — set MAERSK_CARRIER_SERVICE_CODE "
                                 "(the Maersk service on this lane, from Maersk Schedules)")
        data = self._ok(await self._call("POST", self._paths["bookings"], json_body=body), "create-booking")
        return {"ext_ref": data.get("carrierBookingReference") or data.get("carrierBookingRequestReference") or "",
                "provider": "maersk", "carrier": "MAEU", "booking_status": data.get("bookingStatus") or "RECEIVED",
                "booking": data, "request": body}

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if self.stubbed:
            return {"carrierBookingReference": ext_ref, "bookingStatus": "CONFIRMED", "stub": True}
        return self._ok(await self._call("GET", f"{self._paths['bookings']}/{ext_ref}"), "get-booking")

    async def cancel(self, ext_ref: str) -> dict[str, Any]:
        if self.stubbed:
            return {"carrierBookingReference": ext_ref, "bookingStatus": "CANCELLED", "stub": True}
        resp = await self._call("DELETE", f"{self._paths['bookings']}/{ext_ref}")
        self._ok(resp, "cancel-booking")
        return {"carrierBookingReference": ext_ref, "bookingStatus": "CANCELLED"}

    # -- tracking --------------------------------------------------------------

    async def events(self, *, booking_ref: str | None = None, equipment_ref: str | None = None,
                     transport_document_ref: str | None = None) -> list[dict[str, Any]]:
        if self.stubbed:
            return []
        params = {k: v for k, v in (("carrierBookingReference", booking_ref),
                                    ("equipmentReference", equipment_ref),
                                    ("transportDocumentReference", transport_document_ref)) if v}
        data = self._ok(await self._call("GET", self._paths["events"], params=params), "track-and-trace")
        if isinstance(data, dict):
            data = data.get("events") or []
        return [e for e in data if isinstance(e, dict)]

    # -- eBL ---------------------------------------------------------------------

    async def transport_document(self, tdr: str) -> dict[str, Any]:
        if self.stubbed:
            return {"transportDocumentReference": tdr, "transportDocumentStatus": "ISSUED", "stub": True}
        return self._ok(await self._call("GET", f"{self._paths['documents']}/{tdr}"), "get-transport-document")

    async def approve_transport_document(self, tdr: str) -> dict[str, Any]:
        if self.stubbed:
            return {"transportDocumentReference": tdr, "transportDocumentStatus": "APPROVED", "stub": True}
        return self._ok(await self._call("PATCH", f"{self._paths['documents']}/{tdr}/approve"), "approve-transport-document")

    # -- VGM ---------------------------------------------------------------------

    def build_vgm(self, tdr: str, containers: list[dict[str, Any]], responsible: dict[str, Any]) -> dict[str, Any]:
        return {
            "transportDocumentReference": tdr,
            "containers": [{
                "equipmentReference": c["equipment_reference"],
                "verifiedGrossMass": round(float(c["verified_gross_mass_kg"]), 1),
                "massUnit": "KGM",
                "weighingMethod": c.get("method") or "METHOD2",
                "weighingDate": c.get("weighing_date") or _iso(datetime.now(UTC)),
                "responsibleParty": {
                    "partyName": responsible.get("party_name") or "",
                    "signatory": responsible.get("signatory") or "",
                    "contactEmail": responsible.get("contact_email") or "",
                },
            } for c in containers],
        }

    async def submit_vgm(self, tdr: str, containers: list[dict[str, Any]], responsible: dict[str, Any]) -> dict[str, Any]:
        body = self.build_vgm(tdr, containers, responsible)
        if self.stubbed:
            return {"status": "ACCEPTED", "stub": True, "request": body}
        resp = await self._call("POST", self._paths["vgm"].format(tdr=tdr), json_body=body)
        self._ok(resp, "submit-vgm")
        return {"status": "ACCEPTED" if resp.status_code in (200, 201, 202) else str(resp.status_code), "request": body}

    async def get_vgm(self, tdr: str) -> dict[str, Any]:
        if self.stubbed:
            return {"transportDocumentReference": tdr, "containers": [], "stub": True}
        return self._ok(await self._call("GET", self._paths["vgm"].format(tdr=tdr)), "get-vgm")

    # -- webhooks -----------------------------------------------------------------

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """No published Maersk signature: with MAERSK_WEBHOOK_SECRET set the call
        must carry it (``?token=`` → ``x-portless-webhook-token``); the booking is
        always re-read before anything is applied."""
        if not self._webhook_secret:
            logger.warning("MaerskConnector: MAERSK_WEBHOOK_SECRET not set — accepting all webhooks")
            return True
        token = {k.lower(): v for k, v in headers.items()}.get("x-portless-webhook-token", "")
        return bool(token) and hmac.compare_digest(token, self._webhook_secret)


# ---------------------------------------------------------------------------
# Reading DCSA events
# ---------------------------------------------------------------------------

def _event_code(e: dict[str, Any]) -> str:
    return str(e.get("transportEventTypeCode") or e.get("equipmentEventTypeCode")
               or e.get("shipmentEventTypeCode") or e.get("eventTypeCode") or "").upper()


def _location(e: dict[str, Any]) -> dict[str, Any]:
    loc = e.get("eventLocation") or e.get("location") or {}
    call = e.get("transportCall") or {}
    call_loc = call.get("location") or {}
    return {
        "name": loc.get("locationName") or call_loc.get("locationName") or loc.get("facilityName"),
        "unlocode": loc.get("UNLocationCode") or call_loc.get("UNLocationCode") or call.get("UNLocationCode"),
        "country": loc.get("countryCode"),
    }


def normalize_events(events: list[dict[str, Any]], *, origin: str | None = None,
                     destination: str | None = None) -> list[dict[str, Any]]:
    """DCSA events → PortLess timeline entries, oldest first, each with the
    shipment ``milestone`` it stands for (None for an informational event)."""
    out = []
    for e in events:
        code = _event_code(e)
        classifier = str(e.get("eventClassifierCode") or "ACT").upper()
        loc = _location(e)
        call = e.get("transportCall") or {}
        vessel = call.get("vessel") or {}
        at_destination = bool(destination and loc["unlocode"] and loc["unlocode"].upper() == destination.upper())
        at_origin = bool(origin and loc["unlocode"] and loc["unlocode"].upper() == origin.upper())
        milestone = None
        if classifier == "ACT":
            if code == "GTIN" and not at_destination:
                milestone = "GATE_IN"
            elif code == "LOAD" and not at_destination:
                milestone = "LOADED"
            elif code in DEPARTURE_CODES and (at_origin or not destination or not at_destination):
                milestone = "DEPARTED"
            elif code in ARRIVAL_CODES and (at_destination or not destination):
                milestone = "ARRIVED"
            elif code == "DISC" and (at_destination or not destination):
                milestone = "DISCHARGED"
            elif code == "GTOT" and at_destination or code == "DROP":
                milestone = "DELIVERED"
        out.append({
            "event_id": e.get("eventID") or e.get("eventId"),
            "event_type": e.get("eventType"),
            "code": code,
            "classifier": classifier,
            "at": e.get("eventDateTime"),
            "location": loc,
            "vessel": vessel.get("vesselName") or call.get("vesselName"),
            "vessel_imo": vessel.get("vesselIMONumber") or call.get("vesselIMONumber"),
            "voyage": call.get("carrierVoyageNumber") or call.get("exportVoyageNumber"),
            "mode": call.get("modeOfTransport"),
            "equipment_reference": e.get("equipmentReference"),
            "milestone": milestone,
        })
    out.sort(key=lambda x: str(x.get("at") or ""))
    return out
