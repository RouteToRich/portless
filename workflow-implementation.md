# PortLess — Implementation & Reference Guide

> **Companion to [`workflow.md`](workflow.md).**
> `workflow.md` describes the end-to-end functional workflow (Phases 1–4, the
> deal state machine, and the service sequence / I-O reference). This file
> covers everything after that: implementation status, the build plan, the
> third-party API registry, cost-reduction analysis, and per-service
> implementation reference.
>
> ← Back to the workflow narrative: [`workflow.md`](workflow.md)

---

## Table of Contents

- [Implementation Status & Gap Analysis](#implementation-status--gap-analysis)
  - [Services](#services)
  - [Connectors](#connectors)
  - [Domain events — emitted vs planned](#domain-events--emitted-vs-planned)
- [Priority Implementation Order (Phase 4 — Remaining Stubs)](#priority-implementation-order-phase-4--remaining-stubs)
- [Implementation Plan & Build Sequence](#implementation-plan--build-sequence)
  - [Guiding principles](#guiding-principles)
  - [Module dependency graph](#module-dependency-graph)
  - [Recommended build order (8 stages)](#recommended-build-order-8-stages)
  - [Build-stage sequence & validation gates](#build-stage-sequence--validation-gates)
  - [Two ways to sequence the work](#two-ways-to-sequence-the-work)
  - [Testing & validation layers](#testing--validation-layers)
  - [End-to-end validation walkthrough](#end-to-end-validation-walkthrough)
  - [Test-coverage gaps (worth closing during Stage 2–4)](#test-coverage-gaps-worth-closing-during-stage-24)
- [Complete Data-Flow Summary (All Phases)](#complete-data-flow-summary-all-phases)
- [Developer Quick-Start](#developer-quick-start)
  - [Mocked responses (`mocked/`)](#mocked-responses-mocked)
  - [Key file locations](#key-file-locations)
- [Third-Party API Registry & Connection Status](#third-party-api-registry--connection-status)
  - [Summary Table (Last checked: 2026-06-27)](#summary-table-last-checked-2026-06-27)
  - [Phase 1 — Historical Demand (Trade Intelligence)](#phase-1--historical-demand-trade-intelligence)
  - [Phase 2 — Live Demand Signals](#phase-2--live-demand-signals)
  - [Phase 3 — Tariff Gate](#phase-3--tariff-gate)
  - [Phase 4 — Supplier Discovery](#phase-4--supplier-discovery)
  - [Regulatory & Verification](#regulatory--verification)
  - [LLM & AI](#llm--ai)
  - [Database & Persistence](#database--persistence)
  - [Phase 4 Stubs — Connector Implementation Required](#phase-4-stubs--connector-implementation-required)
- [Trade-Min Cost-Reduction — Free / Low-Cost API Alternatives](#trade-min-cost-reduction--free--low-cost-api-alternatives)
  - [What we're actually paying for (the key distinction)](#what-were-actually-paying-for-the-key-distinction)
  - [Current paid connectors (what to replace)](#current-paid-connectors-what-to-replace)
  - [Phase 1 — historical demand (aggregate stats) → **fully replaceable, free**](#phase-1--historical-demand-aggregate-stats--fully-replaceable-free)
  - [Phase 1 — named-buyer discovery (the genuine gap)](#phase-1--named-buyer-discovery-the-genuine-gap)
  - [Phase 3 — tariff gate → **fully replaceable, free** (replaces MacMap)](#phase-3--tariff-gate--fully-replaceable-free-replaces-macmap)
  - [Phase 4 — supplier discovery → **partly replaceable**](#phase-4--supplier-discovery--partly-replaceable)
  - [Country-wise free official sources (quick reference)](#country-wise-free-official-sources-quick-reference)
  - [Recommended free-first stack (keeps the whole flow at ~$0)](#recommended-free-first-stack-keeps-the-whole-flow-at-0)
  - [Implementation notes & caveats](#implementation-notes--caveats)
  - [✅ Implemented (2026-06-27) — what changed in the code](#-implemented-2026-06-27--what-changed-in-the-code)
  - [Replaced / Legacy Trade-Min connectors (kept for future use)](#replaced--legacy-trade-min-connectors-kept-for-future-use)
- [DealMaker & DocuGen — Implementation Reference](#dealmaker--docugen--implementation-reference)
  - [DealMaker Service (`services/dealmaker/`)](#dealmaker-service-servicesdealmaker)
  - [DocuGen Service (`services/docugen/`)](#docugen-service-servicesdocugen)
  - [Cross-Service Flow: Opportunity → Shipping Bill](#cross-service-flow-opportunity--shipping-bill)
  - [Running Services Standalone](#running-services-standalone)
  - [Running Tests](#running-tests)

---

## Implementation Status & Gap Analysis

### Services

| Service | Route file | Service.py | Events emitted | Notes |
|---|---|---|---|---|
| `trade_min` | ✅ | ✅ Full | `opportunity.detected` | 5-phase concurrent pipeline |
| `match_engine` | ✅ | ✅ Full | `match.scored` | Deterministic CMS formula |
| `deals` | ✅ | ✅ Full | `deal.created` + 11 transitions | State machine, orchestration |
| `sourcing` | ✅ | ✅ Full | `quote.requested`, `quote.received` | Verification + re-score pipeline |
| `negotiation_agent` | ✅ | ✅ Full | realized CMS | LangGraph 3-node graph; drafts + HITL-gates emails, delegates SMTP to `email_gateway` |
| `dealmaker` | ✅ | ✅ Full | `pi.sent`, `pi.accepted`, `po.issued` | Jinja2 HTML, SHA-256, margin guard, idempotency |
| `docugen` | ✅ | ✅ Full | `customs.filed` | CI/PL/CoO/SDF/SB templates, ICEGATE stub |
| `shield_link` | ✅ | ✅ Full | `qc.booked`, `qc.passed`, `qc.failed`, `freight.booked`, `freight.milestone`, `customs.filed`, `customs.cleared` | Graceful stub when API keys absent |
| `escrow_guard` | ✅ | ✅ Full | `escrow.funded`, `payout.requested`, `payout.completed`, `deal.exception` | Stripe (hold) + Payoneer (payout); stubs when keys absent |
| `hitl_approval` | ✅ | ✅ Full | `approval.requested`, `approval.resolved`, `deal.exception` | L0/L1/L2/L3 gate; SSE stream + webhook notify; saga callbacks |
| `exception_resolver` | ✅ | ✅ Full | `deal.exception` (OPEN + final status) | LangGraph LLM agent; AUTO_FIX / ESCALATE / CANCEL / MONITOR; HITL saga integration |
| `email_gateway` | ✅ | ✅ Full | — | Email **transport** (single IMAP + SMTP config); auto IMAP poll (15 min) forwards inbound to `email_processor`; SMTP send via `POST /email-gateway/send` |
| `email_processor` | ✅ | ✅ Full | — | Inbound **processing**: match to live negotiation, extract price, persist, trigger `run_round()`; receives from Gateway / webhooks / manual |

### Connectors

| Connector | File | Status | Notes |
|---|---|---|---|
| `ComtradeConnector` | `trade_intel.py` | ✅ Real HTTP (free) | UN Comtrade REST v3 — Phase 1 demand |
| `WitsTariffConnector` | `trade_intel.py` | ✅ Real HTTP, live-verified (free, no key) | World Bank WITS TRAINS — Phase 3 tariff gate (**replaces MacMap**) |
| `CensusTradeConnector` | `trade_intel.py` | ✅ Real HTTP (free key) | US Census imports — Phase 1 US augmentation |
| `Access2MarketsConnector` | `trade_intel.py` | ✅ Implemented (EU-preferred; endpoint via `ACCESS2MARKETS_API_URL`, WITS fallback) | EU tariffs + NTBs + rules of origin — Phase 3 for EU markets |
| `VolzaConnector` | `trade_intel.py` | 🗄️ Legacy (paid) | Shipment manifests — retired from default pipeline |
| `MacmapConnector` | `trade_intel.py` | 🗄️ Legacy (paid) | Tariff data — replaced by `WitsTariffConnector` |
| `TrademoConnector` | `trade_intel.py` | 🗄️ Legacy (paid) | Risk profiles — retired from default pipeline |
| `ImportGeniusConnector` | `trade_intel.py` | 🗄️ Legacy (paid) | US customs manifests — retired from default pipeline |
| `IndiamartConnector` | `trade_intel.py` | ✅ Real HTTP | Supplier search + RFQ |
| `TradeIndiaConnector` | `trade_intel.py` | ✅ Real HTTP | Fallback supplier search |
| `AlibabaRfqConnector` | `demand_signals.py` | ✅ Real HTTP | Live buyer RFQs |
| `EuTedConnector` | `demand_signals.py` | ✅ Real HTTP | EU tenders (open API) |
| `GdeltDisruptionConnector` | `demand_signals.py` | ✅ Real HTTP | Supply disruption signals |
| `GstnConnector` | `regulatory.py` | ⚠️ Headers fixed, path unverified | GST verification — API Setu subscription product; `_PROD_BASE` is a placeholder (404), needs real path |
| `DgftConnector` | `regulatory.py` | ✅ Real HTTP (API Setu) | IEC verification via `dgft/v1/iec/{iec}`; needs `API_SETU_KEY` |
| `McaConnector` | `regulatory.py` | ⚠️ Headers fixed, path unverified | Company registry — API Setu subscription product; `_BASE` is a placeholder (404), needs real path |
| `UdyamConnector` | `regulatory.py` | ⚠️ Headers fixed, path unverified | MSME cert — API Setu subscription product; `_BASE` is a placeholder (404), needs real path |
| `ViesConnector` | `regulatory.py` | ✅ Real HTTP | EU VAT validation |
| `AllianzTradeConnector` | `regulatory.py` | ✅ Real HTTP | Buyer credit risk |
| `SgsConnector` | `shields.py` | ✅ Wired (stub when `SGS_API_KEY` absent) | QC inspection booking + HMAC webhook verify |
| `CogoportConnector` | `shields.py` | ✅ Wired (stub when `COGOPORT_API_KEY` absent) | Freight booking + milestone webhook |
| `FreightosConnector` | `shields.py` | ✅ Wired (stub when `FREIGHTOS_API_KEY` absent) | Freight spot rates fallback |
| `IcegateConnector` | `shields.py` | ✅ Wired (stub when `ICEGATE_API_KEY` absent) | Customs EDI + Shipping Bill |
| `StripeTreasuryConnector` | `payments.py` | ✅ Wired (stub when `STRIPE_SECRET_KEY` absent) | Escrow hold (capture_method=manual) |
| `PayoneerConnector` | `payments.py` | ✅ Wired (stub when `PAYONEER_CLIENT_ID` absent) | INR payout via OAuth2 |

### Domain events — emitted vs planned

| Event topic | Emitted? | By |
|---|---|---|
| `opportunity.detected` | ✅ | trade_min |
| `deal.created` | ✅ | deals |
| `match.scored` | ✅ | match_engine |
| `quote.requested` | ✅ | sourcing |
| `quote.received` | ✅ | sourcing |
| `negotiation.started` | ✅ | deals (status transition) |
| `negotiation.round` | ✅ | negotiation_agent |
| `negotiation.agreed` | ✅ | deals (status transition) |
| `negotiation.failed` | ✅ | deals (status transition) |
| `pi.sent` | ✅ | dealmaker |
| `pi.accepted` | ✅ | dealmaker |
| `escrow.funded` | ✅ | escrow_guard (funded webhook) |
| `po.issued` | ✅ | dealmaker |
| `qc.booked` | ✅ | shield_link |
| `qc.passed` | ✅ | shield_link (QC webhook) |
| `qc.failed` | ✅ | shield_link (QC webhook) |
| `freight.booked` | ✅ | shield_link |
| `freight.milestone` | ✅ | shield_link (freight webhook) |
| `customs.filed` | ✅ | shield_link (CHA) + docugen |
| `customs.cleared` | ✅ | shield_link (freight ARRIVED milestone) |
| `payout.requested` | ✅ | escrow_guard |
| `payout.completed` | ✅ | escrow_guard (payout webhook) |
| `approval.requested` | ✅ | hitl_approval (L0/L1 create) |
| `approval.resolved` | ✅ | hitl_approval (human resolve + timeout + L2/L3 auto) |
| `deal.exception` | ✅ | hitl_approval (REJECT) + escrow_guard (payout fail) + shield_link (callback fail) |

---

## Priority Implementation Order (Phase 4 — Remaining Stubs)

DealMaker, DocuGen, Shield-Link, and Escrow-Guard are ✅ complete. Remaining work:

```
✅ 1. dealmaker/service.py  — issue_pi(), accept_pi(), issue_po()
      DONE: PI/PO rendered, persisted, events emitted, state transitions enforced

✅ 2. escrow_guard/service.py  — create_hold(), handle_escrow_funded_webhook()
      DONE: Stripe PaymentIntent + HMAC webhook; deal → ESCROW_FUNDED

✅ 3. shield_link/service.py  — book_qc(), handle_qc_result()
      DONE: SGS connector + HMAC webhook; deal → QC_PASSED/QC_FAILED

✅ 4. escrow_guard/service.py  — request_payout(), handle_payout_webhook()
      DONE: Payoneer OAuth2 + HMAC webhook; deal → PAID on COMPLETED

✅ 5. docugen/service.py  — generate_document() for CI, PL, CoO, SDF, SB
      DONE: all five templates rendered + file_customs() with ICEGATE stub

✅ 6. shield_link/service.py  — book_freight(), file_cha()
      DONE: Cogoport/Freightos connector + ICEGATE CHA; deal → IN_TRANSIT → CLEARED

✅ 7. hitl_approval/service.py  — create_approval(), resolve_approval()
      DONE: L0/L1/L2/L3 gates; SSE stream; webhook; timeout watcher; saga callbacks

✅ 8. exception_resolver/service.py  — raise_exception(), get_exception(), list_exceptions()
      DONE: LangGraph LLM agent; AUTO_FIX/ESCALATE/CANCEL/MONITOR; HITL saga integration; MongoDB + in-memory repo
```

---

## Implementation Plan & Build Sequence

> **Goal of this section:** a dependency-aware order to build (or rebuild) each
> module so the system is **testable and verifiable one phase at a time**. Every
> stage ends with a concrete **validation gate** — a command to run and a
> condition to assert — before you move on. If a gate is red, you never advance;
> the bug is contained to the stage you're in.

### Guiding principles

1. **Dependencies before dependents.** Build the thing that is imported before
   the thing that imports it. `shared/` first, leaf services next, orchestration
   and cross-cutting concerns last.
2. **Stub the outside world from day one.** Every `shared/connectors/*` returns a
   realistic canned response when its API key is absent, and every repository has
   an in-memory fallback. This lets the whole pipeline run — and be tested —
   end-to-end with **zero credentials and zero MongoDB**.
3. **In-memory before Mongo, Noop before NATS.** Prove the logic with the
   in-memory repo and the `NoopPublisher`; swapping in Motor/Atlas or a real event
   bus is a config change, not a code change (that's Stage 8).
4. **The deal `status` is the contract.** Each stage is "done" when a deal can
   reach a specific state (see [Deal Status](#deal-status--single-source-of-truth)).
   Validation = assert the state transition happened and was legal.
5. **One vertical slice early.** Get a single deal to walk the *whole* pipeline on
   stubs before deepening any module (see "Two ways to sequence the work").

### Module dependency graph

```
                    ┌──────────────────────────────────────────────────────┐
                    │                shared/  (foundations)                 │
                    │  domain (enums · state_machine · events) · config     │
                    │  clients/base · connectors(+graceful stubs) · bus     │
                    │  llm_gateway · rag                                     │
                    └───────────────────────┬──────────────────────────────┘
                                             │  imported by every module
   ┌───────────────┬──────────────┬─────────┴───────┬──────────────┬─────────────────┐
   ▼               ▼              ▼                  ▼              ▼                 ▼
 match_engine  hitl_approval    deals            trade_min      sourcing        connectors
  (pure CMS)   (gate/SSE/cb)  (state machine)   (discovery)   (verify + RFQ)    (SGS/Stripe…)
   │  └─score────┐  ▲             ▲   ▲              │              │
   │             │  │  seed ──────┘   └── patch financials ─────────┘
   │             │  │
   │        negotiation_agent ──► llm_gateway + rag
   │             │   │  └── email draft gate ──► hitl_approval
   │      send ──┘   └── run_round / record_inbound ◄──────────────┐
   │        ▼                                                       │
   │   email_gateway ── forwards fetched mail ──► email_processor ──┘
   │   (IMAP + SMTP)                              (match + persist) └─► hitl_approval
   │
   ├─ dealmaker ──┐
   ├─ docugen     ├── transition ──► deals ;  gate ──► hitl_approval ;  I/O ──► connectors
   ├─ shield_link ┤
   └─ escrow_guard┘

   exception_resolver ──► llm_gateway + hitl_approval  (+ calls services for AUTO_FIX)
```

*Note the two intentional cycles handled by lazy (in-function) imports:
`hitl_approval` callbacks call back into `dealmaker`/`shield_link`/`escrow_guard`,
and `email_processor` calls `negotiation_agent.run_round`. These are safe because
the imports happen at call time, not module load.*

### Recommended build order (8 stages)

Each stage lists **Build → Validate → Exit criteria**. "Validate" commands run
from the repository root.

**Stage 0 — Foundations & the HITL gate** (`shared/`, `services/hitl_approval/`)
- **Build:** `shared/domain` (`DealStatus`, `DEAL_TRANSITIONS`, `can_transition`, `EventTopic`, `DomainEvent`); `shared/config` (`Settings`); `shared/clients/base` (`BaseServiceClient`); `shared/connectors/*` interfaces + graceful stubs; `shared/bus` (`NoopPublisher`); `shared/llm_gateway`; the repository `Protocol` + `InMemoryFallback` template. Then `hitl_approval` (L0–L3 gates, SSE stream, saga callbacks, timeout watcher) — it's self-contained and half the platform depends on it.
- **Validate:** `pytest tests/test_state_machine.py tests/test_hitl_approval.py -q`
- **Exit:** transition matrix + all four autonomy levels green; every module can `import shared.*` without side effects.

**Stage 1 — Deterministic scoring** (`services/match_engine/`)
- **Build:** the CMS formula (margin 50 % · proximity 30 % · reliability 20 %) and the regulatory gatekeeper that zeroes the score on a failed check.
- **Validate:** `pytest tests/test_match_engine.py -q`
- **Exit:** scores are deterministic and match the fixtures; a failed gate ⇒ score 0.

**Stage 2 — Deal lifecycle core** (`services/deals/`)
- **Build:** `DealRecord` + repo; `create_deal()` (seed + score orchestration); `update_status()` guarded by `can_transition()`; `update_financials()`; `status_history` append.
- **Validate:** `pytest tests/test_health.py -q` (deals boots) + an in-process drive: create a deal, push `DRAFT→MATCHING`, assert an **illegal** transition raises. *(No `test_deals.py` yet — see "Test-coverage gaps".)*
- **Exit:** a deal can be created and legally walked; illegal transitions are rejected.

**Stage 3 — Discovery & sourcing** (`services/trade_min/`, `services/sourcing/`)
- **Build:** trade_min 5-phase concurrent pipeline + APScheduler + trade/demand/regulatory connectors (stubbed); sourcing RFQ dispatch + GSTN/DGFT/MCA/Udyam verification (parallel) + Match-Engine re-score + deal patch → `NEGOTIATING`.
- **Validate:** `pytest tests/test_trade_min.py -q`; then `POST /trade-min/scan` → `opportunity.detected` → `POST /deals` seeds a deal; `POST /sourcing/quotes` moves it to `NEGOTIATING`.
- **Exit:** an opportunity becomes a deal at `NEGOTIATING` with financials populated.

**Stage 4 — Negotiation** (`agents/negotiation_agent/`)
- **Build:** LangGraph 3-node graph (orchestrator ↔ buyer/supplier agents); deterministic guards (convergence + margin floor); `run_round()`; outbound email **draft + L0 HITL gate**; realized-CMS recompute on `AGREED`.
- **Validate:** drive `run_round()` for N rounds until `AGREED`; assert each outbound draft creates a `SEND_NEGOTIATION_EMAIL` HITL and the deal reaches `NEGOTIATION_AGREED`. *(No `test_negotiation_agent.py` yet.)*
- **Exit:** a negotiation converges; no email leaves without an HITL approval.

**Stage 5 — Email transport & processing** (`services/email_gateway/`, `services/email_processor/`)
- **Build:** gateway IMAP pollers (fetch + attachments) + SMTP send; processor receiving API + mapper (live-session gate) + price extraction + `run_round()` trigger + SendGrid/Mailgun webhooks + manual endpoint.
- **Validate:** `pytest tests/test_email_gateway.py tests/test_email_processor.py -q`; `POST /email-processor/inbound` with no `[NEG:]` → `UNMATCHED`; with a live negotiation + price → `PROCESSED`; `POST /email-gateway/send` in log-only mode → `LOGGED`.
- **Exit:** an inbound reply advances a live negotiation; an approved draft "sends".

**Stage 6 — Deal execution** (`dealmaker → escrow_guard → shield_link → docugen`)
Build **in deal-state order** so each step's exit state is the next step's entry:
- **Build:** dealmaker `issue_pi/accept_pi/issue_po` (margin guard ≥ 8 %, L0 PO gate) → `PI_SENT/PI_ACCEPTED/PO_ISSUED`; escrow_guard `create_hold` + funded webhook → `ESCROW_FUNDED`; shield_link `book_qc` + SGS webhook → `QC_PASSED/FAILED`; shield_link `book_freight` + docugen documents (CI/PL/CoO/SDF/SB) + ICEGATE customs → `IN_TRANSIT→CLEARED`; escrow_guard `request_payout` + Payoneer webhook → `PAID`.
- **Validate:** `pytest tests/test_dealmaker.py tests/test_escrow_guard.py tests/test_shield_link.py tests/test_docugen.py -q`; then walk `NEGOTIATION_AGREED → … → PAID` by posting the stub webhooks in order; confirm HMAC verification on each webhook.
- **Exit:** a deal walks all execution states end-to-end on stubs.

**Stage 7 — Resilience** (`services/exception_resolver/`)
- **Build:** LangGraph `analyze → execute`; 10 exception categories; `AUTO_FIX/ESCALATE/CANCEL/MONITOR`; HITL saga; crash-safe `OPEN` record; conservative `UNKNOWN/HIGH/ESCALATE` fallback when the LLM chain is exhausted.
- **Validate:** `pytest tests/test_exception_resolver.py -q`; force a `QC_FAILED` and a `CONNECTOR_ERROR`; assert a remediation result is always produced (never an unhandled crash).
- **Exit:** every failure path yields a typed remediation; the pipeline never dies silently.

**Stage 8 — Integration & hardening** (config-only, no new logic)
- **Build:** set `*_MONGO_URI` (InMemory → Motor/Atlas); add provider keys (connectors go live); enable real IMAP/SMTP + SendGrid/Mailgun webhooks; swap `NoopPublisher` for a real bus; set all `*_WEBHOOK_SECRET`.
- **Validate:** `pytest -q` (full suite) + `pytest tests/test_health.py -q` (every app boots) + the golden-path e2e + the failure-path e2e (below).
- **Exit:** full suite green; monolith boots; golden path **and** failure paths verified.

### Build-stage sequence & validation gates

```
 S0 foundations       S1 match_engine   S2 deals          S3 trade_min+sourcing   S4 negotiation
 + hitl_approval  ──► (CMS scoring) ──► (state machine)──► (discovery+verify) ──► (LangGraph+LLM)
   │ GATE               │ GATE            │ GATE              │ GATE                 │ GATE
   ▼                    ▼                 ▼                   ▼                      ▼
 state_machine +      match fixtures    create deal +      scan → deal at         rounds → AGREED
 hitl tests green     green             legal transitions  NEGOTIATING            + every email HITL-gated

 S5 email_gateway     S6 execution: dealmaker→escrow→        S7 exception_        S8 integration
 + email_processor ─► shield→docugen→escrow(payout)     ──►  resolver        ──►  + hardening
   │ GATE               │ GATE                                │ GATE               │ GATE
   ▼                    ▼                                     ▼                    ▼
 inbound advances     AGREED → … → PAID walk                failures always      full suite green +
 a live negotiation   via stub webhooks (HMAC verified)     remediate (no crash) golden + failure e2e
```

### Two ways to sequence the work

| Strategy | How | Best when |
|---|---|---|
| **Dependency-first** (stages above) | Finish and gate each module before the next | Building for the first time; each module gets full unit coverage as you go |
| **Vertical-slice-first** | Stub *every* service to just flip the deal status, walk one deal `DRAFT→PAID` on stubs, then deepen module by module | You want an end-to-end signal on **day one** and prefer to find integration/state-machine bugs before investing in any single module |

Recommended: a **thin vertical slice first** (proves the state machine + wiring), then **dependency-first deepening** using the 8 stages to add real logic and coverage.

### Testing & validation layers

| Layer | Scope | Tools / files | Run |
|---|---|---|---|
| **Unit** | one module's pure logic | `pytest tests/test_<module>.py` | every stage |
| **Contract** | schema ↔ router ↔ client agree | Pydantic models + `client.py` round-trip | when adding a client |
| **Boot / health** | every app starts, `/health` = 200 | `pytest tests/test_health.py` | end of each stage |
| **Integration** | cross-module in-process (monolith) | drive `service.py` fns; assert state transitions | Stage 2 onward |
| **End-to-end** | full deal walk incl. webhooks | golden-path drive + provider webhook posts | Stage 6 & 8 |
| **Resilience** | HITL reject · connector fail · exception | `test_hitl_approval`, `test_exception_resolver`, failure e2e | Stage 7–8 |

### End-to-end validation walkthrough

**Golden path** (assert each `► STATE` transition; all connectors stubbed, in-memory repos):

```
POST /trade-min/scan                         ─► opportunity.detected
POST /deals {opportunity_id}                 ─► DRAFT ► MATCHING          [deals + match_engine]
POST /sourcing/quotes                        ─► NEGOTIATING               [sourcing verify + re-score]
run_round × N  (email via email_gateway;     ─► NEGOTIATION_AGREED        [negotiation_agent + HITL]
   replies ingested by email_processor)
POST /dealmaker/pi → /dealmaker/pi/{id}/accept ─► PI_SENT ► PI_ACCEPTED
POST /escrow-guard/holds → funded webhook    ─► ESCROW_PENDING ► ESCROW_FUNDED
POST /dealmaker/po   (margin ≥ 8 %)          ─► PO_ISSUED
POST /shield-link/qc → SGS webhook           ─► SUPPLIER_DISPATCH_READY ► QC_PASSED
POST /shield-link/freight → arrived webhook  ─► IN_TRANSIT ► CLEARED       [docugen: CI/PL/CoO/SDF/SB + ICEGATE]
POST /escrow-guard/payouts → payout webhook  ─► PAID  ✅
```

**Failure paths** (validate the system degrades safely, not just the happy path):

```
QC webhook = FAIL          ─► QC_FAILED ► DISPUTE            [→ exception_resolver: QC_FAILED]
HITL rejects PO / email    ─► deal.exception emitted         [→ exception_resolver: APPROVAL_REJECTED]
Connector raises (real key)─► deal.exception (crash-safe)    [→ exception_resolver: CONNECTOR_ERROR]
Margin < 8 % at PO         ─► blocked + operator alert       [dealmaker margin guard]
```

### Test-coverage gaps (worth closing during Stage 2–4)

| Module | Unit test today | Suggested addition |
|---|---|---|
| `deals` | ❌ none | `test_deals.py` — create + each legal/illegal transition |
| `sourcing` | ❌ none | `test_sourcing.py` — verification parallelism + re-score → NEGOTIATING |
| `negotiation_agent` | ❌ none | `test_negotiation_agent.py` — convergence, margin-floor guard, email HITL gate |
| everything else | ✅ has `test_*.py` | keep green in every stage |

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
  → Negotiation loads static trade context for HS 090422
  → LangGraph: orchestrator → buyer_agent → HITL_PENDING (first outbound gate)
  → NegotiationSession: status=HITL_PENDING, round=1

POST /negotiation-agent/negotiations/{id}/round  (×3–5 rounds)
  → LangGraph: orchestrator routes buyer_agent ↔ supplier_agent
  → convergence: |bid−ask| ≤ 0.10 AND margin ≥ 8%
  → NegotiationSession: status=AGREED, agreed_price=2.92

POST /dealmaker/pi   ✅
  → Jinja2 HTML Proforma Invoice rendered, SHA-256 hashed, stored
  → deal status: NEGOTIATION_AGREED → PI_SENT
  → pi.sent

POST /dealmaker/pi/{pi_id}/accept   ✅
  → PI status: SENT → ACCEPTED
  → deal status: PI_SENT → PI_ACCEPTED
  → pi.accepted

POST /escrow-guard/holds  ✅
  → Stripe PaymentIntent created (capture_method=manual)
  → deal status: PI_ACCEPTED → ESCROW_PENDING

POST /escrow-guard/webhooks/escrow-funded  ✅
  → HMAC-SHA256 signature verified (Stripe-Signature header)
  → escrow.funded
  → deal status: ESCROW_PENDING → ESCROW_FUNDED

POST /dealmaker/po  ✅
  → Margin guard: (buyer_price − supplier_price) / buyer_price × 100 ≥ 8%
  → Jinja2 HTML Purchase Order rendered, SHA-256 hashed, stored
  → deal status: ESCROW_FUNDED → PO_ISSUED
  → po.issued

POST /shield-link/qc  ✅
  → SGS inspection booked (stub when SGS_API_KEY absent)
  → deal status: PO_ISSUED → SUPPLIER_DISPATCH_READY
  → qc.booked

[SGS inspection completes — webhook]  ✅
  → HMAC-SHA256 signature verified (x-sgs-signature header)
  → qc.passed or qc.failed
  → deal status → QC_PASSED or QC_FAILED

POST /escrow-guard/payouts  (supplier milestone release)  ✅
  → Guard: deal=CLEARED + escrow=FUNDED; idempotency_key deduplication
  → Payoneer payout initiated (OAuth2; stub when keys absent)
  → payout.requested

POST /docugen/documents  (CI, PL, CoO, SDF, SHIPPING_BILL)  ✅
  → Jinja2 templates rendered per doc type, SHA-256 hashed, stored
  → PENDING → RENDERED lifecycle with audit trace on failure

POST /docugen/customs/file  ✅
  → Auto-generates Shipping Bill if absent
  → ICEGATE Shipping Bill filed (stub; wire ICEGATE_API_KEY for live)
  → LEO captured
  → customs.filed

POST /shield-link/freight  ✅
  → Cogoport/Freightos container booked (stub when keys absent)
  → freight.booked
  → deal status → IN_TRANSIT

[Freight milestone webhook — ARRIVED]  ✅
  → HMAC-SHA256 signature verified (x-cogoport-signature)
  → freight.milestone + customs.cleared
  → deal status → CLEARED

POST /escrow-guard/payouts  (PortLess margin release — webhook triggered)  ✅
  → Payoneer payout webhook COMPLETED received
  → payout.completed
  → deal status → PAID  ✓  DEAL CLOSED

# Exception path (any step)
POST /exception-resolver/exceptions  (called by any service on failure)  ✅
  → OPEN → ANALYZING (LLM classifies)
  → REMEDIATING:
      AUTO_FIX  → calls downstream service (e.g. POST /docugen/documents)
                 → on success: RESOLVED
      ESCALATE  → POST /hitl-approval/approvals (action=RESOLVE_EXCEPTION)
                 → operator resolves via SSE stream → ESCALATED
      MONITOR   → log + RESOLVED (low-severity; no human needed)
      CANCEL    → POST /hitl-approval/approvals (cancel_recommended=True)
                 → on APPROVE: deal → CANCELLED
      all paths failed → UNRESOLVABLE → deal.exception emitted
```

---

## Developer Quick-Start

```bash
# Start the combined dev server (all services on one port)
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

### Mocked responses (`mocked/`)

Complete set of realistic JSON fixtures for every service and every deal state. Use these to build UI screens without running the backend.

```
mocked/
├── deals/              6 files — deal detail at key states (NEGOTIATING, ESCROW_FUNDED, IN_TRANSIT, PAID, DISPUTE)
├── trade_min/          scan result with 3 opportunities
├── match_engine/       CMS score breakdown
├── sourcing/           quote submit response
├── negotiation_agent/  agreed + failed negotiation
├── dealmaker/          PI issued, PO issued, margin breach error
├── docugen/            document list, customs filed, render failure
├── shield_link/        QC booking, QC passed/failed webhooks, freight booking, cleared milestone
├── escrow_guard/       hold created, funded webhook, payout completed, payout failed, deal summary
├── hitl_approval/      L0/L1/L2 pending, rejected, list, SSE events
├── exception_resolver/ 8 exception categories + list
└── flows/              happy_path_full_flow.json (17 steps), qc_failure_exception_flow.json, payment_failure_flow.json
```

See `mocked/README.md` for the full index, deal status reference, HITL action table, and SSE integration example.

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
agents/negotiation_agent/   ← Phase 3b — LangGraph price negotiation (calls email_gateway to send)
services/dealmaker/         ← PI + PO generation                  [✅ full]
services/docugen/           ← Document render + ICEGATE filing     [✅ full]
services/shield_link/       ← QC + freight + CHA + webhooks        [✅ full]
services/escrow_guard/      ← Escrow hold + payout + webhooks      [✅ full]
services/hitl_approval/     ← Human-in-the-loop gate + SSE stream  [✅ full]
services/exception_resolver/← LangGraph agent; AUTO_FIX/ESCALATE/CANCEL/MONITOR [✅ full]
services/email_gateway/     ← Email transport: IMAP fetch + SMTP send (single config) [✅ full]
services/email_processor/   ← Inbound processing: match + persist + advance negotiation [✅ full]

shared/llm_gateway/         ← LLM client for the shared gateway + tracking
shared/domain/              ← DealStatus enum, EventTopic enum, state machine
```

---

## Third-Party API Registry & Connection Status

> Run `python scripts/check_trade_api_connections.py` at any time to re-test
> all live connections and print an up-to-date status table.
>
> **Status legend:**
> - ✅ Connected & verified — working right now
> - 🔑 Key needed — credential not set in `.env`, service will be skipped at runtime
> - ⚠️ Network issue — API is real but unreachable from current machine (firewall/proxy)
> - ⏳ Connector stub — `NotImplementedError`, full implementation pending
> - 🔓 No key needed — fully public API, no registration required

---

### Summary Table (Last checked: 2026-06-27)

| # | API / Service | Module | Purpose — why we need it | Status | Env Var(s) | Cost |
|---|---------------|--------|--------------------------|--------|------------|------|
| 1 | UN Comtrade | trade-min Phase 1 | Global trade volumes by HS code & country — detects which markets import what India can supply (core demand-discovery signal) | ✅ Connected & verified | `COMTRADE_SUBSCRIPTION_KEY` (set & validated) | Free 500 calls/day |
| 1a | **World Bank WITS** (TRAINS) | trade-min Phase 3 | Free applied import-tariff data at HS-6 — **replaces ITC MacMap** for the tariff gate | ✅ Connected & verified 2026-06-27 (US HS 020890 → 2.13% MFN; no key, ~31s cold→cached 7d) | None | **Free (open)** |
| 1b | **US Census** Intl Trade | trade-min Phase 1 | Free official US import statistics — augments Comtrade for the US market | 🔑 Key needed — endpoint live (HTTP 302 w/o key), `CENSUS_API_KEY` not set | `CENSUS_API_KEY` | **Free** |
| 1c | **EU Access2Markets** | trade-min Phase 3 (EU) | Free EU tariffs **+ NTBs + rules of origin** — preferred over WITS for EU destinations (richer data) | ⏳ Wired, endpoint-gated — `ACCESS2MARKETS_API_URL` unset ⇒ **WITS fallback active** | `ACCESS2MARKETS_API_URL` (+ `_API_KEY`) | **Free** |
| 2 | Volza | trade-min Phase 1 | (Named BoL buyers) | 🗄️ Legacy — paid, retired from default | `VOLZA_API_KEY` | $0.10/search + $0.02/record |
| 3 | Trademo Intel | trade-min Phase 1 | (Buyer risk profiling) | 🗄️ Legacy — paid, retired from default | `TRADEMO_API_KEY` | Subscription |
| 4 | ImportGenius | trade-min Phase 1 | (US/LatAm BoL manifests) | 🗄️ Legacy — paid, retired from default | `IMPORTGENIUS_API_KEY` | Enterprise plan |
| 5 | Alibaba RFQ | trade-min Phase 2 | Live buying-intent feed — finds buyers who posted an active sourcing request *right now* | 🔑 Key needed | `ALIBABA_APP_KEY` + `ALIBABA_APP_SECRET` | Free developer tier |
| 6 | GDELT Project | trade-min Phase 2 | Supply-chain disruption news monitor — flags when a competitor origin can't supply (urgency boost) | ⚠️ Network blocked | None | Free (open) |
| 7 | EU TED | trade-min Phase 2 | EU public procurement tenders — institutional demand signal for EU markets | ✅ Connected | None | Free (open) |
| 8 | ITC MacMap | trade-min Phase 3 | (Tariffs/NTBs/RoO) | 🗄️ Replaced by WITS (#1a) — paid, retired | `MACMAP_API_KEY` | Subscription |
| 9 | IndiaMART | trade-min Phase 4 | India's largest wholesale directory — discovers Indian manufacturer/supplier candidates to source from | 🔑 Key needed | `INDIAMART_API_KEY` + `INDIAMART_MOBILE` | Seller plan |
| 10 | TradeIndia | trade-min Phase 4 | Fallback wholesale directory — supplementary supplier profiles | 🔑 Key needed | `TRADEINDIA_API_KEY` | Paid membership |
| 11 | GSTN (via API Setu) | sourcing / deals | Validates the supplier's GST registration is ACTIVE — gatekeeper before any financial commitment | 🔑 Key needed + ⚠️ path unverified (subscription-gated; guessed path 404s) | `API_SETU_KEY` + `API_SETU_CLIENT_ID` | Free (govt) |
| 12 | DGFT IEC | sourcing / deals | Confirms the supplier holds an active Import-Export Code and isn't on the denied-party list | 🔑 Key needed (via API Setu; endpoint live, returns 401 without key) | `API_SETU_KEY` + `API_SETU_CLIENT_ID` | Free (govt) |
| 13 | MCA21 (via API Setu) | sourcing / deals | Company registry — CIN status + incorporation date (business age) to weed out shells/brokers | 🔑 Key needed + ⚠️ path unverified (subscription-gated; guessed path 404s) | `API_SETU_KEY` + `API_SETU_CLIENT_ID` | Free (govt) |
| 14 | Udyam (via API Setu) | sourcing / deals | MSME certification — confirms a real manufacturing unit eligible for export incentives | 🔑 Key needed + ⚠️ path unverified (subscription-gated; guessed path 404s) | `API_SETU_KEY` + `API_SETU_CLIENT_ID` | Free (govt) |
| 15 | VIES (EU VAT) | deals | Validates the EU buyer's VAT number — blocks onboarding of fake/unregistered EU entities | 🔓 No key needed | — | Free (open EU) |
| 16 | Allianz Trade | deals | Buyer credit risk + trade credit insurance — sets the credit limit / whether 100% advance escrow is required | 🔑 Key needed | `ALLIANZ_TRADE_CLIENT_ID` + `_CLIENT_SECRET` | Per-check fee |
| 17 | Shared RouteToRich LLM Gateway | negotiation / exception resolver | Chat completion service for negotiation messages and exception classification | ✅ Wired | `LLM_GATEWAY_URL` + service-account credentials | Gateway account pricing |
| 18 | MongoDB Atlas | all services | Primary datastore for every service — deals, opportunities, documents, and shared cache | ✅ Connected | `MONGO_URI` | Free M0 cluster |
| 21 | Gmail SMTP | email_gateway | Sends outbound negotiation emails to buyers and suppliers (SMTP half of the transport) | 🔑 Key needed | `GMAIL_SENDER_ADDRESS` + `GMAIL_APP_PASSWORD` | Free |
| 22 | Gmail IMAP | email_gateway | Monitors the inbox for buyer/supplier replies (auto poll, 15 min) → forwards to email_processor | 🔑 Key needed | `IMAP_USER` + `IMAP_PASSWORD` | Free |
| 23 | Session locks (MongoDB) | negotiation | Prevents two concurrent requests from racing on the same negotiation session | ✅ Shared cache | in-memory dev → shared MongoDB cache (no Redis) | Free (reuses Atlas) |
| 24 | SGS / Intertek | shield-link | Books pre-shipment QC inspection — the pass/fail result gates dispatch and escrow release | ✅ Wired (stub when no key) | Set `SGS_API_KEY` for live bookings | Per-inspection fee |
| 25 | Cogoport | shield-link | Books ocean freight and tracks milestones (in-transit → arrived/cleared) | ✅ Wired (stub when no key) | Set `COGOPORT_API_KEY` for live freight | Per-shipment fee |
| 26 | Freightos | shield-link | Fallback freight spot rates when the primary forwarder is unavailable | ✅ Wired (stub when no key) | Set `FREIGHTOS_API_KEY` for live rates | Per-quote fee |
| 27 | ICEGATE EDI | docugen + shield-link | Files the Indian customs Shipping Bill and obtains the Let-Export-Order | ✅ Wired (stub when no key) | `ICEGATE_API_KEY` to enable live filing | Free (govt) |
| 28 | Stripe Treasury | escrow-guard | Holds buyer funds in escrow (manual-capture PaymentIntent) until the deal is safe to fund | ✅ Wired (stub when no key) | Set `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` | 0.25% per transfer |
| 29 | Payoneer | escrow-guard | Pays out the supplier in INR after customs clearance | ✅ Wired (stub when no key) | Set `PAYONEER_CLIENT_ID` + `PAYONEER_CLIENT_SECRET` + `PAYONEER_WEBHOOK_SECRET` | 1–2% per payout |

---

### Phase 1 — Historical Demand (Trade Intelligence)

---

#### 1. UN Comtrade — ✅ Connected & verified

**What it does:** Global trade volume data by HS code and country. Used to detect
import surges, e.g. "Spain's pepper imports from India grew 40% YoY."

**Connector:** `ComtradeConnector` in `shared/connectors/trade_intel.py`  
**Base URL:** `https://comtradeapi.un.org`  
**Auth:** Query param `?subscription-key=<KEY>`

**Cost:**
- Free tier — 500 API calls/day, up to 100K records/call *(sufficient for dev)*
- Premium — unlimited calls, 250K records/call *(contact comtrade@un.org)*

**Connection status (verified 2026-06-26):**
- `COMTRADE_SUBSCRIPTION_KEY` is set in `.env` and validated against the live API.
- Test call `GET https://comtradeapi.un.org/data/v1/get/C/A/HS` (cmdCode `100190`, period `2023`,
  flow `M`) returned **HTTP 200** with real records (~3.0 s, incl. ~1.07 s server compute).
- Re-test any time with `python scripts/check_trade_api_connections.py` → row shows **✓ OK**.
- **Quota note:** responses are cached for 7 days (see *Raw API response caching* under Phase 1),
  so repeat scans of the same HS × market × period do not re-spend the 500-calls/day free quota.

**Steps to connect (already completed):**
1. Create a free account at https://comtradeplus.un.org
2. Go to https://comtradedeveloper.un.org → **Products** → **Free APIs** → **Subscribe** (auto-approved, instant)
3. Go to **Profile** → **Show keys** → copy the subscription key
4. Add to `.env`:  `COMTRADE_SUBSCRIPTION_KEY=<your-key>`

---

#### 2. Volza — 🔑 Key needed

**What it does:** Bill of Lading & customs shipping manifests for 80+ countries.
Extracts the corporate names of active buyers already importing a specific HS code
from India — enabling warm, verified outreach.

**Connector:** `VolzaConnector` in `shared/connectors/trade_intel.py`  
**Base URL:** `https://backend.volza.com`  
**Auth:** Bearer token (`Authorization: Bearer <KEY>`)

**Cost:**
- $0.10 per search query
- $0.02+ per shipment record returned
- Rate limit: 50 req/min (Standard) / 150 req/min (Custom plan)
- Enterprise plans available

**Steps to connect:**
1. Sign up at https://app.volza.com
2. Navigate to **API Settings** → **Generate Key**
   - OR email helpdesk@volza.com for enterprise / trial access
3. Add to `.env`:  `VOLZA_API_KEY=<your-key>`

> **Note:** A test call to `POST /api/v1/global-shipments` incurs cost ($0.10).
> The connection check only verifies network reachability (HEAD request), not auth.

---

#### 3. Trademo Intel — 🔑 Key needed

**What it does:** Supply-chain records and buyer risk profiling. Identifies
high-volume Western buyers and assesses their supply-chain risk.

**Connector:** `TrademoConnector` in `shared/connectors/trade_intel.py`  
**Base URL:** `https://platform.trademo.com/api/v1`  
**Auth:** `X-API-Key: <KEY>` header

**Cost:** Subscription plan (contact Trademo for pricing)

**Steps to connect:**
1. Register at https://app.trademo.com
2. Navigate to **Settings** → **API Settings** → generate key
3. Add to `.env`:  `TRADEMO_API_KEY=<your-key>`

---

#### 4. ImportGenius / Panjiva — 🔑 Key needed

**What it does:** US Customs and Latin-American marine manifests. Spotlights
high-volume importers who repeatedly buy bulk categories from India.

**Connector:** `ImportGeniusConnector` in `shared/connectors/trade_intel.py`  
**Base URL:** `https://api.importgenius.com/v2`  
**Auth:** Bearer token (`Authorization: Bearer <KEY>`)

**Cost:** Enterprise plan only — contact ImportGenius sales for pricing

**Steps to connect:**
1. Contact https://www.importgenius.com to request an enterprise API account
2. Credentials are issued after account approval
3. Add to `.env`:  `IMPORTGENIUS_API_KEY=<your-key>`

---

### Phase 2 — Live Demand Signals

---

#### 5. Alibaba International RFQ — 🔑 Key needed

**What it does:** Polls active buyer RFQs (buy-offers) posted on Alibaba International.
A live RFQ is the strongest possible buying-intent signal — the buyer is sourcing *right now*.

**Connector:** `AlibabaRfqConnector` in `shared/connectors/demand_signals.py`  
**Base URL:** `https://gw.api.alibaba.com/openapi/param2/1`  
**Auth:** HMAC-SHA256 signed request (app key + secret computed per-call)

**Cost:** Developer tier is free; high-volume plans vary

**Steps to connect:**
1. Go to https://developer.alibaba.com → **Register as a developer**
2. **Create an Application** in the dashboard
3. Apply for the **"International Trade"** API product (review takes 1–3 business days)
4. Once approved, copy **App Key** and **App Secret** from the app dashboard
5. Add to `.env`:
   ```
   ALIBABA_APP_KEY=<your-app-key>
   ALIBABA_APP_SECRET=<your-app-secret>
   ```

---

#### 6. GDELT Project — ⚠️ Network blocked (open API — no key needed)

**What it does:** Real-time global news monitoring. Detects supply-chain disruption
signals — export bans, port strikes, trade sanctions — that create forced demand
for alternative suppliers like India.

**Connector:** `GdeltDisruptionConnector` in `shared/connectors/demand_signals.py`  
**Base URL:** `https://api.gdeltproject.org/api/v2/doc/doc`  
**Auth:** None — fully open, no registration required

**Cost:** Free

**Current status:** ⚠️ `ConnectError` from this machine — the domain
`api.gdeltproject.org` is not reachable from the current corporate network.
This is a **firewall/proxy issue only**; the API is publicly available.

**Action required:**
- If behind a corporate proxy: whitelist `api.gdeltproject.org` (port 443)
- If using a VPN: test connectivity with `curl https://api.gdeltproject.org/api/v2/doc/doc?query=trade&mode=artlist&maxrecords=1&format=json`
- No env var needed; the connector works automatically once the network path is open

> **Pipeline impact:** GDELT is a Phase 2 *optional* connector. The pipeline degrades
> gracefully if it is unreachable — it contributes to the live disruption signal score
> but its absence does not abort the scan.

---

#### 7. EU TED (Tenders Electronic Daily) — ✅ Connected

**What it does:** European institutional procurement notices from hospitals, universities,
and government bodies. These buyers have published budgets, defined quantities,
and hard deadlines — the most commercially predictable buying signal.

**Connector:** `EuTedConnector` in `shared/connectors/demand_signals.py`  
**Base URL:** `https://ted.europa.eu/api/v3.0`  
**Auth:** None — fully open, no registration required

**Cost:** Free

**Last test:** HTTP 202, 832 ms — fully reachable from this machine.  
No action required.

---

### Phase 3 — Tariff Gate

---

#### 8. ITC MacMap — 🔑 Key needed

**What it does:** Import tariffs, non-tariff barriers, and rules of origin.
Used as the Phase 3 gatekeeper: if `net_price_gap < 1%` after applying the
importer-country tariff, the opportunity is hard-discarded.

**Connector:** `MacmapConnector` in `shared/connectors/trade_intel.py`  
**Base URL:** `https://api.macmap.org/v2`  
**Auth:** `Ocp-Apim-Subscription-Key: <KEY>` header

**Cost:** Subscription plan — see https://macmap.org/tools/macmap-api for pricing tiers

**Steps to connect:**
1. Visit https://macmap.org/tools/macmap-api
2. Register and select a subscription plan
3. Retrieve the subscription key from your account dashboard
4. Add to `.env`:  `MACMAP_API_KEY=<your-key>`

---

### Phase 4 — Supplier Discovery

---

#### 9. IndiaMART — 🔑 Key needed

**What it does:** India's largest wholesale B2B directory. Finds verified Indian
manufacturers supplying a specific HS code, with pricing, capacity, and RFQ capability.

**Connector:** `IndiamartConnector` in `shared/connectors/trade_intel.py`  
**Base URL:** `https://seller.indiamart.com/lmsrest/api/v1`  
**Auth:** `IMAPI-KEY: <KEY>` header + `mobile_no` query param

**Cost:** Included in IndiaMART seller subscription (Gold / Platinum plan)  
**Rate limit:** 1,000 calls/day on standard plan

**Steps to connect:**
1. Register as a verified seller at https://seller.indiamart.com
2. Go to **Settings** → **API Settings** → **Generate API Key** (auto-approved for verified sellers)
3. Note your registered mobile number (required on every API call)
4. Add to `.env`:
   ```
   INDIAMART_API_KEY=<your-key>
   INDIAMART_MOBILE=<registered-mobile-number>
   ```

---

#### 10. TradeIndia — 🔑 Key needed

**What it does:** Fallback wholesale directory for Indian supplier discovery.
Used when IndiaMART yields insufficient quotes or for alternative price benchmarking.

**Connector:** `TradeIndiaConnector` in `shared/connectors/trade_intel.py`  
**Base URL:** `https://api.tradeindia.com/v1`  
**Auth:** Bearer token — token valid for 90 days, refresh via `/auth/refresh`

**Cost:** Included in paid membership (Gold / Platinum plan)  
**Rate limit:** 500 calls/day on standard plan

**Steps to connect:**
1. Register a company account at https://www.tradeindia.com
2. Go to **My Account** → **API Access** → **Generate Token**
3. Add to `.env`:  `TRADEINDIA_API_KEY=<your-bearer-token>`

---

### Regulatory & Verification

---

#### 11–14. API Setu (GSTN + MCA21 + Udyam) — 🔑 Key needed + ⚠️ paths unverified

**What it does:** Three Indian government verification APIs covered by a single key:
- **GSTN** — validates supplier GSTIN (GST registration active/cancelled)
- **MCA21** — company registry check (incorporation date, business age)
- **Udyam** — MSME/manufacturer certification verification

These run in parallel on every inbound supplier quote before the Match Engine
re-scores. A failed GSTN check (`gst_verified = false`) hard-discards the supplier
(`C_supplier = 0 → CMS = 0`).

**Connectors:** `GstnConnector`, `McaConnector`, `UdyamConnector` in `shared/connectors/regulatory.py`  
**Auth (fixed 2026-06-27):** `X-APISETU-APIKEY: <KEY>` + optional `X-APISETU-CLIENTID: <ID>`
— the verified API Setu standard (the old `X-API-Key` / `X-Client-Id` names were wrong).

**Connection check (2026-06-27):** ⚠️ **Endpoint paths unverified.** Unlike DGFT
(#15), the GSTN / MCA / Udyam products are **not** in the public API Setu catalog
— they live in the **partner directory** (`partners.apisetu.gov.in`, e.g.
"GSTN Tax Payer API V2") and require a subscription. The base paths currently in
the connectors (`apisetu.gov.in/gstn/v1`, `/mca/v3`, `/udyam/v1`) are **placeholders
that return HTTP 404** — they could not be probed to a 401 without a subscription.
The auth headers are now correct, but the **production paths must be filled in
after subscribing.**

**Cost:** Free — government service (subscription approval required)

**Steps to connect:**
1. Create a developer account at https://apisetu.gov.in and sign in to the
   partner portal at https://partners.apisetu.gov.in
2. Subscribe to the GSTN Tax Payer, MCA/CIN, and Udyam API products
3. Copy the **production base URL + path template** shown for each subscribed
   product and set them as `_PROD_BASE` / `_BASE` + the lookup path in
   `regulatory.py` (replacing the ⚠️ placeholders)
4. Copy your **API Key** and **Client ID** and add to `.env`:
   ```
   API_SETU_KEY=<your-api-key>
   API_SETU_CLIENT_ID=<your-client-id>
   ```
5. For testing, add `GSTN_USE_SANDBOX=1` to target the sandbox endpoint

---

#### 15. DGFT IEC — 🔑 Key needed (via API Setu)

**What it does:** Directorate General of Foreign Trade — verifies a supplier's
Import-Export Code (IEC) status and checks against the denied-party list.

**Connector:** `DgftConnector` in `shared/connectors/regulatory.py`  
**Base URL:** `https://apisetu.gov.in/dgft/v1` → `GET /iec/{iec}`  
**Auth:** API Setu key — headers `X-APISETU-APIKEY` + `X-APISETU-CLIENTID`
(reuses `API_SETU_KEY` / `API_SETU_CLIENT_ID`, same as GSTN/MCA/Udyam).

**Connection check (2026-06-27):**
- ✅ **Fixed.** The legacy free path `www.dgft.gov.in/CP/iecList/GovtAPI/IECDetails/{iec}`
  was decommissioned (HTTP 404). The connector now targets the API Setu
  **Importer-Exporter Details API** (`/dgft/v1/iec/{iec}`).
- Live probe of the new endpoint returns **HTTP 401 `invalid_authentication`**
  (JSON) without a key — i.e. the endpoint is live and only needs valid API Setu
  credentials.

**Cost:** Free (govt)  
**Action required:** Set `API_SETU_KEY` (+ `API_SETU_CLIENT_ID`) in `.env` and
subscribe to the DGFT "Importer-Exporter Details" product on
https://apisetu.gov.in. Without the key the connector is skipped and sourcing
falls back to the `iec_active` flag from the quote.

---

#### 16. VIES (EU VAT Validation) — 🔓 No key needed

**What it does:** Validates EU VAT numbers for overseas buyers to confirm the
buyer entity is a real, registered European business.

**Connector:** `ViesConnector` in `shared/connectors/regulatory.py`  
**Base URL:** `https://ec.europa.eu/taxation_customs/vies/rest-api`  
**Auth:** None — public European Commission API

**Cost:** Free  
**Action required:** None — the connector works out of the box.

---

#### 17. Allianz Trade (Euler Hermes) — 🔑 Key needed

**What it does:** Corporate risk scores and dynamic credit limits for overseas buyers.
Auto-underwrites trade credit insurance — flags if 100% advance escrow is mandatory
for high-risk counterparties.

**Connector:** `AllianzTradeConnector` in `shared/connectors/regulatory.py`  
**Auth URL:** `https://auth.allianz-trade.com/oauth2/token` (OAuth 2.0 `client_credentials`)  
**API URL:** `https://api.allianz-trade.com/v1`

**Cost:** Per-credit-check fee — contact Allianz Trade for pricing tiers

**Steps to connect:**
1. Go to https://developer.allianz-trade.com
2. **Create an account** → **Create an Application**
3. Request access to the **Credit Check** and **Policy** API products
4. Copy **Client ID** and **Client Secret** from the app dashboard
5. Set up a webhook endpoint to receive policy events, and copy the webhook signing secret
6. Add to `.env`:
   ```
   ALLIANZ_TRADE_CLIENT_ID=<your-client-id>
   ALLIANZ_TRADE_CLIENT_SECRET=<your-client-secret>
   ALLIANZ_TRADE_WEBHOOK_SECRET=<your-webhook-secret>
   ```

---

### LLM & AI

---

#### 18. Groq — ✅ Connected (default key active)

**What it does:** Primary LLM inference backend for the Negotiation Agent's
orchestrator, buyer-agent, and supplier-agent nodes via LangGraph.

**Models in fallback chain:**
1. `qwen/qwen3-32b` via Groq *(primary fallback)*
2. `qwen/qwen3.6-27b` via Groq
3. `openai/gpt-oss-safeguard-20b` via Groq

**Config:** `shared/llm_gateway/config.py`  
**Current status:** ✅ Default API key is hardcoded in `config.py` and active.

**Cost:** Pay-per-token — see https://console.groq.com/settings/billing for current rates

**Action for production:**
- Replace the default key with your own to track usage per project
- Add to `.env`:  `LLM_GROQ_API_KEY=<your-groq-key>`
- Get a key at https://console.groq.com → **API Keys** → **Create API key**

> ⚠️ **Security note:** The default Groq key is committed in `config.py`. Rotate it
> before going to production by setting `LLM_GROQ_API_KEY` in `.env`.

---

#### 19. Anthropic Claude — 🔑 Key needed (optional — Groq fallback is active)

**What it does:** Optional primary LLM, tried before the Groq fallback chain.
Claude is the recommended choice for highest-quality negotiation reasoning.

**Recommended model:** `claude-sonnet-4-6`  
**Config:** `LLM_PRIMARY_CONNECTOR=anthropic`

**Cost:** Pay-per-token — see https://www.anthropic.com/pricing for current rates  
*(Sonnet 4.6: ~$3/1M input tokens, ~$15/1M output tokens)*

**Steps to connect:**
1. Go to https://console.anthropic.com → **API Keys** → **Create Key**
2. Add to `.env`:
   ```
   LLM_PRIMARY_CONNECTOR=anthropic
   LLM_PRIMARY_MODEL=claude-sonnet-4-6
   LLM_PRIMARY_API_KEY=<your-anthropic-api-key>
   ```

---

### Database & Persistence

---

#### 20. MongoDB Atlas — persistence layer

**What it does:** Primary persistence layer for all services — opportunities,
deals, RFQs, negotiation sessions, and LLM call tracking.

**Cluster:** your own Atlas cluster (M0 free tier works for dev)  
**Config:** set a single `MONGO_URI` in `.env` — every service uses it (each with
its own DB name). No connection string is hardcoded in the code.

**Cost:** M0 cluster is free. Paid tiers start at ~$57/month (M10).

**Per-service env vars** (if you want to separate collections or clusters):
```
TRADE_MONGO_URI=<uri>                TRADE_MONGO_DB_NAME=trade
DEALS_MONGO_URI=<uri>                DEALS_MONGO_DB_NAME=deals
SOURCING_MONGO_URI=<uri>             SOURCING_MONGO_DB_NAME=sourcing
NEGOTIATION_MONGO_URI=<uri>          NEGOTIATION_MONGO_DB_NAME=negotiation
DEALMAKER_MONGO_URI=<uri>            DEALMAKER_MONGO_DB_NAME=dealmaker
DOCUGEN_MONGO_URI=<uri>              DOCUGEN_MONGO_DB_NAME=docugen
SHIELD_LINK_MONGO_URI=<uri>          SHIELD_LINK_MONGO_DB_NAME=shield_link
ESCROW_GUARD_MONGO_URI=<uri>         ESCROW_GUARD_MONGO_DB_NAME=escrow_guard
HITL_APPROVAL_MONGO_URI=<uri>        HITL_APPROVAL_MONGO_DB_NAME=hitl_approval
EXCEPTION_RESOLVER_MONGO_URI=<uri>   EXCEPTION_RESOLVER_MONGO_DB_NAME=exception_resolver
LLM_MONGO_URI=<uri>                  LLM_MONGO_DB_NAME=llm_gateway
```

> ⚠️ **Security note:** The default MongoDB credentials are committed in source code.
> Rotate them in Atlas before sharing this repository publicly:
> Atlas UI → **Database Access** → reset password → update `.env` files.

---

#### 22. Gmail SMTP (Outbound) — 🔑 Key needed

**What it does:** Sends negotiation messages to buyers and suppliers via email.
Without this configured the Negotiation Agent logs messages instead of sending them
(dev/CI mode — deals still function end-to-end).

**Protocol:** SMTP via `smtp.gmail.com:587` (STARTTLS) using `aiosmtplib`  
**Config:** `services/email_gateway/` (email transport, port 8012; SMTP send called from the negotiation flow)

**Cost:** Free

**Steps to connect (one-time Gmail setup):**
1. Enable 2-Step Verification: https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords
   - App: **Mail**, Device: **Other** → name it `PortLess Negotiation`
   - Copy the 16-character password (spaces are optional)
3. Add to `.env`:
   ```
   GMAIL_SENDER_ADDRESS=your-portless-gmail@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   SMTP_FROM_NAME=PortLess Trade Agent
   ```

---

#### 23. Gmail IMAP (Inbox polling) — 🔑 Key needed

**What it does:** Polls the Gmail inbox for buyer/supplier email replies and routes
them back into the Negotiation Agent's round loop.

**Status:** Implemented in `services/email_gateway/` (email transport, port 8012). The
`aioimaplib` poller runs automatically every 15 minutes and forwards each fetched email to the
Email Processor; polling is disabled (logged, not an error) until `IMAP_USER` / `IMAP_PASSWORD`
are set.

**Steps when ready:**
1. Enable **IMAP access** in Gmail Settings → **See all settings** → **Forwarding and POP/IMAP**
2. Use the same App Password as SMTP above
3. Add to `.env`:
   ```
   IMAP_HOST=imap.gmail.com
   IMAP_PORT=993
   IMAP_USER=your-portless-gmail@gmail.com
   IMAP_PASSWORD=xxxx xxxx xxxx xxxx
   IMAP_USE_SSL=true
   IMAP_POLL_INTERVAL_SECONDS=900   # 15 minutes
   ```

---

#### 24. Session locks — ✅ MongoDB (shared cache)

**What it does:** Prevents two concurrent requests from racing on the same
negotiation session.

**Status:** Single-instance dev uses an in-memory dict. For multi-replica
deployments the shared MongoDB cache (`shared.cache`, the same cluster used
everywhere) is the lock store — **no Redis required**. PortLess uses MongoDB as
its one cache/lock backend.

**Cost:** Free (reuses the existing Atlas cluster).

---

### Phase 4 Stubs — Connector Implementation Required

These connectors are fully scaffolded (correct interface, correct file location)
but raise `NotImplementedError` for every method.

---

#### 25. SGS / Intertek / Bureau Veritas — ✅ Wired

**What it does:** Books third-party QC inspection at the supplier's warehouse.
Result (pass/fail + moisture/weight report) triggers `QC_PASSED` or `QC_FAILED`
deal transitions and releases the supplier payment milestone.

**Connector:** `SgsConnector` in `shared/connectors/shields.py`

**Cost:** Per-inspection fee (~$300–$800 per inspection, varies by commodity and location)

**Steps to implement:**
1. Register as an API customer at https://www.sgs.com/en/our-company/digital-solutions
   or https://www.intertek.com/api/
2. Implement `book()` to place an inspection order (warehouse GPS, HS code, buyer spec)
3. Implement `webhook_verify()` to receive the inspection result callback
4. Transition deal → `QC_PASSED` or `QC_FAILED` on webhook receipt

---

#### 26. Cogoport — ✅ Wired

**What it does:** Freight booking and spot rate queries for FCL / LCL containers.

**Connector:** `CogoportConnector` in `shared/connectors/shields.py`

**Steps to implement:**
1. Register at https://www.cogoport.com/en/api-access
2. Implement `quote()` to get spot rates (origin port, destination port, container type, CBM)
3. Implement `book()` to confirm a container booking
4. Emit `freight.booked` domain event on success

---

#### 27. Freightos — ✅ Wired

**What it does:** Alternative freight spot-rate source and booking platform.
Used as a fallback or benchmark alongside Cogoport.

**Connector:** `FreightosConnector` in `shared/connectors/shields.py`

**Steps to implement:**
1. Register at https://www.freightos.com/freight-api/
2. Implement `quote()` and `book()` using the Freightos Booking API

---

#### 28. ICEGATE EDI — ✅ Wired

**What it does:** Files the Indian Shipping Bill with customs (ICEGATE), captures
the Let Export Order (LEO), and triggers the `CLEARED` deal transition.

**Connector:** `IcegateConnector` in `shared/connectors/shields.py`

**Steps to implement:**
1. Access requires a registered Customs House Agent (CHA) with an ICEGATE digital certificate
2. ICEGATE provides EDI message specs at https://www.icegate.gov.in/Webappl
3. Alternative: Use a CHA partner's API (e.g., Cleardekho, Kargobot) which abstracts the EDI layer
4. Implement `book()` to submit the Shipping Bill XML; `status()` to poll LEO issuance

**Cost:** Free (government service); CHA fees apply (~₹5,000–₹15,000 per shipment)

---

#### 29. Stripe Treasury — ✅ Wired

**What it does:** Holds buyer payment in an escrow-style financial account.
Funded webhook triggers `ESCROW_FUNDED` deal transition.

**Connector:** `StripeTreasuryConnector` in `shared/connectors/payments.py`

**Cost:** 0.25% per transfer; additional Stripe fees apply

**Steps to implement:**
1. Apply for Stripe Treasury access at https://stripe.com/treasury (waitlist / invitation)
2. Alternatively use standard **Stripe Payment Intents** for simpler escrow simulation
3. Implement `book()` to create a hold; `webhook_verify()` using `stripe.Webhook.construct_event()`
4. Emit `escrow.funded` when the PaymentIntent status is `succeeded`

---

#### 30. Payoneer — ✅ Wired

**What it does:** Releases milestone payments in INR to Indian suppliers.
Used at `QC_PASSED` (partial advance) and `CLEARED` (final margin release).

**Connector:** `PayoneerConnector` in `shared/connectors/payments.py`

**Cost:** 1–2% per payout; FX conversion fees apply

**Steps to implement:**
1. Register as a Payoneer partner at https://developer.payoneer.com
2. Complete KYB (Know Your Business) onboarding for INR payout capability
3. Implement `book()` to initiate a payout using the Payoneer Payout API
4. Emit `payout.requested` and `payout.completed` events on success

---

---

## Trade-Min Cost-Reduction — Free / Low-Cost API Alternatives

> **Why this section?** Several Trade-Min connectors are paid (Volza, Trademo,
> ImportGenius, ITC MacMap, IndiaMART, TradeIndia). This maps each to a free or
> cheaper alternative so the 5-phase opportunity pipeline can run end-to-end
> without per-record fees — using **global** sources where possible and official
> **per-country** sources where global ones fall short. Analysis added 2026-06-27.

### What we're actually paying for (the key distinction)

The paid trade APIs deliver **two very different things**, and only one of them is
expensive to replace:

1. **Aggregate trade statistics** — *"how much HS 090422 did Spain import from
   India last year?"* → **fully replaceable for free** from official
   government/IGO sources (UN Comtrade, World Bank WITS, Eurostat, US Census, DGCIS…).
2. **Manifest-level *named-buyer* discovery** — *"which companies in Spain import
   it, with shipment dates"* (Bill-of-Lading data) → **this is the proprietary,
   expensive part.** Free coverage is essentially **US-only** (ImportYeti, via a
   FOIA request). Most countries do not release named BoL data — which is exactly
   why Volza / ImportGenius / Trademo charge per record.

> **Design implication:** keep Phase 1 *demand sizing* on free official stats, and
> treat *named-buyer discovery* as best-effort enrichment (US-only free, paid
> elsewhere) rather than a hard dependency. `_safe_quote` + `_P1_WEIGHTS` already
> redistribute weight when a source returns nothing, so dropping a paid source
> degrades gracefully.

### Current paid connectors (what to replace)

| Connector | Phase | Cost today | What it provides |
|---|---|---|---|
| Volza | 1 | $0.10/search + $0.02/record | Named buyers from BoL manifests |
| Trademo | 1 | Subscription | Named buyers + risk profiling |
| ImportGenius | 1 | Enterprise plan | US / LatAm BoL manifests |
| ITC MacMap | 3 | Subscription | Tariffs, NTBs, rules of origin |
| IndiaMART | 4 | Seller plan | Indian supplier directory |
| TradeIndia | 4 | Paid membership | Indian supplier directory |

*(Already free: UN Comtrade 500/day, GDELT, EU TED, Alibaba RFQ dev tier.)*

### Phase 1 — historical demand (aggregate stats) → **fully replaceable, free**

| Source | Coverage | Free? | API base |
|---|---|---|---|
| UN Comtrade *(in use)* | Global, official, HS6 | ✅ 500 calls/day | `comtradeapi.un.org` |
| **World Bank WITS** | Global trade **+ tariff**, HS6 | ✅ key-less | `https://wits.worldbank.org/API/V1` (SDMX/JSON) |
| US Census Intl Trade | US imports/exports by HS | ✅ free (key) | `api.census.gov/data/timeseries/intltrade/imports/hs` |
| Eurostat Comext | EU + many non-EU | ✅ free | `ec.europa.eu/eurostat` (Comext API / bulk) |
| India DGCIS | India trade, 24-mo principal commodities | ✅ free (portal) | `ftddp.dgciskol.gov.in` / `data.gov.in` |
| OEC | Global, rich data | ⚠️ API needs Pro/Premium token | `oec.world/en/resources/api` |

➡ **Recommendation:** add a **`WitsConnector`** — one free API that covers **both**
Phase 1 demand **and** Phase 3 tariffs globally. Layer Census / Eurostat / DGCIS as
high-resolution per-country add-ons.

### Phase 1 — named-buyer discovery (the genuine gap)

| Source | Coverage | Free? | Notes |
|---|---|---|---|
| **ImportYeti** | US sea imports, BoL Jan-2015+ | ✅ free search | No official API → UI/export only; **US-only** |
| Datamyne (Descartes) / TradeInt / Market Inside | Global | ❌ paid | Sometimes cheaper tiers than Volza |
| *Workaround:* Comtrade/WITS + Alibaba RFQ + EU TED + GDELT | Global | ✅ | Size demand + capture **live intent** instead of named historical buyers |

➡ **Recommendation:** outside the US, drop the hard dependency on named historical
buyers; rely on the already-free **live-intent** signals (Alibaba RFQ, EU TED,
GDELT) for actionable leads, and ImportYeti for US named buyers.

### Phase 3 — tariff gate → **fully replaceable, free** (replaces MacMap)

| Source | Coverage | Free? | API base |
|---|---|---|---|
| **World Bank WITS (TRAINS)** | Global, MFN + preferential, HS6 | ✅ | `wits.worldbank.org/API/V1/SDMX/V21` |
| **EU Access2Markets** | EU + 120 markets; tariffs **+ NTBs + rules of origin** (direct MacMap analog) | ✅ | `trade.ec.europa.eu/access-to-markets` |
| WTO Tariff & Trade Data | 150+ economies | ✅ | `ttd.wto.org` |
| USITC DataWeb / HTS | US tariffs | ✅ (account for API) | `dataweb.usitc.gov` |
| Country customs (EU TARIC, India ICEGATE/CBIC) | per-country | ✅ | — |

➡ **Recommendation:** MacMap → **WITS** (global, same HS6) as default; **Access2Markets**
for EU destinations (it also returns NTBs + rules of origin, matching MacMap's extra
fields used in `_parse_macmap`).

### Phase 4 — supplier discovery → **partly replaceable**

| Source | Coverage | Free? | Notes |
|---|---|---|---|
| Alibaba supplier search | Global | ✅ dev tier | already integrated for RFQs |
| OpenCorporates | Global company registry | ✅ free-tier API | identity/legitimacy, not product catalog |
| India MCA / DGFT IEC | Indian exporters | ✅ (gov) | registry/IEC, not catalog |
| FIEO / Export Promotion Council directories | India | ✅ public | member exporter lists |
| Kompass / Europages / ThomasNet | Global / EU / US | ❌ mostly paid API (free search UI) | |

➡ **Recommendation:** product-catalog directories are inherently commercial. For a
free path, combine **Alibaba supplier search** + **registry/IEC data** (MCA, DGFT) +
public **FIEO/EPC** directories; keep IndiaMART/TradeIndia as optional paid enrichment.

### Country-wise free official sources (quick reference)

| Region | Trade stats (free) | Tariffs (free) |
|---|---|---|
| 🌐 Global | UN Comtrade, World Bank WITS, WTO TTD | WITS TRAINS, WTO TTD |
| 🇺🇸 USA | US Census Intl Trade API; ImportYeti (named BoL) | USITC DataWeb / HTS |
| 🇪🇺 EU | Eurostat Comext | EU Access2Markets, TARIC |
| 🇮🇳 India | DGCIS / data.gov.in; DGFT trade stats | ICEGATE / CBIC; WITS |
| 🇬🇧 UK | uktradeinfo.com | trade-tariff.service.gov.uk (UK Global Tariff) |

### Recommended free-first stack (keeps the whole flow at ~$0)

- **Phase 1 demand:** UN Comtrade + World Bank WITS (global) → Census / Eurostat / DGCIS for per-country resolution
- **Phase 1 buyers:** Alibaba RFQ + EU TED + GDELT (live intent, already free) + ImportYeti (US named buyers)
- **Phase 2 signals:** unchanged (already free)
- **Phase 3 tariffs:** World Bank WITS (default) / EU Access2Markets (EU destinations)
- **Phase 4 suppliers:** Alibaba supplier search + MCA / DGFT registry + FIEO / EPC directories

Net effect: the only capability genuinely lost vs. the paid stack is **non-US named
historical buyers** — substituted by free **live-intent** signals, which are arguably
more actionable for a "right-now" deal anyway.

### Implementation notes & caveats

- **Drop-in via the connector pattern:** each alternative is a new
  `shared/connectors/*.py` adapter implementing `quote/book/status/webhook_verify`;
  Phase 1 weight redistribution (`_P1_WEIGHTS`) and `_safe_quote` mean swapping
  sources needs no pipeline changes — just register the connector.
- **Formats:** WITS is SDMX (XML) + JSON; Comtrade/Census/Eurostat are JSON — add a
  small per-connector normaliser to the existing `_extract_*` shape.
- **Caching:** these are slow-changing → cache aggressively via `shared.cache`
  (extend `cache_ttl_seconds` to include `wits`, `census`, `eurostat`) to stay within
  free rate limits.
- **Latency/granularity:** official stats are monthly/annual and lag ~1–3 months —
  great for *demand trends*, not "shipped yesterday" (that's GDELT / RFQ's job).
- **Legality:** ImportYeti has no official API — respect its ToS (prefer export/UI
  over scraping). Non-US named BoL is generally not legally free.
- **"Low-cost" ≠ free:** OEC API, USITC DataWeb API, and the DGCIS subscriber tier
  need an account/token — treat as low-cost, not fully open.

---

### ✅ Implemented (2026-06-27) — what changed in the code

The free-first stack above is now **wired into the default pipeline**:

| New connector | File | Role | Connection status (checked 2026-06-27) |
|---|---|---|---|
| `WitsTariffConnector` (`wits_tariff`) | `trade_intel.py` | Phase 3 tariff gate — **replaces MacMap** | ✅ **Connected & verified** — live HTTP 200, US HS 020890 → 2.13% MFN (2023). No key. ~31s cold call → cached 7 days. |
| `CensusTradeConnector` (`census`) | `trade_intel.py` | Phase 1 US demand — augments Comtrade | 🔑 **Key needed** — endpoint reachable (HTTP 302 redirect without key); `CENSUS_API_KEY` is currently empty, so Census is skipped and Comtrade alone drives Phase 1. |
| `Access2MarketsConnector` (`access2markets`) | `trade_intel.py` | Phase 3 EU tariffs + NTBs + rules of origin | ⏳ **Wired, endpoint-gated** — `ACCESS2MARKETS_API_URL` unset, so EU tariffs **fall back to WITS** (verified). Set the URL to activate. |

> **Status legend for the above:** ✅ verified working now · 🔑 endpoint live, just needs a free key · ⏳ implemented but inactive pending an endpoint/credential. The "replaced" connectors (`MacmapConnector`, `VolzaConnector`, `TrademoConnector`, `ImportGeniusConnector`) are 🗄️ **Legacy** — retained in code but not called by the default pipeline.

**Pipeline rewiring** (`services/trade_min/service.py`, `config.py`):
- Phase 1 now gathers **Comtrade + Census** only (was Comtrade + Volza + ImportGenius + Trademo).
- Phase 3: **EU destinations** try **Access2Markets** first (adds NTBs + rules of origin), then fall back to **`wits_tariff`**; **non-EU** uses `wits_tariff` directly. (Both replace the paid `macmap`.)
- `phase1_min_connectors` lowered **2 → 1** (UN Comtrade is authoritative + global on its own).
- `_P1_WEIGHTS` → `{comtrade: 0.70, census: 0.30}`; `cache_ttl_seconds` adds `census`, `wits_tariff`, `access2markets` (7-day TTL).
- `DataSource` literal extended with `census` / `wits_tariff` (old values kept so persisted opportunities still validate).

**Mock + UI sync:** `mocked/trade_min/scan_result.json` source tags updated
(`volza`/`trademo`/`macmap` → `census`/`wits_tariff`); the UI flows these through
`portless-ui/src/mocks/adapter.ts`, and the Opportunity Radar / Autonomy Settings copy now
reads "Comtrade, US Census & World Bank WITS".

#### How to get the new API keys

- **World Bank WITS** — **no key, no registration.** Fully open; works out of the box.
- **US Census** (`CENSUS_API_KEY`, free, optional — only needed for the US market):
  1. Request a key at https://api.census.gov/data/key_signup.html
  2. Click the activation link in the confirmation email
  3. Add `CENSUS_API_KEY=<key>` to `.env`
  *(Without it, Census is simply skipped and Comtrade alone drives Phase 1.)*
- **EU Access2Markets** (`ACCESS2MARKETS_API_URL`, optional — EU markets only):
  The official portal API is not openly documented (the "My Trade Assistant"
  backend / Apitalks api.store wrapper need early-access registration). The
  connector is fully wired but **endpoint-gated**: set `ACCESS2MARKETS_API_URL`
  to the confirmed URL template (with `{reporter}`/`{partner}`/`{product}`
  placeholders) and optional `ACCESS2MARKETS_API_KEY`. **Until set, EU tariffs
  fall back to the verified WITS connector** — so the pipeline works either way.

### Replaced / Legacy Trade-Min connectors (kept for future use)

These connector classes remain in `trade_intel.py` and the registry but are **no
longer called by the default pipeline**. They are retained so you can re-enable
them later (e.g. if you buy a subscription) without re-implementing.

| Connector | Replaced by | How to re-enable |
|---|---|---|
| `MacmapConnector` (Phase 3 tariff, paid) | `WitsTariffConnector` (free) | Set `MACMAP_API_KEY`; restore the `macmap` call in `_phase3_tariff_gate` |
| `VolzaConnector` (named BoL buyers, paid) | *no free global equivalent* — live-intent signals (Alibaba RFQ / EU TED / GDELT) cover leads; ImportYeti for US | Set `VOLZA_API_KEY`; add `volza` back to the Phase 1 `gather` + `_P1_WEIGHTS` |
| `TrademoConnector` (buyer risk, paid) | retired | Set `TRADEMO_API_KEY`; re-add to Phase 1 |
| `ImportGeniusConnector` (US/LatAm BoL, paid) | retired | Set `IMPORTGENIUS_API_KEY`; re-add to Phase 1 |

**Phase 4 supplier directories (`IndiaMART`, `TradeIndia`) — unchanged.** There is
**no free product-catalog API** to drop in; they remain paid/optional. For a free
path, combine the already-integrated **Alibaba supplier search** with **MCA / DGFT
registry** data and public **FIEO / EPC** exporter directories (see Phase 4 table
above). The `_extract_volza` / `_extract_importgenius` / `_extract_trademo`
functions are likewise retained (dead but intact) for the same re-enable path.

---

## DealMaker & DocuGen — Implementation Reference

> **Status:** ✅ Production-grade implementation complete (2026-06-25)

---

### DealMaker Service (`services/dealmaker/`)

**Purpose:** Generates and tracks the two core trade-finance documents — the
Proforma Invoice (PI) sent to the buyer and the Purchase Order (PO) sent to the
supplier — at the correct deal lifecycle stages.

#### Deal State Transitions Enforced

| Document | Required Deal State | Target State |
|----------|-------------------|--------------|
| Issue PI | `NEGOTIATION_AGREED` | `PI_SENT` |
| Accept PI | PI status = `SENT` | Deal → `PI_ACCEPTED` |
| Issue PO | `ESCROW_FUNDED` | `PO_ISSUED` |

State transitions are validated via `shared.domain.state_machine.can_transition()`.
Any invalid transition raises HTTP 422.

#### Files

| File | Description |
|------|-------------|
| `config.py` | `DealMakerSettings` — Mongo URI, storage dir, margin floor (env: `DEALMAKER_MONGO_URI`, `PO_MARGIN_FLOOR_PCT`) |
| `schemas.py` | `IssuePiRequest`, `PiRecord`, `AcceptPiRequest`, `IssuePoRequest`, `PoRecord`, `DealDocumentsResponse` |
| `repository.py` | `Protocol` + `InMemoryDealMakerRepository` (fallback) + `MongoDealMakerRepository` (Motor) |
| `service.py` | `issue_pi()`, `accept_pi()`, `get_pi()`, `issue_po()`, `get_po()`, `get_deal_documents()` |
| `router.py` | FastAPI routes with full exception → HTTP status mapping |
| `main.py` | Standalone `uvicorn` app with `asynccontextmanager` lifespan |
| `client.py` | `DealMakerClient` (BaseServiceClient) for microservice mode |
| `templates/pi.html.j2` | Proforma Invoice HTML template (Jinja2) |
| `templates/po.html.j2` | Purchase Order HTML template (Jinja2) |

#### API Endpoints

```
POST /dealmaker/pi                       Issue Proforma Invoice (201)
POST /dealmaker/pi/{pi_id}/accept        Accept a PI (200)
GET  /dealmaker/pi/{pi_id}               Fetch PI (200 / 404)
POST /dealmaker/po                       Issue Purchase Order (201)
GET  /dealmaker/po/{po_id}               Fetch PO (200 / 404)
GET  /dealmaker/deals/{deal_id}/documents Both PI + PO for a deal (200)
GET  /dealmaker/health                   Health check
```

#### PI Request Example

```json
POST /dealmaker/pi
{
  "deal_id": "DEAL-ABCD1234EFGH",
  "agreed_price": 480.00,
  "currency": "USD",
  "quantity_mt": 100.0,
  "incoterm": "CIF",
  "port_of_destination": "Valencia, ES",
  "payment_terms": "30% advance + 70% on BL",
  "validity_days": 7,
  "buyer_name": "Acme Imports SA",
  "buyer_address": "1 Trade Plaza, Madrid, Spain",
  "idempotency_key": "PI-DEAL-ABCD-v1"
}
```

#### PO Request Example

```json
POST /dealmaker/po
{
  "deal_id": "DEAL-ABCD1234EFGH",
  "agreed_price": 420.00,
  "currency": "USD",
  "quantity_mt": 100.0,
  "incoterm": "FOB",
  "port_of_loading": "Chennai, IN",
  "delivery_deadline": "2026-09-30",
  "payment_terms": "30% advance + 70% on BL",
  "quality_spec": "AGMARK Grade 1, moisture < 12%, SGS CRF required",
  "idempotency_key": "PO-DEAL-ABCD-v1"
}
```

#### Margin Guard

PO issuance enforces a net-margin floor (default 8%):

```
net_margin_pct = (buyer_target_price - supplier_price) / buyer_target_price × 100
```

If `net_margin_pct < PO_MARGIN_FLOOR_PCT`, the request is rejected with HTTP 422
(`MarginGuardError`). The computed margin is stored on the `PoRecord` for audit.

If `buyer_target_price_usd` is not set on the deal, the guard is skipped with a
WARNING log — set it via `PATCH /deals/{deal_id}/financials` before issuing the PO.

#### Idempotency

All create operations (`issue_pi`, `issue_po`) accept an optional `idempotency_key`.
Re-submitting the same key returns the existing record without re-rendering or
re-emitting events.

#### Document Storage

Documents are rendered to HTML via Jinja2 and stored at `DEALMAKER_STORAGE_DIR`
(default `/tmp/portless-docs/dealmaker`). Each file is named `{PI_ID}.html` or
`{PO_ID}.html`. A SHA-256 hash of the rendered content is stored on the record
for integrity verification. Swap `_write_document()` in `service.py` for S3/GCS
in production.

#### Repository Fallback

The repository auto-selects at startup:
- `DEALMAKER_MONGO_URI` set and reachable → `MongoDealMakerRepository` (Motor)
- Otherwise → `InMemoryDealMakerRepository` (process-local, dev/CI only)

#### Domain Events Emitted

| Event | Trigger |
|-------|---------|
| `pi.sent` | `issue_pi()` success |
| `pi.accepted` | `accept_pi()` success |
| `po.issued` | `issue_po()` success |

#### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEALMAKER_MONGO_URI` | `""` | MongoDB URI (empty → in-memory) |
| `DEALMAKER_MONGO_DB_NAME` | `dealmaker` | Database name |
| `DEALMAKER_STORAGE_DIR` | `/tmp/portless-docs/dealmaker` | Document output directory |
| `PI_DEFAULT_VALIDITY_DAYS` | `7` | Default PI validity period |
| `PO_MARGIN_FLOOR_PCT` | `8.0` | Minimum acceptable net margin % |
| `PORTLESS_ENTITY_NAME` | `PortLess Trade Solutions Pvt Ltd` | Seller name on documents |
| `PORTLESS_ENTITY_ADDRESS` | (Mumbai address) | Seller address on documents |
| `PORTLESS_ENTITY_GSTIN` | `""` | Seller GSTIN |
| `PORTLESS_ENTITY_IEC` | `""` | Seller IEC code |

---

### DocuGen Service (`services/docugen/`)

**Purpose:** Generates the post-PI export and customs document package that
accompanies physical shipment and customs clearance:
- **CI** — Commercial Invoice (buyer-facing final invoice)
- **PL** — Packing List (logistics / customs)
- **CoO** — Certificate of Origin (non-preferential, FIEO / chamber stamp)
- **SDF** — Shipper's Declaration Form (FEMA / RBI requirement)
- **SHIPPING_BILL** — ICEGATE customs filing document with RoDTEP declaration

PI generation is owned by DealMaker; DocuGen rejects PI requests with HTTP 422.

#### Files

| File | Description |
|------|-------------|
| `config.py` | `DocuGenSettings` — Mongo URI, storage dir, ICEGATE key, entity details |
| `schemas.py` | `GenerateDocumentRequest`, `DocumentRecord`, `DocumentResponse`, `CustomsFilingRequest/Response` |
| `repository.py` | `Protocol` + `InMemoryDocuGenRepository` (fallback) + `MongoDocuGenRepository` (Motor) |
| `service.py` | `generate_document()`, `get_document()`, `list_deal_documents()`, `file_customs()` |
| `router.py` | FastAPI routes with error mapping |
| `main.py` | Standalone `uvicorn` app with `asynccontextmanager` lifespan |
| `client.py` | `DocuGenClient` (BaseServiceClient) for microservice mode |
| `templates/ci.html.j2` | Commercial Invoice template |
| `templates/pl.html.j2` | Packing List template |
| `templates/coo.html.j2` | Certificate of Origin template |
| `templates/sdf.html.j2` | Shipper's Declaration Form template |
| `templates/shipping_bill.html.j2` | Customs Shipping Bill template |

#### API Endpoints

```
POST /docugen/documents                  Generate a document (201)
GET  /docugen/documents/{doc_id}         Fetch a document (200 / 404)
GET  /docugen/deals/{deal_id}/documents  List all docs for a deal (200)
POST /docugen/customs/file               File Shipping Bill via ICEGATE (201)
GET  /docugen/health                     Health check
```

#### Generate Document Request

```json
POST /docugen/documents
{
  "deal_id": "DEAL-ABCD1234EFGH",
  "doc_type": "CI",
  "agreed_price": 480.00,
  "currency": "USD",
  "quantity_mt": 100.0,
  "incoterm": "CIF",
  "port_of_loading": "Chennai, IN",
  "port_of_destination": "Valencia, ES",
  "payment_terms": "30% advance + 70% on BL",
  "buyer_name": "Acme Imports SA",
  "buyer_address": "1 Trade Plaza, Madrid, Spain",
  "idempotency_key": "CI-DEAL-ABCD-v1"
}
```

Supported `doc_type` values: `CI`, `PL`, `CoO`, `SDF`, `SHIPPING_BILL`

#### Document Lifecycle

Each document passes through these statuses:

```
PENDING → RENDERED
       └→ FAILED   (template error; a PENDING record is left for debugging)
```

A `PENDING` record is persisted before rendering begins, so a crash during
template rendering leaves a trace. FAILED records are excluded from deal-scoped
queries (`get_by_deal_and_type`).

#### Customs Filing

```json
POST /docugen/customs/file
{
  "deal_id": "DEAL-ABCD1234EFGH",
  "rodtep_declaration": true,
  "draw_back_claim": false
}
```

`file_customs()` will:
1. Look for an existing RENDERED Shipping Bill for the deal
2. If absent, auto-generate one from the deal record
3. Submit to ICEGATE (stub — wire `ICEGATE_API_KEY` for live filing)
4. Return `shipping_bill_number` + `let_export_order` (LEO)
5. Emit `customs.filed`

#### Repository Fallback

Same pattern as DealMaker:
- `DOCUGEN_MONGO_URI` set and reachable → `MongoDocuGenRepository` (Motor)
- Otherwise → `InMemoryDocuGenRepository` (process-local)

#### Context Snapshot

Every rendered `DocumentRecord` stores a `context_snapshot` dict of all
template variables used for rendering. This enables document re-generation
(e.g. after a correction) without re-querying all source systems.

#### Domain Events Emitted

| Event | Trigger |
|-------|---------|
| `customs.filed` | `file_customs()` success; also emitted when `SHIPPING_BILL` doc is generated |

#### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOCUGEN_MONGO_URI` | `""` | MongoDB URI (empty → in-memory) |
| `DOCUGEN_MONGO_DB_NAME` | `docugen` | Database name |
| `DOCUGEN_STORAGE_DIR` | `/tmp/portless-docs/docugen` | Document output directory |
| `ICEGATE_BASE_URL` | `https://www.icegate.gov.in` | ICEGATE EDI base URL |
| `ICEGATE_API_KEY` | `""` | API key for live Shipping Bill filing |
| `RODTEP_RATE_PCT` | `0.5` | RoDTEP benefit rate (%) for declaration |
| `PORTLESS_ENTITY_NAME` | `PortLess Trade Solutions Pvt Ltd` | Exporter name on documents |
| `PORTLESS_ENTITY_GSTIN` | `""` | Exporter GSTIN |
| `PORTLESS_ENTITY_IEC` | `""` | Exporter IEC code |
| `PORTLESS_ENTITY_PAN` | `""` | Exporter PAN |

---

### Cross-Service Flow: Opportunity → Shipping Bill

```
Trade-Min detects opportunity
    │
    ▼ POST /deals (opportunity_id)
Deals service creates deal (DRAFT → MATCHING)
    │
    ▼ Match Engine scores suppliers
Deals → NEGOTIATING → NEGOTIATION_AGREED
    │
    ▼ POST /dealmaker/pi
DealMaker issues PI → deal: NEGOTIATION_AGREED → PI_SENT
    │
    ▼ POST /dealmaker/pi/{id}/accept
Buyer accepts PI → deal: PI_SENT → PI_ACCEPTED → ESCROW_PENDING
    │
    ▼ Escrow Guard receives payment confirmation
deal: ESCROW_PENDING → ESCROW_FUNDED
    │
    ▼ POST /dealmaker/po
DealMaker issues PO (margin guard passes) → deal: ESCROW_FUNDED → PO_ISSUED
    │
    ▼ Supplier prepares goods, QC passes
deal: PO_ISSUED → SUPPLIER_DISPATCH_READY → QC_PASSED → IN_TRANSIT
    │
    ▼ POST /docugen/documents (CI, PL, CoO, SDF)
DocuGen renders export document package
    │
    ▼ POST /docugen/customs/file
DocuGen files Shipping Bill → ICEGATE → LEO issued
    │
    ▼ deal: IN_TRANSIT → CLEARED → PAID
```

---

### Running Services Standalone

```bash
# DealMaker (port 8006)
uvicorn services.dealmaker.main:app --reload --port 8006

# DocuGen (port 8008)
uvicorn services.docugen.main:app --reload --port 8008

# Shield-Link (port 8007)
uvicorn services.shield_link.main:app --reload --port 8007

# Escrow-Guard (port 8009)
uvicorn services.escrow_guard.main:app --reload --port 8009

# HITL Approval (port 8010)
uvicorn services.hitl_approval.main:app --reload --port 8010

# Exception Resolver (port 8011)
uvicorn services.exception_resolver.main:app --reload --port 8011

# Email Gateway — IMAP fetch + SMTP send transport (port 8012)
uvicorn services.email_gateway.main:app --reload --port 8012

# Email Processor — inbound matching + persistence (port 8013)
uvicorn services.email_processor.main:app --reload --port 8013

# Combined monolith — all services on one port (recommended for local dev)
uvicorn app:app --reload --port 8081
```

### Running Tests

```bash
pytest tests/test_dealmaker.py -v
pytest tests/test_docugen.py -v
pytest tests/test_shield_link.py -v
pytest tests/test_escrow_guard.py -v
pytest tests/test_hitl_approval.py -v
pytest tests/test_exception_resolver.py -v

# Full suite (238+ tests)
pytest -q
```
