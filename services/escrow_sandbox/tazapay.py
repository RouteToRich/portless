"""Tazapay API v3 escrow simulator — the documented request / response shapes.

Contract (developer.tazapay.com/openapi.yaml):
  POST /v3/escrow              → {"status": "success", "message": "", "data": {id: "esc_…", url, payment_status: "unpaid", …}}
  GET  /v3/escrow/{id}         → same envelope
  POST /v3/escrow/{id}/expire  → status "expired" (only while unpaid)
  POST /v3/document            → {data: {id: "doc_…", object: "document", reference, type, url, …}}
  POST /v3/refund              → {data: {id: "rfd_…", object: "refund", payin, amount, status}}

Sandbox controls (not part of Tazapay): ``/_sandbox/escrow/{id}/pay`` plays the
buyer paying on the hosted page, ``/_sandbox/document/{id}/verify`` plays
Tazapay's document check (``auto_verify`` does it on upload), and
``webhook_event()`` builds a webhook signed as Tazapay signs it.
"""
from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from .common import now_iso, require_basic_auth

router = APIRouter(prefix="/tazapay")


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.escrows: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.auto_verify: bool = True


STATE = _State()


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "message": "", "data": data}


def _bad(message: str, code: int = 400) -> HTTPException:
    return HTTPException(code, detail={"status": "error", "message": message,
                                       "errors": [{"code": code, "message": message}]})


def _escrow(escrow_id: str) -> dict[str, Any]:
    esc = STATE.escrows.get(escrow_id)
    if esc is None:
        raise _bad(f"escrow {escrow_id} not found", 404)
    return esc


async def _body(request: Request) -> dict[str, Any]:
    require_basic_auth(request)
    try:
        body = await request.json() if (await request.body()) else {}
    except ValueError as exc:
        raise _bad("invalid JSON") from exc
    STATE.requests.append({"method": request.method, "path": request.url.path, "body": body})
    return body


@router.post("/v3/escrow")
async def create_escrow(request: Request) -> dict[str, Any]:
    body = await _body(request)
    for field in ("invoice_currency", "amount", "customer_details", "transaction_description"):
        if body.get(field) in (None, "", {}):
            raise _bad(f"{field} is required")
    try:
        amount = int(body["amount"])
    except (TypeError, ValueError) as exc:
        raise _bad("amount must be an integer in the currency's minor unit") from exc
    if amount <= 0:
        raise _bad("amount must be positive")
    customer = body["customer_details"]
    for field in ("name", "email", "country"):
        if not customer.get(field):
            raise _bad(f"customer_details.{field} is required")
    escrow_id = f"esc_{uuid4().hex[:20]}"
    esc = {
        "id": escrow_id, "object": "escrow", "amount": amount, "amount_paid": 0,
        "invoice_currency": str(body["invoice_currency"]).upper(), "holding_currency": str(body["invoice_currency"]).upper(),
        "customer": f"cus_{uuid4().hex[:16]}", "customer_details": customer,
        "billing_details": body.get("billing_details"), "shipping_details": body.get("shipping_details"),
        "transaction_description": body["transaction_description"], "reference_id": body.get("reference_id", ""),
        "metadata": body.get("metadata"), "payin": escrow_id, "payment_attempts": [], "latest_payment_attempt": "",
        "payment_methods": body.get("payment_methods") or ["card", "local_bank_transfer"],
        "remove_payment_methods": body.get("remove_payment_methods") or [],
        "payment_status": "unpaid", "payment_status_description": "", "partially_paid": False, "paid_in_excess": False,
        "status": "active", "success_url": body.get("success_url", ""), "cancel_url": body.get("cancel_url", ""),
        "webhook_url": body.get("webhook_url", ""), "expires_at": body.get("expires_at", ""),
        "customer_fee_percentage": int(body.get("customer_fee_percentage") or 0), "statement_descriptor": "",
        "transaction_data": [], "transaction_documents": [], "items": body.get("items") or [],
        "url": f"https://checkout-sandbox.tazapay.com/transaction/{uuid4().hex}", "created_at": now_iso(),
    }
    STATE.escrows[escrow_id] = esc
    return _ok(esc)


@router.get("/v3/escrow/{escrow_id}")
async def fetch_escrow(escrow_id: str, request: Request) -> dict[str, Any]:
    require_basic_auth(request)
    return _ok(_escrow(escrow_id))


@router.put("/v3/escrow/{escrow_id}")
async def update_escrow(escrow_id: str, request: Request) -> dict[str, Any]:
    body = await _body(request)
    esc = _escrow(escrow_id)
    if "metadata" in body:
        esc["metadata"] = body["metadata"]
    if "items" in body:
        esc["items"] = body["items"]
    return _ok(esc)


