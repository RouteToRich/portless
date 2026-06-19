# PortLess — Web Application Build Blueprint

> **From concept to a runnable, event-driven, agentic web application.**
> This document turns the PortLess HLD and Orchestrator concept into a concrete software build plan:
> UI → Backend → Database → Agentic AI → Event-Driven backbone, with the new features needed to run a
> merchant-export business with **minimal manual intervention**.

This is a *companion* to:
- [merchant_exporter_orchestrator_app.md](merchant_exporter_orchestrator_app.md) — business + API landscape
- [merchant_exporter_hld.md](merchant_exporter_hld.md) — phased rollout + risk controls

---

## 0. What This Document Adds (Gap Fixes + New Features)

The existing docs describe *what* the business does. This blueprint describes *how to build it* and closes these gaps:

| # | Gap in existing docs | New feature / fix added here |
|---|---|---|
| 1 | No concrete software architecture (just business flow) | Full **UI / API / Worker / Event-bus / DB** topology (§3) |
| 2 | "Approval via SMS" is vague | Dedicated **HITL Approval Service** with typed approval requests, deep links, expiry, and audit trail (§7) |
| 3 | Agents described conceptually, not as software | **Agent runtime** with typed tool contracts, deterministic guardrails, and a planner/executor split (§6) |
| 4 | No reliability story for money movement | **Transactional Outbox + Idempotency keys + Saga** pattern so no payout/booking ever double-fires (§5.4) |
| 5 | "Double-invoice mask" = legal risk | Reframed to lawful **Merchant-of-record + Switch BoL** with a **Compliance Guard** that blocks unlawful concealment (§9) |
| 6 | No way to recover from a stuck deal | **Deal State Machine** + **Exception/Dispute sub-workflow** + replayable events (§4, §8) |
| 7 | No live visibility | **Real-time Ops Console** (WebSocket) showing every deal, agent action, and pending approval (§2) |
| 8 | No "minimal intervention" definition | **Autonomy Levels L0–L3** per action class with policy config (§7.3) |
| 9 | No local dev path | **Docker-compose dev stack + seed/sandbox mode** so you can run it on a laptop (§11) |

---

## 1. Product Shape & Personas

**Single primary user (you, the operator).** The app should let one person run dozens of concurrent export deals.

| Persona | Goal | Surface |
|---|---|---|
| **Operator** (you) | Approve/monitor, override, view margins | Ops Console (web) + mobile push |
| **Supplier** (factory) | Receive RFQ, accept PO, mark ready-to-dispatch | Tokenized supplier mini-portal (no login sprawl) |
| **Buyer** (importer) | Accept PI, fund escrow, track shipment | Tokenized buyer mini-portal |
| **3rd-party services** | QC / freight / CHA / escrow callbacks | Webhook endpoints (machine-to-machine) |

Design principle: **agents do the work, the operator supervises by exception.**

---

## 2. UI Layer

### 2.1 Stack
| Concern | Choice | Why |
|---|---|---|
| Framework | **Next.js 14 (App Router) + React + TypeScript** | SSR for fast dashboards, API routes for BFF |
| UI kit | **Tailwind CSS + shadcn/ui** | Fast, consistent, accessible components |
| State/data | **TanStack Query** + **Zustand** | Server cache + light client state |
| Realtime | **WebSocket (Socket.IO)** or **SSE** | Live deal + agent activity stream |
| Charts | **Recharts** | Margin / pipeline visualization |
| Auth | **Auth.js (NextAuth)** + passkeys/TOTP | Operator login; tokenized links for buyer/supplier |

### 2.2 Key Screens
1. **Opportunity Radar** — ranked HS-code arbitrage cards (demand surge, est. margin, confidence). One-click "Start Deal".
2. **Deal Board (Kanban)** — columns = state-machine stages (`MATCHING → PI_SENT → ESCROW_FUNDED → QC → IN_TRANSIT → CLEARED → PAID`). Each card streams live agent actions.
3. **Deal Detail** — timeline of events, documents, party info, agent reasoning log, and the **Approve / Reject / Override** action bar.
4. **Approvals Inbox** — every pending HITL decision with deadline countdown and the exact financial impact.
5. **Money & Margin** — escrow balances, scheduled payouts, realized vs. projected margin, FIRC/RoDTEP tracking.
6. **Exceptions** — disputes, failed QC, stuck shipments, with suggested agent remediation.
7. **Autonomy Settings** — per-action-class autonomy level (L0–L3) and guardrail thresholds (§7.3).
8. **Kill Switch** — global "Pause All Deals" with confirmation.

