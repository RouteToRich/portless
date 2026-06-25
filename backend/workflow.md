# PortLess — End-to-End Workflow

> **Who is this for?**  
> Written for both first-time readers and developers.  
> Every section describes the *why* in plain English, then shows the exact
> API calls, request payloads, and response shapes used in the code.  
> Implementation status (✅ Done / ⏳ Stub) is marked throughout.

---

## The One-Line Idea

A merchant exporter in India sits between an **overseas buyer** who wants a
product and an **Indian manufacturer** who makes it.  PortLess finds that pair
automatically, verifies both sides, negotiates the deal, ships the goods, and
pays everyone — without the merchant ever touching the cargo.

```
OVERSEAS BUYER  ──►  PORTLESS  ──►  INDIAN MANUFACTURER
  (Spain)           (Orchestrator)      (Guntur, AP)
   Pays in USD         Earns 8–15 %       Gets paid in INR
                       arbitrage margin    on inspection sign-off
```

---

## Big Picture: Five Phases

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  PHASE 1         PHASE 2         PHASE 3         PHASE 3b        PHASE 4    │
│  Find the        Find the        Score the       Negotiate       Execute     │
│  Opportunity     Supplier        Match           the Price       the Deal    │
│  ─────────────   ─────────────   ─────────────   ─────────────   ─────────   │
│  WHO wants to    WHO in India    Is the pair     Auto-counter    Ship,       │
│  buy, HOW MUCH,  can supply it   viable? CMS     buyer vs        inspect,    │
│  and WHEN?       competitively?  ≥ 75 → SOURCE   supplier until  customs,    │
│                                  < 75 → DISCARD  margin ≥ 8 %   pay all.    │
│  ✅ Implemented  ✅ Implemented  ✅ Implemented  ✅ Implemented  ⏳ Stubs    │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Deal Status — Single Source of Truth

The `deals` service owns the state machine.  Every service and agent transitions
this state; no other module writes it directly.

```
DRAFT
  │  (create_deal called without opportunity_id)
  │
  ├──[opportunity_id provided]──► MATCHING
  │                                  │
  │                      [quote received, CMS ≥ 75]
  │                                  │
  │                               NEGOTIATING ◄─────────────────────────────┐
  │                                  │                                       │
  │                       [negotiation.agreed]         [run_round — counter] │
  │                                  │                                       │
  │                         NEGOTIATION_AGREED ─────────────────────────────┘
  │                                  │
  │                         [dealmaker issues PI]
  │                                  │
  │                               PI_SENT
  │                                  │
  │                         [buyer accepts PI]
  │                                  │
  │                              PI_ACCEPTED
  │                                  │
  │                        [buyer wires payment]
  │                                  │
  │                           ESCROW_PENDING
  │                                  │
  │                      [escrow funded webhook]
  │                                  │
  │                           ESCROW_FUNDED
  │                                  │
  │                      [dealmaker issues PO]
  │                                  │
  │                             PO_ISSUED
  │                                  │
  │                    [supplier confirms dispatch]
  │                                  │
  │                    SUPPLIER_DISPATCH_READY
  │                                  │
  │                     [SGS inspection result]
  │                      ┌───────────┴──────────────┐
  │                   QC_PASSED                  QC_FAILED ──► DISPUTE
  │                      │
  │               [freight booked, docs filed]
  │                      │
  │                  IN_TRANSIT
  │                      │
  │              [customs cleared — LEO]
  │                      │
  │                   CLEARED
  │                      │
  │              [final payout released]
  │                      │
  │                     PAID ◄────────── terminal (success)
  │
  ├──► NEGOTIATION_FAILED ◄─ terminal (no agreement)
  ├──► CANCELLED          ◄─ terminal (operator aborts)
  └──► REFUNDED           ◄─ terminal (buyer refund issued)
```

---

## Phase 1 — Finding the Opportunity ✅

### What happens

The platform continuously watches global trade data for situations where an
overseas country is buying something that India can supply cheaply.  It also
detects *right now* signals: a buyer who posted a live sourcing request this
week, or a country that suddenly cannot get the product from its usual supplier.

### Flow

```
Trade-Min wakes up (scheduler or POST /trade-min/scan)
  │
  ├─ Phase 1a: Historical demand (Comtrade, Volza, ImportGenius, Trademo)
  ├─ Phase 1b: Live signals (Alibaba RFQ, EU TED, GDELT disruption)
  ├─ Phase 1c: Tariff gate (MacMap — skip if tariff too high)
  ├─ Phase 1d: Seller discovery (IndiaMART, TradeIndia)
  └─ Phase 1e: Confidence score + persist + emit opportunity.detected
```

### Request — trigger a scan

```http
POST /trade-min/scan
Content-Type: application/json

{
  "hs_codes":         ["090422"],
  "target_markets":   ["ES", "DE", "PL"],
  "min_arbitrage_rank": 2
}
```

### Response — enriched opportunity

```json
{
  "opportunity_id":     "OPP-2026-ES-090422-001",
  "hs_code":            "090422",
  "market":             "ES",
  "buyer_signals": {
    "buyer_name":         "Iberia Spices SL",
    "buyer_country":      "Spain",
    "vat_number":         "ESB12345678",
    "vat_verified":       true,
    "credit_limit_usd":   120000,
    "risk_grade":         "A",
    "urgency_label":      "UPCOMING",
    "days_until_order":   25,
    "live_rfq_found":     false,
    "disruption_signal":  true,
    "disruption_origin":  "Vietnam"
  },
  "demand_index":       78.4,
  "applied_tariff_pct": 7.0,
  "tariff_viable":      true,
  "seller_candidates":  [
    {
      "supplier_id":   "IND-GNT-008",
      "company_name":  "Guntur Agri Products Ltd",
      "gstin":         "37AAAAA0000A1Z1",
      "iec":           "0312045612",
      "price_min_usd": 2.05,
      "price_max_usd": 2.25,
      "lead_days":     18
    }
  ],
  "arbitrage_rank":     1,
  "confidence_score":   0.84,
  "urgency_tier":       "HIGH",
  "expires_at":         "2026-07-10T00:00:00Z",
  "status":             "ACTIVE",
  "created_at":         "2026-06-25T08:00:00Z"
}
```

