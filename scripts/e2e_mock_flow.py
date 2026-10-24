"""End-to-end HTTP flow test against the live combined dev server.

Drives 3 curated mocked opportunities (see services/trade_min/mock_data.py —
requires TRADE_MIN_MOCK_MODE=true in .env) through the real deal
state machine over real HTTP calls, exercising trade-min, deals, sourcing,
negotiation-agent, dealmaker, escrow-guard, shield-link, docugen, and
exception-resolver:

  Scenario A  090930 Cumin Seeds / US       ACTIVE, high confidence
              -> full happy path all the way to PAID
  Scenario B  100630 Basmati Rice / DE      MARGINAL, thin post-tariff margin
              -> negotiation never converges -> NEGOTIATION_FAILED
  Scenario C  121190 Botanical Extracts/AE  ACTIVE, urgency tier 4 (disruption)
              -> QC inspection fails -> QC_FAILED -> Exception Resolver

Two gaps in the current pipeline are worked around explicitly (and reported
in each scenario's summary) rather than hidden:
  1. buyer_target_price_usd is never populated by the opportunity->deal
     seeding path (the Volza/ImportGenius connectors that used to supply it
     were retired from the live pipeline) — patched manually via
     PATCH /deals/{id}/financials so the PO margin guard has something to
     check and Sourcing's re-score has a price to arbitrage against.
  2. Nothing in the current codebase drives PI_ACCEPTED -> ESCROW_PENDING for
     the legacy single-hold escrow flow (POST /escrow-guard/holds) — only the
     newer staged /escrows endpoint self-drives that transition. Advanced
     manually via PATCH /deals/{id}/status, matching how an operator's
     "buyer funded escrow" action would in the UI.

Run (server must already be up):
    uvicorn app:app --port 8081   # separate terminal
    python scripts/e2e_mock_flow.py
"""
from __future__ import annotations

import sys
import uuid
from datetime import date, timedelta
from typing import Any

import httpx

BASE_URL = "http://localhost:8081"
TEST_EMAIL = "e2e-test@portless.local"  # must be in AUTH_PORTLESS_EMAILS in .env
TEST_PASSWORD = "e2e-mock-flow-test-pw"
TEST_NAME = "E2E Mock Flow"

_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def green(t: str) -> str: return _c("32", t)
def red(t: str) -> str: return _c("31", t)
def cyan(t: str) -> str: return _c("36", t)
def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)


class FlowError(Exception):
    pass


