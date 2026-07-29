# Negotiation Agent — Workflow Deep-Dive

> **Module:** `agents/negotiation_agent/`  
> **Engine:** LangGraph (three-node async state machine)  
> **LLM:** Shared LLM Gateway → Claude Sonnet (primary) → Groq fallback chain  
> **Persistence:** MongoDB Motor (in-memory fallback for dev/CI)

---

> ### ⚠️ 2026-07-16 update — agreement detection & closure changed
>
> After a live incident (a buyer's conditional counter — *"...if the unit price
> would be 1900$. Kindly let me know and confirm"* — was regex-matched on the
> bare keyword "confirm" and auto-closed the negotiation), the acceptance flow
> was rebuilt. Where the sections below say a `NEGOTIATION_AGREED` routing
> immediately sets `status=AGREED` and advances the deal, the current behavior
> is:
>
> 1. **Intent classification is LLM-first** — `classify_inbound_intent()`
>    (graph.py, via the shared LLM Gateway) classifies every inbound reply as
>    ACCEPT / COUNTER_OFFER / REJECT / QUESTION / OTHER. The keyword guard
>    `is_explicit_acceptance()` (tightened: conditional / request-to-confirm
>    phrasing vetoes a match) is only the fallback when the gateway is down.
> 2. **Deterministic price-consistency veto** — an "acceptance" whose new text
>    quotes a USD price differing from the would-be agreed price is treated as
>    a counter (`acceptance_price_consistent()`, plus a cross-check against the
>    classifier's `mentioned_price_usd`).
> 3. **AGREED is HITL-gated** — a `NEGOTIATION_AGREED` graph verdict parks the
>    session at `HITL_PENDING` and creates a `CONFIRM_NEGOTIATION_AGREEMENT`
>    (L0) approval. Only the operator's APPROVE (→ `finalize_agreement()`)
>    moves the session to `AGREED`, computes realized CMS, and advances the
>    deal to `NEGOTIATION_AGREED`. REJECT is a soft reject
>    (→ `reject_agreement()`): the session reopens at `ROUND` and the
>    negotiation continues.
>
> Division of labor: **the LLM reads language, deterministic code checks the
> money, a human confirms the irreversible close.**

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
16. [Known Issues & Gaps (2026-07-10 review)](#16-known-issues--gaps-2026-07-10-review)
17. [Planned — Global Export Origin + Local-Currency Support](#17-planned--global-export-origin--local-currency-support)

---

## 1. Big Picture

```
Deal reaches NEGOTIATING status
         │
         ▼
POST /negotiation-agent/negotiations   ← start_negotiation()
         │
         ├─ load RAG context (HS code lookup, once per session)
         ├─ fetch live market intel (buyer + supplier, concurrently — §9)
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
         ├─ re-fetch live market intel (fresh every round, unlike RAG — §9)
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

> ✅ **Fixed 2026-07-10.** An earlier build short-circuited to `HITL_APPROVAL`
> *before* any agent drafted a message, so no email was created for margins below
> the warning band. The graph now **drafts first, then gates**: the near-floor and
> first-outbound conditions set `hitl_required=True` on the resulting draft instead
> of terminating the pass, and every draft is L0-gated for operator approval. The
> walk-through below is accurate again — one nuance: a non-terminal round ends the
> graph pass after a *single* outbound draft (it no longer loops internally). See
> [§16](#16-known-issues--gaps-2026-07-10-review).

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

`operator_override_price` sets the effective price for that party (here the buyer bid → 447.0); it is **not** a direct "agree now" flag — the orchestrator never reads it. On the next orchestrator pass the deterministic convergence guard runs: with `bid=447.0` and `ask≈447.05` the gap (0.05) is within the concession step, and *if* the override yields a margin ≥ floor, `check_convergence()` returns true, so routing becomes `NEGOTIATION_AGREED` and `agreed_price = midpoint(bid, ask)`:
- Session saved: `status=AGREED`
- `_compute_realized_cms()` attempts to recompute the final CMS via Match Engine (⚠️ currently a no-op — wrong call signature; see [§16.2](#16-known-issues--gaps-2026-07-10-review)).
- Domain event `negotiation.round` published (payload carries `status=AGREED`).
- Deal transitions to `NEGOTIATION_AGREED` → dealmaker issues a PI.

> ⚠️ If the override price still leaves margin below the floor, convergence does **not** fire — the guard routes to `HITL_APPROVAL` (near floor) or `NEGOTIATION_FAILED` (max rounds) instead. There is no code path that forces `AGREED` from the override flag alone.

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
- Presents a counter **at or above `floor_price`** — the minimum CIF price it may quote. The LLM never receives the supplier's raw ask, the midpoint, logistics, or the cost envelope — only this non-invertible floor — so the supplier's price cannot leak into a buyer email (hardened 2026-07-10).
- Uses RAG context to anchor the message (e.g., "Thailand benchmark is $xxx").
- Receives a **party-scoped** `conversation_history` (prior *sent/received* buyer emails, from `email_history`) so it continues the thread instead of restarting each round. The supplier thread is never included — this prevents the supplier's price from leaking to the buyer-facing LLM.
- Signals "final offer" when `concession_budget_remaining < 0.5 × concession_step`.
- Outputs `counter_price_usd`, `outbound_message`, `negotiated_terms` (JSON).
- Updates `current_bid` in the state.

### Node 3: `supplier_agent_node`

Drafts a message to the **supplier**. Key behaviors:
- Computes `implied_max = bid × (1 − floor%) − logistics − cost_envelope`.
- The LLM receives only `implied_max_supplier_price_usd` (its price ceiling), **never the buyer's raw bid** — so the buyer's price cannot leak into a supplier email (hardened 2026-07-10).
- Receives a **party-scoped** `conversation_history` (prior sent/received supplier emails) to continue the thread; the buyer thread is never included.
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

### QC provider costing — the buyer's inspector is priced in (2026-07-16)

The QC inspection is a real cost component of every deal, so it is priced in
**before** the first offer and re-priced when the buyer chooses an inspector:

1. **At `start_negotiation`** — if the caller didn't supply a non-zero
   `cost_envelope.qc_cost`, the service quotes Shield-Link
   (`POST /shield-link/qc/quote`, in-process in the monolith) for the opted
   provider (or the default, SGS) and writes the per-unit cost into the
   envelope. The margin formula above therefore includes the QC cost from
   round 0. On any quote failure it falls back to the static
   `QC_PROVIDER_CATALOG` fee — pricing never blocks on the quote.
2. **In the buyer email** — the buyer agent receives `qc_provider_options`
   (the top-5 supported inspectors: SGS, QIMA, Bureau Veritas, Intertek,
   TÜV SÜD — all available in India, photo/video-documented reports) and
   `buyer_selected_qc_provider`. The first outreach lists the options and asks
   the buyer to opt; later rounds remind once or confirm the choice. The
   inspection cost is included in the CIF price — never quoted separately.
3. **Capturing the buyer's choice** — two layers per inbound buyer reply:
   a deterministic single-name scan (`detect_qc_provider_mention`) runs
   *before* the graph pass so this round's floor already reflects the choice;
   the LLM intent classifier additionally returns `mentioned_qc_providers`
   (an array — positive preferences only, "not SGS" is excluded) applied after
   the pass. **One named agency → that agency. Several named acceptable
   agencies → the cheapest among them** (`cheapest_qc_provider`). The resolved
   key is stored on `NegotiationSession.qc_provider` and
   `cost_envelope.qc_cost` is re-quoted for that provider.
4. **Downstream** — at `POST /shield-link/qc` time, a request without an
   explicit provider books the buyer-opted one from the negotiation session.
   If the buyer never opted, the fallback is the **cheapest catalog provider**
   (`SHIELD_LINK_QC_FALLBACK_STRATEGY=cheapest`, the default; `default` books
   `default_qc_provider` instead). Pricing seeds with the same cheapest
   provider when unopted, so the floor and the eventual booking always price
   the same inspector.

### Agreement completeness checklist (2026-07-17)

Price convergence alone does not close a deal. When the orchestrator would
declare `NEGOTIATION_AGREED` (convergence or explicit acceptance at healthy
margin), a **deterministic checklist** (`guards.missing_agreement_info`) first
verifies every piece of information the downstream flow needs has actually
been received:

- the buyer's **QC inspection agency choice** (a choice arriving in the same
  accepting reply counts), and
- the **confirmed order quantity (MT)**.

If anything is missing, agreement is **withheld**: the buyer agent drafts an
email (guideline 1c) stating the commercial terms are provisionally acceptable
and explicitly requesting each missing item, with final confirmation to follow
— it never declares the deal closed. The draft is L0 HITL-gated like every
outbound message. An operator price override
(`RoundRequest.operator_override_price`) bypasses the checklist (a human took
the call), and `NEGOTIATION_AGREEMENT_CHECKLIST_ENABLED=false` disables it
entirely. Dev auto-flow pre-seeds `quantity_mt` + `qc_provider` so the
automated pipeline still reaches the agreement HITL gate.

### Freight (logistics) costing — CIF vs Ex-Works gap is priced in (2026-07-16)

Buyer prices are **CIF destination**, supplier quotes **Ex-Works/FOB** — so
ocean freight is PortLess's cost and belongs inside the margin formula's
`logistics` term. Historically `logistics_cost` was hard-coded `0.0`
(margins overstated by the entire freight cost). Now:

1. **At `start_negotiation`** — `_seed_logistics_cost()` quotes Shield-Link
   (`POST /shield-link/freight/quote`, read-only, no booking) for the lane,
   converts the per-container rate to per-unit (25 MT/40HC assumption,
   `ceil` for multi-container lots) and stores it on
   `NegotiationSession.logistics_cost_usd`. Both `start_negotiation` and
   `run_round` feed it into the graph's `logistics_cost` state, so every
   floor, convergence, and acceptance check includes freight from round 0.
2. **Caller override** — `StartNegotiationRequest.logistics_cost_usd` wins
   when supplied (e.g. Match Engine already computed the lane cost);
   `origin_port` / `destination_port` are optional lane hints for the quote.
3. **Escape hatch** — `NEGOTIATION_FREIGHT_COST_ENABLED=false` restores the
   legacy `logistics_cost=0` behavior (dev/demo whose seeded price gaps can't
   absorb a realistic freight cost). Quote failures also fall back to 0 with
   a loud log — pricing never blocks on the quote.
4. **Quotes vs commitments** — only the *quote* happens early; the freight
   *booking* stays gated on `QC_PASSED` + the `BOOK_FREIGHT` HITL approval
   (parallel quotes, sequential commitments).

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
result = await asyncio.wait_for(
    rag_query(question, metadata_filter={"hs_code": hs_code}),
    timeout=settings.rag_timeout_seconds,
)
```

The returned `rag_context` string is stored in the `NegotiationSession` document and injected into **every LLM call** for the lifetime of the negotiation — both orchestrator and both sub-agents receive it.

**Escape hatch + hang protection.** `NEGOTIATION_RAG_ENABLED=false` skips the
shared RAG pipeline entirely and always uses the static playbook stub below —
added because the RAG pipeline's local cross-encoder reranker can block on a
HuggingFace Hub connectivity check that never returns, a genuinely synchronous
call that freezes the whole single-process asyncio server (not just the one
request) since `asyncio.wait_for` can't preempt non-yielding code. The
`rag_timeout_seconds` wrap above (default 15s) catches every other kind of
hang (a vector-store or embedding call with no timeout of its own) and is
treated the same as any other RAG failure — fall back to the static stub.

### What RAG provides

| HS Code | RAG Playbook Content |
|---------|---------------------|
| `1006` (Rice) | Thailand 5% broken benchmark price, LC at sight, 21–30 day lead time, moisture ≤14%, buyer counters −8% |
| `1001` (Wheat) | CME CBOT front-month reference, protein premium $8–12/MT, Black Sea lead times, MOQ 500 MT |
| `6101` (Apparel) | Factory-gate pricing, 500–1000 pcs MOQ, 30% TT advance, OEKO-TEX 100 for EU |
| Any other | Generic: anchor buyer −10%, supplier +12–15%, 3–5 rounds to converge, FOB/CIF defaults |

### Fallback behavior

If the MongoDB Atlas vector store is unconfigured or returns no results, `rag.py` falls back to static product playbooks hardcoded in `_FALLBACK_PLAYBOOKS`. The negotiation always proceeds — the LLM just has less precise market intelligence.

### Market Intelligence — live web search (`market_intel.py`)

Unlike `rag_context` (loaded once per session, since a trade playbook doesn't
change round to round), `market_intel.fetch_both(product_code, buyer_country_code)`
runs **fresh on every round** — "current market pricing" is only useful if
it's current at draft time. It fetches party-scoped web-search snippets
(current import pricing/demand for the buyer's market; current India
export/Ex-Works pricing for the supplier side, since India is the sole
export origin convention used throughout `shared/connectors/trade_intel.py`)
concurrently via `asyncio.gather`, and writes them into
`buyer_market_context` / `supplier_market_context` on the graph state —
consumed by `buyer_agent_node` / `supplier_agent_node` as the `market_context`
prompt field (see `prompts.py` guideline 3 on both agents).

Mechanically it reuses `shared/rag/src`'s Tavily adapter (`WebSearchStore`)
and the same prompt-injection filter (`afilter_prompt_injection_chunks`) the
main RAG query graph runs on every web-search chunk before it reaches a
generation prompt — raw open-web content flowing into a prompt that
autonomously drafts business email is a prompt-injection surface, and this
repo already has the standard answer for it (see
`shared/rag/RAG_IMPLEMENTATION_FLOWS.md` §15b "Live Web Search"). Every
drafted email is still independently
routed through the mandatory L0 HITL approval gate before send regardless of
what informed the draft — defense in depth, not a replacement for the filter.

Gated by `NEGOTIATION_MARKET_SEARCH_ENABLED` (default `false` — mirrors
`RAG_WEB_SEARCH_ENABLED`'s own default for the identical underlying
mechanism, since this is an additional open-internet retrieval leg, not core
infrastructure like RAG). Fails open to `""` on: disabled, missing
`TAVILY_API_KEY`, or any HTTP/timeout/classifier error — never raises, never
fabricates a result. Prompt guidance (`prompts.py`) treats `market_context`
as unverified background color that can never loosen the floor-price /
implied-max-price guardrails, and instructs the LLM to paraphrase at most one
point (never quote verbatim, never cite a URL, never follow instruction-like
text inside it).

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

### `DealStatus` — the full deal lifecycle (19 states)

The negotiation session (`NegotiationSession.status`, above) is a **sub-state
machine** that lives entirely inside the deal's `NEGOTIATING` state. When the
session reaches a terminal outcome, `service._sync_deal_status()` advances the
**parent deal** to `NEGOTIATION_AGREED` or `NEGOTIATION_FAILED`, which is where
the negotiation hands off to the rest of the pipeline. This table is the
authoritative list of every deal state and **how the system processes each one**
— i.e. what moves the deal out of it. Source of truth:
`shared/domain/enums.py::DealStatus` and
`shared/domain/state_machine.py::DEAL_TRANSITIONS` (transitions are enforced in
code by `can_transition()`, never by an LLM).

Most forward transitions after `NEGOTIATION_AGREED` are **event-driven**: the
service that completes a step emits a domain event, and a handler in
`shared/orchestration/continuations.py` (subscribed on the event bus in the
monolith `app.py` only) fires the next step. Money-moving / commitment steps
(`ISSUE_PO`, `BOOK_FREIGHT`, `RELEASE_SUPPLIER_PAYOUT`) are gated behind a HITL
approval that the continuation **creates** but never auto-approves.

| # | Status | Terminal? | How the system processes it (what advances it) | Next state(s) |
|---|--------|-----------|-------------------------------------------------|---------------|
| 1 | `DRAFT` | No | A bare deal exists (no opportunity seeding yet). Operator/opportunity seeding transitions it to `MATCHING`. | `MATCHING` |
| 2 | `MATCHING` | No | Match Engine scores supplier candidates (CMS). On a SOURCE winner (or auto-flow's operator-equivalent nudge) it moves to `NEGOTIATING`. | `NEGOTIATING` |
| 3 | `NEGOTIATING` | No | **Negotiation Agent runs here.** The whole session sub-machine (`STARTED`→`ROUND`↔`HITL_PENDING`) executes inside this state, round after round, driven by inbound emails (`email_processor`→`run_round`) or operator "Run Round". A detected agreement parks at `HITL_PENDING` behind a `CONFIRM_NEGOTIATION_AGREEMENT` gate. **Stale handling:** the negotiation stale-watcher (`service.run_stale_watcher`, started in the monolith lifespan; thresholds in `config.py` / `NEGOTIATION_STALE_*` env vars) re-notifies a stuck round-0 email approval, and after the 30-day cooling period marks a quiet post-round-0 session `FAILED` so the deal leaves `NEGOTIATING`. | `NEGOTIATING` (counter-round), `NEGOTIATION_AGREED`, `NEGOTIATION_FAILED` |
| 4 | `NEGOTIATION_AGREED` | No | Reached **only** via the `CONFIRM_NEGOTIATION_AGREEMENT` HITL approve → `finalize_agreement()`, which publishes `NEGOTIATION_AGREED`. Continuation `on_negotiation_agreed` auto-issues the Proforma Invoice → `PI_SENT`. | `PI_SENT` |
| 5 | `NEGOTIATION_FAILED` | No | Set by `_sync_deal_status` when the session is `FAILED` (max rounds, or the stale-watcher's cooling-period timeout). Routes to the Exception Resolver / re-match: operator can send it back to `MATCHING` or `CANCELLED`. | `MATCHING`, `CANCELLED` |
| 6 | `PI_SENT` | No | DealMaker has issued the PI. Buyer acceptance (`POST /dealmaker/pi/{id}/accept`) → `PI_ACCEPTED`. Acceptance is a **buyer action** — no handler auto-accepts. | `PI_ACCEPTED` |
| 7 | `PI_ACCEPTED` | No | Continuation `on_pi_accepted` opens the neutral escrow (sized from the accepted PI) → `ESCROW_PENDING`. | `ESCROW_PENDING` |
| 8 | `ESCROW_PENDING` | No | Escrow is open; **buyer funds it**. Stripe/escrow-funded webhook → `ESCROW_FUNDED`. Funding is a buyer action; no handler fakes it. | `ESCROW_FUNDED` |
| 9 | `ESCROW_FUNDED` | No | Continuation `on_escrow_funded` creates the **`ISSUE_PO` HITL gate**. Operator approve → `dealmaker.issue_po()` (margin guard ≥8%) → `PO_ISSUED`. | `PO_ISSUED` |
| 10 | `PO_ISSUED` | No | Continuation `on_po_issued` books QC with the buyer-opted provider → deal `SUPPLIER_DISPATCH_READY`. | `SUPPLIER_DISPATCH_READY` |
| 11 | `SUPPLIER_DISPATCH_READY` | No | Awaiting the QC inspection result webhook (SGS/QIMA/etc.). PASS → `QC_PASSED`; FAIL → `QC_FAILED`. | `QC_PASSED`, `QC_FAILED` |
| 12 | `QC_PASSED` | No | Continuation `on_qc_passed` creates the **`BOOK_FREIGHT` HITL gate**. Operator approve → `shield_link.book_freight()` → `IN_TRANSIT`. | `IN_TRANSIT` |
| 13 | `QC_FAILED` | No | Quality failure → `DISPUTE` for resolution (rework, partial refund, cancel). | `DISPUTE` |
| 14 | `IN_TRANSIT` | No | Freight booked and moving. Freight milestone webhook `ARRIVED`/`CLEARED` → `CLEARED`. | `CLEARED` |
| 15 | `CLEARED` | No | Cargo cleared customs. Continuation `on_customs_cleared` creates the **`RELEASE_SUPPLIER_PAYOUT` HITL gate**. Operator approve → payout; payout-completed webhook → `PAID`. | `PAID` |
| 16 | `DISPUTE` | No | Exception/dispute handling (from `QC_FAILED`). Resolves to `CANCELLED` (abort) or `REFUNDED` (buyer refunded). | `CANCELLED`, `REFUNDED` |
| 17 | `PAID` | **Yes** | **Happy-path terminal.** Supplier paid, deal complete. No further transitions. | — |
| 18 | `CANCELLED` | **Yes** | Terminal — deal aborted (from `NEGOTIATION_FAILED` or `DISPUTE`). | — |
| 19 | `REFUNDED` | **Yes** | Terminal — buyer refunded (from `DISPUTE`). | — |

> Count: **19 `DealStatus` values** (one per row above; verified against
> `shared/domain/enums.py::DealStatus`). Three are truly terminal — `PAID`
> (happy path), `CANCELLED`, `REFUNDED` — with no outgoing transitions.
> `NEGOTIATION_FAILED` is a *hand-off* state, not terminal: it always routes on
> to `MATCHING` (re-match) or `CANCELLED` via the Exception Resolver, so a deal
> never sits in it forever.

**Where the negotiation agent touches the deal machine:** only two edges.
It runs the entire round loop *within* `NEGOTIATING`, then calls
`_sync_deal_status()` exactly once at a terminal session outcome to emit either
`NEGOTIATION_AGREED` (→ PI issuance downstream) or `NEGOTIATION_FAILED` (→
Exception Resolver). Everything after `NEGOTIATION_AGREED` is out of the agent's
hands and driven by the event-bus continuations above.

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
| `NEGOTIATION_RAG_ENABLED` | `true` | `false` skips the shared RAG pipeline entirely, always using the static playbook stub (see §9 hang-protection note) |
| `rag_timeout_seconds` (field default; no `NEGOTIATION_` env prefix — set via `RAG_TIMEOUT_SECONDS`) | `15.0` | Wall-clock budget for `load_rag_context()`; exceeding it falls back to the static stub |
| `NEGOTIATION_MARKET_SEARCH_ENABLED` | `false` | Fetch live web-search market pricing/demand color (Tavily) before each buyer/supplier draft — see §9 |
| `TAVILY_API_KEY` | `""` | Shared with `shared/rag`'s own web-search leg; market search is a no-op without it even when enabled |

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

### Real contact addresses (`buyer_email` / `seller_email`)

`StartNegotiationRequest` accepts optional `buyer_email` and `seller_email`
fields, stored on the `NegotiationSession` and used in place of the
placeholder addresses when dispatching outbound drafts:

```python
to_address = session.buyer_email or f"{session.buyer_id}@portless.placeholder"
to_address = session.seller_email or f"{session.seller_id}@portless.placeholder"
```

`buyer_id` / `seller_id` remain the identifiers used for matching and
reporting — these two fields only control where the actual email is sent.
When unset, outbound drafts still target the unreachable
`@portless.placeholder` address, same as before (log-only/no-op send).

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

> Last reviewed: 2026-07-10; RAG resilience + market-intel rows added 2026-07-14.  
> Legend: ✅ Fully implemented · ⚠️ Partial / stub · ❌ Not started / broken · 💡 Nice-to-have  
> **See [§16](#16-known-issues--gaps-2026-07-10-review) for the code-vs-doc audit** — several rows below were corrected on 2026-07-10.

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
| Operator override price (`operator_override_price`) | ⚠️ | Sets the effective bid/ask for the round; does **not** force `NEGOTIATION_AGREED` — agreement still requires the convergence guard. See §16.3 |
| Deal idempotency (reuse existing session) | ✅ | Reuses non-terminal session for same `deal_id` |
| NegotiationSession MongoDB persistence | ✅ | Motor + InMemory fallback |
| NegotiationRound immutable audit log | ✅ | One record per `run_round()` call |
| Post-agreement Realized CMS via Match Engine | ✅ | Fixed 2026-07-10 — builds a proper `ScoreRequest`; `realized_cms` now populated. See §16.2 |
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
| RAG escape hatch + hang protection (`NEGOTIATION_RAG_ENABLED`, `rag_timeout_seconds`) | ✅ | `rag.py`, `config.py` | Added 2026-07-14 — see §9 |
| Live web-search market intelligence (`market_intel.py`, Tavily) | ✅ | `market_intel.py`, `graph.py`, `prompts.py` | Added 2026-07-14 — fresh per-round buyer/supplier market color, gated by `NEGOTIATION_MARKET_SEARCH_ENABLED` (default off), prompt-injection filtered. See §9. Supersedes the "planned" web-search note that used to live here. |

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
| Deal status transition on AGREED/FAILED | ✅ | Wired 2026-07-10 — `_sync_deal_status()` calls `deals.update_status` (AGREED → `NEGOTIATION_AGREED`, FAILED → `NEGOTIATION_FAILED`) from `_post_graph_notify`; best-effort + idempotent. The deal-service transition emits the domain event, so this is the single hand-off point to other services. |
| Dealmaker PI trigger on AGREED | ⚠️ | The AGREED deal transition **unblocks** DealMaker's `issue_pi` (which gates on `NEGOTIATION_AGREED`), but the PI is **not** auto-fired here — it needs quantity/incoterm/ports/buyer-address the negotiation session doesn't hold, so `POST /dealmaker/pi` remains a separate operator/orchestrator step. |
| Mock seed data for UI dev | ✅ | `_mock_seed.py` — 5 scenarios, 15 emails |
| Automated tests | ✅ | Added 2026-07-10, extended 2026-07-14 — `tests/test_negotiation_agent.py` (35 tests): guards, orchestrator routing, graph draft, email lifecycle, realized-CMS, conversation-history, market-context wiring, price-secrecy, deal hand-off. Plus `tests/test_market_intel.py` (5 tests) for the market-intel fetch/format/fail-open behavior. See §16.4 |
| Conversation-history context in agent drafts | ✅ | Party-scoped transcript of sent/received emails injected into buyer/supplier LLM payloads so drafts continue the thread. Live web-search market color now also injected per round — see §9. |
| Counterparty price secrecy (hard guarantee) | ✅ | Hardened 2026-07-10 — the buyer agent gets `floor_price` (not the raw ask/midpoint); the supplier agent gets `implied_max` (not the raw bid). Raw counterparty price never enters the LLM payload; leak-audit tests assert it. |

---

### Nice-to-have (not started)

| Feature | Priority | Notes |
|---------|----------|-------|
| **Global export origin + local-currency support** (multi-country supplier side) | **High** | See the dedicated spec in §17 below. Today the **supplier/origin side is hard-locked to India + INR** while only the buyer/destination side is country-aware — so sourcing from Vietnam (VND), China (CNY), Bangladesh (BDT), etc. still drafts supplier emails in INR and searches India export pricing. This item generalizes origin + currency across the negotiation so PortLess can trade worldwide. |
| Dedicated commodity-exchange price feed (CME/Comdty/Volza) | Medium | §9's `market_intel.py` now covers general live market color via Tavily web search; a structured intraday exchange-price feed would be more precise but is a separate, not-yet-built integration |
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

---

## 16. Known Issues & Gaps (2026-07-10 review)

A code-vs-doc audit on **2026-07-10** found that sections 1–14 described the
**intended** design while the shipped code diverged. Items **16.1, 16.2 and 16.4
were fixed the same day** (see the ✅ resolution notes); **16.3** was a doc-only
overstatement (now corrected) and **16.5** has been cleaned up. Ranked
most-impactful first.

### 16.1 — Agent nodes are suppressed for below-floor margins → few / no emails drafted (High)

`orchestrator_node` evaluates its guards in priority order and **returns to
`__end__` as soon as one fires, before any buyer/supplier agent node runs**. Two
of those guards dominate the body of a real negotiation:

- **Near-floor guard** — `is_near_margin_floor()` returns
  `margin_pct < margin_floor_pct + hitl_margin_warning_pct`. Because a negotiation
  runs *below* the floor until it converges, this is true on essentially every
  early round (a −22 % opening margin counts as "near floor"), so routing goes to
  `HITL_APPROVAL` rather than to an agent.
- **First-outbound guard** — `is_first_round AND hitl_on_first_outbound`
  (default `True`) routes to `HITL_APPROVAL` on round 0.

When routing is `HITL_APPROVAL` / `NEGOTIATION_AGREED` / `NEGOTIATION_FAILED`, no
agent node runs, so `buyer_outbound_message` / `supplier_outbound_message` stay
empty and `_post_graph_notify()` creates **no email draft and no
`SEND_NEGOTIATION_EMAIL` approval**. Net effect: the agents draft a counter-offer
(and therefore an email) only in the narrow window where margin is *already*
≥ floor + warning — i.e. when the deal is already profitable. During the hard part
of the negotiation the agent produces no outreach, and the autonomous email
back-and-forth in §2/§14 does not occur unless an operator manually drives each
round with `operator_override_price`.

**This is the answer to "does the agent email whenever needed?":** the *decision*
to draft an email is driven purely by graph output (did an agent node produce a
message?), and the *send* is correctly gated by the DB-status lifecycle
(DRAFT → PENDING_APPROVAL → SENT via an L0 HITL approval — see §7, which is
accurate). But the guard layer suppresses the agent nodes for below-floor margins,
so the drafting step rarely runs in practice.

**Recommended fix (needs a design decision):** gate the *email send* (already
L0-HITL-gated in `_create_and_gate_email`), not the *drafting*. Let the agent node
run and draft the message, then route to HITL for approval. Concrete options:
(a) reorder so the agent drafts first and the HITL gate applies to the resulting
draft; (b) narrow `is_near_margin_floor` to the warning *band*
(`floor ≤ margin < floor + warn`) so deeply-negative margins keep negotiating;
(c) drop the redundant first-outbound short-circuit, since every email already
passes an L0 gate regardless.

**✅ Fixed 2026-07-10 (option a).** `orchestrator_node` no longer returns before
drafting: near-floor and first-outbound now set `hitl_required=True` and fall
through to agent routing (with a safety clamp that forces a buyer/supplier route on
any non-terminal pass), and the agent nodes end the pass after one draft
(`buyer_agent` / `supplier_agent` → `END`). A below-floor round now produces exactly
one L0-gated outbound draft. Covered by
`tests/test_negotiation_agent.py::test_graph_below_floor_drafts_one_message_and_ends`
plus the orchestrator routing tests.

### 16.2 — `_compute_realized_cms()` always fails (Medium)

`service._compute_realized_cms()` calls
`score_match(opportunity_id=…, buyer_price=…, supplier_price=…)`, but Match
Engine's real signature is `score_match(payload: ScoreRequest) -> ScoreResponse`.
The call raises `TypeError` on every invocation; it is wrapped in `try/except` and
logged as "non-fatal", so **`realized_cms` is never populated** after an agreement.
Fix: build a `ScoreRequest` and pass it, exactly as `sourcing._rescore` and
`deals._score_seller` already do.

**✅ Fixed 2026-07-10.** `_compute_realized_cms()` now builds a `ScoreRequest`
(agreed price as buyer target vs the supplier's final ask, pass-through
verification) and reads `resp.composite_score`. Covered by
`test_compute_realized_cms_populates_score`.

### 16.3 — `operator_override_price` is not an "agree now" flag (Medium, doc-only)

`operator_override_price` only sets the effective bid/ask for that round
(`service.run_round`). The orchestrator never reads it, so **no code path forces
`NEGOTIATION_AGREED` from the override alone** — agreement still requires the
convergence guard (gap ≤ step AND margin ≥ floor). The §2 "Operator Override →
AGREED" narrative and the §15 override row overstated this; both corrected in this
revision.

### 16.4 — No automated tests (Medium)

~~There is no `tests/test_negotiation_agent.py`.~~

**✅ Fixed 2026-07-10, extended 2026-07-14.** Added `tests/test_negotiation_agent.py`
(35 tests): `guards.py` pure functions, orchestrator routing (AGREED / FAILED / near-floor /
first-outbound / healthy / terminal-clamp), a graph end-to-end draft, the email
lifecycle (send / modify / reject / fail / inbound), realized-CMS, party-scoped
conversation-history injection, market-context wiring (buyer/supplier isolation
guard), the price-secrecy hardening (raw counterparty
price / BATNA never enter either agent's LLM payload), and the deal hand-off
(AGREED/FAILED → `deals.update_status`, idempotent + error-safe). Plus
`tests/test_market_intel.py` (5 tests) covering `market_intel.py`'s query
building, result formatting/truncation, and fail-open behavior (disabled,
missing key, HTTP/timeout error).

### 16.5 — Stale bytecode (Low)

`__pycache__/email_dispatcher.cpython-313.pyc` had no corresponding source — the
module was renamed to `email_history.py`. **✅ Removed 2026-07-10.**

---

## 17. Planned — Global Export Origin + Local-Currency Support

> **Status: NOT STARTED** (nice-to-have, High priority — see §15). This section is
> a design spec for the backlog item, not shipped behavior. It exists so whoever
> picks it up has the full picture without re-deriving it.

### 17.1 — Why (the gap today)

PortLess negotiation is **buyer-side multi-country but supplier-side India-only**.
The buyer/destination is genuinely parameterized (`buyer_country_code`, buyer-CIF
import market intel), but the supplier/origin side is hard-coded to India + INR:

| Hard-coded assumption | Location | Effect on a non-India source (e.g. Vietnam) |
|-----------------------|----------|----------------------------------------------|
| `_SUPPLIER_ORIGIN_COUNTRY = "India"` | `market_intel.py:36` | Supplier market-intel search always queries **India** export pricing, never Vietnam |
| No `seller_country_code` on the session | `schemas.py::NegotiationSession` | Nothing records where the supplier actually is |
| INR is the only supplier quote currency | `prompts.py` (supplier prompt) | Supplier email is drafted in **INR**, not VND |
| USD↔INR is the only FX path | `market_intel.py` (`get_usd_inr_rate`, `_parse_usd_inr_rate`, `_USD_INR_PLAUSIBLE_RANGE`, `fallback_usd_inr_rate`) | No VND/CNY/BDT/etc. conversion exists — a wrong or missing rate |

Net: sourcing from Vietnam still emails the supplier an **INR** price computed from
**India** market color — wrong currency, wrong market. This blocks true global
trading; the goal is any export origin quoting in its own local currency.

### 17.2 — What to change (scope)

1. **Schema** (`schemas.py`) — add to `NegotiationSession` **and**
   `StartNegotiationRequest`:
   - `seller_country_code: str` (ISO-3166 alpha-2, e.g. `"VN"`, `"CN"`, `"BD"`).
   - `seller_currency: str` (ISO-4217, e.g. `"VND"`, `"CNY"`, `"BDT"`); default
     derivable from `seller_country_code` via a small country→currency map, or
     explicit override.
   - Keep **USD as the source-of-truth unit** everywhere internally (margin math,
     `agreed_price`, escrow, PI) — only the *supplier-facing quote* is localized.
     This preserves every downstream contract (DealMaker/Escrow all speak USD).

2. **Generalize FX** (`market_intel.py`) — replace the USD/INR-specific trio with a
   parameterized `get_usd_to_local_rate(currency_code)`:
   - Same Tavily web-search + strict-parse + plausible-band pattern, but the query,
     regex anchors, and band are keyed on the target currency (a per-currency
     plausible band table replaces `_USD_INR_PLAUSIBLE_RANGE`).
   - Per-currency static fallback replacing the single `fallback_usd_inr_rate`
     (e.g. a `NEGOTIATION_FALLBACK_FX_RATES` map / JSON env var).
   - `INR` stays as one entry — this is a generalization, not a rewrite; India
     keeps working unchanged.

3. **Origin-aware market intel** (`market_intel.py`) — `fetch_both` /
   `fetch_market_intel` take the real `seller_country_code` instead of the
   `"India"` constant, so the supplier-side search reflects the true origin's
   export/Ex-Works conditions.

4. **Supplier prompt** (`prompts.py`) — parameterize the "quote in INR" instruction
   to "quote in `{seller_currency}`", passing the pre-computed
   `implied_max_supplier_price_{local}` figures (same deterministic-conversion
   discipline already used for INR — the agent must **never** compute FX itself;
   §"USD/INR exchange rate" note in `market_intel.py` explains why).

5. **Auto-flow / seeding** (`shared/orchestration/auto_flow.py`, deal→negotiation
   seeding) — thread `seller_country_code` through from the match/opportunity data
   so it's populated automatically, with a safe default of `"IN"`/`"INR"` for
   back-compat when the source doesn't supply it.

### 17.3 — Guardrails / non-goals

- **Do not** change the internal accounting currency — USD stays the pivot; a
  bug here would ripple into escrow/PI/margin. Only the outbound supplier quote is
  localized.
- **Never let the LLM compute FX** — keep the "rate fetched + converted in code,
  passed as ready figures" contract; it's the documented fix for the token-burn
  spiral (`market_intel.py` §"USD/INR exchange rate").
- Fail-open exactly as today: no rate / disabled search → per-currency static
  fallback, never a guessed or zero rate.
- Buyer side is already country-aware — **no change** there.

### 17.4 — Test additions

- Per-currency FX parse/fallback (mirror `tests/test_market_intel.py`'s USD/INR
  cases for at least VND + one more).
- Origin-aware supplier market-intel query building (Vietnam query ≠ India query).
- Supplier draft quotes in the session's `seller_currency`, and a leak-style
  assertion that internal USD figures still drive margin math unchanged.
- Back-compat: a session with no `seller_country_code` still behaves as India/INR.