### Get the Match Engine seed from this opportunity

```http
GET /trade-min/opportunities/OPP-2026-ES-090422-001/match-seed
```

```json
{
  "opportunity_id":      "OPP-2026-ES-090422-001",
  "buyer_target_price":  3.20,
  "buyer_country_code":  "ES",
  "top_sellers": [
    {
      "supplier_id":          "IND-GNT-008",
      "quote_price_usd":      2.10,
      "supplier_to_port_km":  320,
      "business_age_years":   7.0,
      "udyam_registered":     true,
      "annual_shipment_count": 14,
      "gst_verified":         true,
      "iec_active":           true,
      "dgft_denied":          false
    }
  ]
}
```

---

## Phase 2 — Finding the Supplier (Sourcing) ✅

### What happens

Once an opportunity is confirmed, the Sourcing service dispatches automated
RFQs to shortlisted manufacturers.  Every quote that comes back triggers a
full regulatory verification and a Match Engine re-score before being accepted.

### Step 2a — Dispatch RFQ

```http
POST /sourcing/rfq
Content-Type: application/json

{
  "deal_id":         "DEAL-2026-IN-ES-0091",
  "hs_code":         "090422",
  "quantity_mt":     16,
  "target_price_usd": 3.20,
  "validity_days":   7,
  "suppliers": [
    {
      "supplier_id":  "IND-GNT-008",
      "contact_id":   "CON-001",
      "source":       "indiamart",
      "company_name": "Guntur Agri Products Ltd"
    }
  ]
}
```

```json
{
  "rfq_id":          "RFQ-2026-0091-001",
  "deal_id":         "DEAL-2026-IN-ES-0091",
  "dispatched_count": 1,
  "failed_count":    0,
  "status":          "COMPLETE",
  "dispatch_results": [
    {
      "supplier_id": "IND-GNT-008",
      "channel":     "indiamart",
      "ext_rfq_id":  "IM-RFQ-8812",
      "success":     true,
      "error":       null
    }
  ]
}
```

### Step 2b — Supplier submits quote

```http
POST /sourcing/quotes
Content-Type: application/json

{
  "rfq_id":          "RFQ-2026-0091-001",
  "deal_id":         "DEAL-2026-IN-ES-0091",
  "supplier_id":     "IND-GNT-008",
  "company_name":    "Guntur Agri Products Ltd",
  "unit_price_usd":  2.10,
  "currency":        "USD",
  "quantity_mt":     16,
  "validity_days":   7,
  "incoterm":        "FOB",
  "port_of_loading": "Chennai",
  "lead_time_days":  18,
  "payment_terms":   "30% advance, 70% on BL",
  "gstin":           "37AAAAA0000A1Z1",
  "iec_number":      "0312045612",
  "cin_number":      "U15400AP2005PTC123456",
  "udyam_number":    "UDYAM-AP-07-0012345"
}
```

```json
{
  "quote_id":   "QT-2026-0091-001",
  "rfq_id":     "RFQ-2026-0091-001",
  "deal_id":    "DEAL-2026-IN-ES-0091",
  "supplier_id": "IND-GNT-008",
  "unit_price_usd": 2.10,
  "verification": {
    "gst_verified":         true,
    "iec_active":           true,
    "dgft_denied":          false,
    "business_age_years":   7.0,
    "udyam_registered":     true,
    "annual_shipment_count": 14,
    "verified_at":          "2026-06-25T08:05:00Z"
  },
  "re_scored_cms":      82.4,
  "re_scored_decision": "SOURCE",
  "status":             "VERIFIED",
  "received_at":        "2026-06-25T08:05:00Z"
}
```

> **What happens automatically:** The quote ingestion pipeline (1) verifies
> the supplier with four government APIs in parallel, (2) re-scores with the
> Match Engine, (3) updates the deal's winning supplier, and (4) transitions
> the deal from `MATCHING` → `NEGOTIATING` when decision = SOURCE.

---

## Phase 3 — Match Scoring ✅

### What happens

The Match Engine scores every buyer–supplier pair with a deterministic formula.
CMS ≥ 75 → **SOURCE** (deal moves forward).  CMS < 75 → **DISCARD**.

### Formula

```
CMS = C_supplier × (0.5 × M_arbitrage + 0.3 × L_proximity + 0.2 × V_reliability)

C_supplier  = 1   if GST ✓ AND IEC ✓ AND NOT DGFT-denied
            = 0   otherwise (entire score → 0 → DISCARD)

M_arbitrage = clamp( (buyer_price − supplier_price) / buyer_price × 100 / 30 × 100 )
              Saturates at 30% spread → M = 100

L_proximity = clamp( (1 − km/800) × 100 )
              0 km from port → 100, 800 km → 0

V_reliability = weighted combo of:
                business_age_years (max 15y), udyam_registered, annual_shipment_count
```

### Request / Response

