"""End-to-end money-chain flow over HTTP against the live combined dev server.

Drives one mocked opportunity (needs TRADE_MIN_MOCK_MODE=true) from deal
creation to PAID through the **event-driven continuations and HITL gates**
— not by calling the money endpoints directly — and checks the escrow ledger
at every step:

  deal.created ─▶ DISPATCH_RFQ (L2) + counterparty enrichment (stub without APIFY_TOKEN)
  NEGOTIATION_AGREED (forced: no LLM needed) ─▶ POST /dealmaker/pi ─▶ accept
  pi.accepted ─▶ neutral escrow opened (ESCROW_PENDING) + funding e-mail
  funded webhook SHORT ─▶ PARTIALLY_FUNDED, deal still ESCROW_PENDING
  funded webhook FULL  ─▶ ESCROW_FUNDED ─▶ ISSUE_PO gate ─▶ approve
  po.issued ─▶ QC booked + T1 tranche gate ─▶ approve (T2 before T1 must fail)
  QC PASS ─▶ documents + T2 gate + BOOK_FREIGHT gate ─▶ approve both
  freight.booked ─▶ FILE_CUSTOMS gate ─▶ approve
  freight CLEARED ─▶ T3 gate + supplier payout gate CAPPED at released funds
  payout COMPLETED ─▶ deal stays CLEARED (supplier not fully paid)
  buyer accepts goods ─▶ T4 released ─▶ escrow.settled ─▶ balance payout gate
  balance COMPLETED ─▶ PAID; ledger reconciled, supplier balance 0

Run (server must already be up, in-memory stores are fine):
    cd backend
    TRADE_MIN_MOCK_MODE=true AUTH_PORTLESS_EMAILS=e2e-test@portless.local \\
        uvicorn app:app --port 8081
    python scripts/e2e_money_chain.py
"""
from __future__ import annotations

import sys
import time
import uuid
from typing import Any

import httpx

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "scripts")
from e2e_mock_flow import (  # noqa: E402
    Api, FlowError, authenticate, create_deal, fail, find_opportunity, force_status, ok, step,
)

BASE_URL = "http://localhost:8081"
HS, MARKET = "090930", "US"
BUYER_PRICE, SUPPLIER_PRICE, QTY = 1500.0, 1200.0, 25.0
POLL_SECONDS, POLL_TIMEOUT = 0.5, 25.0


