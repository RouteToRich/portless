# PortLess — Master Specification

> **Zero-Inventory Autonomous Merchant Exporter Platform**
> Unified specification combining business concept, high-level design, low-level design, workflows, and architecture.
>
> *Consolidates: `merchant_exporter_orchestrator_app.md`, `merchant_exporter_hld.md`, `portless_web_application_blueprint.md`.*

---

## Table of Contents
1. [Requirements](#1-requirements)
2. [Goals](#2-goals)
3. [High-Level Design (HLD)](#3-high-level-design-hld)
4. [Low-Level Design (LLD)](#4-low-level-design-lld)
5. [Workflow](#5-workflow)
6. [Architecture Diagram](#6-architecture-diagram)
7. [Flowchart](#7-flowchart)
8. [Appendices](#8-appendices)

---

## 1. Requirements

### 1.1 Problem Statement
Traditional merchant exporters face massive friction: sourcing reliable suppliers, validating international buyers, managing regulatory export documentation, checking cargo quality, and coordinating freight forwarders. PortLess is an **agentic orchestration platform** that runs this entire cycle with minimal human involvement using a **pure matching-and-service model** (zero inventory, distributed trust).

### 1.2 Functional Requirements
| # | Requirement | Description |
|---|---|---|
| FR-1 | **Opportunity Discovery** | Detect global demand surges per HS code and rank arbitrage opportunities (UN Comtrade, Volza, MacMap). |
| FR-2 | **Buyer/Supplier Matching** | Auto-match buyers and suppliers with a deterministic Composite Match Score (CMS). |
| FR-2a | **Autonomous Negotiation Agent** | After a match clears (CMS ≥ 75), the Negotiation Agent is triggered with match parameters (scores, costs, party details). It conducts intelligent multi-round negotiation with both buyer and seller via email/monitored inboxes/portal, leveraging RAG knowledge (trade rules, product specs, country factors) and real-time internet data. The agent persists negotiation flow in memory, extracts quotes from attachments, and escalates to human-in-the-loop for approval at key decision points. Intermediate cycles use local Python guards for margin verification. |
| FR-3 | **Credential Verification** | Verify supplier GST/IEC/MCA/Udyam and buyer VAT/credit before committing. |
| FR-4 | **Automated RFQ & Negotiation** | Dispatch RFQs and run quote/counter-quote rounds via IndiaMART/TradeIndia + Twilio/email. |
| FR-5 | **Document Generation** | Auto-generate PI, CI, Packing List, CoO, SDF, Shipping Bill. |
| FR-6 | **Escrow & Payments** | Hold buyer funds in multi-currency escrow; release payouts on validated milestones. |
| FR-7 | **Quality Control** | Trigger 3rd-party inspection (SGS/Bureau Veritas) and gate dispatch on a signed CRF. |
| FR-8 | **Logistics Booking** | Book freight (Cogoport/Freightos), generate Switch BoL, track milestones. |
| FR-9 | **Customs Clearance** | File Shipping Bill via ICEGATE/CHA; capture LEO + RoDTEP. |
| FR-10 | **HITL Approvals** | Typed approval requests with deep links, expiry, and safe timeout defaults. |
| FR-11 | **Exception Handling** | Auto-remediate or escalate stuck deals (QC fail, truck no-show, disputes). |
| FR-12 | **Real-time Ops Console** | Live view of every deal, agent action, and pending approval. |
| FR-13 | **Compliance & Tax** | LUT/IGST, FEMA/FIRC realization, RoDTEP harvesting, audit trail. |

### 1.3 Non-Functional Requirements
| Category | Requirement |
|---|---|
| **Reliability** | No double payout/booking (idempotency + outbox + sagas). Replayable event log. |
| **Autonomy** | Configurable autonomy levels (L0–L3) per action class. |
| **Security** | Tokenized buyer/supplier portals, signed webhooks, encrypted secrets, RBAC. |
| **Auditability** | Every agent decision (prompt, tools, output) and state transition logged. |
| **Scalability** | Event-driven decoupling; single operator runs dozens of concurrent deals. |
| **Compliance** | Compliance Guard blocks value/HS/origin falsification before customs filing. |
| **Observability** | OpenTelemetry tracing + LLM tracing (Langfuse) + DLQ runbooks. |

### 1.4 Personas
| Persona | Goal | Surface |
|---|---|---|
| **Operator** (you) | Supervise by exception, approve, override | Ops Console (web) + mobile push |
| **Supplier** (factory) | Receive RFQ, accept PO, mark ready-to-dispatch | Tokenized mini-portal |
| **Buyer** (importer) | Accept PI, fund escrow, track shipment | Tokenized mini-portal |
| **3rd-party services** | QC / freight / CHA / escrow callbacks | Webhook endpoints (M2M) |

---

## 2. Goals

### 2.1 Primary Goal
Run a profitable merchant-export business end-to-end (**opportunity → payout**) with **minimal manual intervention** — agents do the work, the operator supervises by exception.

### 2.2 Business Objectives
- **Zero Warehouse Overhead:** Goods move directly factory → port → buyer.
- **High Net Margin:** Capture 8%–15% net arbitrage per deal.
- **Freight Cost Reduction:** Real-time multi-platform spot-rate bidding.
- **Incentive Harvesting:** Auto-capture RoDTEP credits (0.5%–4.5% of FOB).
- **Information Protection:** Lawful merchant-of-record + Switch BoL prevents disintermediation.

### 2.3 Capital Preservation Guardrails
1. **Profit Margin Guardrail:** Freeze deal if net margin < 8%; system auto-generates a renegotiation ticket to the operator.
2. **Credit Guardrail:** No credit-term PO unless buyer is credit-insured (Euler Hermes/Allianz Trade); else 100% cash-in-escrow before sourcing. No factory PO can be sent under credit terms.
3. **Emergency Kill Switch:** A global WebSocket connection links the Ops Console to active LangGraph execution loops. Pressing "Stop All Deals" instantly halts transport orders, locks escrow releases, and pauses customs filings.
4. **Dual-Document Verification:** Azure Document Intelligence OCR cross-checks weight, description, and exporter details on the SGS Quality Report against the Carrier BoL and Commercial Invoice; >0.1% mismatch in cargo net weight or HS description suspends supplier payout and initiates manual dispute review.

### 2.4 Definition of "Minimal Manual Intervention"
After maturity, the operator's only recurring jobs:
1. Glance at the **Approvals Inbox** (mostly auto-clears at L1).
2. Resolve genuine **Exceptions**.
3. Tune **autonomy + guardrail thresholds**.

---

## 3. High-Level Design (HLD)

### 3.1 Core Concept
- **Zero Inventory:** The platform never holds goods.
- **Distributed Trust:** Independent 3rd parties handle QC, transport, customs (CHA), and escrow.
- **Minimal Human Involvement:** AI agents process leads, validate credentials, generate documents, and book logistics.

### 3.2 System Context
```mermaid
flowchart LR
    SUP["Indian Supplier<br/>(Manufacturer)"] -->|RFQ / PO| CORE
    BUY["Overseas Buyer<br/>(Importer)"] -->|PI / Escrow| CORE
    CORE["PortLess<br/>AI Agent Core Platform"] --> SHIELDS
    subgraph SHIELDS["Third-Party Shields"]
        QC["QC: SGS / Intertek"]
        FR["Freight: Freightos / Cogoport"]
        DOC["Docs: DGFT / ICEGATE"]
    end
    SHIELDS --> SUP
    SHIELDS --> BUY
```

### 3.3 Orchestration Architecture (Agents + Services)

The platform uses a **hybrid architecture**: AI agents handle tasks requiring reasoning/language/ambiguity, while deterministic services handle exact, auditable operations (money, compliance, documents). Both are coordinated via the event bus.

```mermaid
flowchart TD
    BUS["Event Bus (NATS / Kafka)"]

    subgraph AGENTS["AI Agents (LLM + RAG + Tools)"]
        NA["Negotiation Agent<br/>Multi-round buyer/seller negotiation<br/>(RAG, internet, inbox, memory, HITL)"]
        ER["Exception-Resolver Agent<br/>Creative remediation for novel failures"]
    end

    subgraph SERVICES["Deterministic Services (Pure Logic)"]
        TM["Trade-Min<br/>Opportunity discovery & ranking"]
        ME["Match Engine<br/>CMS scoring (pure math)"]
        SO["Sourcing<br/>RFQ dispatch & quote intake"]
        DM["DealMaker<br/>PI/PO generation"]
        SL["Shield-Link<br/>QC, freight, CHA booking"]
        DG["DocuGen<br/>Document generation & customs filing"]
        EG["Escrow-Guard<br/>Payment holds & milestone releases"]
        HITL["HITL Approval<br/>Autonomy-gated operator decisions"]
    end

    BUS --> AGENTS
    BUS --> SERVICES
    AGENTS --> BUS
    SERVICES --> BUS
```

| Category | Modules | Why This Category |
|---|---|---|
| **AI Agents** | Negotiation Agent, Exception-Resolver | Require LLM reasoning, natural language, strategy, ambiguity handling |
| **Deterministic Services** | Trade-Min, Match Engine, Sourcing, DealMaker, Shield-Link, DocuGen, Escrow-Guard, HITL Approval | Execute exact logic — math, API calls, templates, state transitions. LLM adds risk, not value. |

> **Key principle:** Agents *propose*, services *validate and execute*. No LLM can bypass a deterministic guard (margin check, escrow precondition, dual-doc match).

### 3.4 Implementation Phases
```mermaid
flowchart LR
    P1["PHASE 1: ASSISTED<br/>(Weeks 1-4)<br/>• API integrations<br/>• Auto doc gen<br/>• Manual approvals"]
    P2["PHASE 2: SEMI-AUTO<br/>(Weeks 5-8)<br/>• LangGraph orchestrator<br/>• Escrow locked<br/>• Auto freight webhook"]
    P3["PHASE 3: FULL AUTO<br/>(Weeks 9-12)<br/>• Zero-human matching<br/>• Dynamic bidding<br/>• Autonomous exceptions"]
    P1 --> P2 --> P3
```

| Phase | Focus | Key Rule |
|---|---|---|
| **1 — Assisted** | Core pipelines + manual compliance | Every doc/booking needs one-click operator approval. |
| **2 — Semi-Auto** | Inter-agent orchestration + financial security | App runs pipeline but halts for approval before payout & freight booking. |
| **3 — Full Auto** | Full automation + dynamic profit harvesting | Autonomous exception resolution unless margin impact > 5%. |

**Phase Details:**
- **Phase 1:** Establish secure API connections to ICEGATE, DGFT, IndiaMART, and Cogoport sandbox environments. Deploy DocuGen for auto-drafting invoices, packing lists, and CoO. Run real-world sandbox test cycles.
- **Phase 2:** Initialize LangGraph orchestrator. Integrate Stripe/Payoneer escrow (block dispatch until buyer funds secured). Deploy Negotiation Agent (RAG + internet + inbox monitoring + memory + HITL). Activate Compliance Guard (merchant-of-record + Switch BoL).
- **Phase 3:** Launch Automated Freight Bidding Engine for real-time container rate negotiation. Integrate automated ICEGATE Shipping Bill submission with RoDTEP harvesting. Implement Autonomous Exception Resolution (e.g., if a truck fails pickup, system auto-rebooks via alternative carrier without operator notification unless margin impact > 5%).

### 3.5 Third-Party Integration Framework
| Stage | Provider Examples | Integration | Agent Action |
|---|---|---|---|
| **QC Check** | SGS, Bureau Veritas, Intertek | SGS Co-care API | Book inspection on "Ready for Dispatch"; hold payment until signed CRF. |
| **Logistics** | Freightos, Cogoport, Flexport | Freightos / Cogoport API | Fetch spot rates, auto-book container, generate tracking webhooks. |
| **Customs** | Registered CHAs | ICEGATE EDI / API | Submit docs, generate Shipping Bill + LEO, track clearance. |
| **Escrow** | Stripe Treasury, Payoneer | B2B APIs | Hold funds, release on validated milestones. |

### 3.6 Technical Stack (HLD-level)
| Layer | Technology | Rationale |
|---|---|---|
| Agent Framework | **LangGraph** | Stateful, cyclical multi-agent loops. |
| LLM Core | **Claude / GPT-class** (routed via LiteLLM) | Reasoning/negotiation: frontier model. Bulk extraction: smaller/cheaper model. |
| RAG | **pgvector** | Negotiation playbooks, trade rules, product specs, country factors, deal history. |
| Database | **PostgreSQL + pgvector** | Supplier coords, HS classifications, price history, embeddings. |
| Backend | **Python / FastAPI** | High-performance async, native AI integration. |
| Escrow/FinTech | **Stripe Treasury / Payoneer** | Secure global SWIFT/SEPA, multi-currency. |
| Doc Parsing | **Azure Document Intelligence** | High-accuracy OCR for lab tests & BoLs (not an LLM). |

### 3.7 Compliance & Anti-Bypass (Corrected)
> **Correction:** The earlier "double-invoice mask" wording implied unlawful concealment. Replaced with lawful practice.

- **Merchant-of-record model:** PortLess is the contractual exporter/importer-of-record; all invoices and POs are genuine.
- **Switch Bill of Lading:** Recognized carrier service; shipper changed to PortLess (buyer never sees factory identity).
- **Compliance Guard:** Blocks any action that under-declares value, falsifies HS/origin, or hides a sanctioned party — runs before customs filing.
- **Trust Lock (Escrow):** Pays supplier on CRF sign-off; secures buyer funds until delivery — neither party can safely bypass.

#### 3.7.1 Switch Bill of Lading Mechanism
The Bill of Lading is the primary title document in international shipping. Normally, it lists the original shipper (factory) and consignee (buyer).
- **First BoL (Set 1):** Lists the Indian Manufacturer as *Shipper* and **PortLess** as *Consignee*. Kept confidential by PortLess and the CHA.
- **Second BoL (Set 2 / Switched):** Lists **PortLess** as *Shipper* and the International Importer as *Consignee*. This is the document delivered to the buyer.
- **Result:** The overseas buyer never sees the name or factory address of the Indian supplier on maritime transit documents.

#### 3.7.2 Financial Shield (Trust Lock)
Why can’t buyer and seller bypass PortLess even if they suspect each other’s existence?
1. **Credit & Cash Flow Disconnect:** Small manufacturers cannot afford to wait 45 days for international credit payments — they want payment upon factory dispatch.
2. **Risk Aversion:** The overseas buyer will not wire 100% advance payment directly to an unverified factory’s bank account due to high scam risk.
3. **PortLess Solution:** PortLess pays the manufacturer immediately when the SGS inspector signs the CRF (mitigating supplier cashflow risk), while securing buyer funds in multi-currency escrow (mitigating buyer delivery risk). Bypassing PortLess forces both parties to assume 100% of trade and payment default risk.

#### 3.7.3 Merchant-of-Record Documentation (Compliance Guard)
The DocuGen Agent generates documentation under the merchant-of-record model:
- **Supplier-Facing Pack:** Local POs and GST Tax Invoices. All references to the overseas importer are replaced with PortLess’s local entity details.
- **Buyer-Facing Pack:** Export Commercial Invoices, Packing Lists, and CoO. Origin listed as “Port of Loading: Chennai, India”; manufacturer’s name replaced with PortLess’s export license branding.

#### 3.7.4 Platform Discovery Mitigation (Operational Moats)
Since PortLess gathers contacts from public directories (IndiaMART, Volza), four operational moats prevent disintermediation:
1. **High Subscription & Technical Barriers:** Overseas buyers are importers, not sourcing agents — navigating Indian wholesale portals is chaotic. Indian manufacturers (especially tier-2/3 factories) don’t pay for global manifest databases like Volza ($thousands/year) and lack international sales agents.
2. **The “Export Readiness” Chasm:** Over 90% of IndiaMART suppliers cannot handle international logistics, Shipping Bills, customs clearance, or container booking. A direct contact results in an Ex-Works offer the buyer cannot execute.
3. **Information Asymmetry (Blind Sourcing):** The Sourcing Agent instructs the manufacturer that cargo is for a “PortLess Domestic Consolidation Hub”. The Logistics Agent switches the shipping bills — buyer sees only “PortLess” as supplier.
4. **The Ultimate Trust Bridge:** Even if they identified each other, the buyer won’t wire $40K to an unverified rural factory, and the factory won’t ship without guaranteed domestic currency payment upon dispatch. PortLess’s escrow protocol is the glue.

### 3.8 India Regulatory & Tax Automation
- **GST Zero-Rated Supply (Sec 16 IGST):** Export of goods qualifies as Zero-Rated Supply under Section 16 IGST Act. Two paths:
  1. **LUT (Letter of Undertaking):** Export without paying IGST, then claim ITC. Highly recommended for zero-inventory model — PortLess auto-attaches LUT reference number (from GST profile) to all generated shipping invoices.
  2. **IGST Refund:** Pay tax at export time, claim refund later.
- **FEMA / FIRC:** Under FEMA, all export proceeds must be realized in India within 9 months from date of export. PortLess matches incoming bank wire files (via corporate bank API, e.g., ICICI) to corresponding shipping bills, flagging missing FIRCs automatically to keep the corporate account audit-clean.
- **RoDTEP (0.5%–4.5% of FOB):** DocuGen stamps RoDTEP declaration on Shipping Bills filed to ICEGATE for auto duty-credit scrips in the customs ledger.

---

## 4. Low-Level Design (LLD)

### 4.1 Runtime Tiers
| Tier | Role | Tech |
|---|---|---|
| **API Gateway** | Sync edge: auth, validation, webhook ingest, idempotency, command publishing | FastAPI, Pydantic v2, JWT |
| **Agent Runtime** | Long-running reasoning loops ("brain") | LangGraph, LiteLLM router |
| **Workers** | Deterministic integration + saga steps ("hands"); idempotent, retryable | Python consumers, httpx adapters |
| **Outbox Relay** | Publishes DB outbox → bus | Python relay |

### 4.2 Deal State Machine
```mermaid
stateDiagram-v2
    [*] --> DRAFT
    DRAFT --> MATCHING
    MATCHING --> NEGOTIATING
    NEGOTIATING --> NEGOTIATING: counter-round
    NEGOTIATING --> NEGOTIATION_AGREED
    NEGOTIATING --> NEGOTIATION_FAILED
    NEGOTIATION_FAILED --> MATCHING: re-source
    NEGOTIATION_FAILED --> CANCELLED
    NEGOTIATION_AGREED --> PI_SENT
    PI_SENT --> PI_ACCEPTED
    PI_ACCEPTED --> ESCROW_PENDING
    ESCROW_PENDING --> ESCROW_FUNDED
    ESCROW_FUNDED --> PO_ISSUED
    PO_ISSUED --> SUPPLIER_DISPATCH_READY
    SUPPLIER_DISPATCH_READY --> QC_PASSED
    SUPPLIER_DISPATCH_READY --> QC_FAILED
    QC_PASSED --> IN_TRANSIT
    IN_TRANSIT --> CLEARED
    CLEARED --> PAID
    PAID --> [*]
    QC_FAILED --> DISPUTE
    DISPUTE --> CANCELLED
    DISPUTE --> REFUNDED
    CANCELLED --> [*]
    REFUNDED --> [*]
```

**Guard conditions (enforced in code, never by LLM):**
- `NEGOTIATION_AGREED` requires the converged terms to satisfy `net_margin ≥ margin_floor` (the LLM proposes, the pure-function guard decides).
- `ESCROW_FUNDED` requires verified escrow webhook.
- `PO_ISSUED` requires `net_margin ≥ margin_floor` AND (buyer credit-cleared OR fully prepaid).
- Payout requires `QC_PASSED` + dual-doc match pass.

### 4.3 Composite Match Score (Matching Engine)
$$\text{CMS} = C_{\text{supplier}} \times \left( w_1 \cdot M_{\text{arbitrage}} + w_2 \cdot L_{\text{proximity}} + w_3 \cdot V_{\text{reliability}} \right)$$

| Symbol | Meaning |
|---|---|
| $C_{\text{supplier}}$ | Gatekeeper multiplier (0/1): 0 if GST fail / inactive IEC / DGFT denied list. |
| $M_{\text{arbitrage}}$ | Arbitrage score 0–100: spread between buyer target price and supplier quote. |
| $L_{\text{proximity}}$ | Logistics proximity 0–100: supplier warehouse → nearest exit port distance. |
| $V_{\text{reliability}}$ | Verification/capacity 0–100: business age (MCA), Udyam, shipment frequency. |
| $w_1,w_2,w_3$ | Weights, $\sum w_i = 1.0$ (default 0.5 / 0.3 / 0.2). |

**Profitability:**
$$\text{Net Margin} = \text{Buyer Price (CIF)} - (\text{Sourcing} + \text{QC} + \text{Logistics} + \text{Customs} + \text{Buffer})$$

Decision: CMS ≥ 75 → trigger Negotiation Agent (with match parameters + cost envelope); CMS < 75 → auto-discard.

**Execution Pipeline:**
```
  [UN Comtrade / Volza] ──► Global Demand Trend Spotted (HS code)
                                    │
                                    ▼
  [IndiaMART / TradeIndia] ─► Extract Supplier Candidates (Filter: Active GST/IEC)
                                    │
                                    ▼
  [PortLess Match Engine] ──► Run CMS Calculation for top 20 pairs
                                    │
             ┌──────────────────────┴──────────────────────┐
             ▼ (CMS < 75%)                                 ▼ (CMS >= 75%)
       [Auto-Discard]                              [Trigger Negotiation Agent]
                                                           │
                                                           ▼
                                                   Load RAG + Costs + Live Data
                                                           │
                                                           ▼
                                                   Multi-round Negotiation
                                                   (email/inbox/portal + HITL)
```

**Match State Example:**
```json
{
  "match_id": "MATCH-2026-SPICE-004",
  "hsn_code": "091030",
  "composite_score": 86.5,
  "factors": {
    "gatekeeper_passed": true,
    "arbitrage_score": 92.0,
    "logistics_proximity_score": 85.0,
    "reliability_score": 75.0
  },
  "candidate_buyer": {
    "buyer_id": "BUYER-EUR-441",
    "target_fob_price_usd": 4.20
  },
  "candidate_supplier": {
    "supplier_id": "SUPPLIER-IND-9012",
    "wholesale_quote_usd_equiv": 2.90,
    "inland_port_distance_km": 310
  },
  "cost_envelope": {
    "travel_cost_usd": 150.00,
    "qc_cost_usd": 450.00,
    "doc_cost_usd": 200.00
  }
}
```

### 4.3.1 Negotiation Agent (Buyer ⇄ Seller)

The **Negotiation Agent** is an intelligent, autonomous agent triggered directly after the Match Engine produces a qualifying match (CMS ≥ 75). The Match Engine passes key parameters — `trade_match_id`, `buyer_id`, `seller_id`, `product_code`, `buyer_country_code`, `date_of_expectation`, `date_of_packaging`, composite scores, and cost breakdown (travel cost, quality check cost, documentation cost) — which seed the agent's negotiation context.

The agent conducts bounded, auditable multi-round bargaining with **both buyer and seller** on behalf of the merchant exporter, using email, monitored inboxes, or the portal as communication channels.

#### Agent Capabilities

| Capability | Description |
|---|---|
| **RAG Knowledge** | Before negotiation starts, the agent is loaded with ingested knowledge: negotiation playbooks, product specifications, country-specific trade rules, HS-code regulations, quality standards, and historical deal patterns (pgvector). |
| **Real-time Internet Access** | The agent accesses live data — shipping rates, currency exchange rates, commodity price indices, geopolitical news, trade sanctions — to inform negotiation decisions contextually. |
| **Score & Profit Loading** | The agent is pre-loaded with the CMS score breakdown, estimated costs (travel + QC + documentation), and target/floor profit margins before any communication begins. |
| **Monitored Inboxes** | Supports configured merchant exporter email inbox (API key integrated); auto-reads incoming messages from buyer/seller, extracts quotes and counter-offers from email body and attachments (PDF, Excel). |
| **Negotiation Memory** | The entire negotiation flow (rounds, counter-offers, concessions, reasoning) is persisted in memory, enabling the agent to reference prior context, maintain consistency, and resume after interruptions. |
| **Human-in-the-Loop (HITL)** | The agent escalates to the operator for approval at key decision points: marginal margins, first-time parties, large concessions, round exhaustion, or any decision outside autonomy bounds. |
| **Multi-channel Communication** | Sends and receives counter-offers via Twilio (SMS/WhatsApp), email (SMTP/IMAP), or the tokenized buyer/supplier portal. |

#### Input Parameters (from Match Engine)
| Parameter | Source | Purpose |
|---|---|---|
| `trade_match_id` | Match Engine | Links negotiation to the scored match |
| `buyer_id` / `seller_id` | Match Engine | Identifies the negotiating parties |
| `product_code` (HS code) | Match Engine | Loads product-specific RAG knowledge |
| `buyer_country_code` | Match Engine | Loads country rules, restrictions, duties |
| `composite_match_score` | Match Engine | Informs agent confidence and strategy |
| `buyer_target_price` | Match Engine / Buyer | Starting bid reference |
| `supplier_quote_price` | Match Engine / Sourcing | Starting ask reference |
| `travel_cost` | Match Engine | Factored into margin calculation |
| `qc_cost` | Match Engine | Factored into margin calculation |
| `doc_cost` | Match Engine | Factored into margin calculation |
| `date_of_expectation` | Buyer | Deadline pressure for negotiation |
| `date_of_packaging` | Supplier | Feasibility constraint |

> 💡 **Match Engine & Guardrail Interaction Pattern:**
> 1. **Intermediate Negotiation Cycles:** The Negotiation Agent **does not** call the Match Engine on each round. Static scores (port proximity $L$, reliability $V$) and costs (travel, QC, docs) are cached at initialization. The agent relies on local Python guardrails (`verify_payout_and_margin()`) to verify dynamic profit margins in real-time.
> 2. **Post-Negotiation Sign-Off:** Once terms converge (`negotiation.agreed`), the system invokes the Match Engine **one final time** to compute and persist the final **Realized CMS** against the finalized pricing, logging the result for analytics.

#### Negotiation Policy (per deal)
| Parameter | Meaning |
|---|---|
| `target_margin_pct` | Desired net margin the agent aims for (considers travel + QC + doc costs). |
| `margin_floor_pct` | Hard walk-away threshold (deal frozen below this). |
| `max_rounds` | Max counter-offer rounds before escalation (default 5). |
| `concession_step` | Max price movement allowed per round (anti-overshoot). |
| `batna` | Best Alternative — the next-best matched supplier/buyer to fall back on. |
| `deadline` | Time box; on expiry → escalate or fail per autonomy level. |
| `rag_context` | Pre-loaded negotiation knowledge for product/country/factors. |
| `cost_envelope` | Total non-negotiable costs (travel + QC + docs) from Match Engine. |

**Convergence rule (pure function, outside the LLM):** an offer/counter pair is accepted only if
$$\text{net\_margin}(P_{\text{buyer}}, P_{\text{supplier}}) \ge \text{margin\_floor} \quad\text{and}\quad |P_{\text{ask}} - P_{\text{bid}}| \le \text{concession\_step}.$$

**Loop outcomes:** `NEGOTIATION_AGREED` (terms locked → PI), `NEGOTIATION_FAILED` (re-source via BATNA or cancel), or `approval.requested` (margin marginal / first-time party / rounds exhausted).

```mermaid
flowchart TD
    START(["match.scored (CMS ≥ 75)"]) --> LOAD["Load RAG context<br/>(product/country/rules/playbooks)"]
    LOAD --> COSTS["Load cost envelope<br/>(travel + QC + docs from Match Engine)"]
    COSTS --> LIVE["Fetch live data<br/>(shipping rates, FX, news)"]
    LIVE --> OPEN["negotiation.started<br/>seed: buyer target vs supplier quote"]
    OPEN --> ROUND["Agent proposes price/terms<br/>(informed by RAG + live data + memory)"]
    ROUND --> SEND["Send counter via email / inbox / portal"]
    SEND --> INBOX["Monitor inbox for response<br/>(extract quotes from attachments)"]
    INBOX --> RESP{"Counter-party<br/>response?"}
    RESP -->|Accept| GUARD{"margin ≥ floor?<br/>(incl. travel+QC+doc costs)"}
    RESP -->|Counter| LIMIT{"round < max_rounds<br/>& step ≤ concession_step?"}
    RESP -->|No reply / timeout| TIME{"deadline hit?"}
    LIMIT -->|Yes| PERSIST["Persist round in memory"] --> ROUND
    LIMIT -->|No| BATNA["Switch to BATNA<br/>(next supplier/buyer)"]
    TIME -->|Yes| BATNA
    TIME -->|No| SEND
    GUARD -->|Yes| OK["negotiation.agreed → PI_SENT"]
    GUARD -->|No / marginal| HITL["approval.requested (HITL)"]
    BATNA --> RESRC{"BATNA available?"}
    RESRC -->|Yes| ROUND
    RESRC -->|No| FAIL["negotiation.failed → re-source / cancel"]
    HITL --> OK
    HITL --> FAIL
```

### 4.4 Event-Driven Backbone
**Bus:** NATS JetStream (dev) → Kafka/Redpanda (scale).

**Core Event Catalog:**
| Topic | Producer | Key Consumers |
|---|---|---|
| `opportunity.detected` | Trade-Min Agent | UI, Operator |
| `deal.created` | API | Agent Runtime |
| `match.scored` | Match Engine | Agent Runtime, UI |
| `quote.requested` / `quote.received` | Sourcing Agent | Worker (IndiaMART), Negotiation Agent |
| `negotiation.started` | Negotiation Agent | UI, Operator |
| `negotiation.round` | Negotiation Agent | UI (live bargaining log), Memory store |
| `negotiation.agreed` / `negotiation.failed` | Negotiation Agent | DealMaker, UI |
| `pi.sent` / `pi.accepted` | DealMaker / Buyer portal | Escrow Worker |
| `escrow.funded` | Stripe webhook → API | Saga |
| `po.issued` | DealMaker Agent | Supplier portal |
| `qc.booked` / `qc.passed` / `qc.failed` | Shield-Link / SGS webhook | Saga, DocuGen |
| `freight.booked` / `freight.milestone` | Logistics Worker / carrier | UI |
| `customs.filed` / `customs.cleared` | DocuGen / ICEGATE | Payout Saga |
| `payout.requested` / `payout.completed` | Payout Saga | Ledger, UI |
| `approval.requested` / `approval.resolved` | Any agent | HITL Service, UI |
| `deal.exception` | Any | Exception workflow |

**Reliability patterns:**
- **Transactional Outbox** — atomic DB write + publish; no lost events.
- **Idempotency keys** — dedupe commands/webhooks; no double payouts.
- **Saga orchestration** — compensating actions on failure (e.g., freight fail → `po.cancel`).
- **Dead-letter queue** — poison messages surface as UI Exceptions.

### 4.5 Agent Runtime (Planner / Executor / Critic)
```mermaid
flowchart TD
    PLAN["Planner"] -->|plan| EXEC["Executor"]
    EXEC -->|tool call| TOOLS["Tools / Connectors"]
    TOOLS -->|result| GUARD["Guardrail<br/>(schema + policy + autonomy)"]
    GUARD -->|ok| EMIT["Emit domain event"]
    GUARD -->|fail / risk| CRITIC["Critic"]
    CRITIC -->|revise| PLAN
    CRITIC -->|needs human| APR["approval.requested"]
```

**Agent roster (as services):**
| Agent | Trigger | Tools | Output events |
|---|---|---|---|
| Trade-Min | cron / `scan.tick` | Comtrade, Volza, MacMap | `opportunity.detected` |
| Match Engine | `deal.created` | GSTN, DGFT, MCA, geo | `match.scored` |
| Sourcing | `match.scored(cms≥75)` | IndiaMART, TradeIndia, Twilio | `quote.requested/received` |
| Negotiation Agent | `match.scored(cms≥75)` | RAG (pgvector: trade playbooks, product specs, country rules), Internet (shipping rates, FX, news APIs), Monitored inbox (IMAP/API), Pricing calc, BATNA model, Negotiation memory, Twilio/email/portal, HITL escalation | `negotiation.started/round/agreed/failed` |
| DealMaker | `negotiation.agreed` | Pricing calc, PI/PO gen | `pi.sent`, `po.issued` |
| Shield-Link | `escrow.funded` | SGS, Cogoport/Freightos, CHA | `qc.booked`, `freight.booked` |
| DocuGen | `qc.passed` | Doc templates, DGFT CoO, ICEGATE | `customs.filed` |
| Escrow-Guard | `pi.accepted`, `qc.passed` | Stripe/Payoneer, Ledger | `payout.requested` |
| Exception-Resolver | `deal.exception` | All tools + HITL | remediation events |

**Hard guardrails (deterministic, outside LLM):** schema-validated output; money/booking tools require state precondition + policy check + autonomy-level check; margin floor / credit check / dual-doc match are pure functions an LLM cannot override.

### 4.6 HITL Approval Service
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

**Autonomy Levels (per action class, configurable):**
| Level | Meaning | Default mapping |
|---|---|---|
| **L0** | Always ask | First-time supplier, payout > $50k |
| **L1** | Ask, auto-approve on timeout | Freight booking within quote |
| **L2** | Auto, notify after | Doc generation, RFQ dispatch |
| **L3** | Fully autonomous, log only | Opportunity scanning, QC booking |

### 4.7 Data Model (PostgreSQL)
```sql
deals(deal_id PK, operator_id, hs_code, status, autonomy_profile,
      margin_floor_pct, est_margin_usd, realized_margin_usd, created_at, updated_at)

parties(party_id PK, deal_id FK, role ENUM('buyer','supplier'),
        legal_name, country, gstin, iec, vat_number,
        verification_status, credit_status, risk_score)

quotes(quote_id PK, deal_id FK, supplier_id, unit_price, currency, valid_until)

match_scores(match_id PK, deal_id FK, composite_score, gatekeeper_passed BOOL,
             arbitrage_score, proximity_score, reliability_score)

negotiation_sessions(session_id PK, deal_id FK, trade_match_id FK,
                     buyer_id FK, seller_id FK, product_code, buyer_country_code,
                     status ENUM('active','agreed','failed','escalated'),
                     cost_envelope JSONB, rag_context_ref TEXT,
                     current_round INT, max_rounds INT,
                     target_margin_pct DECIMAL, margin_floor_pct DECIMAL,
                     concession_step DECIMAL, batna JSONB,
                     created_at, updated_at)

negotiation_rounds(round_id PK, session_id FK, round_number INT,
                   direction ENUM('to_buyer','to_seller','from_buyer','from_seller'),
                   channel ENUM('email','portal','sms','whatsapp'),
                   offer_price DECIMAL, currency, terms JSONB,
                   attachment_refs JSONB, reasoning TEXT,
                   margin_at_offer DECIMAL, hitl_required BOOL,
                   created_at)

escrow(escrow_id PK, deal_id FK, provider, amount, currency, status, ext_ref)

third_party_orders(tpo_id PK, deal_id FK, type ENUM('qc','freight','cha'),
                   provider, ext_ref, status, crf_hash, eta, payload JSONB)

documents(doc_id PK, deal_id FK, type, storage_uri, hash, generated_by, signed BOOL)

ledger(entry_id PK, deal_id FK, payee, amount, currency, direction, status,
       idempotency_key UNIQUE)

approvals(approval_id PK, deal_id FK, action, impact_usd, status, expires_at,
          default_on_timeout, resolved_by, resolved_at)

events(event_id PK, deal_id FK, topic, payload JSONB, occurred_at, seq)
outbox(id PK, topic, payload JSONB, published BOOL, created_at)
embeddings(id PK, owner_type, owner_id, embedding VECTOR(1536), metadata JSONB)
agent_runs(run_id PK, deal_id FK, agent, prompt, tools_called JSONB,
           output JSONB, cost_usd, created_at)
```
Indexes: `deals(status)`, `events(deal_id, seq)`, `ledger(idempotency_key)`, `embeddings USING ivfflat`.

### 4.8 Deal Context (shared state object)
```json
{
  "deal_id": "DEAL-2026-IN-ES-0091",
  "operator_id": "OP-901",
  "hs_code": "090422",
  "deal_status": "QC_IN_PROGRESS",
  "arbitrage_metrics": {
    "estimated_revenue_usd": 45000.00,
    "supplier_payable_inr": 2940000.00,
    "logistics_payable_usd": 3200.00,
    "qc_payable_usd": 450.00,
    "net_margin_usd": 6350.00
  },
  "parties": {
    "buyer": { "company_name": "Iberia Spices SL", "country": "Spain", "verified_vat_number": "ESB12345678" },
    "supplier": { "company_name": "Guntur Agri Products Ltd", "gstin": "37AAAAA0000A1Z1", "iec": "0312045612" }
  },
  "third_party_orders": {
    "quality_check": { "provider": "SGS India", "inspection_status": "SCHEDULED", "certificate_hash_ipfs": null },
    "freight_booking": { "provider": "Freightos", "incoterm": "FOB-Chennai", "carrier": "Maersk Line" },
    "customs_house_agent": { "shipping_bill_number": null, "customs_status": "PENDING_INSPECTION_CRF" }
  }
}
```

### 4.9 Tech Stack & Repo Layout
| Layer | Technology |
|---|---|
| Frontend | Next.js 14, TypeScript, Tailwind, shadcn/ui, TanStack Query, Socket.IO |
| API Gateway | FastAPI (async), Pydantic v2, JWT, Webhook router |
| Agent Runtime | LangGraph, LiteLLM model router, Pydantic tool schemas |
| Workers | Python consumers, httpx adapters |
| Event Bus | NATS JetStream → Kafka/Redpanda |
| Database | PostgreSQL 16 + pgvector |
| Cache/locks | Redis (idempotency, rate-limit, agent memory) |
| Object store | S3-compatible (MinIO in dev) |
| Payments | Stripe Treasury / Payoneer B2B |
| OCR | Azure Document Intelligence |
| Observability | OpenTelemetry + Grafana/Tempo + Langfuse |
| Deploy | Docker → Azure Container Apps / AWS ECS; IaC via Bicep/Terraform |

```
portless/
├─ apps/
│  ├─ web/                 # Next.js Ops Console + mini-portals
│  └─ api/                 # FastAPI gateway (REST + webhooks + WS)
├─ agents/                 # AI agent modules (LLM + RAG + tools + memory)
│  └─ negotiation_agent/   # intelligent multi-round buyer/seller negotiation
├─ services/               # deterministic service modules
│  ├─ trade_min/           # opportunity discovery & ranking
│  ├─ match_engine/        # composite match scoring
│  ├─ sourcing/            # RFQ dispatch & quote intake
│  ├─ dealmaker/           # PI / PO generation
│  ├─ shield_link/         # QC / freight / CHA booking
│  ├─ docugen/             # document generation + customs filing
│  ├─ escrow_guard/        # escrow holds & milestone payouts
│  ├─ hitl_approval/       # human-in-the-loop approval service
│  └─ exception_resolver/  # exception classification & remediation
├─ shared/                 # cross-cutting: domain, clients, bus, connectors
├─ packages/
│  ├─ domain/              # shared types, state machine, event schemas
│  └─ connectors/          # adapters: comtrade, gstn, dgft, sgs, cogoport...
├─ infra/                  # docker-compose, bicep/terraform
└─ docs/                   # specs
```

---

## 5. Workflow

### 5.1 End-to-End Transact Cycle (4 Phases)
```mermaid
flowchart TD
    subgraph A["Phase 1: Arbitrage & Matching"]
        A1["UN Comtrade & Volza detect demand<br/>(HS 090422 in Spain)"]
        A2["Query IndiaMART, filter Guntur<br/>exporters with active GST/IEC"]
        A3["Negotiation Agent runs intelligent rounds<br/>(RAG + live data + inbox monitoring)<br/>buyer target ⇄ supplier quote"]
        A4["Terms converge → buyer accepts PI"]
        A1 --> A2 --> A3 --> A4
    end
    subgraph B["Phase 2: Escrow & Financial Lock"]
        B1["Buyer deposits to multi-currency escrow"]
        B2["DealMaker issues PO,<br/>locks trade margin"]
        B1 --> B2
    end
    subgraph C["Phase 3: Shielded Execution"]
        C1["Supplier packs → SGS inspects & signs CRF"]
        C2["Shield-Link books container,<br/>truck picks up cargo"]
        C3["DocuGen compiles CI, PL, CoO,<br/>files ICEGATE Shipping Bill"]
        C1 --> C2 --> C3
    end
    subgraph D["Phase 4: Clearance & Payout"]
        D1["Cargo clears port → BoL issued"]
        D2["Auto payout: supplier, logistics,<br/>QC, customs, operator margin"]
        D1 --> D2
    end
    A --> B --> C --> D
```

### 5.2 Sourcing & Validation Loop
```mermaid
flowchart LR
    S1["Surge Detected<br/>(UN Comtrade)"] --> S2["Query GSTN / IndiaMART"]
    S2 --> S3["Match Trade Capacity<br/>+ verify IEC via DGFT"]
    S3 --> S4["Auto RFQ issued<br/>to local sellers"]
```

### 5.2.1 Negotiation Phase (after match)
```mermaid
flowchart LR
    N1["match.scored<br/>(CMS ≥ 75)"] --> N2["Negotiation Agent seeds offer<br/>(loads RAG + costs, buyer target vs supplier quote)"]
    N2 --> N3["Counter-offer rounds<br/>(≤ max_rounds)"]
    N3 --> N4{"Converged within<br/>margin floor?"}
    N4 -->|Yes| N5["negotiation.agreed → PI"]
    N4 -->|Exhausted / marginal| N6["Escalate (HITL)"]
    N4 -->|Walk away| N7["BATNA → re-source / cancel"]
```

### 5.3 Exception & Dispute Sub-Workflow
```mermaid
flowchart TD
    EX["deal.exception"] --> CL["Classify (agent)"]
    CL --> PLAN["Build remediation plan"]
    PLAN --> GATE{"Autonomy gate"}
    GATE -->|"Auto-fixable (L2/L3)"| AUTO["Execute compensating saga<br/>(rebook truck / re-quote)"]
    GATE -->|"Needs human (L0/L1)"| HUM["approval.requested"]
    AUTO --> RES{"Resolved?"}
    HUM --> RES
    RES -->|Yes| DONE["Resume deal"]
    RES -->|No| ESC["Escalate to operator"]
```

### 5.4 Build Roadmap
| Sprint | Deliverable | HLD Phase |
|---|---|---|
| S1 | Monorepo, docker stack, state machine, schema, event bus, outbox | 1 |
| S2 | Trade-Min + Match Engine, Opportunity Radar UI, GSTN/DGFT connectors | 1 |
| S3 | DealMaker + DocuGen, PI/PO/Invoice gen, mini-portals | 1 |
| S4 | Escrow (sandbox), HITL Approval Service, Autonomy Levels | 2 |
| S5 | Shield-Link (QC + freight), sagas, dual-doc verification | 2 |
| S6 | ICEGATE/CHA filing, RoDTEP/FIRC tracking, Compliance Guard | 2→3 |
| S7 | Exception-Resolver, freight bidding, autonomy promotion, observability | 3 |
| S8 | Hardening: load test, DLQ runbooks, security review, go-live | 3 |

> **Go-live safety rule:** launch money-movement actions at **L0** (always ask); promote to L2/L3 only after N successful supervised deals.

---

## 6. Architecture Diagram

### 6.1 System Topology
```mermaid
flowchart TB
    subgraph UI["UI (Next.js)"]
        OPS["Ops Console · Approvals"]
        PORT["Buyer/Supplier Mini-Portals"]
    end
    subgraph GW["API Gateway (FastAPI)"]
        AUTH["AuthZ · Validation · Idempotency · Webhooks"]
    end
    BUS["EVENT BUS<br/>(NATS JetStream / Kafka)"]
    DB[("PostgreSQL + pgvector<br/>source of truth")]
    subgraph RT["Runtime Services"]
        AGENT["Agent Runtime<br/>(LangGraph)"]
        WORK["Workers<br/>(integrations + sagas)"]
        RELAY["Outbox Relay"]
    end
    subgraph EXT["External Connectors (Adapters)"]
        E1["Comtrade · Volza · IndiaMART"]
        E2["GSTN · DGFT · MCA"]
        E3["SGS · Cogoport/Freightos · ICEGATE"]
        E4["Stripe · Payoneer · Azure DI"]
    end

    UI <-->|REST / tRPC| GW
    UI <-->|WS / SSE live| GW
    GW -->|publish / consume| BUS
    GW <-->|read / write| DB
    BUS --> AGENT
    BUS --> WORK
    AGENT -->|tool calls| EXT
    WORK -->|external APIs| EXT
    WORK --> DB
    DB --> RELAY
    RELAY --> BUS
```

### 6.2 Tier Responsibilities
| Tier | Responsibility |
|---|---|
| **UI** | Operator supervision, approvals, tokenized party actions, live stream. |
| **API Gateway** | Sync edge, webhook ingest, idempotency, command publishing. |
| **Event Bus** | Decouples all tiers; replayable, resilient. |
| **Agent Runtime** | Reasoning over messy data (matching, parsing, negotiation, exceptions). |
| **Workers** | Deterministic money/booking sagas; idempotent + retryable. |
| **Database** | Source of truth; events derive via outbox. |

### 6.3 UI Layer Specification

#### Stack
| Concern | Choice | Rationale |
|---|---|---|
| Framework | Next.js 14 (App Router) + React + TypeScript | SSR for fast dashboards, API routes for BFF |
| UI kit | Tailwind CSS + shadcn/ui | Fast, consistent, accessible components |
| State/data | TanStack Query + Zustand | Server cache + light client state |
| Realtime | WebSocket (Socket.IO) or SSE | Live deal + agent activity stream |
| Charts | Recharts | Margin / pipeline visualization |
| Auth | Auth.js (NextAuth) + passkeys/TOTP | Operator login; tokenized links for buyer/supplier |

#### Key Screens
1. **Opportunity Radar** — Ranked HS-code arbitrage cards (demand surge, est. margin, confidence). One-click "Start Deal".
2. **Deal Board (Kanban)** — Columns = state-machine stages (`MATCHING → NEGOTIATING → PI_SENT → ESCROW_FUNDED → QC → IN_TRANSIT → CLEARED → PAID`). Each card streams live agent actions.
3. **Deal Detail** — Timeline of events, documents, party info, agent reasoning log, and the **Approve / Reject / Override** action bar.
4. **Approvals Inbox** — Every pending HITL decision with deadline countdown and exact financial impact.
5. **Money & Margin** — Escrow balances, scheduled payouts, realized vs. projected margin, FIRC/RoDTEP tracking.
6. **Exceptions** — Disputes, failed QC, stuck shipments, with suggested agent remediation.
7. **Autonomy Settings** — Per-action-class autonomy level (L0–L3) and guardrail thresholds.
8. **Kill Switch** — Global "Pause All Deals" with confirmation (WebSocket → LangGraph execution halt).

#### Mini-Portals (tokenized, no account)
- Buyer/supplier receive a **signed, expiring magic-link** (JWT, single-deal scope).
- Actions are intentionally tiny: *Accept Quote*, *Upload Doc*, *Mark Ready*, *Fund Escrow*.
- Every action emits a domain event into the bus.

### 6.4 Local Development

```bash
# 1. Start infra: Postgres+pgvector, NATS, Redis, MinIO
docker compose -f infra/docker-compose.dev.yml up -d

# 2. Migrate + seed sandbox data (fake suppliers/buyers, sandbox API keys)
make migrate && make seed

# 3. Start services (each in its own process)
make api          # FastAPI on :8000
make agents       # LangGraph runtime (includes negotiation_agent)
make workers      # saga/integration consumers
make web          # Next.js on :3000

# 4. SANDBOX MODE = all external calls hit local mock connectors,
#    so you can run a full deal cycle end-to-end with no real money.
```

A **Sandbox Mode** flag swaps every external connector for a deterministic mock, enabling a full `opportunity → negotiation → payout` cycle on a laptop.

---

## 7. Flowchart

### 7.1 Happy-Path Deal (Sequence)
```mermaid
sequenceDiagram
    participant TM as Trade-Min
    participant OP as Operator
    participant ME as Match Engine
    participant SO as Sourcing
    participant NG as Negotiation Agent
    participant DM as DealMaker
    participant BU as Buyer
    participant EG as Escrow-Guard
    participant SL as Shield-Link
    participant SGS as SGS (QC)
    participant DG as DocuGen
    participant IC as ICEGATE

    TM->>OP: opportunity.detected
    OP->>ME: deal.created
    ME->>SO: match.scored (86)
    SO->>NG: quote.received
    Note over NG: Load RAG context + costs + live data
    NG->>NG: negotiation.round (counter-offers via email/inbox)
    Note over NG: Persist each round in memory
    NG->>NG: HITL approval at key decisions
    NG->>DM: negotiation.agreed (terms within margin floor)
    DM->>BU: pi.sent
    BU->>EG: escrow.funded
    EG-->>DM: guard: margin ok
    DM->>SL: po.issued
    SL->>SGS: qc.booked
    SGS-->>EG: qc.passed (crf_hash)
    EG->>OP: approval.requested (payout, L1)
    OP-->>EG: auto-approve
    EG->>SL: payout.completed (supplier)
    SL->>DG: freight.booked
    DG->>IC: customs.filed
    IC-->>DG: customs.cleared
    DG->>OP: payout.completed (3rd parties + margin) → PAID
```

### 7.2 Decision Flowchart (Deal Gatekeeping)
```mermaid
flowchart TD
    START(["Opportunity detected"]) --> CMS{"CMS ≥ 75?"}
    CMS -->|No| DISCARD["Auto-discard"]
    CMS -->|Yes| VERIFY{"Supplier GST/IEC<br/>verified?"}
    VERIFY -->|No| DISCARD
    VERIFY -->|Yes| RFQ["Issue RFQ"]
    RFQ --> NEG["Negotiate (bounded rounds)"]
    NEG --> CONV{"Terms converge<br/>≥ margin floor?"}
    CONV -->|No → BATNA| DISCARD
    CONV -->|Marginal| ESCAL["Escalate to operator"]
    ESCAL --> PI
    CONV -->|Yes| PI["Send PI to buyer"]
    PI --> ESC{"Escrow funded?"}
    ESC -->|No| HOLD["Hold / remind buyer"]
    ESC -->|Yes| MARGIN{"Net margin ≥ floor<br/>AND credit OK?"}
    MARGIN -->|No| FREEZE["Freeze + renegotiation ticket"]
    MARGIN -->|Yes| PO["Issue PO"]
    PO --> QC{"QC passed?"}
    QC -->|No| DISPUTE["Lock escrow → Dispute workflow"]
    QC -->|Yes| DUAL{"Dual-doc match<br/>(< 0.1% mismatch)?"}
    DUAL -->|No| DISPUTE
    DUAL -->|Yes| PAY["Release payouts"]
    PAY --> SHIP["Book freight + file customs"]
    SHIP --> CLEAR{"Customs cleared?"}
    CLEAR -->|Yes| DONE(["Deal PAID + margin captured"])
    CLEAR -->|No| EXC["Exception-Resolver"]
```

---

## 8. Appendices

### 8.1 Trade Intelligence API Landscape

#### A. Global Opportunity Discovery (Trade-Min Agent)
| Provider | Portal / API | Intelligence | Agent Usage |
|---|---|---|---|
| **UN Comtrade** | `comtradeplus.un.org` / `comtradeapi.un.org` | Global trade volumes, HS code flow, YoY demand growth | Detects surging import demands (e.g., “Turmeric import in Poland grew 40%”) |
| **ITC MacMap** | `macmap.org` / `macmap.org/api` | Import tariffs, trade barriers, rules of origin per country | Calculates target country tariffs: CIF Cost + Tariff competitiveness |
| **Volza** | `volza.com` / Proprietary API | BoL and customs manifests for 80+ countries | Extracts active buyer names importing specific HS codes from India |
| **Trademo** | `trademo.com` / `docs.trademo.com` | Supply chain records, buyer risk profiling, trade frequencies | Identifies high-volume Western buyers seeking stable supply partners |
| **ImportGenius / Panjiva** | Enterprise REST APIs | US Customs and Latin American marine shipping manifests | Spotlights high-volume importers who repeatedly buy bulk categories |

#### B. Buyer Verification
| Provider | Portal / API | Intelligence | Agent Usage |
|---|---|---|---|
| **VIES (EU Commission)** | `ec.europa.eu/taxation_customs/vies` / REST/SOAP | European buyer VAT validation, company registration, address | Free instant validation — prevents onboarding fake/unregistered EU entities |
| **Allianz Trade / Euler Hermes** | `allianz-trade.com` / Credit APIs | Corporate risk scores, payment defaults, credit underwriting limits | Auto-underwritten trade credit insurance; flags if 100% advance escrow mandatory |

#### C. Domestic Supplier Discovery (Sourcing Agent)
| Provider | Portal / API | Intelligence | Agent Usage |
|---|---|---|---|
| **IndiaMART** | `indiamart.com` / `partner.indiamart.com` | Indian manufacturers, wholesale pricing, factory addresses | Automates RFQ dispatch to verified manufacturers, aggregates quotes |
| **TradeIndia** | `tradeindia.com` / Inquiry API | Supplier profiles, competitor wholesale listings, product catalogs | Fallback RFQ dispatch for alternative wholesale suppliers |

#### D. Indian Regulatory Verification
| Provider | Portal / API | Intelligence | Agent Usage |
|---|---|---|---|
| **GSTN** | `gst.gov.in` / Authorized GSPs | Active GSTIN status, filing histories, legal name | Validates supplier is active with no compliance filing blocks |
| **DGFT** | `dgft.gov.in` / Developer Portal | IEC status, active licenses (EPCG, LUT, Advance Auth) | Verifies active export status, checks DGFT Denied Entity List (DEN) |
| **MCA** | `api.setu.co` / API Setu | CIN, director listings, active LLP/Pvt Ltd status | Verifies corporate legitimacy via government-signed records |
| **Udyam** | `udyamregistration.gov.in` / Signzy/Zoop | Micro, small, and medium enterprise certifications | Filters out retail brokers — ensures sourcing from real factories |

Each becomes an adapter in `packages/connectors/` with a uniform interface: `quote()`, `book()`, `status()`, `webhook_verify()`.

### 8.2 Risk Management Matrix
| Risk Event | Impact | Mitigation |
|---|---|---|
| Supplier cargo fails QC | High | QC flags before cargo leaves; payment locked in escrow; logistics pickup auto-cancelled. |
| Demurrage / port delays | Medium | Dynamic buffer rates factored into quotations; carrier webhook milestones notify buyer of scheduling updates. |
| Incorrect HS code | High | Specialized LLM-RAG classifier matches buyer descriptions to exact, validated HS codes from Harmonized System database. |
| FX volatility | Medium | Multi-currency escrow buffers locked via treasury spot contracts at PI signing, mitigating INR-USD risk. |
| Buyer/seller disintermediation | Medium | Switch BoL + merchant-of-record docs + escrow trust lock + blind sourcing (see §3.7). |
| SGS batch inspection failure | High | System auto-pauses logistics pickup, raises dispute ticket, drafts corrective actions for supplier, locks escrow to protect both parties. |

### 8.3 Future Roadmap — Web3 Trust Layer (Nice-to-Have)
- **Stablecoin Escrow (USDC/USDT):** Layer-2 smart contracts release funds on validated Switch BoL + CRF.
- **Immutable Cargo Provenance:** CRF hash stamped on-chain (IPFS/Arweave).
- **Tokenized BoL (eBoL):** ERC-721/1155 title transfer on final escrow payment.
- **Decentralized Notary (SBTs):** Soulbound reputation tokens for buyers/sellers.
- **Schema hooks:** optional `onchain_transaction_hash`, `buyer_wallet_address` on Deal Context.

### 8.4 Developer Portals
- ICEGATE: https://www.icegate.gov.in
- DGFT: https://www.dgft.gov.in
- GSTN Developer: https://developer.gst.gov.in
- UN Comtrade: https://comtradeplus.un.org
- VIES: https://ec.europa.eu/taxation_customs/vies