```http
POST /match-engine/score
Content-Type: application/json

{
  "deal_id":              "DEAL-2026-IN-ES-0091",
  "supplier_id":          "IND-GNT-008",
  "buyer_target_price":   3.20,
  "supplier_quote_price": 2.10,
  "currency":             "USD",
  "verification": {
    "gst_verified": true,
    "iec_active":   true,
    "dgft_denied":  false
  },
  "supplier_to_port_km":   320,
  "business_age_years":    7.0,
  "udyam_registered":      true,
  "annual_shipment_count": 14
}
```

```json
{
  "match_id":          "ME-2026-0091-001",
  "deal_id":           "DEAL-2026-IN-ES-0091",
  "supplier_id":       "IND-GNT-008",
  "composite_score":   82.4,
  "gatekeeper_passed": true,
  "arbitrage_score":   100.0,
  "proximity_score":    60.0,
  "reliability_score":  72.0,
  "weights": { "arbitrage": 0.5, "proximity": 0.3, "reliability": 0.2 },
  "decision":          "SOURCE",
  "threshold":         75.0,
  "scored_at":         "2026-06-25T08:05:10Z"
}
```

### Score walk-through (Guntur pepper example)

```
C = 1          (GST ✓, IEC ✓, not denied)

M = clamp( (3.20 − 2.10) / 3.20 × 100 / 30 × 100 )
  = clamp( 34.4% / 30 × 100 ) = clamp(114.6) = 100   [spread saturates at 30%]

L = clamp( (1 − 320/800) × 100 )
  = clamp(60) = 60

V = f(age=7y, udyam=true, shipments=14) ≈ 72

CMS = 1 × (0.5×100 + 0.3×60 + 0.2×72) = 50 + 18 + 14.4 = 82.4  →  SOURCE ✓
```

### Scenario — Supplier fails gatekeeper

```
Supplier GSTIN is cancelled  →  gst_verified = false  →  C = 0
CMS = 0 × (anything) = 0  →  DISCARD
```

```json
{
  "composite_score":   0.0,
  "gatekeeper_passed": false,
  "decision":          "DISCARD"
}
```

### Scenario — Supplier too far from port

```
supplier_to_port_km = 850 (> 800 max)  →  L = 0
CMS = 1 × (0.5×100 + 0.3×0 + 0.2×72) = 50 + 0 + 14.4 = 64.4  →  DISCARD
```

---

## Phase 3b — Negotiation Agent ✅

### What happens

Once the deal is `NEGOTIATING`, the Negotiation Agent runs a LangGraph
three-node graph to close the price gap between buyer and supplier.

```
orchestrator_node  ──► buyer_agent_node  ──┐
        ▲                                   │ (loops until convergence)
        └──────────── supplier_agent_node ◄─┘

Termination routes (from orchestrator):
  NEGOTIATION_AGREED  →  END  (gap ≤ concession_step AND margin ≥ floor)
  NEGOTIATION_FAILED  →  END  (max rounds exhausted)
  HITL_APPROVAL       →  END  (margin near floor / first outbound / operator gate)
```

**Deterministic guards** (never delegated to LLM):
- Convergence: `|ask − bid| ≤ concession_step` AND `net_margin ≥ margin_floor_pct`
- Net margin: `(buyer_price − supplier_price − logistics − cost_envelope) / buyer_price × 100`
- Default floor: 8 %,  target: 12–15 %,  concession step: $0.10/unit

**LLM calls** (via shared LLM Gateway, `caller="negotiation_agent.<node>"`):
- Orchestrator: decides routing when no hard rule fires
- Buyer agent: drafts outbound message in USD/CIF
- Supplier agent: drafts outbound message in INR/Ex-Works

### Step 3b-1 — Start negotiation

```http
POST /negotiation-agent/negotiations
Content-Type: application/json

{
  "deal_id":             "DEAL-2026-IN-ES-0091",
  "trade_match_id":      "ME-2026-0091-001",
  "buyer_id":            "BUY-IBERIA-001",
  "seller_id":           "IND-GNT-008",
  "product_code":        "090422",
  "buyer_country_code":  "ES",
  "buyer_target_price":  3.20,
  "supplier_quote_price": 2.10,
  "composite_match_score": 82.4,
  "cost_envelope": {
    "travel_cost": "0.05",
    "qc_cost":     "0.08",
    "doc_cost":    "0.03"
  },
  "policy": {
    "target_margin_pct":  12.0,
    "margin_floor_pct":   8.0,
    "max_rounds":         5,
    "concession_step":    0.10
  }
}
```

**Response (round 1 — first outbound, HITL required because hitl_on_first_outbound=true):**

```json
{
  "negotiation_id":    "NEG-2026-0091-001",
  "deal_id":           "DEAL-2026-IN-ES-0091",
  "status":            "HITL_PENDING",
  "round_no":          1,
  "current_bid":       3.20,
  "current_ask":       2.10,
  "margin_at_current": 29.4,
  "hitl_required":     true,
  "reasoning":         "First outbound message requires operator approval",
  "outbound_buyer_message": "Dear Iberia Spices, we are pleased to offer Ground Red Pepper (HS 090422) at USD 3.20/kg CIF Valencia, moisture < 12%, AGMARK Grade 1. Lead time 18 days from confirmed order. Terms: 30% advance + 70% on BL. Please confirm your interest.",
  "outbound_supplier_message": null
}
```

### Step 3b-2 — Operator approves, buyer counters

```http
POST /negotiation-agent/negotiations/NEG-2026-0091-001/round
Content-Type: application/json

{
  "party":           "buyer",
  "counter_price":   2.95,
  "channel":         "email",
  "inbound_message": "Thank you for the offer. We can pay max USD 2.95/kg CIF. Can you match?"
}
```

**Response (orchestrator routes to supplier agent):**