### 2.3 Mini-Portals (tokenized, no account)
- Buyer/supplier receive a **signed, expiring magic-link** (JWT, single-deal scope).
- Actions are intentionally tiny: *Accept Quote*, *Upload Doc*, *Mark Ready*, *Fund Escrow*.
- Every action emits a domain event.

---

## 3. System Architecture (Topology)

```
                       ┌───────────────────────────────────────────────┐
                       │                  UI (Next.js)                  │
                       │  Ops Console · Approvals · Mini-Portals (BFF)  │
                       └───────────────┬───────────────┬───────────────┘
                              REST/tRPC│         WS/SSE │ (live stream)
                                       ▼               ▲
                       ┌───────────────────────────────────────────────┐
                       │              API Gateway (FastAPI)             │
                       │  AuthZ · Validation · Idempotency · Webhooks   │
                       └───────┬───────────────────────────┬───────────┘
                               │ publish/consume           │ read/write
                               ▼                           ▼
              ┌────────────────────────────┐      ┌──────────────────────┐
              │   EVENT BUS (Kafka/Redpanda │      │  PostgreSQL+pgvector │
              │   or NATS JetStream)        │      │  (source of truth)   │
              └───────┬───────────┬─────────┘      └──────────────────────┘
         consume      │           │ consume                  ▲  outbox relay
            ┌─────────▼──┐   ┌────▼─────────┐                │
            │  AGENT      │   │  WORKERS     │────────────────┘
            │  RUNTIME    │   │ (integrations│
            │ (LangGraph) │   │  + sagas)    │
            └─────┬───────┘   └────┬─────────┘
                  │ tool calls     │ external APIs
                  ▼                ▼
        ┌───────────────────────────────────────────────┐
        │  EXTERNAL TOOLS / CONNECTORS (adapters)        │
        │  Comtrade · Volza · IndiaMART · GSTN · DGFT    │
        │  SGS · Cogoport/Freightos · ICEGATE · Stripe   │
        └───────────────────────────────────────────────┘
```

**Three runtime tiers:**
1. **API Gateway** — synchronous edge: auth, validation, webhook ingest, idempotency, command publishing.
2. **Agent Runtime** — long-running reasoning loops (the "brain"); calls tools, requests approvals.
3. **Workers** — deterministic integration + saga steps (the "hands"); idempotent, retryable.

The event bus decouples all three so any tier can crash/restart without losing work.

---

## 4. Deal State Machine (Core Domain)

Every deal is a state machine. Transitions are triggered **only** by events, making the system replayable and auditable.

```
 DRAFT ──► MATCHING ──► PI_SENT ──► PI_ACCEPTED ──► ESCROW_PENDING
                                                        │
                                                        ▼
   PAID ◄── CLEARED ◄── IN_TRANSIT ◄── QC_PASSED ◄── ESCROW_FUNDED ──► PO_ISSUED
     ▲                                    │                                │
     │                                    ▼                                ▼
     └─────────────── DISPUTE ◄──── QC_FAILED               SUPPLIER_DISPATCH_READY
                          │
                          └──► CANCELLED / REFUNDED
```

Guard conditions (enforced in code, never by an LLM alone):
- `ESCROW_FUNDED` cannot be entered unless escrow webhook is verified.
- `PO_ISSUED` cannot fire unless `net_margin ≥ margin_floor` **and** buyer is credit-cleared or fully prepaid.
- `PAYOUT` cannot fire unless `QC_PASSED` + matching docs (dual-doc check) pass.

State is persisted; transitions are emitted as events (§5.3).

---

## 5. Event-Driven Backbone