def wait_for(label: str, fn, timeout: float = POLL_TIMEOUT):
    """Poll ``fn`` until it returns a truthy value (continuations run as background tasks)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(POLL_SECONDS)
    raise FlowError(f"timed out waiting for: {label}")


def deal_status(api: Api, deal_id: str) -> str:
    return api.call("GET", f"/deals/{deal_id}")["status"]


def pending_gates(api: Api, deal_id: str, action: str | None = None) -> list[dict[str, Any]]:
    gates = api.call("GET", f"/hitl-approval/approvals?deal_id={deal_id}&status=PENDING")
    return [g for g in gates if action is None or g["action"] == action]


def approve(api: Api, deal_id: str, action: str, note: str = "") -> dict[str, Any]:
    gate = wait_for(f"{action} gate", lambda: (pending_gates(api, deal_id, action) or [None])[0])
    step(f"{action}: {gate['summary'][:110]}")
    return api.call("POST", f"/hitl-approval/approvals/{gate['approval_id']}/resolve",
                    {"decision": "APPROVE", "resolved_by": "e2e-money-chain", "note": note})


def ledger(api: Api, deal_id: str) -> dict[str, Any]:
    return api.call("GET", f"/escrow-guard/deals/{deal_id}/ledger")


def hold(api: Api, deal_id: str) -> dict[str, Any]:
    return api.call("GET", f"/escrow-guard/deals/{deal_id}/hold")


def tranche(h: dict[str, Any], tid: str) -> dict[str, Any]:
    return next(t for t in h["tranches"] if t["tranche_id"] == tid)


def run(api: Api) -> dict[str, Any]:
    opp = find_opportunity(api, HS, MARKET)
    ok(f"Opportunity {opp['opportunity_id']} benchmark={opp.get('price_benchmark_usd_per_mt')} "
       f"buyers={len(opp.get('buyer_signals', []))} sellers={len(opp.get('seller_candidates', []))}")

    deal = create_deal(api, opp, BUYER_PRICE)
    deal_id = deal["deal_id"]
    buyer = next((p for p in deal["parties"] if p["role"] == "buyer"), {})
    ok(f"Deal {deal_id} -> {deal['status']} (buyer {buyer.get('legal_name')!r} "
       f"sanctions={buyer.get('sanctions_status')} email={buyer.get('email')})")
    wait_for("DISPATCH_RFQ auto-approved", lambda: [
        g for g in api.call("GET", f"/hitl-approval/approvals?deal_id={deal_id}") if g["action"] == "DISPATCH_RFQ"
    ])
    ok("DISPATCH_RFQ gate created by the deal.created leg (policy L2)")
    enrich = api.call("POST", f"/deals/{deal_id}/enrich")
    ok(f"Enrichment ran (actors={enrich['actors_used'] or 'stub'}, warnings={len(enrich['warnings'])}, "
       f"blocked={enrich['blocked']})")

    # Negotiation without an LLM: open the session (so supplier ask/quantity exist), then
    # confirm the agreement the way the CONFIRM_NEGOTIATION_AGREEMENT callback would.
    force_status(api, deal_id, "NEGOTIATING", "e2e: supplier confirmed")
    neg = api.call("POST", "/negotiation-agent/negotiations", {
        "deal_id": deal_id, "trade_match_id": deal_id, "buyer_id": "BUYER-US-001",
        "seller_id": deal.get("winning_supplier_id") or "SUP-E2E", "product_code": HS,
        "buyer_country_code": MARKET, "buyer_target_price": BUYER_PRICE,
        "supplier_quote_price": SUPPLIER_PRICE, "quantity_mt": QTY,
        "buyer_email": "buyer@acme-imports.example", "seller_email": "sales@mill.example",
    })
    ok(f"Negotiation {neg['negotiation_id']} status={neg['status']} ask={neg.get('current_ask')}")
    force_status(api, deal_id, "NEGOTIATION_AGREED", "e2e: agreement confirmed")
    pi = api.call("POST", "/dealmaker/pi", {
        "deal_id": deal_id, "agreed_price": BUYER_PRICE, "quantity_mt": QTY,
        "port_of_destination": "New York, US", "payment_terms": "escrow, staged release",
    })
    ok(f"PI {pi['pi_id']} issued, total={pi['total_value']}")
    api.call("POST", f"/dealmaker/pi/{pi['pi_id']}/accept", {"accepted_by": "buyer@acme-imports.example"})
    wait_for("escrow opened by pi.accepted leg", lambda: deal_status(api, deal_id) == "ESCROW_PENDING")
    h = hold(api, deal_id)
    ok(f"Escrow {h['escrow_id']} opened on {h['provider']} ({h['amount']} {h['currency']}, "
       f"fee {h['fee_pct']}%, {len(h['tranches'])} tranches) -> deal ESCROW_PENDING")

    # Funding: short first, then the balance.
    short = api.call("POST", "/escrow-guard/webhooks/escrow-funded",
                     {"ext_ref": h["ext_ref"], "status": "FUNDED", "amount": 20000, "event_id": "evt-short"})
    h = hold(api, deal_id)
    if short["status"] != "shortfall" or h["status"] != "PARTIALLY_FUNDED" or deal_status(api, deal_id) != "ESCROW_PENDING":
        raise FlowError(f"shortfall not handled: {short} hold={h['status']} deal={deal_status(api, deal_id)}")
    ok(f"Short funding (20,000 of {h['amount']}) -> PARTIALLY_FUNDED, deal still ESCROW_PENDING, shortfall={h['shortfall']}")
    replay = api.call("POST", "/escrow-guard/webhooks/escrow-funded",
                      {"ext_ref": h["ext_ref"], "status": "FUNDED", "amount": 20000, "event_id": "evt-short"})
    ok(f"Replayed webhook ignored (replayed={replay.get('replayed')})")
    api.call("POST", "/escrow-guard/webhooks/escrow-funded",
             {"ext_ref": h["ext_ref"], "status": "FUNDED", "amount": h["amount"], "event_id": "evt-full"})
    wait_for("ESCROW_FUNDED", lambda: deal_status(api, deal_id) == "ESCROW_FUNDED")
    ok("Full funding -> ESCROW_FUNDED")

    approve(api, deal_id, "ISSUE_PO")
    wait_for("PO_ISSUED", lambda: deal_status(api, deal_id) in ("PO_ISSUED", "SUPPLIER_DISPATCH_READY"))
    wait_for("QC booked", lambda: deal_status(api, deal_id) == "SUPPLIER_DISPATCH_READY")
    ok("PO issued -> QC booked -> SUPPLIER_DISPATCH_READY")

    # Sequential release rule: T2 cannot go before T1.
    h = hold(api, deal_id)
    resp = api.client.post(f"/escrow-guard/escrows/{h['escrow_id']}/release", json={"tranche_id": "T2"})
    if resp.status_code != 422:
        raise FlowError(f"T2 before T1 should be refused, got {resp.status_code}: {resp.text[:200]}")
    ok("Releasing T2 before T1 refused (422)")
    approve(api, deal_id, "RELEASE_ESCROW_TRANCHE", "T1")
    wait_for("T1 released", lambda: tranche(hold(api, deal_id), "T1")["status"] == "RELEASED")
    ok(f"T1 released: released_total={hold(api, deal_id)['released_total']}")

    orders = api.call("GET", f"/shield-link/deals/{deal_id}/orders")["orders"]
    qc = next(o for o in orders if o["order_type"] == "QC")
    api.call("POST", "/shield-link/qc/webhook", {"ext_ref": qc["ext_ref"], "result": "PASS"})
    wait_for("QC_PASSED", lambda: deal_status(api, deal_id) == "QC_PASSED")
    approve(api, deal_id, "RELEASE_ESCROW_TRANCHE", "T2")
    wait_for("T2 released", lambda: tranche(hold(api, deal_id), "T2")["status"] == "RELEASED")
    approve(api, deal_id, "BOOK_FREIGHT")
    wait_for("IN_TRANSIT", lambda: deal_status(api, deal_id) == "IN_TRANSIT")
    docs = api.call("GET", f"/docugen/deals/{deal_id}/documents")["documents"]
    ok(f"QC passed -> T2 released, {len(docs)} documents rendered, freight booked -> IN_TRANSIT")
    approve(api, deal_id, "FILE_CUSTOMS")

    freight = next(o for o in api.call("GET", f"/shield-link/deals/{deal_id}/orders")["orders"]
                   if o["order_type"] == "FREIGHT")
    api.call("POST", "/shield-link/freight/webhook", {"ext_ref": freight["ext_ref"], "milestone": "CLEARED"})
    wait_for("CLEARED", lambda: deal_status(api, deal_id) == "CLEARED")
    approve(api, deal_id, "RELEASE_ESCROW_TRANCHE", "T3")
    wait_for("T3 released", lambda: tranche(hold(api, deal_id), "T3")["status"] == "RELEASED")
    h = hold(api, deal_id)
    if h["inspection_deadline"] is not None:
        raise FlowError("inspection window opened on the proof tranche — it must wait for delivery")
    ok("Cleared -> T3 released, acceptance window NOT open yet (goods not delivered)")
    api.call("POST", f"/shield-link/freight/{freight['tpo_id']}/milestone",
             {"milestone": "DELIVERED", "location": "Hamburg warehouse", "source": "operator"})
    wait_for("inspection window", lambda: hold(api, deal_id)["inspection_deadline"] is not None)
    h = hold(api, deal_id)
    ok(f"DELIVERED -> inspection window ({h['inspection_trigger']}) until {h['inspection_deadline']}")

    led = ledger(api, deal_id)
    gate = wait_for("payout gate", lambda: (pending_gates(api, deal_id, "RELEASE_SUPPLIER_PAYOUT") or [None])[0])
    amount, owed = gate["callback_payload"]["amount"], gate["callback_payload"]["total_owed"]
    if amount > led["available_for_payout"] + 0.01 or amount >= owed:
        raise FlowError(f"payout gate not capped: amount={amount} available={led['available_for_payout']} owed={owed}")
    ok(f"Supplier payout gate capped: {amount} of {owed} owed (available {led['available_for_payout']}, "
       f"net released {led['net_to_portless']})")
    approve(api, deal_id, "RELEASE_SUPPLIER_PAYOUT", "capped payout")
    payout = wait_for("payout requested", lambda: (api.call("GET", f"/escrow-guard/deals/{deal_id}/summary")["payouts"] or [None])[0])
    api.call("POST", "/escrow-guard/webhooks/payout", {"ext_ref": payout["ext_ref"], "status": "COMPLETED", "event_id": "pay-1"})
    time.sleep(1.0)
    if deal_status(api, deal_id) != "CLEARED":
        raise FlowError(f"deal moved to {deal_status(api, deal_id)} although the supplier is not fully paid")
    led = ledger(api, deal_id)
    ok(f"Partial payout completed -> deal stays CLEARED, supplier balance due {led['supplier_balance_due']}")

    # Buyer accepts the goods (the e-mail path would create CONFIRM_BUYER_ACCEPTANCE; the API is the same authority).
    api.call("POST", f"/escrow-guard/escrows/{h['escrow_id']}/accept", {"accepted_by": "buyer@acme-imports.example"})
    h = hold(api, deal_id)
    if h["status"] != "RELEASED" or not h["settled_at"]:
        raise FlowError(f"escrow not settled after acceptance: {h['status']} settled_at={h['settled_at']}")
    ok(f"Buyer acceptance -> T4 released, escrow settled (released {h['released_total']}, fees {h['fees_total']})")
    gate = wait_for("balance payout gate", lambda: (pending_gates(api, deal_id, "RELEASE_SUPPLIER_PAYOUT") or [None])[0])
    if gate["callback_payload"]["kind"] != "supplier_balance" or abs(gate["callback_payload"]["amount"] - led["supplier_balance_due"]) > 0.01:
        raise FlowError(f"balance gate wrong: {gate['callback_payload']}")
    approve(api, deal_id, "RELEASE_SUPPLIER_PAYOUT", "balance")
    balance = wait_for("balance payout requested", lambda: next(
        (p for p in api.call("GET", f"/escrow-guard/deals/{deal_id}/summary")["payouts"] if p["kind"] == "supplier_balance"), None))
    api.call("POST", "/escrow-guard/webhooks/payout", {"ext_ref": balance["ext_ref"], "status": "COMPLETED", "event_id": "pay-2"})
    wait_for("PAID", lambda: deal_status(api, deal_id) == "PAID")
    led = ledger(api, deal_id)
    if not led["reconciled"] or abs(led["supplier_balance_due"]) > 0.01 or led["outstanding"] > 0.01:
        raise FlowError(f"ledger not closed: {led}")
    deal = api.call("GET", f"/deals/{deal_id}")
    ok(f"Balance paid -> PAID. Ledger: principal {led['principal']}, released {led['released_total']}, fees "
       f"{led['fees_total']}, payouts {led['payouts_total']}, realised margin {led['realized_margin']} "
       f"(deal.realized_margin_usd={deal.get('realized_margin_usd')}), reconciled={led['reconciled']}")
    return deal


def main() -> int:
    print(f"PortLess E2E money chain — target {BASE_URL}")
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
        try:
            client.get("/health").raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"Server not reachable at {BASE_URL}: {exc}")
            return 2
        token = authenticate(client)
        client.headers["Authorization"] = f"Bearer {token}"
        api = Api(client)
        try:
            deal = run(api)
        except FlowError as exc:
            fail(str(exc))
            return 1
    print(f"\n*** money chain complete: deal {deal['deal_id']} is {deal['status']} ***")
    return 0


if __name__ == "__main__":
    sys.exit(main())