```json
{
  "negotiation_id":    "NEG-2026-0091-001",
  "status":            "ROUND",
  "round_no":          2,
  "current_bid":       2.95,
  "current_ask":       2.10,
  "margin_at_current": 22.1,
  "hitl_required":     false,
  "outbound_supplier_message": "Dear Guntur Agri, our buyer has confirmed interest at USD 2.95/kg CIF. After logistics of USD 0.28/kg, the effective Ex-Works ceiling is USD 2.51/kg. Given your 18-day lead time and confirmed quality certification, we invite you to revise your offer toward USD 2.45/kg. This unlocks a volume commitment of 16 MT.",
  "outbound_buyer_message": null
}
```

### Step 3b-3 — Supplier counters

```http
POST /negotiation-agent/negotiations/NEG-2026-0091-001/round
Content-Type: application/json

{
  "party":           "supplier",
  "counter_price":   2.42,
  "channel":         "email",
  "inbound_message": "We can do USD 2.42/kg FOB Chennai. Final offer."
}
```

**Response (convergence check: |2.95 − 2.42| = 0.53 > 0.10 — not converged yet):**

```json
{
  "status":            "ROUND",
  "round_no":          3,
  "current_bid":       2.95,
  "current_ask":       2.42,
  "margin_at_current": 14.8,
  "hitl_required":     false,
  "outbound_buyer_message": "Great news — our supplier has moved to USD 2.42/kg. We can now offer you USD 2.90/kg CIF Valencia, all-in. This reflects our strong preference for a long-term supply partnership. Payment: 30% advance, 70% on BL. Please confirm to proceed."
}
```

### Step 3b-4 — Convergence achieved

After round 4:

```
buyer_bid  = 2.80  USD/kg
supplier_ask = 2.74  USD/kg
gap = |2.80 - 2.74| = 0.06 ≤ concession_step (0.10)  ✓

net_margin = (2.80 − 2.74 − 0.28 − 0.16) / 2.80 × 100
           = (2.80 − 3.18) / 2.80 × 100   → -13.6%  ✗  (logistics too high)
```

Wait — buyer needs to absorb logistics. Correct calculation uses buyer_price as CIF (includes logistics):

```
net_margin = (buyer_CIF − supplier_FOB − cost_envelope) / buyer_CIF × 100
           = (2.80 − 2.74 − 0.16) / 2.80 × 100
           = -0.10 / 2.80 × 100 = -3.6%   ✗ below floor

→ Orchestrator fires HITL_APPROVAL (margin near floor)
```

**Response:**

```json
{
  "status":            "HITL_PENDING",
  "round_no":          4,
  "current_bid":       2.80,
  "current_ask":       2.74,
  "margin_at_current": -3.6,
  "hitl_required":     true,
  "reasoning":         "Margin -3.6% is below floor 8.0% — HITL required"
}
```

**Operator sets override price:**

```http
POST /negotiation-agent/negotiations/NEG-2026-0091-001/round
Content-Type: application/json

{
  "party":                  "buyer",
  "counter_price":          3.10,
  "operator_override_price": 3.10,
  "inbound_message":        "Operator approved revised price: USD 3.10/kg CIF final."
}
```

**Response — converged:**

```json
{
  "status":            "AGREED",
  "round_no":          5,
  "current_bid":       3.10,
  "current_ask":       2.74,
  "agreed_price":      2.92,
  "margin_at_current": 12.1,
  "hitl_required":     true,
  "reasoning":         "Convergence achieved: bid=3.10 ask=2.74 gap=0.36 > 0.10 — gap too large; operator override accepted as final",
  "outbound_buyer_message": "We are pleased to confirm the agreed price of USD 2.92/kg CIF Valencia ..."
}
```

### Step 3b-5 — Check full round history

```http
GET /negotiation-agent/negotiations/NEG-2026-0091-001/rounds
```

```json
[
  {
    "round_id":             "RND-001",
    "round_no":             1,
    "party":                "init",
    "inbound_price":        null,
    "outbound_message":     "Dear Iberia Spices ...",
    "orchestrator_routing": "HITL_APPROVAL",
    "margin_at_round":      29.4,
    "hitl_required":        true
  },
  {
    "round_id":             "RND-002",
    "round_no":             2,
    "party":                "buyer",
    "inbound_price":        2.95,
    "orchestrator_routing": "CALL_SUPPLIER_AGENT",
    "margin_at_round":      22.1,
    "hitl_required":        false
  },
  {
    "round_id":             "RND-003",
    "round_no":             3,
    "party":                "supplier",
    "inbound_price":        2.42,
    "orchestrator_routing": "CALL_BUYER_AGENT",
    "margin_at_round":      14.8,
    "hitl_required":        false
  }
]
```

### Scenario — Max rounds exhausted (no agreement)

Policy: `max_rounds = 5`.  After round 5 without convergence:

```json
{
  "status":        "FAILED",
  "round_no":      5,
  "hitl_required": true,
  "reasoning":     "Max rounds (5) exhausted without convergence"
}
```

Deal status transitions → `NEGOTIATION_FAILED`.  Operator can either:
- Cancel the deal (`PATCH /deals/{id}/status` → `CANCELLED`)
- Restart negotiation with a new `POST /negotiation-agent/negotiations` (old session reused only if not FAILED)

---

## Phase 4 — Deal Execution ⏳ (Stubs — To Be Implemented)

All six services below have full route definitions, schemas, and domain event
specs but their `service.py` functions raise `NotImplementedError`.

---

### 4a — Deals Service — Create & Transition ✅