### 5.1 Why event-driven
- **Resilience:** a failed freight API call retries without blocking QC.
- **Auditability:** the event log *is* the truth; you can replay any deal.
- **Extensibility:** add a new agent/worker by subscribing to existing events.

### 5.2 Bus choice
- **Default: NATS JetStream** (lightweight, easy to self-host, great for a solo operator).
- **Scale option: Kafka / Redpanda** when throughput grows.

### 5.3 Core Event Catalog (topics)
| Topic | Producer | Key consumers | Payload (essentials) |
|---|---|---|---|
| `opportunity.detected` | Trade-Min Agent | UI, Operator | hs_code, country, demand_delta, est_margin |
| `deal.created` | API | Agent Runtime | deal_id, hs_code, parties? |
| `match.scored` | Match Engine | Agent Runtime, UI | deal_id, cms, factors |
| `quote.requested` | Sourcing Agent | Worker (IndiaMART) | deal_id, supplier_id, specs |
| `pi.sent` / `pi.accepted` | DealMaker Agent / Buyer portal | Escrow Worker | deal_id, amounts |
| `escrow.funded` | Stripe webhook → API | Saga | deal_id, amount, currency |
| `po.issued` | DealMaker Agent | Supplier portal | deal_id, supplier_id, terms |
| `qc.booked` / `qc.passed` / `qc.failed` | Shield-Link / SGS webhook | Saga, DocuGen | deal_id, crf_hash |
| `freight.booked` / `freight.milestone` | Logistics Worker / carrier webhook | UI | deal_id, booking_id, eta |
| `customs.filed` / `customs.cleared` | DocuGen / ICEGATE | Payout Saga | deal_id, shipping_bill_no |
| `payout.requested` / `payout.completed` | Payout Saga | Ledger, UI | deal_id, payee, amount |
| `approval.requested` / `approval.resolved` | Any agent | HITL Service, UI | approval_id, action, impact |
| `deal.exception` | Any | Exception workflow | deal_id, reason, severity |

### 5.4 Reliability patterns (the new reliability layer)
- **Transactional Outbox:** writes to Postgres and the "to-publish" outbox in one transaction; a relay publishes to the bus. No lost events.
- **Idempotency keys:** every command/webhook carries an idempotency key; workers dedupe. No double payouts.
- **Saga orchestration:** money/booking flows are sagas with compensating actions (e.g., `freight.booked` fails after `po.issued` → emit `po.cancel`).
- **Dead-letter queue:** poison messages route to DLQ → surfaced as an Exception in the UI.

---

## 6. Agentic AI Layer (Latest Agentic Patterns)

Agentic AI is used **where reasoning over messy real-world data is required** (matching, doc parsing, negotiation, exception handling) — **not** for deterministic money/state logic.

### 6.1 Framework
- **Orchestration:** **LangGraph** (stateful, cyclical multi-agent graphs).
- **Model strategy (router):**
  - **Reasoning/negotiation:** a frontier model (e.g., Claude / GPT-class).
  - **Bulk extraction/classification:** a smaller, cheaper model.
  - **Document OCR/structure:** Azure Document Intelligence (not an LLM).
- **Tool calling:** typed tools (Pydantic schemas) → adapters in the Worker tier.
- **Memory:** pgvector for supplier/buyer history + HS-code RAG; short-term scratchpad per deal.

### 6.2 Agent roster (as software services)
| Agent | Trigger event | Tools it may call | Output events |
|---|---|---|---|
| **Trade-Min** | cron / `scan.tick` | Comtrade, Volza, MacMap | `opportunity.detected` |
| **Match Engine** (deterministic + LLM tie-break) | `deal.created` | GSTN, DGFT, MCA, geo | `match.scored` |
| **Sourcing** | `match.scored(cms≥75)` | IndiaMART, TradeIndia RFQ, Twilio | `quote.requested`, `quote.received` |
| **DealMaker** | `quote.received` | Pricing calc, PI/PO generator | `pi.sent`, `po.issued` |
| **Shield-Link** | `escrow.funded` | SGS, Cogoport/Freightos, CHA | `qc.booked`, `freight.booked` |
| **DocuGen** | `qc.passed` | Doc templates, DGFT CoO, ICEGATE | `customs.filed` |
| **Escrow-Guard** | `pi.accepted`, `qc.passed` | Stripe/Payoneer, Ledger | `payout.requested` |
| **Exception-Resolver** | `deal.exception` | All tools + HITL | remediation events |