@router.post("/v3/escrow/{escrow_id}/expire")
async def expire_escrow(escrow_id: str, request: Request) -> dict[str, Any]:
    await _body(request)
    esc = _escrow(escrow_id)
    if esc["payment_status"] != "unpaid":
        raise _bad("a paid escrow cannot be expired — refund it instead")
    esc["status"] = "expired"
    esc["expires_at"] = now_iso()
    return _ok(esc)


@router.post("/v3/document")
async def upload_document(request: Request) -> dict[str, Any]:
    body = await _body(request)
    for field in ("url", "type", "reference"):
        if not body.get(field):
            raise _bad(f"{field} is required")
    esc = _escrow(body["reference"])
    if esc["payment_status"] not in ("paid", "partially_paid"):
        raise _bad("documents can be submitted only once the escrow is paid")
    doc = {
        "id": f"doc_{uuid4().hex[:20]}", "object": "document", "reference": body["reference"],
        "type": body["type"], "name": body.get("name") or body["type"], "url": body["url"],
        "description": body.get("description", ""), "metadata": body.get("metadata"),
        "status": "verified" if STATE.auto_verify else "submitted", "created_at": now_iso(),
    }
    STATE.documents[doc["id"]] = doc
    esc["transaction_documents"].append(doc)
    return _ok({k: v for k, v in doc.items() if k != "status"})


@router.post("/v3/refund")
async def refund(request: Request) -> dict[str, Any]:
    body = await _body(request)
    if not body.get("payin"):
        raise _bad("payin is required")
    esc = _escrow(body["payin"])
    if esc["amount_paid"] <= 0:
        raise _bad("nothing paid to refund")
    amount = int(body.get("amount") or esc["amount_paid"])
    if amount > esc["amount_paid"]:
        raise _bad("refund exceeds the amount paid")
    rfd = {"id": f"rfd_{uuid4().hex[:20]}", "object": "refund", "payin": esc["id"], "amount": amount,
           "currency": (body.get("currency") or esc["invoice_currency"]).lower(), "reason": body.get("reason", ""),
           "status": "succeeded", "created_at": now_iso()}
    STATE.refunds[rfd["id"]] = rfd
    esc["payment_status"] = "refunded"
    return _ok(rfd)


# ---------------------------------------------------------------------------
# Sandbox controls — the buyer and Tazapay's operations team, played
# ---------------------------------------------------------------------------

@router.post("/_sandbox/escrow/{escrow_id}/pay")
async def sandbox_pay(escrow_id: str, request: Request) -> dict[str, Any]:
    """The buyer pays on the hosted page. ``{"amount": <major>}`` pays part of it."""
    body = (await request.json()) if (await request.body()) else {}
    esc = _escrow(escrow_id)
    paid = esc["amount"] if body.get("amount") is None else round(float(body["amount"]) * 100)
    esc["amount_paid"] = min(esc["amount"], esc["amount_paid"] + paid) if body.get("amount") is not None else esc["amount"]
    full = esc["amount_paid"] >= esc["amount"]
    esc["payment_status"] = "paid" if full else "partially_paid"
    esc["partially_paid"] = not full
    attempt = f"pat_{uuid4().hex[:20]}"
    esc["latest_payment_attempt"] = attempt
    esc["payment_attempts"].append({"id": attempt, "object": "payment_attempt", "payin": escrow_id,
                                    "status": "succeeded", "amount": paid, "created_at": now_iso()})
    return _ok(esc)


@router.post("/_sandbox/document/{doc_id}/verify")
async def sandbox_verify(doc_id: str, request: Request) -> dict[str, Any]:
    body = (await request.json()) if (await request.body()) else {}
    doc = STATE.documents.get(doc_id)
    if doc is None:
        raise _bad(f"document {doc_id} not found", 404)
    doc["status"] = "failed" if body.get("status") == "failed" else "verified"
    return _ok(doc)


@router.post("/_sandbox/config")
async def sandbox_config(request: Request) -> dict[str, Any]:
    body = (await request.json()) if (await request.body()) else {}
    if "auto_verify" in body:
        STATE.auto_verify = bool(body["auto_verify"])
    return {"auto_verify": STATE.auto_verify}


def webhook_event(event_type: str, escrow_id: str, *, secret: str | None = None,
                  data: dict[str, Any] | None = None) -> tuple[bytes, dict[str, str]]:
    """A webhook body + headers exactly as Tazapay sends them (signed with
    ``TAZAPAY_WEBHOOK_SECRET`` or *secret*)."""
    from shared.connectors.escrow import tazapay_signature

    payload = {"id": f"evt_{uuid4().hex[:20]}", "object": "event", "type": event_type, "created_at": now_iso(),
               "data": data if data is not None else dict(STATE.escrows.get(escrow_id) or {"id": escrow_id})}
    body = json.dumps(payload, separators=(",", ":")).encode()
    key = secret if secret is not None else os.getenv("TAZAPAY_WEBHOOK_SECRET", "")
    return body, ({"signature": tazapay_signature(key, body)} if key else {})