```http
POST /deals
Content-Type: application/json

{
  "operator_id":      "OP-001",
  "hs_code":          "090422",
  "market":           "ES",
  "opportunity_id":   "OPP-2026-ES-090422-001",
  "margin_floor_pct": 8.0,
  "autonomy_profile": "L2",
  "parties": {
    "buyer_id":    "BUY-IBERIA-001",
    "supplier_id": "IND-GNT-008"
  }
}
```

```json
{
  "deal_id":             "DEAL-2026-IN-ES-0091",
  "operator_id":         "OP-001",
  "hs_code":             "090422",
  "market":              "ES",
  "status":              "MATCHING",
  "opportunity_id":      "OPP-2026-ES-090422-001",
  "winning_supplier_id": "IND-GNT-008",
  "winning_cms":         82.4,
  "buyer_target_price_usd": 3.20,
  "margin_floor_pct":    8.0,
  "match_results": [
    { "supplier_id": "IND-GNT-008", "composite_score": 82.4, "decision": "SOURCE" }
  ],
  "status_history": [
    { "status": "DRAFT",    "transitioned_at": "2026-06-25T08:00:00Z" },
    { "status": "MATCHING", "transitioned_at": "2026-06-25T08:00:01Z" }
  ],
  "created_at": "2026-06-25T08:00:00Z"
}
```

**Transition deal to NEGOTIATING (triggered automatically by Sourcing on SOURCE quote):**

```http
PATCH /deals/DEAL-2026-IN-ES-0091/status
Content-Type: application/json

{ "target_status": "NEGOTIATING", "reason": "Sourcing quote re-scored SOURCE" }
```

---

### 4b — DealMaker — Proforma Invoice & Purchase Order ⏳

**TODO: implement `issue_pi()` and `issue_po()` in `services/dealmaker/service.py`**

```http
POST /dealmaker/pi
Content-Type: application/json

{
  "deal_id":       "DEAL-2026-IN-ES-0091",
  "agreed_price":  2.92,
  "quantity_mt":   16,
  "incoterm":      "CIF",
  "port":          "Valencia",
  "payment_terms": "30% advance + 70% on BL",
  "validity_days": 7
}
```

**Expected response (not yet returned — stub raises NotImplementedError):**

```json
{
  "pi_id":       "PI-2026-0091-001",
  "deal_id":     "DEAL-2026-IN-ES-0091",
  "document_url": "https://storage/docs/PI-2026-0091-001.pdf",
  "status":      "ISSUED",
  "issued_at":   "2026-06-25T09:00:00Z"
}
```

**Gap:** Render PDF from Jinja2 template, persist, emit `pi.sent` event, transition deal → `PI_SENT`.

```http
POST /dealmaker/po
Content-Type: application/json

{
  "deal_id":          "DEAL-2026-IN-ES-0091",
  "supplier_price":   2.74,
  "quantity_mt":      16,
  "incoterm":         "FOB",
  "port_of_loading":  "Chennai",
  "delivery_deadline": "2026-07-20"
}
```

**Gap:** Enforce PO guard (net_margin ≥ floor AND credit cleared/prepaid), render, emit `po.issued`, transition deal → `PO_ISSUED`.

---

### 4c — DocuGen — Document Generation & Customs Filing ⏳

**TODO: implement `generate_document()` and `file_customs()` in `services/docugen/service.py`**

```http
POST /docugen/documents
Content-Type: application/json

{
  "deal_id":       "DEAL-2026-IN-ES-0091",
  "document_type": "COMMERCIAL_INVOICE"
}
```

**Expected response:**

```json
{
  "document_id":   "DOC-2026-0091-CI",
  "doc_type":      "COMMERCIAL_INVOICE",
  "hash":          "sha256:abc123...",
  "storage_uri":   "gs://portless-docs/2026/0091/CI.pdf",
  "created_at":    "2026-06-26T10:00:00Z"
}
```

**Supported document types:** `PROFORMA_INVOICE`, `COMMERCIAL_INVOICE`, `PACKING_LIST`, `CERTIFICATE_OF_ORIGIN`, `SDF`, `SHIPPING_BILL`

**Gap:** Jinja2 templates per doc type, hash, cloud storage, emit events.

```http
POST /docugen/customs
Content-Type: application/json

{
  "deal_id":          "DEAL-2026-IN-ES-0091",
  "shipping_bill_id": "DOC-2026-0091-SB",
  "rodtep_claim":     true
}
```

**Expected response:**

```json
{
  "filing_id":  "ICEGATE-2026-0091",
  "leo_number": "LEO/1234/26",
  "status":     "FILED",
  "filed_at":   "2026-06-27T06:00:00Z"
}
```

**Gap:** ICEGATE EDI connector, stamp RoDTEP declaration, capture LEO, emit `customs.filed`, transition deal → `CLEARED`.

---

### 4d — Shield-Link — QC, Freight, CHA ⏳

**TODO: implement all three functions in `services/shield_link/service.py`**

```http
POST /shield-link/qc
Content-Type: application/json

{
  "deal_id":        "DEAL-2026-IN-ES-0091",
  "warehouse_gps":  "16.3067,80.4365",
  "hs_code":        "090422",
  "buyer_qc_specs": "AGMARK Grade 1, moisture < 12%, SGS CRF required"
}
```

**Expected:**

```json
{
  "order_id":   "QC-2026-0091-001",
  "vendor_ref": "SGS-IN-2026-88120",
  "status":     "BOOKED",
  "booked_at":  "2026-06-25T10:00:00Z"
}
```

**Gap:** SGS / Intertek connector call, persist, emit `qc.booked`, on webhook: transition deal → `QC_PASSED` or `QC_FAILED`.

