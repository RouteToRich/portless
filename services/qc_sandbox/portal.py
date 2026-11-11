"""SGS SMART and Bureau Veritas OneSource Connect — booking-portal sandbox.

Neither provider publishes a public booking / status / report API: SGS SMART
(sgs.com/sgs-smart, inspection.sgsmart-online.com) and BV OneSource Connect
(app.onesourceconnect.com, which replaced inspec-bv.com) are customer portals,
with integration only under a key-account agreement. PortLess therefore books
them through the portal channel: a booking request (the fields their booking
forms ask for) goes to the provider's booking desk, and the confirmation and the
report come back to ``POST /shield-link/qc/webhook`` (signed with the provider's
``*_WEBHOOK_SECRET``) or from an operator through
``POST /shield-link/qc/{ref}/status``. The request and the webhook body are
PortLess's own contract — this sandbox plays the provider's side of it:

  POST /_sandbox/{provider}/bookings               PortLess hands over a booking request
  GET  /_sandbox/{provider}/bookings               what the desk has received
  POST /_sandbox/{provider}/bookings/{ref}/confirm the desk confirms date + its order number
  POST /_sandbox/{provider}/bookings/{ref}/report  the inspector's report (PASS / FAIL + loading)
  GET  /_sandbox/{provider}/files/{file_id}        report PDF
  POST /_sandbox/portal/reset

``confirm`` and ``report`` answer the signed webhook PortLess expects
(``{"path", "headers", "body"}``, ``body`` as the exact bytes signed) and, with a
``callback_url``, also deliver it there.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .pdf import report_pdf

PORTALS = {
    "sgs": {"name": "SGS SMART", "order_prefix": "SGS-IN-", "env_prefix": "SGS", "header": "x-sgs-signature"},
    "bv": {"name": "BV OneSource Connect", "order_prefix": "BV-OSC-", "env_prefix": "BUREAU_VERITAS",
           "header": "x-bv-signature"},
}
WEBHOOK_PATH = "/shield-link/qc/webhook"


def sandbox_base_url() -> str:
    return (os.getenv("QC_SANDBOX_URL") or "http://qc-sandbox.local").rstrip("/")


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.bookings: dict[str, dict[str, dict[str, Any]]] = {p: {} for p in PORTALS}
        self.files: dict[str, bytes] = {}
        self.seq = 4100


STATE = _State()
router = APIRouter()


def _not_found(what: str) -> JSONResponse:
    return JSONResponse({"detail": f"{what} not found"}, status_code=404)


def signed_webhook(provider: str, body: dict[str, Any]) -> dict[str, Any]:
    """The webhook PortLess's ``/shield-link/qc/webhook`` accepts, signed with
    HMAC-SHA256 over the exact body bytes (``<PREFIX>_WEBHOOK_SECRET``)."""
    meta = PORTALS[provider]
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    headers = {"content-type": "application/json"}
    secret = os.getenv(f"{meta['env_prefix']}_WEBHOOK_SECRET", "")
    if secret:
        headers[meta["header"]] = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return {"path": WEBHOOK_PATH, "headers": headers, "body": raw}


async def _deliver(webhook: dict[str, Any], callback_url: str | None) -> dict[str, Any]:
    if not callback_url:
        return webhook
    # ``callback_url`` is the PortLess base URL (e.g. http://localhost:8081).
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(callback_url.rstrip("/") + webhook["path"],
                                 content=webhook["body"], headers=webhook["headers"])
    return {**webhook, "delivered_status": resp.status_code}


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    return json.loads(raw) if raw else {}


@router.post("/_sandbox/{provider}/bookings")
async def receive_booking(provider: str, request: Request) -> JSONResponse:
    if provider not in PORTALS:
        return _not_found(f"portal {provider!r}")
    body = await _body(request)
    ref = body.get("portless_ref")
    if not ref or not body.get("inspection_date"):
        return JSONResponse({"detail": "portless_ref and inspection_date are required"}, status_code=422)
    STATE.bookings[provider][ref] = {"request": body, "status": "RECEIVED", "received_at": datetime.now(timezone.utc).isoformat()}
    return JSONResponse({"request_ref": ref, "status": "RECEIVED", "portal": PORTALS[provider]["name"]}, status_code=201)


@router.get("/_sandbox/{provider}/bookings")
async def list_bookings(provider: str) -> JSONResponse:
    if provider not in PORTALS:
        return _not_found(f"portal {provider!r}")
    return JSONResponse({"bookings": STATE.bookings[provider]})


@router.post("/_sandbox/{provider}/bookings/{ref}/confirm")
async def confirm_booking(provider: str, ref: str, request: Request) -> JSONResponse:
    if provider not in PORTALS:
        return _not_found(f"portal {provider!r}")
    booking = STATE.bookings[provider].get(ref)
    if booking is None:
        return _not_found(f"booking {ref!r}")
    body = await _body(request)
    STATE.seq += 1
    booking["order_number"] = booking.get("order_number") or f"{PORTALS[provider]['order_prefix']}{date.today():%y}-{STATE.seq:06d}"
    booking["inspection_date"] = body.get("inspection_date") or booking["request"]["inspection_date"]
    booking["status"] = "CONFIRMED"
    webhook = signed_webhook(provider, {
        "ext_ref": ref, "status": "CONFIRMED", "provider_order_number": booking["order_number"],
        "inspection_date": booking["inspection_date"], "notes": body.get("notes") or "Inspector assigned",
    })
    return JSONResponse({"booking": booking, "webhook": await _deliver(webhook, body.get("callback_url"))})


@router.post("/_sandbox/{provider}/bookings/{ref}/report")
async def report_booking(provider: str, ref: str, request: Request) -> JSONResponse:
    if provider not in PORTALS:
        return _not_found(f"portal {provider!r}")
    booking = STATE.bookings[provider].get(ref)
    if booking is None:
        return _not_found(f"booking {ref!r}")
    body = await _body(request)
    verdict = str(body.get("result") or "PASS").upper()
    req = booking["request"]
    pdf = report_pdf([
        f"{PORTALS[provider]['name']} SANDBOX - Inspection report (not a real report)",
        f"Order {booking.get('order_number') or ref}  PortLess ref {ref}",
        f"Product {req.get('product', {}).get('description', '')}  HS {req.get('product', {}).get('hs_code', '')}",
        f"Quantity {req.get('product', {}).get('quantity_mt', '')} MT  PO {req.get('po_number', '')}",
        f"Result {verdict}",
        f"Containers {', '.join(body.get('container_numbers') or [])}  Seals {', '.join(body.get('seal_numbers') or [])}",
    ])
    file_id = uuid4().hex
    STATE.files[file_id] = pdf
    booking["status"] = "REPORT_ISSUED"
    payload: dict[str, Any] = {
        "ext_ref": ref, "status": "REPORT_ISSUED", "result": verdict,
        "provider_order_number": booking.get("order_number"),
        "inspection_date": booking.get("inspection_date") or req.get("inspection_date"),
        "crf_hash": hashlib.sha256(pdf).hexdigest(),
        "report_url": f"{sandbox_base_url()}/_sandbox/{provider}/files/{file_id}",
        "documents": [{"name": f"{ref}-inspection-report.pdf", "type": "INSPECTION_REPORT",
                       "url": f"{sandbox_base_url()}/_sandbox/{provider}/files/{file_id}"}],
        "notes": body.get("notes"),
        "defects": body.get("defects") or {},
    }
    for key in ("container_numbers", "seal_numbers", "loaded_quantity_mt", "loaded_hs_code", "goods_description", "photos"):
        if body.get(key) is not None:
            payload[key] = body[key]
    webhook = signed_webhook(provider, payload)
    return JSONResponse({"booking": booking, "webhook": await _deliver(webhook, body.get("callback_url"))})


@router.get("/_sandbox/{provider}/files/{file_id}")
async def report_file(provider: str, file_id: str) -> Response:
    data = STATE.files.get(file_id)
    if provider not in PORTALS or data is None:
        return _not_found("file")
    return Response(data, media_type="application/pdf")


@router.post("/_sandbox/portal/reset")
async def reset() -> JSONResponse:
    STATE.reset()
    return JSONResponse({"ok": True})
