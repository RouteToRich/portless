"""Escrow.com API 2017-09-01 simulator — milestone transactions.

Contract (escrow.com/api/docs):
  POST  /transaction                       Transaction object (``id``, ``parties[]``, ``items[]`` with ``id`` / ``status`` flags / ``schedule[]``)
  GET   /transaction/{id}
  PATCH /transaction/{id}                  ``{"action": "agree" | "cancel", …}``
  PATCH /transaction/{id}/item/{item_id}   ``{"action": "ship" | "receive" | "accept" | "reject", …}``

Escrow.com rules kept: buyer + seller required, one milestone item per stage
with a ``due_date`` on its schedule, inspection period 1–30 days in whole days,
a milestone can be shipped only once it is funded (``schedule[].status.secured``),
and only the buyer accepts (``auto_accept`` plays the buyer accepting / the
inspection period lapsing at once). Sandbox controls: ``/_sandbox/transaction/{id}/pay``,
``/_sandbox/transaction/{id}/item/{item_id}/accept|reject``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .common import now_iso, require_basic_auth

router = APIRouter(prefix="/escrow-com")
_API = "/2017-09-01"


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.transactions: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.next_id = 3_000_001
        self.auto_accept: bool = True


STATE = _State()


def _bad(message: str, code: int = 422) -> HTTPException:
    return HTTPException(code, detail={"errors": [{"message": message}]})


def _txn(txn_id: str) -> dict[str, Any]:
    txn = STATE.transactions.get(str(txn_id))
    if txn is None:
        raise _bad(f"transaction {txn_id} not found", 404)
    return txn


def _item(txn: dict[str, Any], item_id: str) -> dict[str, Any]:
    item = next((i for i in txn["items"] if str(i["id"]) == str(item_id)), None)
    if item is None:
        raise _bad(f"item {item_id} not found", 404)
    return item


async def _body(request: Request) -> tuple[str, dict[str, Any]]:
    user, _ = require_basic_auth(request)
    body = (await request.json()) if (await request.body()) else {}
    STATE.requests.append({"method": request.method, "path": request.url.path, "body": body})
    return user, body


def _status_flags() -> dict[str, bool]:
    return {k: False for k in ("shipped", "received", "accepted", "rejected", "shipped_returned",
                                "received_returned", "accepted_returned", "rejected_returned", "canceled", "in_dispute")}


def _accept(item: dict[str, Any]) -> None:
    item["status"].update({"received": True, "accepted": True})
    for s in item["schedule"]:
        s["status"]["disbursed_to_beneficiary"] = True


@router.post(f"{_API}/transaction")
async def create_transaction(request: Request) -> dict[str, Any]:
    user, body = await _body(request)
    if not body.get("description"):
        raise _bad("description is required")
    parties = body.get("parties") or []
    roles = [p.get("role") for p in parties]
    if roles.count("buyer") != 1 or roles.count("seller") != 1:
        raise _bad("exactly one buyer and one seller are required")
    items = body.get("items") or []
    if not items:
        raise _bad("at least one item is required")
    txn_id = str(STATE.next_id)
    STATE.next_id += 1
    out_items = []
    for n, item in enumerate(items, start=1):
        if item.get("type") != "milestone":
            raise _bad("this simulator only models milestone transactions")
        period = int(item.get("inspection_period") or 0)
        if period < 86400 or period > 2592000 or period % 86400:
            raise _bad("inspection_period must be 1-30 whole days in seconds")
        schedules = item.get("schedule") or []
        if not schedules:
            raise _bad("each item needs a schedule")
        for s in schedules:
            if not s.get("due_date"):
                raise _bad("milestone schedules need a due_date")
            if not s.get("payer_customer") or not s.get("beneficiary_customer"):
                raise _bad("payer_customer and beneficiary_customer are required")
        out_items.append({
            "id": int(txn_id) * 10 + n, "title": item.get("title"), "description": item.get("description"),
            "type": "milestone", "reference": item.get("reference"), "quantity": item.get("quantity", 1),
            "inspection_period": period, "status": _status_flags(), "fees": item.get("fees") or [],
            "schedule": [{"amount": float(s["amount"]), "payer_customer": s["payer_customer"],
                          "beneficiary_customer": user if s["beneficiary_customer"] == "me" else s["beneficiary_customer"],
                          "due_date": s["due_date"], "status": {"secured": False, "disbursed_to_beneficiary": False}}
                         for s in schedules],
        })
    txn = {
        "id": int(txn_id), "currency": body.get("currency", "usd"), "description": body["description"],
        "reference": body.get("reference"), "creation_date": now_iso(), "close_date": None,
        "is_cancelled": False, "is_draft": False,
        "parties": [{
            "id": n, "role": p["role"], "customer": user if p.get("customer") == "me" else p.get("customer"),
            "agreed": p.get("customer") == "me", "initiator": p.get("customer") == "me",
            "verification_required": False, "disbursement_method_selected": p["role"] == "seller",
            "next_step": (f"https://www.escrow-sandbox.com/transaction/{txn_id}" if p["role"] == "buyer" else None),
        } for n, p in enumerate(parties, start=1)] + [{"id": 99, "role": "partner", "customer": user, "agreed": True}],
        "items": out_items,
    }
    STATE.transactions[txn_id] = txn
    return txn


@router.get(f"{_API}/transaction/{{txn_id}}")
async def fetch_transaction(txn_id: str, request: Request) -> dict[str, Any]:
    require_basic_auth(request)
    return _txn(txn_id)


@router.patch(f"{_API}/transaction/{{txn_id}}")
async def transaction_action(txn_id: str, request: Request) -> dict[str, Any]:
    _, body = await _body(request)
    txn = _txn(txn_id)
    action = body.get("action")
    if action == "agree":
        return txn
    if action == "cancel":
        if txn["is_cancelled"]:
            raise _bad("transaction is already cancelled")
        if all(i["status"]["accepted"] for i in txn["items"]):
            raise _bad("transaction is complete and cannot be cancelled")
        txn["is_cancelled"] = True
        txn["close_date"] = now_iso()
        for item in txn["items"]:
            if not item["status"]["accepted"]:
                item["status"]["canceled"] = True
        return txn
    raise _bad(f"unsupported action {action!r}")


@router.patch(f"{_API}/transaction/{{txn_id}}/item/{{item_id}}")
async def item_action(txn_id: str, item_id: str, request: Request) -> dict[str, Any]:
    _, body = await _body(request)
    txn = _txn(txn_id)
    if txn["is_cancelled"]:
        raise _bad("transaction is cancelled")
    item = _item(txn, item_id)
    action = body.get("action")
    if action == "ship":
        if not all(s["status"]["secured"] for s in item["schedule"]):
            raise _bad("the milestone is not funded yet")
        if item["status"]["shipped"]:
            return txn
        item["status"]["shipped"] = True
        item["shipping_information"] = body.get("shipping_information") or {}
        if STATE.auto_accept:
            _accept(item)
        return txn
    if action in ("receive", "accept", "reject"):
        raise _bad(f"{action} is the buyer's action — a partner cannot perform it", 403)
    raise _bad(f"unsupported action {action!r}")


# ---------------------------------------------------------------------------
# Sandbox controls — the buyer, played
# ---------------------------------------------------------------------------

@router.post("/_sandbox/transaction/{txn_id}/pay")
async def sandbox_pay(txn_id: str) -> dict[str, Any]:
    txn = _txn(txn_id)
    for party in txn["parties"]:
        party["agreed"] = True
    for item in txn["items"]:
        for s in item["schedule"]:
            s["status"]["secured"] = True
    return txn


@router.post("/_sandbox/transaction/{txn_id}/item/{item_id}/accept")
async def sandbox_accept(txn_id: str, item_id: str) -> dict[str, Any]:
    txn = _txn(txn_id)
    item = _item(txn, item_id)
    if not item["status"]["shipped"]:
        raise _bad("the seller has not delivered this milestone yet")
    _accept(item)
    return txn


@router.post("/_sandbox/transaction/{txn_id}/item/{item_id}/reject")
async def sandbox_reject(txn_id: str, item_id: str) -> dict[str, Any]:
    txn = _txn(txn_id)
    item = _item(txn, item_id)
    item["status"]["rejected"] = True
    item["status"]["in_dispute"] = True
    return txn


@router.post("/_sandbox/config")
async def sandbox_config(request: Request) -> dict[str, Any]:
    body = (await request.json()) if (await request.body()) else {}
    if "auto_accept" in body:
        STATE.auto_accept = bool(body["auto_accept"])
    return {"auto_accept": STATE.auto_accept}


def webhook_event(event: str, txn_id: str) -> bytes:
    """An Escrow.com webhook body (a hint — the receiver fetches the transaction)."""
    import json
    return json.dumps({"event_type": "transaction", "event": event, "transaction_id": int(txn_id)}).encode()