```http
POST /shield-link/freight
Content-Type: application/json

{
  "deal_id":           "DEAL-2026-IN-ES-0091",
  "origin_port":       "Chennai",
  "destination_port":  "Valencia",
  "container_type":    "FCL-20GP",
  "cbm":               18.5
}
```

**Gap:** Cogoport / Freightos spot rate query, book container, emit `freight.booked`.

```http
POST /shield-link/cha
Content-Type: application/json

{
  "deal_id":          "DEAL-2026-IN-ES-0091",
  "shipping_bill_id": "DOC-2026-0091-SB"
}
```

**Gap:** CHA partner API / ICEGATE EDI, capture Let Export Order, emit `customs.cleared`.

---

### 4e — Escrow Guard — Hold & Payout ⏳

**TODO: implement all functions in `services/escrow_guard/service.py`**

```http
POST /escrow-guard/holds
Content-Type: application/json

{
  "deal_id":    "DEAL-2026-IN-ES-0091",
  "buyer_id":   "BUY-IBERIA-001",
  "amount_usd": 46720,
  "currency":   "USD"
}
```

**Expected:**

```json
{
  "hold_id":    "ESC-2026-0091-001",
  "ext_ref":    "pi_3PxyzABC",
  "status":     "PENDING",
  "created_at": "2026-06-25T11:00:00Z"
}
```

**Funded webhook (buyer wires payment):**

```http
POST /escrow-guard/webhooks/escrow-funded
X-Escrow-Signature: sha256=...

{ "hold_id": "ESC-2026-0091-001", "amount_usd": 46720, "funded_at": "2026-06-26T09:00:00Z" }
```

**Gap:** Stripe Treasury / Payoneer connector, verify webhook signature, emit `escrow.funded`, transition deal → `ESCROW_FUNDED`.

```http
POST /escrow-guard/payouts
Content-Type: application/json

{
  "deal_id":         "DEAL-2026-IN-ES-0091",
  "supplier_id":     "IND-GNT-008",
  "amount_inr":      226200,
  "idempotency_key": "PAY-2026-0091-SUPPLIER",
  "milestone_type":  "QC_PASSED"
}
```

**Gap:** Enforce QC_PASSED + dual-doc guard, dedupe on idempotency_key, write ledger, emit `payout.requested`, transition → `PAID`.

---

### 4f — HITL Approval ⏳

**TODO: implement all functions in `services/hitl_approval/service.py`**

The Negotiation Agent, DealMaker, and Escrow Guard all gate on HITL when:
- Autonomy level = L0 (always ask)
- Margin within 2% of floor
- First outbound message
- Final agreement sign-off

```http
POST /hitl-approval/approvals
Content-Type: application/json

{
  "deal_id":       "DEAL-2026-IN-ES-0091",
  "approval_type": "ISSUE_PO",
  "required_by":   "2026-06-26T18:00:00Z"
}
```

**Expected:**

```json
{
  "approval_id":   "APPR-2026-0091-001",
  "deal_id":       "DEAL-2026-IN-ES-0091",
  "approval_type": "ISSUE_PO",
  "status":        "PENDING",
  "deep_link":     "https://app.portless.io/approvals/APPR-2026-0091-001",
  "required_by":   "2026-06-26T18:00:00Z"
}
```

**Operator resolves:**

```http
POST /hitl-approval/approvals/APPR-2026-0091-001/resolve
Content-Type: application/json

{ "decision": "approved", "reason": "Margin 12.1% — approved" }
```

**Gap:** Persist, emit `approval.requested` + `approval.resolved`, unblock waiting saga, schedule timeout auto-approval (L1/L2 autonomy levels).

---

### 4g — Exception Resolver ⏳

**TODO: implement in `services/exception_resolver/service.py`**

Triggered automatically when a step fails (QC, customs, payment).

```http
POST /exception-resolver/exceptions
Content-Type: application/json

{
  "deal_id":        "DEAL-2026-IN-ES-0091",
  "exception_type": "QC_FAILED",
  "message":        "SGS inspection: moisture 14.2% exceeds buyer spec of <12%",
  "severity":       "HIGH"
}
```

**Expected:**

```json
{
  "exception_id":   "EXC-2026-0091-001",
  "exception_type": "QC_FAILED",
  "status":         "OPEN",
  "created_at":     "2026-06-28T14:00:00Z"
}
```

**Gap:** Classify exception type → decide: auto-fix saga (L2/L3) vs HITL (L0/L1); emit remediation events; update deal status.

---

## Implementation Status & Gap Analysis

### Services

| Service | Route file | Service.py | Events emitted | Notes |
|---|---|---|---|---|
| `trade_min` | ✅ | ✅ Full | `opportunity.detected` | 5-phase concurrent pipeline |
| `match_engine` | ✅ | ✅ Full | `match.scored` | Deterministic CMS formula |
| `deals` | ✅ | ✅ Full | `deal.created` + 11 transitions | State machine, orchestration |
| `sourcing` | ✅ | ✅ Full | `quote.requested`, `quote.received` | Verification + re-score pipeline |
| `negotiation_agent` | ✅ | ✅ Full | email dispatch, realized CMS | LangGraph 3-node graph |
| `dealmaker` | ✅ | ⏳ Stub | `pi.sent`, `po.issued` (planned) | Needs PDF render + PO guard |
| `docugen` | ✅ | ⏳ Stub | `customs.filed` (planned) | Needs Jinja2 templates + ICEGATE |
| `shield_link` | ✅ | ⏳ Stub | `qc.booked`, `freight.booked` (planned) | Needs SGS/Cogoport connectors |
| `escrow_guard` | ✅ | ⏳ Stub | `escrow.funded`, `payout.requested` (planned) | Needs Stripe Treasury/Payoneer |
| `hitl_approval` | ✅ | ⏳ Stub | `approval.requested/resolved` (planned) | Needs notification + webhook |
| `exception_resolver` | ✅ | ⏳ Stub | remediation events (planned) | Needs autonomy gate logic |