### 6.3 Planner / Executor / Critic loop (per agent)
```
        ┌──────────┐   plan    ┌──────────┐  tool call  ┌──────────┐
        │ PLANNER  ├──────────►│ EXECUTOR ├────────────►│  TOOLS   │
        └────▲─────┘           └────┬─────┘             └────┬─────┘
             │  revise              │ result                 │
             │                      ▼                        │
        ┌────┴─────┐  fail/risk ┌──────────┐                 │
        │  CRITIC  │◄───────────┤ GUARDRAIL│◄────────────────┘
        └────┬─────┘            └────┬─────┘
             │ needs human           │ ok
             ▼                       ▼
      approval.requested        emit domain event
```

### 6.4 Hard guardrails (deterministic, outside the LLM)
- LLM output is **validated against a JSON schema**; invalid → retry/critic, never executed.
- **Money/booking tools require** (a) state-machine precondition + (b) policy check + (c) autonomy-level check.
- **Margin floor, credit check, dual-doc match** are pure functions — an LLM cannot override them.
- All agent reasoning is logged (prompt, tools, result) for audit.

---

## 7. Human-in-the-Loop (HITL) Approval Service

The piece that makes "minimal manual intervention" *safe*.

### 7.1 Approval object
```json
{
  "approval_id": "APR-2026-0091",
  "deal_id": "DEAL-2026-IN-ES-0091",
  "action": "RELEASE_SUPPLIER_PAYOUT",
  "summary": "Pay ₹29,40,000 to Guntur Agri after SGS pass",
  "financial_impact_usd": 35400,
  "risk_flags": ["large_payout"],
  "expires_at": "2026-06-20T10:00:00Z",
  "default_on_timeout": "REJECT",
  "deep_link": "https://app/approvals/APR-2026-0091"
}
```

### 7.2 Channels & flow
- Push to **Ops Console Approvals Inbox** + **mobile push / Telegram / SMS** with deep link.
- Operator taps **Approve/Reject/Override** → emits `approval.resolved` → saga resumes.
- On timeout, apply `default_on_timeout` (safe default = REJECT for money movement).

### 7.3 Autonomy Levels (per action class — configurable)
| Level | Meaning | Example default mapping |
|---|---|---|
| **L0** | Always ask | First-time supplier, payout > $50k |
| **L1** | Ask, auto-approve on timeout | Freight booking within quote |
| **L2** | Auto, notify after | Document generation, RFQ dispatch |
| **L3** | Fully autonomous, log only | Opportunity scanning, QC booking |

As trust grows, the operator promotes action classes from L0 → L3. This is the dial for "minimal manual intervention."

---

## 8. Exception & Dispute Sub-Workflow (New)

A dedicated workflow so a stuck deal never silently dies.

```
 deal.exception ─► classify (agent) ─► remediation plan ─► [autonomy gate]
                                                              │
                        ┌─────────────────────────────────────┤
                        ▼ (auto-fixable, L2/L3)                ▼ (needs human, L0/L1)
                  execute compensating saga             approval.requested
                  (rebook truck / re-quote)             (operator decides)
                        │                                      │
                        └──────────────► resolve or escalate ◄─┘
```
Examples: QC fail → pause logistics, lock escrow, draft corrective RFQ. Truck no-show → auto-rebook (BlackBuck/Porter) if cost delta < threshold, else ask.

---

## 9. Compliance Guard (Corrected from "double-invoice mask")

> **Fix:** The original "double-invoice mask" wording implies concealment that can be unlawful (customs mis-declaration / trade-based money-laundering exposure). This blueprint replaces it with lawful, standard trade practice.