class Api:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def call(self, method: str, path: str, json: dict[str, Any] | None = None,
              ok: tuple[int, ...] = (200, 201)) -> dict[str, Any]:
        resp = self.client.request(method, path, json=json)
        if resp.status_code not in ok:
            raise FlowError(
                f"{method} {path} -> {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()


def step(label: str) -> None:
    print(f"  {cyan('->')} {label}")


def ok(label: str) -> None:
    print(f"  {green('OK')} {label}")


def fail(label: str) -> None:
    print(f"  {red('FAIL')} {label}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authenticate(client: httpx.Client) -> str:
    resp = client.post("/auth/register", json={
        "email": TEST_EMAIL, "name": TEST_NAME, "password": TEST_PASSWORD,
    })
    if resp.status_code == 201:
        token = resp.json()["access_token"]
        ok(f"Registered {TEST_EMAIL}")
    else:
        resp = client.post("/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
        if resp.status_code != 200:
            raise FlowError(f"Auth failed: {resp.status_code} {resp.text[:300]}")
        token = resp.json()["access_token"]
        ok(f"Logged in as {TEST_EMAIL}")

    is_portless = resp.json()["user"]["is_portless"]
    if not is_portless:
        raise FlowError(
            f"{TEST_EMAIL} is not Portless staff (is_portless=False) — "
            "check AUTH_PORTLESS_EMAILS in .env"
        )
    return token


# ---------------------------------------------------------------------------
# Shared setup: scan for the 3 mocked opportunities
# ---------------------------------------------------------------------------

SCENARIOS = {
    "A": {"hs_code": "090930", "market": "US", "label": "Cumin Seeds (strong ACTIVE)"},
    "B": {"hs_code": "100630", "market": "DE", "label": "Basmati Rice (MARGINAL)"},
    "C": {"hs_code": "121190", "market": "AE", "label": "Botanical Extracts (ACTIVE, disruption)"},
}


def find_opportunity(api: Api, hs_code: str, market: str) -> dict[str, Any]:
    scan = api.call("POST", "/trade-min/scan", {
        "hs_codes": [hs_code], "target_markets": [market], "min_arbitrage_rank": 0.0,
    })
    for opp in scan:
        if opp["hs_code"] == hs_code:
            return opp
    # Below the scan endpoint's own floor (config default 20.0) — fetch the
    # full persisted list instead, which has no such floor.
    listed = api.call("GET", "/trade-min/opportunities?min_rank=0", ok=(200,))
    for opp in listed:
        if opp["hs_code"] == hs_code and opp["market"] == market:
            return opp
    raise FlowError(f"No opportunity produced for {hs_code}/{market} — is TRADE_MIN_MOCK_MODE=true?")


def create_deal(api: Api, opportunity: dict[str, Any], buyer_target_price_usd: float) -> dict[str, Any]:
    deal = api.call("POST", "/deals", {
        "operator_id": "e2e-script",
        "hs_code": opportunity["hs_code"],
        "market": opportunity["market"],
        "opportunity_id": opportunity["opportunity_id"],
    })
    assert deal["status"] == "MATCHING", f"expected MATCHING, got {deal['status']}"

    # Gap #1 — see module docstring: opportunity->deal seeding never populates
    # buyer_target_price_usd today, so patch it in explicitly.
    deal = api.call("PATCH", f"/deals/{deal['deal_id']}/financials", {
        "buyer_target_price_usd": buyer_target_price_usd,
    })
    return deal


def submit_quote(api: Api, deal_id: str, hs_code: str, supplier_price: float) -> dict[str, Any]:
    rfq = api.call("POST", "/sourcing/rfq", {
        "deal_id": deal_id, "hs_code": hs_code, "suppliers": [],
    })
    quote = api.call("POST", "/sourcing/quotes", {
        "rfq_id": rfq["rfq_id"],
        "deal_id": deal_id,
        "supplier_id": f"SUP-{uuid.uuid4().hex[:8].upper()}",
        "company_name": "Mock Supplier Pvt Ltd",
        "unit_price_usd": supplier_price,
        "quantity_mt": 25,
        "incoterm": "FOB",
        "port_of_loading": "Chennai, IN",
    })
    return quote


def force_status(api: Api, deal_id: str, target_status: str, reason: str) -> dict[str, Any]:
    return api.call("PATCH", f"/deals/{deal_id}/status", {
        "target_status": target_status, "reason": reason,
    })


# ---------------------------------------------------------------------------
# Scenario A — full happy path to PAID
# ---------------------------------------------------------------------------

def run_happy_path(api: Api) -> dict[str, Any]:
    cfg = SCENARIOS["A"]
    print(bold(f"\n=== Scenario A: {cfg['label']} -> PAID ==="))
    opp = find_opportunity(api, cfg["hs_code"], cfg["market"])
    ok(f"Opportunity {opp['opportunity_id']} status={opp['status']} "
       f"arb_rank={opp['arbitrage_rank']} confidence={opp['confidence_score']} "
       f"urgency={opp['urgency_tier']}")

    buyer_price, supplier_price = 1500.0, 1200.0
    deal = create_deal(api, opp, buyer_price)
    deal_id = deal["deal_id"]
    ok(f"Deal {deal_id} created -> MATCHING (buyer_target_price_usd={buyer_price})")

    quote = submit_quote(api, deal_id, cfg["hs_code"], supplier_price)
    step(f"Quote {quote['quote_id']} re_scored_decision={quote.get('re_scored_decision')} "
         f"cms={quote.get('re_scored_cms')}")
    deal = api.call("GET", f"/deals/{deal_id}")
    if deal["status"] == "MATCHING":
        # Gap #1 consequence: proximity/reliability defaults cap CMS at 65 <
        # the 75 SOURCE threshold when match_results was never seeded at deal
        # creation (see module docstring) — advance manually, as an operator
        # confirming a supplier quote in the UI would.
        deal = force_status(api, deal_id, "NEGOTIATING",
                             "Operator confirmed supplier quote (CMS below auto-SOURCE threshold)")
    ok(f"Deal -> {deal['status']}")

    neg = api.call("POST", "/negotiation-agent/negotiations", {
        "deal_id": deal_id, "trade_match_id": deal_id,
        "buyer_id": "BUYER-US-001", "seller_id": quote["supplier_id"],
        "product_code": cfg["hs_code"], "buyer_country_code": cfg["market"],
        "buyer_target_price": buyer_price, "supplier_quote_price": supplier_price,
        "policy": {"concession_step": 300, "max_rounds": 5},
    })
    ok(f"Negotiation {neg['negotiation_id']} status={neg['status']} agreed_price={neg.get('agreed_price')}")
    if neg["status"] != "AGREED":
        raise FlowError(f"Expected immediate AGREED, got {neg['status']}: {neg.get('reasoning')}")
    agreed_price = neg["agreed_price"]

    deal = api.call("GET", f"/deals/{deal_id}")
    ok(f"Deal -> {deal['status']}")

    pi = api.call("POST", "/dealmaker/pi", {
        "deal_id": deal_id, "agreed_price": agreed_price, "quantity_mt": 25,
        "port_of_destination": "New York, US",
        "payment_terms": "30% advance + 70% on BL",
    })
    ok(f"PI {pi['pi_id']} issued (agreed_price={agreed_price})")

    pi = api.call("POST", f"/dealmaker/pi/{pi['pi_id']}/accept", {"accepted_by": "buyer@example.com"})
    ok(f"PI accepted -> status={pi['status']}")

    # Gap #2 — see module docstring: nothing drives PI_ACCEPTED->ESCROW_PENDING
    # for the legacy single-hold flow; advance manually.
    deal = force_status(api, deal_id, "ESCROW_PENDING", "Buyer initiated escrow funding")
    ok(f"Deal -> {deal['status']}")

    hold_amount = round(agreed_price * 25, 2)
    hold = api.call("POST", "/escrow-guard/holds", {
        "deal_id": deal_id, "amount": hold_amount, "idempotency_key": str(uuid.uuid4()),
    })
    ok(f"Escrow hold {hold['escrow_id']} created (ext_ref={hold['ext_ref']}, amount={hold_amount})")

    deal = api.call("POST", "/escrow-guard/webhooks/escrow-funded", {
        "ext_ref": hold["ext_ref"], "status": "FUNDED",
    })
    deal = api.call("GET", f"/deals/{deal_id}")
    ok(f"Escrow funded webhook -> deal status={deal['status']}")

    po = api.call("POST", "/dealmaker/po", {
        "deal_id": deal_id, "agreed_price": supplier_price, "quantity_mt": 25,
        "port_of_loading": "Chennai, IN",
        "delivery_deadline": str(date.today() + timedelta(days=21)),
        "payment_terms": "30% advance + 70% on BL",
    })
    ok(f"PO {po['po_id']} issued, net_margin_pct={po.get('net_margin_pct')}")

    qc = api.call("POST", "/shield-link/qc", {"deal_id": deal_id})
    ok(f"QC booked (tpo={qc['tpo_id']}, ext_ref={qc['ext_ref']})")

    api.call("POST", "/shield-link/qc/webhook", {"ext_ref": qc["ext_ref"], "result": "PASS"})
    deal = api.call("GET", f"/deals/{deal_id}")
    ok(f"QC PASS webhook -> deal status={deal['status']}")

    for doc_type in ("CI", "PL", "CoO", "SDF", "SHIPPING_BILL"):
        doc = api.call("POST", "/docugen/documents", {"deal_id": deal_id, "doc_type": doc_type})
        step(f"Document {doc_type} -> {doc['doc_id']} status={doc['status']}")
    ok("All 5 shipment documents generated")

    freight = api.call("POST", "/shield-link/freight", {"deal_id": deal_id})
    deal = api.call("GET", f"/deals/{deal_id}")
    ok(f"Freight booked (tpo={freight['tpo_id']}, ext_ref={freight['ext_ref']}) -> deal status={deal['status']}")

    customs = api.call("POST", "/docugen/customs/file", {"deal_id": deal_id})
    ok(f"Customs filed status={customs['status']} shipping_bill={customs.get('shipping_bill_number')}")

    api.call("POST", "/shield-link/freight/webhook", {"ext_ref": freight["ext_ref"], "milestone": "CLEARED"})
    deal = api.call("GET", f"/deals/{deal_id}")
    ok(f"Freight CLEARED webhook -> deal status={deal['status']}")

    payout_amount = round(supplier_price * 25, 2)
    payout = api.call("POST", "/escrow-guard/payouts", {
        "deal_id": deal_id, "payee_id": "SUPPLIER-PAYEE-001",
        "amount": payout_amount, "idempotency_key": str(uuid.uuid4()),
    })
    ok(f"Payout {payout['payout_id']} requested (ext_ref={payout['ext_ref']}, amount={payout_amount})")

    api.call("POST", "/escrow-guard/webhooks/payout", {"ext_ref": payout["ext_ref"], "status": "COMPLETED"})
    deal = api.call("GET", f"/deals/{deal_id}")
    if deal["status"] == "PAID":
        ok(f"Payout COMPLETED webhook -> deal status={deal['status']}  *** PAID ***")
    else:
        fail(f"Payout COMPLETED webhook -> deal status={deal['status']} (expected PAID)")
    return deal


# ---------------------------------------------------------------------------
# Scenario B — negotiation never converges -> NEGOTIATION_FAILED
# ---------------------------------------------------------------------------

def run_negotiation_failure(api: Api) -> dict[str, Any]:
    cfg = SCENARIOS["B"]
    print(bold(f"\n=== Scenario B: {cfg['label']} -> NEGOTIATION_FAILED ==="))
    opp = find_opportunity(api, cfg["hs_code"], cfg["market"])
    ok(f"Opportunity {opp['opportunity_id']} status={opp['status']} "
       f"arb_rank={opp['arbitrage_rank']} confidence={opp['confidence_score']}")

    buyer_price, supplier_price = 1500.0, 800.0
    deal = create_deal(api, opp, buyer_price)
    deal_id = deal["deal_id"]
    ok(f"Deal {deal_id} created -> MATCHING")

    quote = submit_quote(api, deal_id, cfg["hs_code"], supplier_price)
    deal = force_status(api, deal_id, "NEGOTIATING", "Operator confirmed supplier quote")
    ok(f"Deal -> {deal['status']}")

    neg = api.call("POST", "/negotiation-agent/negotiations", {
        "deal_id": deal_id, "trade_match_id": deal_id,
        "buyer_id": "BUYER-DE-001", "seller_id": quote["supplier_id"],
        "product_code": cfg["hs_code"], "buyer_country_code": cfg["market"],
        "buyer_target_price": buyer_price, "supplier_quote_price": supplier_price,
        # Deliberately tight concession_step + low max_rounds so a wide
        # buyer/supplier gap that only halves each round cannot converge in
        # time -> exercises the NEGOTIATION_FAILED branch.
        "policy": {"concession_step": 5, "max_rounds": 3},
    })
    ok(f"Negotiation {neg['negotiation_id']} started, status={neg['status']} round={neg['round_no']}")

    for i in range(3):
        neg = api.call("POST", f"/negotiation-agent/negotiations/{neg['negotiation_id']}/round", {
            "counter_price": supplier_price, "party": "supplier",
        })
        step(f"Round {neg['round_no']}: bid={neg['current_bid']} ask={neg['current_ask']} "
             f"status={neg['status']}")
        if neg["status"] in ("AGREED", "FAILED"):
            break

    deal = api.call("GET", f"/deals/{deal_id}")
    if neg["status"] == "FAILED" and deal["status"] == "NEGOTIATION_FAILED":
        ok(f"Negotiation FAILED after {neg['round_no']} rounds -> deal status={deal['status']}  *** as intended ***")
    else:
        fail(f"Expected negotiation FAILED / deal NEGOTIATION_FAILED, got "
             f"negotiation={neg['status']} deal={deal['status']}")
    return deal


# ---------------------------------------------------------------------------
# Scenario C — QC fails -> QC_FAILED -> Exception Resolver
# ---------------------------------------------------------------------------

def run_qc_failure(api: Api) -> dict[str, Any]:
    cfg = SCENARIOS["C"]
    print(bold(f"\n=== Scenario C: {cfg['label']} -> QC_FAILED -> Exception Resolver ==="))
    opp = find_opportunity(api, cfg["hs_code"], cfg["market"])
    ok(f"Opportunity {opp['opportunity_id']} status={opp['status']} "
       f"arb_rank={opp['arbitrage_rank']} confidence={opp['confidence_score']} "
       f"urgency={opp['urgency_tier']} (tier 4 = disruption-driven)")

    buyer_price, supplier_price = 1200.0, 900.0
    deal = create_deal(api, opp, buyer_price)
    deal_id = deal["deal_id"]
    ok(f"Deal {deal_id} created -> MATCHING")

    quote = submit_quote(api, deal_id, cfg["hs_code"], supplier_price)
    deal = force_status(api, deal_id, "NEGOTIATING", "Operator confirmed supplier quote")

    neg = api.call("POST", "/negotiation-agent/negotiations", {
        "deal_id": deal_id, "trade_match_id": deal_id,
        "buyer_id": "BUYER-AE-001", "seller_id": quote["supplier_id"],
        "product_code": cfg["hs_code"], "buyer_country_code": cfg["market"],
        "buyer_target_price": buyer_price, "supplier_quote_price": supplier_price,
        "policy": {"concession_step": 300, "max_rounds": 5},
    })
    if neg["status"] != "AGREED":
        raise FlowError(f"Expected immediate AGREED, got {neg['status']}")
    agreed_price = neg["agreed_price"]
    ok(f"Negotiation AGREED at {agreed_price}")

    pi = api.call("POST", "/dealmaker/pi", {
        "deal_id": deal_id, "agreed_price": agreed_price, "quantity_mt": 20,
        "port_of_destination": "Jebel Ali, AE",
        "payment_terms": "50% advance + 50% on BL",
    })
    pi = api.call("POST", f"/dealmaker/pi/{pi['pi_id']}/accept", {"accepted_by": "buyer@example.ae"})
    deal = force_status(api, deal_id, "ESCROW_PENDING", "Buyer initiated escrow funding")

    hold = api.call("POST", "/escrow-guard/holds", {
        "deal_id": deal_id, "amount": round(agreed_price * 20, 2),
        "idempotency_key": str(uuid.uuid4()),
    })
    api.call("POST", "/escrow-guard/webhooks/escrow-funded", {"ext_ref": hold["ext_ref"], "status": "FUNDED"})
    ok("PI accepted, escrow funded")

    po = api.call("POST", "/dealmaker/po", {
        "deal_id": deal_id, "agreed_price": supplier_price, "quantity_mt": 20,
        "port_of_loading": "Kochi, IN",
        "delivery_deadline": str(date.today() + timedelta(days=18)),
        "payment_terms": "50% advance + 50% on BL",
    })
    ok(f"PO {po['po_id']} issued, net_margin_pct={po.get('net_margin_pct')}")

    qc = api.call("POST", "/shield-link/qc", {"deal_id": deal_id})
    api.call("POST", "/shield-link/qc/webhook", {
        "ext_ref": qc["ext_ref"], "result": "FAIL",
        "notes": "Moisture content 15.2% exceeds AGMARK Grade 1 spec (max 12%)",
    })
    deal = api.call("GET", f"/deals/{deal_id}")
    if deal["status"] == "QC_FAILED":
        ok(f"QC FAIL webhook -> deal status={deal['status']}  *** as intended ***")
    else:
        fail(f"Expected QC_FAILED, got {deal['status']}")

    exc = api.call("POST", "/exception-resolver/exceptions", {
        "deal_id": deal_id, "source_event": "deal.exception", "source_service": "shield_link",
        "detail": "QC inspection failed — moisture content out of spec",
        "context": {"qc_result": "FAIL", "tpo_id": qc["tpo_id"]},
    })
    ok(f"Exception {exc['exception_id']} raised -> status={exc['status']} "
       f"category={exc.get('category')} decision={exc.get('decision')}")
    if exc["status"] == "UNRESOLVABLE" and exc.get("decision") is None:
        step("(UNRESOLVABLE/decision=None is expected here — no LLM key is configured "
             "in .env, so the agent's analysis step cannot run; the endpoint "
             "still degrades gracefully to a synchronous 201 rather than hanging or 500ing)")
    return deal


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(bold(f"PortLess E2E mock flow — target {BASE_URL}"))
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
        try:
            resp = client.get("/health")
            resp.raise_for_status()
        except Exception as exc:
            print(red(f"Server not reachable at {BASE_URL}: {exc}"))
            print(dim("Start it first: uvicorn app:app --port 8081"))
            return 1

        token = authenticate(client)
        client.headers["Authorization"] = f"Bearer {token}"
        api = Api(client)

        results: dict[str, str] = {}
        failures: list[str] = []
        for name, runner in (
            ("A (happy path -> PAID)", run_happy_path),
            ("B (negotiation -> FAILED)", run_negotiation_failure),
            ("C (QC -> FAILED -> exception)", run_qc_failure),
        ):
            try:
                deal = runner(api)
                results[name] = deal["status"]
            except FlowError as exc:
                fail(str(exc))
                results[name] = "ERROR"
                failures.append(name)

        print(bold("\n=== Summary ==="))
        for name, status in results.items():
            marker = green("OK") if name not in failures else red("FAIL")
            print(f"  [{marker}] Scenario {name}: final deal status = {status}")

        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