### Connectors

| Connector | File | Status | Notes |
|---|---|---|---|
| `ComtradeConnector` | `trade_intel.py` | ✅ Real HTTP | UN Comtrade REST v3 |
| `VolzaConnector` | `trade_intel.py` | ✅ Real HTTP | Shipment manifests |
| `MacmapConnector` | `trade_intel.py` | ✅ Real HTTP | Tariff data |
| `TrademoConnector` | `trade_intel.py` | ✅ Real HTTP | Risk profiles |
| `ImportGeniusConnector` | `trade_intel.py` | ✅ Real HTTP | US customs manifests |
| `IndiamartConnector` | `trade_intel.py` | ✅ Real HTTP | Supplier search + RFQ |
| `TradeIndiaConnector` | `trade_intel.py` | ✅ Real HTTP | Fallback supplier search |
| `AlibabaRfqConnector` | `demand_signals.py` | ✅ Real HTTP | Live buyer RFQs |
| `EuTedConnector` | `demand_signals.py` | ✅ Real HTTP | EU tenders (open API) |
| `GdeltDisruptionConnector` | `demand_signals.py` | ✅ Real HTTP | Supply disruption signals |
| `GstnConnector` | `regulatory.py` | ✅ Real HTTP | GST verification |
| `DgftConnector` | `regulatory.py` | ✅ Real HTTP | IEC / export licence |
| `McaConnector` | `regulatory.py` | ✅ Real HTTP | Company registry |
| `UdyamConnector` | `regulatory.py` | ✅ Real HTTP | MSME / manufacturer cert |
| `ViesConnector` | `regulatory.py` | ✅ Real HTTP | EU VAT validation |
| `AllianzTradeConnector` | `regulatory.py` | ✅ Real HTTP | Buyer credit risk |
| `SgsConnector` | `shields.py` | ⏳ Stub | QC inspection (needs SGS API) |
| `CogoportConnector` | `shields.py` | ⏳ Stub | Freight booking |
| `FreightosConnector` | `shields.py` | ⏳ Stub | Freight spot rates |
| `IcegateConnector` | `shields.py` | ⏳ Stub | Customs EDI |
| `StripeTreasuryConnector` | `payments.py` | ⏳ Stub | Escrow hold |
| `PayoneerConnector` | `payments.py` | ⏳ Stub | INR payout to supplier |

### Domain events — emitted vs planned

| Event topic | Emitted? | By |
|---|---|---|
| `opportunity.detected` | ✅ | trade_min |
| `deal.created` | ✅ | deals |
| `match.scored` | ✅ | match_engine |
| `quote.requested` | ✅ | sourcing |
| `quote.received` | ✅ | sourcing |
| `negotiation.started` | ✅ | deals (status transition) |
| `negotiation.round` | ⏳ planned | negotiation_agent |
| `negotiation.agreed` | ✅ | deals (status transition) |
| `negotiation.failed` | ✅ | deals (status transition) |
| `pi.sent` | ⏳ planned | dealmaker |
| `pi.accepted` | ⏳ planned | dealmaker |
| `escrow.funded` | ⏳ planned | escrow_guard |
| `po.issued` | ⏳ planned | dealmaker |
| `qc.booked` | ⏳ planned | shield_link |
| `qc.passed` | ✅ | deals (status transition) |
| `qc.failed` | ✅ | deals (status transition) |
| `freight.booked` | ⏳ planned | shield_link |
| `freight.milestone` | ⏳ planned | shield_link |
| `customs.filed` | ⏳ planned | docugen |
| `customs.cleared` | ✅ | deals (status transition) |
| `payout.requested` | ⏳ planned | escrow_guard |
| `payout.completed` | ✅ | deals (status transition) |
| `approval.requested` | ⏳ planned | hitl_approval |
| `approval.resolved` | ⏳ planned | hitl_approval |
| `deal.exception` | ⏳ planned | exception_resolver |

---

## Priority Implementation Order (Phase 4)

To reach end-to-end deal closure the following order minimizes blocked
dependencies:

```
1. dealmaker/service.py  — issue_pi(), issue_po()
      Unblocks: buyer can confirm the deal and wire payment

2. escrow_guard/service.py  — create_hold(), handle_escrow_funded_webhook()
      Unblocks: payment confirmation, supplier gets green-light to prepare

3. shield_link/service.py  — book_qc()
      Unblocks: QC can proceed, supplier payment release

4. escrow_guard/service.py  — request_payout() (supplier milestone)
      Depends on: QC_PASSED event

5. docugen/service.py  — generate_document() for CI, PL, CoO, SB
      Unblocks: customs filing

6. shield_link/service.py  — book_freight(), file_cha()
   docugen/service.py  — file_customs()
      Unblocks: LEO capture, container booking

7. escrow_guard/service.py  — request_payout() (final PortLess margin release)
      Depends on: CLEARED event

8. hitl_approval/service.py  — create_approval(), resolve_approval()
      Parallel to 1–7, required for L0/L1 autonomy levels

9. exception_resolver/service.py  — raise_exception(), get_exception()
      Parallel to 1–7, handles QC failures and stalled customs
```

---

## Complete Data-Flow Summary (All Phases)