**Legitimate anti-disintermediation, implemented safely:**
- **Merchant-of-record model:** PortLess is the contractual exporter/importer-of-record. Invoices to the buyer are *genuine* invoices from PortLess; POs to the supplier are *genuine* POs from PortLess. No false figures.
- **Switch Bill of Lading:** a recognized carrier service (shipper changed to PortLess). Allowed; used widely in merchant trade.
- **Compliance Guard service** blocks any action that would: under-declare value, falsify HS code, falsify origin, or hide a sanctioned party. These checks run before `customs.filed`.
- All true values are retained internally for audit, FEMA/FIRC realization, and RoDTEP claims.

This keeps margins protected **and** keeps you audit-clean.

---

## 10. Data Model (PostgreSQL)

Core tables (source of truth; events derive from these via outbox):

```sql
-- Deals (the aggregate root)
deals(
  deal_id PK, operator_id, hs_code, status, autonomy_profile,
  margin_floor_pct, est_margin_usd, realized_margin_usd,
  created_at, updated_at
)

parties(
  party_id PK, deal_id FK, role ENUM('buyer','supplier'),
  legal_name, country, gstin, iec, vat_number,
  verification_status, credit_status, risk_score
)

quotes(quote_id PK, deal_id FK, supplier_id, unit_price, currency, valid_until)

match_scores(
  match_id PK, deal_id FK, composite_score,
  gatekeeper_passed BOOL, arbitrage_score, proximity_score, reliability_score
)

escrow(escrow_id PK, deal_id FK, provider, amount, currency, status, ext_ref)

third_party_orders(
  tpo_id PK, deal_id FK,
  type ENUM('qc','freight','cha'),
  provider, ext_ref, status, crf_hash, eta, payload JSONB
)

documents(doc_id PK, deal_id FK, type, storage_uri, hash, generated_by, signed BOOL)

ledger(entry_id PK, deal_id FK, payee, amount, currency, direction, status, idempotency_key UNIQUE)

approvals(approval_id PK, deal_id FK, action, impact_usd, status, expires_at, default_on_timeout, resolved_by, resolved_at)

events(event_id PK, deal_id FK, topic, payload JSONB, occurred_at, seq)         -- event log
outbox(id PK, topic, payload JSONB, published BOOL, created_at)                 -- transactional outbox

embeddings(id PK, owner_type, owner_id, embedding VECTOR(1536), metadata JSONB) -- pgvector RAG
agent_runs(run_id PK, deal_id FK, agent, prompt, tools_called JSONB, output JSONB, cost_usd, created_at)
```

Indexes: `deals(status)`, `events(deal_id, seq)`, `ledger(idempotency_key)`, `embeddings USING ivfflat`.

---

## 11. Tech Stack Summary & Local Dev

### 11.1 Stack
| Layer | Technology |
|---|---|
| Frontend | Next.js 14, TypeScript, Tailwind, shadcn/ui, TanStack Query, Socket.IO |
| API Gateway | Python **FastAPI** (async), Pydantic v2, JWT, Webhook router |
| Agent Runtime | **LangGraph**, LiteLLM model router, Pydantic tool schemas |
| Workers | Python (Faststream/Celery-style consumers), httpx adapters |
| Event Bus | **NATS JetStream** (dev) → Kafka/Redpanda (scale) |
| Database | **PostgreSQL 16 + pgvector** |
| Cache/locks | Redis (idempotency, rate-limit, short-term agent memory) |
| Object store | S3-compatible (MinIO in dev) for documents |
| Payments | Stripe Treasury / Payoneer B2B (sandbox first) |
| OCR | Azure Document Intelligence |
| Observability | OpenTelemetry + Grafana/Tempo + structured logs; LLM tracing (Langfuse) |
| Deploy | Docker → Azure Container Apps / AWS ECS; IaC via Bicep/Terraform |

