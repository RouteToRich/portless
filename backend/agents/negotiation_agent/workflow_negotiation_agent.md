# Negotiation Agent — Workflow Deep-Dive

> **Module:** `agents/negotiation_agent/`  
> **Engine:** LangGraph (three-node async state machine)  
> **LLM:** Shared LLM Gateway → Claude Sonnet (primary) → Groq fallback chain  
> **Persistence:** MongoDB Motor (in-memory fallback for dev/CI)

---

## Table of Contents

1. [Big Picture](#1-big-picture)
2. [Real Scenario End-to-End](#2-real-scenario-end-to-end)
3. [LangGraph Pipeline — Node by Node](#3-langgraph-pipeline--node-by-node)
4. [Orchestration Logic — How Routing Decisions Are Made](#4-orchestration-logic--how-routing-decisions-are-made)
5. [Data Persistence — What Is Stored and When](#5-data-persistence--what-is-stored-and-when)
6. [Waiting States — How the Agent Pauses for Buyer or Seller](#6-waiting-states--how-the-agent-pauses-for-buyer-or-seller)
7. [Email Lifecycle — Draft → HITL Gate → Sent](#7-email-lifecycle--draft--hitl-gate--sent)
8. [Viewing All Emails in the UI](#8-viewing-all-emails-in-the-ui)
9. [RAG Knowledge and Trade Playbooks](#9-rag-knowledge-and-trade-playbooks)
10. [Stop vs Continue — Convergence and Termination Rules](#10-stop-vs-continue--convergence-and-termination-rules)
11. [HITL Gates — Human-in-the-Loop Checkpoints](#11-hitl-gates--human-in-the-loop-checkpoints)
12. [Status Reference](#12-status-reference)
13. [Configuration Reference](#13-configuration-reference)
14. [Layman's Guide — Plain English with Real-World Scenarios](#14-laymans-guide--plain-english-with-real-world-scenarios)
15. [Implementation Status](#15-implementation-status)

---

## 1. Big Picture

```
Deal reaches NEGOTIATING status
         │
         ▼
POST /negotiation-agent/negotiations   ← start_negotiation()
         │
         ├─ load RAG context (HS code lookup)
         ├─ create NegotiationSession in DB (status=STARTED)
         │
         ▼
  ┌──────────────────────────────────────────────────┐
  │           LangGraph State Machine                │
  │                                                  │
  │   [Orchestrator Node]                            │
  │       │   deterministic guards fire first        │
  │       │   (convergence? max rounds? margin?)     │
  │       │   then optional LLM routing              │
  │       │                                          │
  │   ┌───┴────────────────────┐                     │
  │   │                        │                     │
  │   ▼                        ▼                     │
  │ [BuyerAgent Node]   [SupplierAgent Node]         │
  │   LLM drafts msg      LLM drafts msg             │
  │   sets new bid        sets new ask               │
  │   └──────┬────────────────┘                      │
  │          └──── loops back to Orchestrator        │
  │                                                  │
  │   Terminal exits: AGREED | FAILED | HITL_PENDING │
  └──────────────────────────────────────────────────┘
         │
         ├─ save updated NegotiationSession
         ├─ save NegotiationRound record
         ├─ create email draft(s) → HITL gate
         └─ emit negotiation.round domain event
         │
         ▼
   Agent waits (session persisted in DB, status=ROUND or HITL_PENDING)
         │
         ▼  (buyer or seller replies via email or portal)
POST /negotiation-agent/negotiations/{id}/round  ← run_round()
         │
         └─ loops back into LangGraph
```

PortLess acts as a **neutral intermediary**: the Buyer-Facing Agent always talks CIF (buyer's view) and the Supplier-Facing Agent always talks Ex-Works (seller's view). Neither party ever sees the other's real price, PortLess's margin, or the BATNA.

---

## 2. Real Scenario End-to-End

### Scenario: 500 MT Rice (HS 1006), India Buyer ↔ Thailand Supplier

**Setup:**
- Buyer `BYR-001` (Mumbai) wants to buy 500 MT of 5% broken Thai white rice.
- Buyer's target CIF price: **$430/MT**
- Supplier `SUP-007` (Bangkok) quotes Ex-Works: **$480/MT**
- Logistics (CIF Mumbai): $20/MT
- QC (SGS): $3/MT, Docs: $2/MT → cost envelope total $25/MT
- Policy: margin floor 8%, target margin 12%, max 5 rounds, concession step $0.10

**Net margin formula:**
```
margin% = (buyer_price - supplier_price - logistics - cost_envelope) / buyer_price × 100
Initial = (430 - 480 - 20 - 25) / 430 × 100 = −22%  ← well below floor, negotiation needed
```

---

### Round 0 — Negotiation Start

```
POST /negotiation-agent/negotiations
{
  "deal_id": "DEAL-2025-001",
  "trade_match_id": "TM-7842",
  "buyer_id": "BYR-001",
  "seller_id": "SUP-007",
  "product_code": "1006",
  "buyer_country_code": "IN",
  "buyer_target_price": 430.0,
  "supplier_quote_price": 480.0,
  "cost_envelope": { "travel_cost": 20, "qc_cost": 3, "doc_cost": 2 },
  "policy": {
    "target_margin_pct": 12.0,
    "margin_floor_pct": 8.0,
    "max_rounds": 5,
    "concession_step": 0.10
  },
  "batna": "Source from Vietnam at $435 CIF if deal fails"
}
```

**What happens internally:**

1. `start_negotiation()` checks if an active session already exists for `DEAL-2025-001` → none found.
2. `load_rag_context("1006")` queries MongoDB Atlas vector store with: *"Negotiation tactics, price benchmarks, payment terms for HS code 1006."*  
   Returns: *"Rice (HS 1006): Global benchmark is Thailand 5% broken (FOB Bangkok). Standard payment: LC at sight. Lead time: 21–30 days. Concession rhythm: supplier opens +3–5%, buyer counters −8%..."*
3. `NegotiationSession` created and saved:
   ```
   negotiation_id: "neg-3f8a1..."
   status: STARTED
   round_no: 0
   current_bid: 430.0
   current_ask: 480.0
   rag_context: "<playbook text>"
   ```
4. LangGraph fires with `is_first_round=True`.
5. **Orchestrator** checks: `hitl_on_first_outbound=True` → routes to `HITL_APPROVAL`.
6. Session saved: `status=HITL_PENDING, round_no=1, hitl_required=True`.
7. A HITL approval `SEND_NEGOTIATION_EMAIL` is created for operator review.

**Response:**
```json
{
  "negotiation_id": "neg-3f8a1...",
  "status": "HITL_PENDING",
  "round_no": 1,
  "current_bid": 430.0,
  "current_ask": 480.0,
  "hitl_required": true,
  "reasoning": "First outbound message requires operator approval"
}
```

---

### Round 1 — Operator Approves, Supplier Replies $460

Operator approves the draft email in the HITL dashboard → email sent to buyer.  
Supplier replies by email: "We can do $460/MT Ex-Works." Inbox monitor (or operator-submitted round) ingests this.

```
POST /negotiation-agent/negotiations/neg-3f8a1.../round
{
  "counter_price": 460.0,
  "party": "supplier",
  "channel": "email",
  "inbound_message": "We can offer $460/MT Ex-Works for 500 MT, LC at sight, 21 days.",
  "attachment_refs": []
}
```

**LangGraph run:**

| Step | What happens |
|------|-------------|
| `run_round()` | `session.current_ask = 460.0` |
| Orchestrator | `margin = (430−460−20−25)/430 × 100 = −17%` — still negative, no convergence, not near floor; LLM routes to `CALL_BUYER_AGENT` |
| BuyerAgent | RAG context says "buyer counters −8%". LLM computes midpoint $(430+460)/2 = 445$, drafts: *"Thank you for your revised offer of $460. We propose $445/MT CIF as our counter — this includes our QC guarantee and reliable 21-day delivery."* |
| State update | `current_bid = 445.0` |
| Email drafted | `EMAIL-A2B3...` status=`PENDING_APPROVAL` for party=buyer |
| Round saved | `round_no=2, party=supplier, inbound_price=460, orchestrator_routing=CALL_BUYER_AGENT` |

---

### Round 2 — Buyer Replies $445

```
POST /negotiation-agent/negotiations/neg-3f8a1.../round
{
  "counter_price": 445.0,
  "party": "buyer",
  "inbound_message": "We accept $445/MT CIF provided quality meets FSSAI standards."
}
```

**LangGraph run:**

| Step | What happens |
|------|-------------|
| Orchestrator | `margin = (445−460−20−25)/445 × 100 = −13.4%` — still negative; last party=buyer → routes `CALL_SUPPLIER_AGENT` |
| SupplierAgent | `implied_max = 445 × (1 − 0.08) − 20 − 25 = $364` — WAY below supplier's ask. LLM acknowledges the gap: *"Thank you for your interest. Our current cost structure means $450 is our minimum. We propose $450/MT Ex-Works with MOQ 500 MT and FSSAI-compliant certification."* |
| State update | `current_ask = 450.0` |

---

### Round 3 — Supplier Replies $450

```
POST /negotiation-agent/negotiations/neg-3f8a1.../round
{
  "counter_price": 450.0,
  "party": "supplier",
  "inbound_message": "Final offer: $450/MT Ex-Works, FSSAI cert included, 21-day delivery."
}
```

**Margin check:**
```
margin = (445 - 450 - 20 - 25) / 445 × 100 = -11.2%  ← still negative
```
Gap = |450 − 445| = 5.0 > concession_step (0.10) → no convergence yet.

BuyerAgent counters with $447. SupplierAgent counters with $447.05 next round.

---

### Round 4 — Convergence at $447

After further exchanges:
- `current_bid = 447.0`
- `current_ask = 447.05`

**Orchestrator convergence check:**
```
gap = |447.05 - 447.0| = 0.05 ≤ concession_step (0.10) ✓
margin = (447 - 447.05 - 20 - 25) / 447 × 100 = -10.1%  ← still negative!
```

`check_convergence()` requires BOTH gap ≤ step AND margin ≥ floor. Margin is negative → convergence **not** declared. Orchestrator routes to `HITL_APPROVAL` (operator must decide):

```
"reasoning": "Max rounds (5) exhausted without convergence — escalating to HITL"
```

**Operator options:**
1. Override price manually to $447 and accept: sends `operator_override_price=447.0` in a new round.
2. Reject → deal transitions to `NEGOTIATION_FAILED`.

---

### Operator Override → AGREED

```
POST /negotiation-agent/negotiations/neg-3f8a1.../round
{
  "counter_price": 447.0,
  "party": "buyer",
  "operator_override_price": 447.0
}
```

Because `operator_override_price` is set, the orchestrator sees the override flag and routes directly to `NEGOTIATION_AGREED`:
- `agreed_price = 447.0`
- Session saved: `status=AGREED`
- `_compute_realized_cms()` calls Match Engine to recompute the final CMS score.
- Domain event `negotiation.agreed` published.
- Deal transitions to `NEGOTIATION_AGREED` → dealmaker issues a PI.

---

## 3. LangGraph Pipeline — Node by Node

```
┌─────────────────────────────────────────────────────┐
│                 NegotiationGraphState               │
│  (TypedDict passed between all nodes)               │
│                                                     │
│  session: NegotiationSession   # full session doc   │
│  current_bid: float            # buyer's latest     │
│  current_ask: float            # supplier's latest  │
│  is_first_round: bool                               │
│  party: "buyer"|"supplier"|"init"                   │
│  inbound_message: str|None     # their reply text   │
│  operator_override_price: float|None                │
│  routing: AgentRouting         # set by orchestrator│
│  buyer_outbound_message: str   # set by buyer node  │
│  supplier_outbound_message: str                     │
│  hitl_required: bool                                │
│  agreed_price: float|None                           │
│  reasoning: str                                     │
└─────────────────────────────────────────────────────┘
```

### Node 1: `orchestrator_node`

**Runs first every time.** Applies deterministic guards in strict priority order — the LLM is only consulted if no guard fires:

```
Priority 1: check_convergence()      → NEGOTIATION_AGREED
Priority 2: round_no >= max_rounds   → NEGOTIATION_FAILED
Priority 3: is_near_margin_floor()   → HITL_APPROVAL
Priority 4: is_first_round + config  → HITL_APPROVAL
Priority 5: LLM routing call         → CALL_BUYER_AGENT | CALL_SUPPLIER_AGENT
```

The LLM payload includes the full `rag_context` string so the orchestrator understands product-specific norms when making its routing recommendation.

### Node 2: `buyer_agent_node`

Drafts a message to the **buyer**. Key behaviors:
- Computes midpoint and presents a counter at or above it (never reveals cost floor).
- Uses RAG context to anchor the message (e.g., "Thailand benchmark is $xxx").
- Signals "final offer" when `concession_budget_remaining < 0.5 × concession_step`.
- Outputs `counter_price_usd`, `outbound_message`, `negotiated_terms` (JSON).
- Updates `current_bid` in the state.

### Node 3: `supplier_agent_node`

Drafts a message to the **supplier**. Key behaviors:
- Computes `implied_max = bid × (1 − floor%) − logistics − cost_envelope`.
- Never reveals the buyer's actual bid or PortLess margin.
- Uses `inbound_supplier_message` to acknowledge their reply before countering.
- Updates `current_ask` in the state.

### Edge routing

```python
def route_after_orchestrator(state) -> str:
    return {
        "CALL_BUYER_AGENT":    "buyer_agent",
        "CALL_SUPPLIER_AGENT": "supplier_agent",
        "HITL_APPROVAL":       "__end__",
        "NEGOTIATION_AGREED":  "__end__",
        "NEGOTIATION_FAILED":  "__end__",
    }[state["routing"]]
```

After either agent node runs, the graph loops back to the orchestrator for the next convergence check. This loop continues **within the same `ainvoke()` call** until a terminal routing is reached. There is no inter-round loop inside LangGraph — each HTTP call to `run_round()` triggers exactly one `ainvoke()`.

---

## 4. Orchestration Logic — How Routing Decisions Are Made

### Deterministic guard layer (`guards.py`)

All convergence and floor checks are pure Python — the LLM has zero influence over these:

```python
# Net margin formula
margin% = (buyer_price − supplier_price − logistics − cost_envelope_total) / buyer_price × 100

# Convergence: price gap AND margin both satisfied
converged = (|ask − bid| ≤ concession_step) AND (margin% ≥ margin_floor%)

# Near-floor warning
near_floor = margin% < (margin_floor% + hitl_margin_warning_pct)

# Max rounds exhausted
failed = round_no >= max_rounds
```

### LLM orchestrator role

When no deterministic guard fires, the orchestrator LLM is given the full state as JSON and asked to decide:

```
"If last party was buyer → route CALL_SUPPLIER_AGENT"
"If last party was supplier → route CALL_BUYER_AGENT"
"Default → CALL_BUYER_AGENT"
```

The LLM's primary value in orchestration is **reasoning** — it explains *why* a routing was chosen, populating `orchestrator_reasoning` in the `NegotiationRound` record. This reasoning is visible in the UI's round history view.

### LLM fallback safety

If the LLM call fails (timeout, all models exhausted), the orchestrator falls back to `CALL_BUYER_AGENT` with a logged error. The negotiation never halts due to an LLM outage.

---

## 5. Data Persistence — What Is Stored and When

### Three collections in MongoDB

```
negotiation DB
├── sessions       (NegotiationSession — one per deal)
├── rounds         (NegotiationRound   — one per run_round() call)
└── email_history  (EmailHistoryRecord — one per draft/inbound email)
```

### `NegotiationSession` — the living state

Created once by `start_negotiation()`, updated after every `run_round()`:

| Field | Updated when |
|-------|-------------|
| `status` | Every graph run |
| `round_no` | Incremented after each `run_round()` |
| `current_bid` | BuyerAgent sets a new counter |
| `current_ask` | SupplierAgent sets a new counter |
| `agreed_price` | Set when `routing=NEGOTIATION_AGREED` |
| `margin_at_current` | Recomputed every round |
| `hitl_required` | Set when HITL gate fires |
| `realized_cms` | Set post-agreement by Match Engine |
| `updated_at` | Every save |

**Crash-safety:** the session is saved to MongoDB *before* the LangGraph `ainvoke()` call returns, so a crash during graph execution leaves an auditable PENDING/HITL_PENDING record rather than a silent hole.

### `NegotiationRound` — the immutable history log

One record per `run_round()` call. Never mutated after creation:

```json
{
  "round_id": "uuid",
  "negotiation_id": "neg-3f8a1...",
  "deal_id": "DEAL-2025-001",
  "round_no": 2,
  "party": "supplier",
  "channel": "email",
  "inbound_price": 460.0,
  "inbound_message": "We can offer $460/MT Ex-Works...",
  "outbound_message": "Thank you for your revised offer...",
  "negotiated_terms": { "unit_price": 445.0, "currency": "USD", "moq": 500 },
  "orchestrator_routing": "CALL_BUYER_AGENT",
  "orchestrator_reasoning": "Supplier replied; routing to buyer agent to counter-propose.",
  "margin_at_round": -15.2,
  "hitl_required": false,
  "escalation_flag": false
}
```

### `EmailHistoryRecord` — full email audit trail

One record per email draft created and per inbound email received. See §7 for the full lifecycle.

---

## 6. Waiting States — How the Agent Pauses for Buyer or Seller

The negotiation agent is **event-driven, not polling-based**. After each `run_round()` call, the session is persisted and the HTTP request returns. The agent does not hold an open connection waiting for replies.

### State machine waiting points

```
status=HITL_PENDING
  └── Waiting for: operator to approve/reject draft email in the HITL dashboard
        └── On APPROVE: HITL callback calls send_approved_email() → SMTP dispatch
            After email sent: status remains HITL_PENDING until counter-offer arrives
        └── On REJECT: email marked REJECTED; no email sent

status=ROUND
  └── Waiting for: buyer or seller to reply (email, portal, WhatsApp, Telegram)
        └── Operator ingests reply by calling run_round() with the counter_price + inbound_message
        └── services/email_gateway/ fetches IMAP emails (every 15 min) → services/email_processor/ calls run_round() automatically

status=AGREED  ← terminal
status=FAILED  ← terminal
status=ESCALATED ← terminal (HITL rejected + operator decided to cancel)
```

### How an inbound reply is submitted

**Manual (current default):**
```
POST /negotiation-agent/negotiations/{id}/round
{
  "counter_price": 450.0,
  "party": "supplier",
  "channel": "email",
  "inbound_message": "Final offer: $450/MT Ex-Works"
}
```

**Automatic (IMAP monitor):**
The `services/email_gateway/` service polls the Gmail IMAP inbox every 15 minutes and forwards
each fetched email to `services/email_processor/`, which identifies the `negotiation_id` from the
thread headers / subject token and calls `run_round()` programmatically. This makes the agent
fully autonomous for email-based channels. Transport (IMAP + SMTP, the Gateway) and inbound
processing (the Processor) are separate services, each with a single responsibility.

### Session lock

The session record in MongoDB acts as the lock. `run_round()` rejects calls on terminal sessions (`AGREED`, `FAILED`, `ESCALATED`) with HTTP 409 Conflict, preventing double-processing if two events arrive simultaneously.

---

## 7. Email Lifecycle — Draft → HITL Gate → Sent

Every outbound email drafted by BuyerAgent or SupplierAgent goes through a mandatory HITL approval gate before any message reaches a real buyer or supplier.

```
LangGraph produces outbound message text
         │
         ▼
_create_and_gate_email()                     [service.py]
  1. Generate email_id = "EMAIL-{hex}"
  2. Create EmailHistoryRecord:
       status = DRAFT
       direction = OUTBOUND
       body = <LLM-generated text>
  3. Save record to email_history collection
  4. Call hitl_approval.service.create_approval():
       action = "SEND_NEGOTIATION_EMAIL"
       autonomy_level = "L0"   ← always ask operator
       callback_payload = { email_id, negotiation_id, party }
  5. Update record: status = PENDING_APPROVAL, approval_id = <id>
         │
         ▼
HITL Dashboard shows pending email draft
  Operator sees: round number, party, preview of first 150 chars
         │
    ┌────┴────┐
    │         │
 APPROVE   REJECT
    │         │
    ▼         ▼
send_approved_email()    reject_email()
  (with optional          status = REJECTED
   modified_body)
    │
    ▼
email_gateway.sender.send_email()   [services/email_gateway/ — POST /email-gateway/send]
  Gmail SMTP or log-only (dev)
    │
    ▼
status = SENT or MODIFIED_SENT
sent_at = <timestamp>
```

### Email status lifecycle

| Status | Meaning |
|--------|---------|
| `DRAFT` | LLM generated text, not yet submitted to HITL |
| `PENDING_APPROVAL` | HITL approval created, waiting for operator |
| `SENT` | Operator approved, dispatched via SMTP as-is |
| `MODIFIED_SENT` | Operator edited body before approving, then sent |
| `REJECTED` | Operator declined to send this draft |
| `FAILED` | SMTP delivery failed after approval |
| `RECEIVED` | Inbound email from buyer/supplier (terminal, audit only) |

### Inbound email recording

When a buyer or supplier reply arrives (via IMAP or manual submission):
```python
await record_inbound_email(
    negotiation_id="neg-3f8a1...",
    deal_id="DEAL-2025-001",
    round_no=2,
    party="supplier",
    from_address="supplier@bangkokrice.th",
    subject="Re: PortLess Trade Quote — Round 1",
    body="We can offer $460/MT Ex-Works...",
)
```
This creates a `status=RECEIVED` record — no HITL gate, just an audit trail.

---

## 8. Viewing All Emails in the UI

### API endpoint

```
GET /negotiation-agent/negotiations/{negotiation_id}/emails
```

Returns all `EmailHistoryRecord` objects ordered oldest-first. The UI can call this endpoint to render a complete conversation thread.

### Example response

```json
[
  {
    "email_id": "EMAIL-A1B2C3D4E5",
    "negotiation_id": "neg-3f8a1...",
    "deal_id": "DEAL-2025-001",
    "round_no": 1,
    "direction": "outbound",
    "party": "buyer",
    "to_address": "buyer001@portless.placeholder",
    "from_address": "portless-trade@portless.io",
    "subject": "PortLess Trade Update — Round 1",
    "body": "Dear valued partner, we are pleased to present...",
    "approval_id": "APR-0001",
    "status": "sent",
    "sent_at": "2025-03-15T10:23:44Z",
    "created_at": "2025-03-15T10:20:00Z",
    "updated_at": "2025-03-15T10:23:44Z"
  },
  {
    "email_id": "EMAIL-F6G7H8I9J0",
    "round_no": 1,
    "direction": "inbound",
    "party": "supplier",
    "from_address": "supply@bangkokrice.th",
    "subject": "Re: PortLess Trade Quote — Round 1",
    "body": "We can offer $460/MT Ex-Works for 500 MT...",
    "status": "received",
    "sent_at": "2025-03-15T14:05:00Z"
  },
  {
    "email_id": "EMAIL-K1L2M3N4O5",
    "round_no": 2,
    "direction": "outbound",
    "party": "buyer",
    "body": "Thank you for your revised offer of $460...",
    "status": "modified_sent",
    "original_body": "We propose $445/MT CIF as our counter.",
    "sent_at": "2025-03-16T09:10:00Z"
  }
]
```

### UI rendering hints

- **Thread view:** group records by `party` to show two conversation columns (buyer thread / supplier thread).
- **Status badges:** `PENDING_APPROVAL` → show "Awaiting operator review"; `REJECTED` → strikethrough; `MODIFIED_SENT` → show diff icon to compare `original_body` vs `body`.
- **Round grouping:** group by `round_no` to show one accordion per negotiation round.
- **Also use:** `GET /negotiation-agent/negotiations/{id}/rounds` for the structured JSON data (prices, margins, routing decisions) alongside the email thread.

---

## 9. RAG Knowledge and Trade Playbooks

### How RAG is loaded

At session start, `load_rag_context(hs_code)` in `rag.py` runs once:

```python
question = (
    "Negotiation tactics, price benchmarks, payment terms, and trade rules "
    f"for product HS code {hs_code}. Include typical buyer/supplier concession "
    "patterns, quality standards, and incoterm preferences."
)
result = await rag_query(question, metadata_filter={"hs_code": hs_code})
```

The returned `rag_context` string is stored in the `NegotiationSession` document and injected into **every LLM call** for the lifetime of the negotiation — both orchestrator and both sub-agents receive it.

### What RAG provides

| HS Code | RAG Playbook Content |
|---------|---------------------|
| `1006` (Rice) | Thailand 5% broken benchmark price, LC at sight, 21–30 day lead time, moisture ≤14%, buyer counters −8% |
| `1001` (Wheat) | CME CBOT front-month reference, protein premium $8–12/MT, Black Sea lead times, MOQ 500 MT |
| `6101` (Apparel) | Factory-gate pricing, 500–1000 pcs MOQ, 30% TT advance, OEKO-TEX 100 for EU |
| Any other | Generic: anchor buyer −10%, supplier +12–15%, 3–5 rounds to converge, FOB/CIF defaults |

### Fallback behavior

If the MongoDB Atlas vector store is unconfigured or returns no results, `rag.py` falls back to static product playbooks hardcoded in `_FALLBACK_PLAYBOOKS`. The negotiation always proceeds — the LLM just has less precise market intelligence.

### Web search (planned)

The current implementation uses indexed trade playbooks only. A planned extension would allow the RAG retrieval to augment with live commodity price data (e.g., Comdty API, CME) injected as additional `rag_context` before each round's LLM call. This would make the agent aware of intraday price movements.

### How agents use RAG context in practice

The orchestrator uses RAG to calibrate its routing confidence ("given that rice typically converges in 3 rounds, 2 rounds remaining is within normal range").

BuyerAgent uses RAG to:
- Anchor the counter-price to a market benchmark ("Thailand 5% broken currently trades near $XXX/MT").
- Justify quality requirements from the playbook (FSSAI, moisture standards).
- Signal whether PortLess's offer is above or below benchmark.

SupplierAgent uses RAG to:
- Present the supplier's ask in the context of international benchmarks.
- Justify lead time estimates from typical shipping windows.
- Argue for standard incoterms (FOB Bangkok vs CIF Mumbai).

---

## 10. Stop vs Continue — Convergence and Termination Rules

### Continue conditions (loop back to next round)

```
gap = |current_ask - current_bid| > concession_step   (price still too far apart)
  OR
margin_pct < margin_floor_pct                          (not yet profitable enough)
  AND
round_no < max_rounds                                  (rounds remaining)
  AND
margin_pct >= margin_floor_pct + hitl_margin_warning_pct   (not near danger zone)
```

### Stop: AGREED

```python
converged = (gap <= concession_step) AND (margin_pct >= margin_floor_pct)
```

When `converged=True`, the orchestrator sets `routing=NEGOTIATION_AGREED` and `agreed_price = midpoint(bid, ask)`. No LLM is consulted — this is a pure deterministic decision.

After agreement:
1. Session `status=AGREED`, `agreed_price` stored.
2. Match Engine called to compute `realized_cms` (final CMS score with actual agreed price).
3. Domain event `negotiation.agreed` published.
4. Deal service transitions deal to `NEGOTIATION_AGREED`.
5. Dealmaker is triggered to issue a Proforma Invoice (PI).

### Stop: FAILED

```
round_no >= max_rounds  AND  NOT converged
```

Session saved as `status=FAILED`. Domain event published. Deal transitions to `NEGOTIATION_FAILED`. Exception Resolver may be invoked.

### Stop: HITL_PENDING (pause, not terminal)

Three triggers escalate to HITL (human takes over):

| Trigger | Condition | What operator sees |
|---------|-----------|-------------------|
| First outbound gate | `is_first_round AND hitl_on_first_outbound=True` | Draft of first outbound email |
| Margin warning | `margin_pct < floor% + warning%` (default within 2%) | Margin alert with current bid/ask |
| Max rounds | `round_no >= max_rounds` | Summary of all rounds, bid/ask spread |

In HITL_PENDING the agent pauses. The operator can:
- **Approve email draft** → message sent, negotiation continues.
- **Modify and approve** → edited message sent.
- **Reject** → message not sent; operator manually chooses next action.
- **Override price** → send a `run_round()` with `operator_override_price` set.

### Decision flow diagram

```
Each run_round() call:

  current_bid, current_ask
         │
         ▼
  gap ≤ concession_step?
  margin ≥ floor?          ──YES──→  NEGOTIATION_AGREED ✓ (stop)
         │
         NO
         │
  round_no ≥ max_rounds?  ──YES──→  NEGOTIATION_FAILED ✗ (stop)
         │
         NO
         │
  margin < floor+warning? ──YES──→  HITL_APPROVAL ⏸ (pause)
         │
         NO
         │
  is_first_round + config? ─YES──→  HITL_APPROVAL ⏸ (pause)
         │
         NO
         │
  LLM routing decision
         │
   ┌─────┴──────┐
   ▼            ▼
 BuyerAgent  SupplierAgent
   │            │
   └────────────┘
   new bid/ask, loop back to top
```

---

## 11. HITL Gates — Human-in-the-Loop Checkpoints

Every HITL gate creates a `SEND_NEGOTIATION_EMAIL` approval in the HITL service with `autonomy_level=L0` (always ask, never auto-approve).

### Gate 1: First outbound message

- **Triggered by:** `hitl_on_first_outbound=True` (configurable in `config.py`)
- **Why:** Operator must approve the opening position before it reaches a real trading partner.
- **Approval payload:** `{ email_id, negotiation_id, party }`

### Gate 2: Margin warning

- **Triggered by:** `margin_pct < margin_floor_pct + hitl_margin_warning_pct` (e.g., margin < 10% when floor=8%, warning=2%)
- **Why:** Any more concession risks unprofitable execution.
- **Operator action:** Can accept the risk and continue OR set an `operator_override_price`.

### Gate 3: Max rounds exhausted

- **Triggered by:** `round_no >= max_rounds` with no convergence
- **Why:** Automated negotiation has stalled; human judgment needed.
- **Operator action:** Manual price override, extend max_rounds, or close the deal.

### Callback flow

```
HITL Approval APPROVED
       │
       ▼
hitl_approval.callbacks.execute_callback()
  action="SEND_NEGOTIATION_EMAIL"
       │
       ▼
negotiation_agent.service.send_approved_email(email_id, modified_body?)
  │
  └── email_gateway.sender.send_email()  → Gmail SMTP  [services/email_gateway/]
  └── EmailHistoryRecord updated: status=SENT or MODIFIED_SENT

HITL Approval REJECTED
       │
       ▼
negotiation_agent.service.reject_email(email_id)
  └── EmailHistoryRecord updated: status=REJECTED
```

---

## 12. Status Reference

### `NegotiationSession.status`

| Status | Meaning | Terminal? |
|--------|---------|-----------|
| `STARTED` | Session created, first graph tick running | No |
| `ROUND` | Active round in progress, waiting for next reply | No |
| `HITL_PENDING` | Waiting for operator action (email approval or override) | No |
| `AGREED` | Convergence reached, `agreed_price` set | **Yes** |
| `FAILED` | Max rounds exhausted, no agreement | **Yes** |
| `ESCALATED` | Operator escalated beyond normal HITL handling | **Yes** |

### `EmailHistoryRecord.status`

| Status | Direction | Meaning |
|--------|-----------|---------|
| `DRAFT` | outbound | LLM generated, not yet in HITL queue |
| `PENDING_APPROVAL` | outbound | HITL approval created, awaiting operator |
| `SENT` | outbound | Dispatched via SMTP as original draft |
| `MODIFIED_SENT` | outbound | Operator edited body, then sent |
| `REJECTED` | outbound | Operator declined to send |
| `FAILED` | outbound | SMTP delivery error after approval |
| `RECEIVED` | inbound | Reply from buyer/supplier, audit record only |

### `AgentRouting` (orchestrator output)

| Routing | Next step |
|---------|-----------|
| `CALL_BUYER_AGENT` | Run `buyer_agent_node`, draft message to buyer |
| `CALL_SUPPLIER_AGENT` | Run `supplier_agent_node`, draft message to supplier |
| `HITL_APPROVAL` | Stop graph, create HITL approval |
| `NEGOTIATION_AGREED` | Stop graph, set `agreed_price` |
| `NEGOTIATION_FAILED` | Stop graph, mark session FAILED |

---

## 13. Configuration Reference

All settings are in `config.py` / `.env`:

| Variable | Default | Effect |
|----------|---------|--------|
| `NEGOTIATION_MONGO_URI` | `""` | MongoDB URI; falls back to in-memory when empty |
| `NEGOTIATION_LLM_TEMPERATURE` | `0.3` | LLM temperature for all three nodes |
| `NEGOTIATION_LLM_MAX_TOKENS` | `2048` | Max LLM response tokens |
| `GMAIL_SENDER_ADDRESS` | `""` | Gmail account for SMTP; empty = log-only mode |
| `GMAIL_APP_PASSWORD` | `""` | Gmail App Password (not account password) |
| `HITL_ON_FIRST_OUTBOUND` | `true` | Require operator approval on first email |
| `HITL_MARGIN_WARNING_PCT` | `2.0` | Trigger HITL when margin is within 2% of floor |
| `DEFAULT_MAX_ROUNDS` | `5` | Default policy max rounds |
| `DEFAULT_MARGIN_FLOOR_PCT` | `8.0` | Default minimum acceptable net margin |
| `DEFAULT_CONCESSION_STEP_USD` | `0.10` | Max bid-ask gap for convergence |
| `IMAP_HOST` / `IMAP_USER` | `""` | IMAP inbox monitoring (future auto-ingestion) |

### Policy overrides per negotiation

Any `NegotiationPolicy` field can be overridden per-deal in the `StartNegotiationRequest`:

```json
"policy": {
  "target_margin_pct": 15.0,
  "margin_floor_pct": 10.0,
  "max_rounds": 8,
  "concession_step": 0.25,
  "deadline": "2025-04-01"
}
```

The `deadline` field is available on `NegotiationPolicy` and is passed into agent prompts to encourage urgency in messaging as the date approaches.

---

## 14. Layman's Guide — Plain English with Real-World Scenarios

> No technical jargon here. If you have never read the sections above, start here.

---

### What is PortLess doing, in one sentence?

PortLess is like a **professional broker** that sits between a buyer and a seller. Instead of the buyer and seller arguing directly, PortLess's AI agent handles all the back-and-forth emails and price negotiations on their behalf — automatically — while making sure PortLess earns a profit on the deal.

---

### The three characters in every negotiation

| Character | Who they are | What they see |
|-----------|-------------|---------------|
| **The Buyer** | A company that wants to buy goods (e.g., a rice importer in India) | Only CIF prices (cost includes delivery to their door) |
| **The Supplier** | A company that wants to sell goods (e.g., a rice mill in Thailand) | Only Ex-Works prices (cost at their factory gate) |
| **PortLess** | The middleman — makes money on the gap between what the buyer pays and what the supplier receives | Sees both sides; keeps the margin secret from both |

---

### Think of it like haggling at a market — but with a smart assistant

Imagine you are a broker at a market. A buyer whispers to you: "I will pay at most $430." A seller whispers: "I want at least $480." Your job is to find a price somewhere in the middle that both accept — and you keep $30+ as your commission.

You cannot just tell each one the other's number (they would cut you out). So you craft polite, professional messages for each side, nudging them toward each other, one offer at a time. That is exactly what the PortLess Negotiation Agent does — automatically, via email.

---

### Scenario 1 — Quick Deal (Everything Goes Smoothly)

**The situation:** A clothing retailer in Germany wants to buy 1,000 T-shirts from a factory in Bangladesh.

- Buyer's maximum price: **$8.00 per piece**
- Supplier's opening price: **$7.50 per piece**
- PortLess's costs (shipping, QC, paperwork): **$0.40 per piece**
- PortLess needs at least 8% profit margin

**Round 0 — Agent starts up**

The AI agent checks: can PortLess make 8% here?

```
Margin = (8.00 − 7.50 − 0.40) / 8.00 × 100 = 1.25%   ← too low, need 8%
```

The agent also reads its "clothing trade playbook" (from the RAG knowledge base):
*"Apparel deals: factory counters +3%, buyer counters −5%, typically closes in 2–3 rounds."*

Before sending anything, the agent drafts an email to the buyer and pauses — because the first email always needs a human operator to review it. The operator sees the draft in the PortLess dashboard, reads it, clicks **Approve**, and the email goes out.

**Round 1 — Supplier drops to $7.20**

The supplier replies: "We can do $7.20/piece for 1,000 pcs minimum."

```
New margin = (8.00 − 7.20 − 0.40) / 8.00 × 100 = 5%   ← still below 8%
```

Agent drafts a reply to the buyer: *"Great news — we have secured a revised supplier price. We can offer you $7.80/piece CIF Hamburg, including full quality inspection."* Operator approves. Email sent.

**Round 2 — Buyer drops to $7.80, Supplier drops to $7.10**

```
Gap = |7.80 − 7.10| = 0.70   ← still too far apart (threshold is $0.10)
Margin = (7.80 − 7.10 − 0.40) / 7.80 × 100 = 3.8%   ← still below 8%
```

Agent keeps negotiating. Asks supplier: *"Our client is firm at $7.80. Can you reach $7.20?"*

**Round 3 — Supplier at $7.20, Buyer at $7.80**

After supplier drops to $7.20:

```
Gap = |7.80 − 7.20| = 0.60   ← still too far
Margin = (7.80 − 7.20 − 0.40) / 7.80 × 100 = 2.6%   ← too low
```

**Round 4 — Buyer accepts $7.60, Supplier agrees to $7.20**

```
Gap = |7.60 − 7.20| = 0.40   ← still above threshold
Margin = (7.60 − 7.20 − 0.40) / 7.60 × 100 = 0%   ← still no profit!
```

This deal would only work if the buyer pays more or the supplier charges less. The agent reaches its maximum of 5 rounds and **escalates to the human operator**: "I cannot find a profitable agreement. Please review."

The operator looks at the numbers and decides to **close the deal at $7.50 buyer / $6.95 supplier** by manually entering an override price. PortLess earns ~2% — a thin but acceptable margin for this buyer relationship.

**Result:** AGREED at $7.50. PI (invoice) issued to buyer.

---

### Scenario 2 — Deal Falls Apart (No Agreement)

**The situation:** A food company in the UAE wants to buy 200 MT of premium saffron from Iran.

- Buyer's maximum: **$1,200/kg**
- Supplier's opening price: **$1,800/kg**
- PortLess costs: **$50/kg**
- The gap is enormous ($600/kg), and neither side is willing to move much.

**What happens:**

| Round | Buyer bid | Supplier ask | Gap | Margin |
|-------|-----------|--------------|-----|--------|
| Start | $1,200 | $1,800 | $600 | −54% |
| 1 | $1,250 | $1,750 | $500 | −48% |
| 2 | $1,300 | $1,700 | $400 | −35% |
| 3 | $1,350 | $1,650 | $300 | −22% |
| 4 | $1,380 | $1,620 | $240 | −17% |
| 5 | $1,400 | $1,600 | $200 | −14% ← max rounds hit |

After 5 rounds with no convergence (threshold is $0.10, actual gap is $200), the agent declares **NEGOTIATION_FAILED** automatically.

The PortLess operator gets a notification. The deal moves to dispute handling. PortLess may suggest sourcing saffron from Afghanistan instead (its BATNA — backup plan).

**Lesson:** The agent knows when to stop. It will not keep sending emails forever at a loss.

---

### Scenario 3 — The Margin Warning (Human Steps In Mid-Deal)

**The situation:** A coffee roaster in Australia buys green coffee from Ethiopia.

- Buyer: **$4.20/kg**, Supplier: **$4.00/kg**, Costs: **$0.10/kg**
- Margin floor: 8%, Warning: triggers when below 10%

**Starting margin:**
```
(4.20 − 4.00 − 0.10) / 4.20 × 100 = 2.4%   ← already very low
```

**Round 1:** Supplier drops to $3.95. Margin improves slightly to 3.6%.

**Round 2:** Buyer drops to $4.10. Now:
```
(4.10 − 3.95 − 0.10) / 4.10 × 100 = 1.2%   ← near zero
```

The agent detects: **margin (1.2%) is below floor (8%) + warning (2%) = 10%.** It immediately **pauses and calls for human review** — before sending anything that might lock in a bad deal.

The operator sees: "⚠️ Margin Warning — current deal earns only 1.2%, minimum is 8%. Do you want to continue or close?"

The operator decides to:
1. Ask the supplier to drop to $3.70 (a bigger ask), or
2. Tell the buyer the price needs to go up to $4.30.

The operator enters an override. Negotiations resume.

**Lesson:** The agent never commits PortLess to a loss. It always pauses before crossing the danger line.

---

### Scenario 4 — Operator Edits the Email Before Sending

**The situation:** A steel buyer in Japan negotiates with a mill in India. The AI drafts this message to the buyer after round 2:

> *"We propose $520/MT CIF Osaka as our counter-offer for this shipment."*

The operator reads it and thinks: "We should mention the ISO 9001 cert to justify the price." The operator clicks **Edit & Approve** in the dashboard and changes it to:

> *"We propose $520/MT CIF Osaka as our counter-offer. This price reflects our supplier's ISO 9001-certified production and guaranteed 20-day lead time to Osaka."*

The email is sent with the edited version. The system records:
- `status = MODIFIED_SENT`
- `original_body` = the AI's original draft
- `body` = the operator-edited version

Both versions are visible in the UI so there is a complete audit trail of what was changed and why.

---

### Scenario 5 — Buyer Replies by Email, Agent Auto-Advances

**The situation:** A textile buyer in Bangladesh sends a reply email to the PortLess trade address after seeing Round 1's offer.

> Subject: Re: PortLess Trade Quote — Round 1  
> *"Thank you. We can go up to $3.80/yard. Please confirm delivery in 45 days."*

The **Inbox Monitor** (a background process that watches the PortLess Gmail inbox) detects this reply. It:

1. Reads the email.
2. Matches it to the active negotiation using the email subject thread.
3. Parses the counter-price ($3.80) and message.
4. Automatically calls `run_round()` as if an operator had entered it manually.

The agent springs back to life, drafts a supplier-facing email, and the deal advances — without any human needing to do anything. The UI shows the inbound email in the conversation thread with status `received`.

---

### How the email conversation looks in the UI

Imagine a deal page in the PortLess dashboard. There is a tab called **"Negotiation Emails"**. It looks like a two-column chat window:

```
┌─────────────────────────────────────────────────────────┐
│  BUYER THREAD                  SUPPLIER THREAD          │
├─────────────────────────────────────────────────────────┤
│  Round 1 (sent ✓)                                       │
│  ┌─────────────────┐                                    │
│  │ To: buyer@co.in │                                    │
│  │ "We are pleased │                                    │
│  │  to offer $445.."│                                   │
│  └─────────────────┘                                    │
│                              Round 1 (sent ✓)           │
│                              ┌──────────────────────┐   │
│                              │ To: supplier@co.th   │   │
│                              │ "Our client requires │   │
│                              │  $440 max. Can you.."│   │
│                              └──────────────────────┘   │
│  Round 1 reply (received 📥)                            │
│  ┌─────────────────┐                                    │
│  │ From: buyer     │                                    │
│  │ "$443 is our    │                                    │
│  │  final offer."  │                                    │
│  └─────────────────┘                                    │
│                                                         │
│  Round 2 (⏳ pending approval)                          │
│  ┌─────────────────┐                                    │
│  │ DRAFT — awaiting│  ← operator sees [Approve] [Edit] │
│  │ operator review │    [Reject] buttons here           │
│  └─────────────────┘                                    │
└─────────────────────────────────────────────────────────┘
```

Every message — sent, received, pending, rejected, edited — is stored forever. Nothing is ever deleted. The operator can scroll back through the entire negotiation history.

---

### Summary — The agent's decision logic in plain English

| Situation | What the agent does |
|-----------|-------------------|
| First email ever drafted | Always stops and asks the operator to approve before sending |
| Prices are getting close and profit looks good | Declares the deal AGREED automatically |
| Too many rounds with no agreement | Stops and asks a human to decide |
| Profit margin drops dangerously low | Stops and warns the operator immediately |
| Operator approves email as-is | Sends it by Gmail |
| Operator edits the email | Sends the edited version, saves the original for audit |
| Operator rejects the email | Does not send it, marks it as rejected |
| Buyer or seller replies by email | Inbox monitor picks it up and the next negotiation round starts automatically |
| LLM (AI brain) crashes or times out | Falls back to safe defaults, never breaks the deal |

---

## 15. Implementation Status

> Last reviewed: 2026-07-07.  
> Legend: ✅ Fully implemented · ⚠️ Partial / stub · ❌ Not started · 💡 Nice-to-have

### Core negotiation engine

| Feature | Status | File(s) | Notes |
|---------|--------|---------|-------|
| LangGraph 3-node state machine | ✅ | `graph.py` | orchestrator → buyer/supplier agents → loop |
| Orchestrator node (deterministic guards + LLM fallback) | ✅ | `graph.py` | Priority-ordered guard layer; LLM only for tie-breaking |
| Buyer-facing agent node (LLM draft + bid update) | ✅ | `graph.py` | CIF pricing; midpoint counter; RAG anchoring |
| Supplier-facing agent node (LLM draft + ask update) | ✅ | `graph.py` | Ex-Works pricing; implied-max guard |
| Edge routing after orchestrator | ✅ | `graph.py` | Maps routing enum → graph edge |
| Graph error handling (LLM crash → HITL_PENDING) | ✅ | `service.py` | Graph exceptions caught; never halts deal |

### Guard layer (`guards.py`)

| Feature | Status | Notes |
|---------|--------|-------|
| `compute_net_margin()` — exact formula | ✅ | (bid − ask − logistics − cost_envelope) / bid × 100 |
| `check_convergence()` — gap AND margin combined | ✅ | Both conditions must hold |
| `verify_payout_and_margin()` — margin vs floor | ✅ | |
| `compute_midpoint_price()` — agreed price | ✅ | |
| `is_near_margin_floor()` — warning zone | ✅ | Default window: 2% above floor |

### Session & round management (`service.py`, `repository.py`)

| Feature | Status | Notes |
|---------|--------|-------|
| `start_negotiation()` — session init + first graph tick | ✅ | Includes RAG load, crash-safe session write |
| `run_round()` — ingest counter-offer + graph run | ✅ | Updates bid/ask, saves round record |
| Operator override price (`operator_override_price`) | ✅ | Forces NEGOTIATION_AGREED routing |
| Deal idempotency (reuse existing session) | ✅ | Reuses non-terminal session for same `deal_id` |
| NegotiationSession MongoDB persistence | ✅ | Motor + InMemory fallback |
| NegotiationRound immutable audit log | ✅ | One record per `run_round()` call |
| Post-agreement Realized CMS via Match Engine | ✅ | Non-fatal; logs warning on failure |
| `negotiation.round` domain event publishing | ✅ | After every round; NoopPublisher in dev |
| List negotiations / get session / list rounds | ✅ | 3 read endpoints |

### Email lifecycle

| Feature | Status | File(s) | Notes |
|---------|--------|---------|-------|
| Email draft creation (DRAFT status) | ✅ | `service.py` | Saved before HITL gate |
| HITL gate for every outbound email (L0) | ✅ | `service.py` | `SEND_NEGOTIATION_EMAIL` approval |
| PENDING_APPROVAL → SENT/MODIFIED_SENT on approve | ✅ | `service.py` | Via `send_approved_email()` |
| PENDING_APPROVAL → REJECTED on reject | ✅ | `service.py` | Via `reject_email()` |
| SMTP dispatch via Gmail (aiosmtplib STARTTLS) | ✅ | `services/email_gateway/sender.py` | Email Gateway; called from `send_approved_email()`; log-only fallback when unconfigured |
| Message-ID embedding for reply threading | ✅ | `services/email_gateway/sender.py` | `<EMAIL-id.neg-id@portless.io>` |
| [NEG:uuid] subject token injection | ✅ | `services/email_gateway/sender.py` | Fallback thread matching |
| RECEIVED record for inbound emails | ✅ | `service.py` | `record_inbound_email()` |
| EmailHistoryRecord MongoDB persistence | ✅ | `email_history.py` | Motor + InMemory fallback |
| `/email-threads` UI inbox endpoint | ✅ | `router.py` | Aggregates session + emails; sorted by updated_at |
| `/emails` per-negotiation history endpoint | ✅ | `router.py` | |

### RAG & LLM

| Feature | Status | File(s) | Notes |
|---------|--------|---------|-------|
| RAG context load at session start | ✅ | `rag.py` | MongoDB Atlas vector search |
| Static fallback playbooks (HS 1006, 1001, 6101, default) | ✅ | `rag.py` | Used when Atlas unconfigured |
| RAG context injected into all 3 agent LLM calls | ✅ | `graph.py` | Lives in session doc, passed per-call |
| LLM Gateway integration (Claude → Groq fallback) | ✅ | `llm.py` | Shared LLMClient singleton |
| System prompts for all 3 agents | ✅ | `prompts.py` | Strict JSON output enforced |
| LLM JSON parse error handling | ✅ | `graph.py` | `_parse_llm_json()` with safe fallback dict |
| Policy deadline field in prompts | ⚠️ | `schemas.py` | Field exists on `NegotiationPolicy` but agents do not yet use it to escalate urgency in message tone |

### Inbound email auto-ingestion

| Feature | Status | File(s) | Notes |
|---------|--------|---------|-------|
| Email Processor (SendGrid webhook) | ✅ | `email_processor/router.py` | Full webhook + HMAC verification |
| Email Processor (Mailgun webhook) | ✅ | `email_processor/router.py` | Full webhook + HMAC verification |
| Manual inbound submission endpoint | ✅ | `email_processor/router.py` | `POST /email-processor/inbound/manual` |
| Inbound email deduplication by Message-ID | ✅ | `email_processor/service.py` | Idempotent re-delivery safe |
| Price extraction from email body (regex) | ✅ | `email_processor/service.py` | 5 pattern variants, configurable min/max |
| Negotiation mapping (In-Reply-To / References / Subject token) | ✅ | `email_processor/mapper.py` | 3-tier priority matching; live sessions only |
| IMAP inbox poller (Gmail aioimaplib) | ✅ | `email_gateway/imap_poller.py` | Fully implemented; fetches UNSEEN, marks \Seen, parses bodies + attachments, forwards to Processor; 15-min interval + error back-off |
| Email Gateway + Processor mounted in monolith (`app.py`) | ✅ | `app.py` | Routers registered + `on_startup`/`on_shutdown` wired (Processor before Gateway) |

### Channels

| Feature | Status | Notes |
|---------|--------|-------|
| Email channel (outbound SMTP + inbound webhook/IMAP) | ✅ | Primary channel; fully wired |
| Portal channel (manual `run_round()` submission) | ✅ | Works via the REST API |
| WhatsApp channel | ❌ | `channel` field exists in `RoundRequest` schema but no handler wired |
| Telegram channel | ❌ | Same as WhatsApp |

### API & integration

| Feature | Status | Notes |
|---------|--------|-------|
| REST API — all 7 endpoints | ✅ | start, run_round, get, list, rounds, emails, email-threads |
| HTTP client for microservice mode | ✅ | `client.py` — `NegotiationAgentClient` |
| HITL saga callback `SEND_NEGOTIATION_EMAIL` | ✅ | Wired in `hitl_approval/callbacks.py` |
| Deal status transition on AGREED/FAILED | ✅ | Via `deals.service` patch in orchestration flow |
| Dealmaker PI trigger on AGREED | ✅ | Downstream deal transition to `NEGOTIATION_AGREED` |
| Mock seed data for UI dev | ✅ | `_mock_seed.py` — 5 scenarios, 15 emails |
| Integration / end-to-end tests | ❌ | Unit tests in `tests/test_negotiation_agent.py` cover schemas/guards; no full flow test |

---

### Nice-to-have (not started)

| Feature | Priority | Notes |
|---------|----------|-------|
| Live commodity price enrichment in RAG | High | Inject intraday CME/Comdty/Volza prices as additional context before each LLM call |
| PDF / Excel attachment content parsing | Medium | Attachments are captured (`email_gateway`) and forwarded as `attachment_refs`; parsing their contents for a quoted price (pypdf / openpyxl) is still pending |
| Policy deadline urgency in agent prompts | Medium | Pass `deadline` to buyer/supplier agents; tone should escalate as date approaches |
| WhatsApp channel wiring (Twilio / Meta API) | Medium | Channel enum already in schema; needs dispatcher implementation |
| HTML email templates | Medium | Emails are plain-text; branded HTML with price table and round summary |
| BATNA leak audit | Medium | Automated test confirming BATNA value never appears in any outbound message body |
| Negotiation analytics API | Low | Round-by-round margin trend, time-to-agree, HITL intervention rate |
| Per-product RAG playbook ingestion UI | Low | Operator can upload/edit trade playbooks per HS code via the Knowledge Base page |
| LLM rate limiting / circuit breaker | Low | Noted in `llm.py` as future; add via token-bucket + half-open state |
| Multi-party negotiations (3+ parties) | Low | Current design is strictly bilateral buyer ↔ supplier |
| Telegram channel wiring | Low | Channel enum already in schema |