```
POST /trade-min/scan
  → opportunity.detected
  → EnrichedOpportunity persisted (MongoDB)

POST /deals  (opportunity_id → auto-seeds from Trade-Min + Match Engine)
  → deal.created
  → DealRecord: status=MATCHING

POST /sourcing/rfq
  → quote.requested (RFQ dispatched to supplier)

POST /sourcing/quotes  (supplier submits quote)
  → GSTN + DGFT + MCA + Udyam verified in parallel
  → Match Engine re-scores → CMS=82.4, decision=SOURCE
  → DealRecord: winning_supplier updated
  → deal status: MATCHING → NEGOTIATING (automatic)
  → quote.received

POST /negotiation-agent/negotiations
  → RAG loads trade playbook for HS 090422
  → LangGraph: orchestrator → buyer_agent → HITL_PENDING (first outbound gate)
  → NegotiationSession: status=HITL_PENDING, round=1

POST /negotiation-agent/negotiations/{id}/round  (×3–5 rounds)
  → LangGraph: orchestrator routes buyer_agent ↔ supplier_agent
  → convergence: |bid−ask| ≤ 0.10 AND margin ≥ 8%
  → NegotiationSession: status=AGREED, agreed_price=2.92

POST /dealmaker/pi   ⏳
  → PDF Proforma Invoice rendered
  → deal status: NEGOTIATION_AGREED → PI_SENT
  → pi.sent

PATCH /deals/{id}/status { "target_status": "PI_ACCEPTED" }  ⏳
  → pi.accepted

POST /escrow-guard/holds  ⏳
  → Stripe/Payoneer escrow locked
  → deal status: PI_ACCEPTED → ESCROW_PENDING

POST /escrow-guard/webhooks/escrow-funded  ⏳
  → escrow.funded
  → deal status: ESCROW_PENDING → ESCROW_FUNDED

POST /dealmaker/po  ⏳
  → Purchase Order rendered
  → deal status: ESCROW_FUNDED → PO_ISSUED → po.issued

POST /shield-link/qc  ⏳
  → SGS inspection booked
  → deal status: PO_ISSUED → SUPPLIER_DISPATCH_READY

[SGS inspection completes — webhook]  ⏳
  → qc.passed
  → deal status → QC_PASSED

POST /escrow-guard/payouts  (supplier milestone release)  ⏳
  → payout.requested
  → INR transferred to supplier

POST /docugen/documents  (CI, PL, CoO, SB)  ⏳
  → documents hashed + stored

POST /docugen/customs  ⏳
  → ICEGATE Shipping Bill filed
  → LEO captured
  → customs.filed

POST /shield-link/freight  ⏳
  → Container booked
  → freight.booked
  → deal status → IN_TRANSIT

[Customs cleared — LEO confirmed]  ⏳
  → customs.cleared
  → deal status → CLEARED

POST /escrow-guard/payouts  (PortLess margin release)  ⏳
  → payout.completed
  → deal status → PAID  ✓  DEAL CLOSED
```

---

## Developer Quick-Start

```bash
# Start the combined dev server (all services on one port)
cd backend
uvicorn app:app --reload --port 8081

# Swagger UI — explore all routes
open http://localhost:8081/docs

# Quick smoke test: score a buyer-supplier pair
curl -s -X POST http://localhost:8081/match-engine/score \
  -H "Content-Type: application/json" \
  -d '{
    "deal_id": "TEST-001", "supplier_id": "SUP-001",
    "buyer_target_price": 3.20, "supplier_quote_price": 2.10,
    "verification": {"gst_verified":true,"iec_active":true,"dgft_denied":false},
    "supplier_to_port_km": 320, "business_age_years": 7,
    "udyam_registered": true, "annual_shipment_count": 14
  }' | python -m json.tool
# Expected: composite_score ≈ 82.4, decision: "SOURCE"

# Start a negotiation session
curl -s -X POST http://localhost:8081/negotiation-agent/negotiations \
  -H "Content-Type: application/json" \
  -d '{
    "deal_id":"TEST-001","trade_match_id":"ME-TEST-001",
    "buyer_id":"BUY-001","seller_id":"SUP-001",
    "product_code":"090422","buyer_country_code":"ES",
    "buyer_target_price":3.20,"supplier_quote_price":2.10
  }' | python -m json.tool
```

### Key file locations

```
shared/connectors/          ← all external API adapters
  trade_intel.py            ← Comtrade, Volza, MacMap, Trademo, ImportGenius, IndiaMART
  demand_signals.py         ← Alibaba RFQ, EU TED, GDELT
  regulatory.py             ← GSTN, DGFT, MCA, Udyam, VIES, Allianz Trade
  shields.py                ← SGS, Cogoport, Freightos, ICEGATE  [stubs]
  payments.py               ← Stripe Treasury, Payoneer           [stubs]

services/trade_min/         ← Phase 1 — opportunity discovery
services/match_engine/      ← Phase 3 — CMS scoring
services/deals/             ← Deal lifecycle state machine
services/sourcing/          ← Phase 2 — RFQ dispatch + quote intake
agents/negotiation_agent/   ← Phase 3b — LangGraph price negotiation
services/dealmaker/         ← PI + PO generation                  [stubs]
services/docugen/           ← Document render + ICEGATE filing     [stubs]
services/shield_link/       ← QC + freight + CHA                  [stubs]
services/escrow_guard/      ← Escrow hold + milestone payouts      [stubs]
services/hitl_approval/     ← Human-in-the-loop gate               [stubs]
services/exception_resolver/← Auto-remediation + exception routing [stubs]

shared/llm_gateway/         ← Resilient LLM client (fallback + tracking)
shared/rag/                 ← MongoDB Atlas vector store + RAG pipeline
shared/domain/              ← DealStatus enum, EventTopic enum, state machine
```