### 11.2 Repo layout (monorepo)
```
portless/
├─ apps/
│  ├─ web/                 # Next.js Ops Console + mini-portals
│  └─ api/                 # FastAPI gateway (REST + webhooks + WS)
├─ services/
│  ├─ agent-runtime/       # LangGraph graphs + tools
│  ├─ workers/             # saga + integration consumers
│  └─ outbox-relay/        # Postgres → bus publisher
├─ packages/
│  ├─ domain/              # shared types, state machine, event schemas
│  └─ connectors/          # adapters: comtrade, gstn, dgft, sgs, cogoport...
├─ infra/                  # docker-compose, bicep/terraform
└─ docs/                   # this blueprint, HLD, orchestrator
```

### 11.3 Run it locally
```bash
# 1. Start infra: Postgres+pgvector, NATS, Redis, MinIO
docker compose -f infra/docker-compose.dev.yml up -d

# 2. Migrate + seed sandbox data (fake suppliers/buyers, sandbox API keys)
make migrate && make seed

# 3. Start services (each in its own process)
make api          # FastAPI on :8000
make agents       # LangGraph runtime
make workers      # saga/integration consumers
make web          # Next.js on :3000

# 4. SANDBOX MODE = all external calls hit local mock connectors,
#    so you can run a full deal cycle end-to-end with no real money.
```

A **Sandbox Mode** flag swaps every external connector for a deterministic mock, so you can demo a full `opportunity → payout` cycle on a laptop.

---

## 12. Build Roadmap (Aligned to HLD Phases)

| Sprint | Deliverable | Maps to HLD |
|---|---|---|
| **S1** | Monorepo, docker dev stack, Deal state machine, Postgres schema, event bus, outbox | Phase 1 foundation |
| **S2** | Trade-Min + Match Engine agents, Opportunity Radar UI, GSTN/DGFT connectors | Phase 1 |
| **S3** | DealMaker + DocuGen, PI/PO/Invoice generation, supplier/buyer mini-portals | Phase 1 |
| **S4** | Escrow integration (sandbox), HITL Approval Service, Autonomy Levels | Phase 2 |
| **S5** | Shield-Link (QC + freight), saga + compensations, dual-doc verification | Phase 2 |
| **S6** | ICEGATE/CHA filing, RoDTEP/FIRC tracking, Compliance Guard | Phase 2→3 |
| **S7** | Exception-Resolver, freight bidding loop, full autonomy promotion, observability | Phase 3 |
| **S8** | Hardening: load test, DLQ runbooks, security review, go-live with L0 caps | Phase 3 |

**Go-live safety rule:** launch with money-movement actions at **L0** (always ask). Promote to L2/L3 only after N successful supervised deals.

---

## 13. Definition of "Minimal Manual Intervention"

The operator's *only* recurring jobs after maturity:
1. Glance at the **Approvals Inbox** (mostly L1 auto-clears).
2. Resolve genuine **Exceptions** (QC fails, disputes).
3. Tune **Autonomy + guardrail thresholds**.

Everything else — discovery, matching, RFQs, documents, bookings, filings, payouts within policy — runs autonomously through the agent + event backbone described above.

---

## Appendix A — Sequence: A Fully Automated Happy-Path Deal

```
Trade-Min ─opportunity.detected─► Operator starts deal ─deal.created─►
Match Engine ─match.scored(86)─► Sourcing ─quote.requested─► quote.received ─►
DealMaker ─pi.sent─► Buyer funds ─escrow.funded─► [guard: margin ok] ─po.issued─►
Supplier ready ─► Shield-Link ─qc.booked─► SGS ─qc.passed(crf_hash)─►
Escrow-Guard ─approval.requested(payout, L1)─► auto-approve ─payout.completed(supplier)─►
Shield-Link ─freight.booked─► DocuGen ─customs.filed─► ICEGATE ─customs.cleared─►
Payout Saga ─payout.completed(3rd parties + operator margin)─► deal.status = PAID
```

## Appendix B — Connector Reference
See [merchant_exporter_orchestrator_app.md](merchant_exporter_orchestrator_app.md) §3–§4 for the full API/portal landscape (Comtrade, Volza, IndiaMART, GSTN, DGFT, MCA, SGS, Cogoport/Freightos, ICEGATE, Stripe). Each becomes an adapter in `packages/connectors/` with a uniform interface: `quote()`, `book()`, `status()`, `webhook_verify()`.
```
