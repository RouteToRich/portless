# PortLess — Negotiation Agent Unified Implementation Plan

> **Technical implementation guide for the autonomous Negotiation Agent.**
> Aligned with [`portless_master_specification.md`](portless_master_specification.md) §4.3.1 (Negotiation Agent).
> Defines the technical stack, multi-agent prompts, schemas, execution workflows, and staging milestones.

This agent is triggered after the Match Engine produces a qualifying match (CMS ≥ 75). It receives match parameters (`trade_match_id`, `buyer_id`, `seller_id`, `product_code`, `buyer_country_code`, `cost_envelope`) and conducts intelligent multi-round negotiation with both buyer and seller via email, monitored inboxes, or portal.

---

## 1. Information Acquisition & Parsing Framework

The agent acquires, parses, and enforces business information using distinct layers to guarantee both linguistic versatility and mathematical safety:

* **Bargaining Tactics, Framing, and Tone (Base LLM):** The general negotiation strategy (e.g., anchoring, the "reciprocity rule" where concessions demand counters, professional phrasing) is entirely handled by the Base LLM's pre-trained weights. It does not need internet access or RAG to know how to negotiate persuasively.
* **Product Specs and Trade Rules (Local RAG Database):** Knowing specific limits (such as moisture content under 9.5% for Ground Pepper, or quality grades like AGMARK) is powered by RAG (Retrieval-Augmented Generation) pointing to local files like trade playbooks, PDFs, and compliance docs indexed in `pgvector`. This does not require internet, but relies on a local database.
* **Real-time Pricing & Logistics (External APIs):** To fetch actual live shipping rates, port delays, or currency exchange rates (USD/INR conversion), the agent relies on programmatic External API integrations (e.g., Cogoport or FX APIs). This requires live internet access but uses tool-calling rather than LLM memory to avoid hallucinations.
* **Enforcing Margins (Deterministic Python Guards):** Ensuring that the agent never agrees to a deal where profit drops below 8% is hard-coded in the backend validation rules (`verify_payout_and_margin()`). It does not trust the LLM or RAG with this check. Travel cost + QC cost + doc cost (from Match Engine's `cost_envelope`) are factored into the margin calculation.
* **Monitored Inboxes & Attachment Extraction:** The agent monitors configured merchant exporter email inbox (IMAP/API), auto-reads buyer/seller responses, and extracts quotes and counter-offers from email body and attachments (PDF, Excel).
* **Negotiation Memory:** The entire negotiation flow (rounds, counter-offers, concessions, reasoning) is persisted in `negotiation_sessions` and `negotiation_rounds` tables, enabling the agent to reference prior context, maintain consistency, and resume after interruptions.

---

## 2. Technical Stack & Capability Matrix

### 2.1 Technical Stack

| Layer | Selected Technology | Rationale |
|---|---|---|
| **Agentic State Engine** | **LangGraph** | Enables cyclical loops, message state tracking, and branching logics for dual-sided negotiation loops. |
| **Foundation LLM** | **Claude / GPT-class** (routed via LiteLLM) | Reasoning/negotiation: frontier model. Bulk extraction: smaller/cheaper model. |
| **Retrieval Engine (RAG)** | **PostgreSQL with `pgvector`** | Indexes trade playbooks, quality limits (e.g. moisture cap, color ASTA), and regional compliance rules. |
| **Negotiation Memory** | **PostgreSQL (`negotiation_sessions` + `negotiation_rounds`)** | Persists full negotiation flow for context continuity across rounds and sessions. |
| **Session Cache & Locks** | **MongoDB TTL cache (`shared.cache`)** | Tracks real-time active counter rounds, locks session routes, and maintains short-term conversational context. |
| **Deterministic Guards** | **Pure Python Functions (FastAPI)** | Enforces mathematically absolute margin validations ($\ge 8\%$) including travel + QC + doc costs. |
| **Schema Parser** | **Pydantic v2** | Structurally compiles model responses and guarantees execution tool parameters. |
| **Real-time API Bridges** | **Cogoport SDK / FX APIs** | Fetches live transportation bids and multi-currency conversions. |
| **Inbox Monitor** | **IMAP / Email API** | Auto-reads buyer/seller responses, extracts quotes from attachments (PDF, Excel). |
| **Communication Channels** | **Twilio / SMTP / WhatsApp Business API** | Multi-channel outbound communication to buyer and seller. |

### 2.2 Summary Matrix

| Layer / Source | Provided By | Requires Internet? | Requires RAG? | Enforced By |
|---|---|---|---|---|
| **Persuasion & Tone** | Base LLM pre-trained weights | ❌ No | ❌ No | Linguistic models |
| **Specs & Guidelines** | pgvector + Local Trade PDF docs | ❌ No (Local DB) | ✅ Yes | Vector search matching HSN |
| **Live Pricing & FX** | External API HTTP requests | ✅ Yes | ❌ No | Tool-calling JSON payloads |
| **Safety Boundaries** | FastAPI mathematical functions | ❌ No | ❌ No | Hardcoded business rules |
| **Inbox Monitoring** | IMAP / Email API | ✅ Yes | ❌ No | Structured extraction pipeline |
| **Negotiation Memory** | PostgreSQL (sessions + rounds) | ❌ No (Local DB) | ❌ No | Persistent state store |

---

## 3. System Architecture & Communication Channels

```
  ┌────────────────────────────────────────────────────────┐
  │              NEGOTIATION AGENT ORCHESTRATOR             │
  │    Triggered by Match Engine (CMS ≥ 75)                │
  │    Loaded with: RAG context + cost_envelope + live data │
  │    Enforces margin ≥ 8% (incl. travel+QC+doc costs)    │
  └───────────────┬────────────────────────┬───────────────┘
                  │                        │
                  ▼                        ▼
       ┌────────────────────┐    ┌────────────────────┐
       │ BUYER-FACING AGENT │    │ SUPPLIER-FACING AGT│
       │ (USD/EUR, CIF/FOB) │    │   (INR, Ex-Works)  │
       └──────────┬─────────┘    └──────────┬─────────┘
                  │                         │
                  ▼                         ▼
       ┌──────────────────────────────────────────────┐
       │     HUMAN-IN-THE-LOOP (HITL) APPROVAL GATE   │
       │  Intercepts at key decisions, allows edits    │
       └──────────┬─────────────────────────┬─────────┘
                  │ Channels:               │ Channels:
                  │ Email, Portal, Telegram │ Email, WhatsApp, Portal
                  ▼                         ▼
       ┌────────────────────┐    ┌────────────────────┐
       │   Overseas Buyer   │    │  Indian Supplier   │
       └────────────────────┘    └────────────────────┘
                  │                         │
                  ▼                         ▼
       ┌──────────────────────────────────────────────┐
       │        MONITORED INBOX (IMAP/API)             │
       │  Auto-reads responses, extracts quotes from   │
       │  attachments (PDF, Excel), routes to session  │
       └──────────────────────────────────────────────┘
```

### Input Parameters (from Match Engine)
| Parameter | Source | Purpose |
|---|---|---|
| `trade_match_id` | Match Engine | Links negotiation to the scored match |
| `buyer_id` / `seller_id` | Match Engine | Identifies the negotiating parties |
| `product_code` (HS code) | Match Engine | Loads product-specific RAG knowledge |
| `buyer_country_code` | Match Engine | Loads country rules, restrictions, duties |
| `composite_match_score` | Match Engine | Informs agent confidence and strategy |
| `buyer_target_price` | Match Engine / Buyer | Starting bid reference |
| `supplier_quote_price` | Match Engine / Sourcing | Starting ask reference |
| `cost_envelope` | Match Engine | travel_cost + qc_cost + doc_cost (factored into margin) |
| `date_of_expectation` | Buyer | Deadline pressure for negotiation |
| `date_of_packaging` | Supplier | Feasibility constraint |

### Match Engine Interaction Pattern
> 1. **Intermediate Negotiation Cycles:** The Negotiation Agent **does not** call the Match Engine on each round. Static scores (port proximity, reliability) and costs (travel, QC, docs) are cached at initialization. The agent relies on local Python guardrails (`verify_payout_and_margin()`) to verify dynamic profit margins in real-time.
> 2. **Post-Negotiation Sign-Off:** Once terms converge (`negotiation.agreed`), the system invokes the Match Engine **one final time** to compute and persist the final **Realized CMS** against the finalized pricing.

### 3.1 Development Phases
*   **Phase 1: Channel Connectors & State Definitions:** Build email (SMTP/IMAP) inbox monitor, portal webhook, and Telegram Bot to parse incoming messages. Define unified Pydantic JSON structure for extraction. Implement monitored inbox with attachment extraction (PDF, Excel).
*   **Phase 2: Negotiation Memory & Stateful Boundary:** Implement `negotiation_sessions` and `negotiation_rounds` tables in PostgreSQL. State cannot transition to `NEGOTIATION_AGREED` unless convergence rule is met (margin ≥ floor AND |ask - bid| ≤ concession_step). Default `max_rounds`: 5.
*   **Phase 3: The Arbitrage Guardrail Layer:** Build pure-function `verify_payout_and_margin()` that factors in cost_envelope (travel + QC + docs) and prevents the agents from accepting terms that drop net margin below 8%.
*   **Phase 4: Multi-Agent Coordination Loops:** Build the LangGraph framework connecting the **Buyer-Facing Agent**, the **Supplier-Facing Agent**, and the **Negotiation Orchestrator** with RAG context loading and live data fetching at initialization.

---

## 2. Multi-Agent System Prompts

> These three LangGraph nodes cooperate within the `agents/negotiation_agent` module:

1. **The Negotiation Orchestrator (The Central Arbitrage Brain):** Manages the dual-sided margins, tracks round counts, enforces HITL gates, and acts as the gatekeeper.
2. **The Buyer-Facing Agent (The Commercial Ambassador):** Speaks to the importer via email/portal/Telegram.
3. **The Supplier-Facing Agent (The Sourcing Desk):** Speaks to the factory via email/WhatsApp/portal.

### 2.1 Agent A: System Prompt for the Negotiation Orchestrator

```markdown
You are the Central Commercial Orchestrator for PortLess, a top-tier global trade platform.
Your objective is to coordinate the Buyer-Facing Agent and the Supplier-Facing Agent to secure a double-sided trade match.

CRITICAL BOUNDARIES:
1. Your target net margin is 12% to 15%. Your absolute walk-away margin floor is 8%.
2. You must never allow either negotiation side to finalize a contract unless the combined math satisfies:
   Net Margin = Buyer Price (CIF/FOB converted to USD) - Sourcing Costs (INR converted to USD) - Logistics - QC - CHA - cost_envelope.travel - cost_envelope.doc - Buffer >= 8%
3. Default `max_rounds`: 5. Negotiate at least 3 rounds before concluding terms to test price elasticity, unless a party explicitly accepts the opening offer. Apply `concession_step` to control bid/ask convergence pace.
4. Monitor key factors: Unit Price, Minimum Order Quantity (MOQ), Lead Time, Payment Terms (Escrow/LC/Advance), Quality Specs, Packaging, and Logistics (Incoterms).
5. When `hitl_required` triggers (first outbound, margin within 2% of floor, or final agreement), route to HITL Approval gate before sending.

Your output must strictly be a JSON routing decision pointing to the next agent action, along with current metrics:
{
  "trade_match_id": "string",
  "buyer_round": int,
  "supplier_round": int,
  "current_estimated_margin_pct": float,
  "action_routing": "CALL_BUYER_AGENT" | "CALL_SUPPLIER_AGENT" | "HITL_APPROVAL" | "NEGOTIATION_AGREED" | "NEGOTIATION_FAILED",
  "reasoning": "Confidence check on margin spread.",
  "margin_at_current": float,
  "hitl_required": bool
}
```

### 2.2 Agent B: System Prompt for the Buyer-Facing Agent

```markdown
You are a world-class International Trade Negotiator representing a leading Indian Merchant Exporter.
Your goal is to negotiate high-value, cross-border purchase agreements with overseas importers.

COMMUNICATION STYLE:
- Confident, commercially mature, crisp, respectful, and highly persuasive.
- Communicate in English (complying with standard western corporate etiquette).
- Your primary communication channels are Email, Portal, and Telegram.

NEGOTIATION PROTOCOLS:
1. **Round Limit:** Negotiate for at least 3 communication rounds (max_rounds default: 5). Do not accept initial target prices immediately unless they are significantly above market indicators.
2. **Reciprocity Rule:** Never give a concession (e.g., lower price, faster delivery, or deferred payment terms) without demanding a reciprocal concession (e.g., higher order quantity, multi-shipment commitment, or immediate escrow funding).
3. **Parameters to Trade:** You must play with multiple levers, not just unit price. If price is a bottleneck, negotiate on:
   - **Minimum Order Quantity (MOQ):** Offer discount brackets for higher volume.
   - **Payment Terms:** 100% Escrow funding, 30% advance + 70% LC, or CAD.
   - **Quality/Certification Specs:** Standard specifications vs AGMARK premium standards.
   - **Logistics:** Switch between FOB (Port of Loading) and CIF (Port of Destination) pricing to find freight arbitrage.
4. **Prior History:** Always read and reference previous conversation steps. Do not repeat terms or send contradictory proposals.

CONSTRAINTS:
- Never commit to prices below the dynamic floor supplied to you by the Orchestrator.
- Keep your tone confident and commercially mature. Avoid looking desperate for the order.

Your output must strictly conclude with this meta-data contract block (formatted in valid markdown or raw YAML/JSON at the end of your response):

---
### BUYER-FACING AGENT METADATA CONTRACT
```json
{
  "proposed_response": "The actual email/portal/Telegram message to send to the buyer.",
  "extracted_negotiated_terms": {
    "unit_price_usd_per_kg": 0.00,
    "incoterm": "CIF" | "FOB",
    "moq_mt": 0.00,
    "lead_time_days": 0,
    "payment_terms": "string",
    "quality_specifications": "string"
  },
  "current_round_number": 0,
  "confidence_score": 0.00,
  "escalation_flag": false,
  "hitl_required": false,
  "next_recommended_move": "State your strategic intention for the next message loop."
}
```
```

### 2.3 Agent C: System Prompt for the Supplier-Facing Agent

```markdown
You are a highly experienced Sourcing and Procurement Manager representing a tier-1 Indian trading house.
Your goal is to secure optimal wholesale pricing, reliable shipping timelines, and compliance standards directly from local Indian manufacturers/factories.

COMMUNICATION STYLE:
- Direct, localized, commercially astute, confident, and firm but respectful.
- Use Indian business context (referencing GST compliance, local ports like Chennai/Mundra, and transport standards).
- Your primary channels are Email, WhatsApp, and Telegram.

NEGOTIATION PROTOCOLS:
1. **Squeezing the Arbitrage:** Indian manufacturers often include premium buffers. Negotiate hard across at least 3 rounds to discover their actual cost-floor.
2. **Reciprocity Rule:** If they ask for payment advances, demand faster factory-ready times, customized export-strength packaging, or loading assistance.
3. **Parameters to Trade:**
   - **Unit Price (INR):** Push for lower per-metric-ton pricing based on immediate cash escrow commitment.
   - **Lead Time:** Minimize preparation days. Demand guaranteed cargo-ready dates with penalty clauses for delays.
   - **Payment Release Ladder:** Tie payments directly to SGS/Inspection passes (post-CRF certificate) and Let Export Order (LEO) generation at customs. Avoid 100% upfront cash advances.
   - **Local Transport/Logistics:** Align on who handles inland transport to the port of loading (Ex-Works vs FOB Port).
4. **Prior History:** Read history to hold suppliers accountable to their prior verbal quotes or listed prices.

CONSTRAINTS:
- Do not onboard any supplier who fails the GST, IEC, or DGFT gatekeepers.
- Do not agree to prices that exceed the maximum sourcing limit assigned by the Orchestrator.

Your output must strictly conclude with this meta-data contract block:

---
### PROCUREMENT METADATA CONTRACT
```json
{
  "proposed_response": "The actual email, WhatsApp, or Telegram message to send to the manufacturer.",
  "extracted_negotiated_terms": {
    "unit_price_inr_per_kg": 0.00,
    "delivery_terms": "Ex-Works" | "FOB-Port",
    "lead_time_days": 0,
    "moq_mt": 0.00,
    "payment_terms": "string",
    "packaging_standard": "string"
  },
  "current_round_number": 0,
  "confidence_score": 0.00,
  "escalation_flag": false,
  "hitl_required": false,
  "next_recommended_move": "State your strategic intention for the next supplier push."
}
```
```

---

## 3. Step-by-Step Negotiation Round Sequence

Below is a sequence showing how conversation state is tracked over multiple rounds before final agreement (default `max_rounds`: 5):

```
ROUND   BUYER ACTION (Email/Portal/Telegram)  SUPPLIER ACTION (Email/WhatsApp/Portal)
────────────────────────────────────────────────────────────────────────────────
Round 1 Buyer posts:                          Supplier quotes:
        "Need 16MT ground pepper,              "₹195/kg, ex-works Guntur.
         targeting $3.50/kg CIF."              Need 50% advance."

Round 2 Buyer rejects opening margin:         Supplier-Facing Agent pushes:
        "Your price $3.30 is high.             "We can secure payment post-SGS
         Market rate is $3.15."                CRF, but quote must drop to ₹180."

Round 3 Buyer accepts compromise:             Supplier agrees:
        "We can agree to $3.25/kg CIF          "Okay, final at ₹182/kg.
         if shipment is within 25 days."       Delivery ready in 18 days."

→ Convergence met: margin ≥ 8%, |ask - bid| ≤ concession_step → emits `negotiation.agreed`
```

### 3.1 Pydantic Output Validation Schema

All responses must be routed through this Pydantic parser within the `agents/negotiation_agent` module to validate agent compliance with the convergence rule before writing any state change to the `deals` service:

```python
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from decimal import Decimal

class CostEnvelope(BaseModel):
    travel_cost: Decimal = Field(..., description="Travel/inspection cost from Match Engine")
    qc_cost: Decimal = Field(..., description="Quality control cost")
    doc_cost: Decimal = Field(..., description="Documentation/compliance cost")

class NegotiatedTerms(BaseModel):
    unit_price: float = Field(..., description="The agreed or countered price.")
    currency: str = Field(..., description="USD, EUR, or INR")
    moq: float = Field(..., description="Minimum order quantity in MT")
    lead_time_days: int = Field(..., description="Factory prep or shipping lead days")
    payment_terms: str = Field(..., description="Escrow lock, LC terms, or advance milestones")
    logistics_incoterm: str = Field(..., description="FOB, CIF, or Ex-Works")

class NegotiationResponse(BaseModel):
    proposed_response: str = Field(..., description="Formatted message for channel dispatch")
    extracted_negotiated_terms: NegotiatedTerms
    current_round_number: int = Field(..., ge=1, description="Round tracker increments on each party turnaround.")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Confidence in party compliance")
    escalation_flag: bool = Field(default=False, description="True if HITL approval is required")
    margin_at_current: float = Field(..., description="Current net margin % factoring cost_envelope")
    hitl_required: bool = Field(default=False, description="True if this message needs HITL gate")
    next_recommended_move: str = Field(..., description="Actionable strategy summary for the operator")
```

---

## 4. Disaster Recovery & Operator Escapes

If a loop gets stuck, the Orchestrator acts as the release valve:
*   **Max Round Expiry:** If `round_number >= max_rounds` (default: 5) and terms have not converged within the concession limit, `escalation_flag` is set to `True`, freezing the agents. The system emits `approval.requested` event and routes to HITL Approval queue.
*   **Margin Floor Breach:** If any counterparty demands terms that force the net margin below 8% (factoring in cost_envelope: travel + QC + doc costs), the loop is suspended. The session moves to `NEGOTIATION_FAILED` state, and an `approval.requested` event is emitted for operator review on the Ops Console.
*   **Operator Override:** The operator can manually insert a fixed `override_unit_price` via the HITL approval portal, allowing the agent to use the overridden terms and proceed to convergence.

---

## 5. Multi-Lever Dual-Sided Negotiation Scenarios (5 Practical Examples)

Below are 5 end-to-end simulation logs of negotiations conducted by the PortLess Negotiation Agent. Each log demonstrates a multi-round double-sided loop, leveraging price, quantity, quality specifications, payment terms, and logistics configurations to protect the net profit margin. HITL approval gates are marked `[HITL Approved]`.

### Scenario 1: Ground Red Pepper (Spices)
*   **Target Country:** Spain (Iberia Spices SL)
*   **Sourcing Hub:** Guntur, Andhra Pradesh (Guntur Agri Products Ltd)
*   **Target Margins:** Target: 12% - 15% | Floor: 8%

```
================================────────────────================================
CYCLE 1: SEEDING BASES
================================────────────────────────────────────────────────
* BUYER-FACING AGENT (BFA) to Iberia Spices SL (Spain):
  Subject: Offer Ref: REDPEP-ES-0091 - 16MT Premium Ground Red Pepper
  Dear Procurement Team, 
  Following up on your import manifest, we can supply 16MT of export-grade Ground Red Pepper (min 5% ASTA color value) at $3.45/kg CIF Valencia, shipment ready within 21 days. Payment secure via 100% Escrow lock. Let us know if this aligns with your scheduling.

* SUPPLIER-FACING AGENT (SFA) to Guntur Agri Products Ltd:
  Subject: RFQ: 16 Tonnes Ground Red Pepper for Export (Chennai Port Loading)
  Dear Sourcing Team,
  We have an active requirement for 16MT of export-standard Ground Red Pepper (ASTA 120+, moisture <10%). Please quote your best Ex-Works Guntur price. Payment will be released immediately into escrow on successful local SGS inspection.

* COUNTERPARTY RESPONSES:
  - Buyer (Spain): "Our budget cap is $3.15/kg CIF Valencia. Also, we require moisture under 9.5%."
  - Supplier (India): "We quote ₹210/kg Ex-Works. 50% advance is required to start grinding and packaging."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer $3.15 ($50,400) - Supplier ₹210 ($40,320 equiv) - Logistics/QC ($4,800) = ~10.4% margin.
  Status: Margin is above floor (8%), but we must negotiate hard to squeeze supplier and push buyer up.

================================────────────────================================
CYCLE 2: LEVER PLAY & PRESSURE
================================────────────────================================
* BFA to Iberia Spices SL (Spain) [HITL Approved]:
  Subject: Re: Offer Ref: REDPEP-ES-0091 - Specification & Costing
  Dear Procurement Team,
  To guarantee a moisture level below 9.5%, our processing unit must apply specialized dehumidification cycles. We can commit to this premium quality standard. However, we cannot match $3.15/kg at 16MT. We can offer $3.35/kg CIF Valencia, or we can drop to $3.25/kg if you increase the order size to a full 20ft container (20MT). Please confirm your preference.

* SFA to Guntur Agri Products Ltd [HITL Approved]:
  Subject: Re: RFQ: 16 Tonnes Ground Red Pepper - Payment Terms Counter
  Dear Sourcing Team,
  We are a verified Merchant of Record with corporate escrow backing. We cannot release 50% raw cash advance for a first-time transaction. However, we can offer a 20% advance milestone, with the remaining 80% locked in a verified bank escrow, released within 24 hours of SGS CRF certification. In return, we expect your bulk price to drop to ₹195/kg Ex-Works to account for our immediate financial commitment.

* COUNTERPARTY RESPONSES:
  - Buyer (Spain): "We will stick to 16MT. We can compromise at $3.25/kg CIF Valencia with 9.5% moisture."
  - Supplier (India): "Agreed to 20% advance + 80% SGS Escrow. Best price we can do is ₹200/kg."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer $3.25/kg ($52,000) - Supplier ₹200/kg ($38,400 equiv) - Logistics/QC ($4,600) = ~17.3% margin. 
  Status: Excellent convergence. Proceed to close.

================================────────────────================================
CYCLE 3: CONVERGENCE & SIGN-OFF
================================────────────────================================
* BFA to Iberia Spices SL (Spain) [HITL Approved]:
  Dear Procurement Team,
  We accept your compromise of $3.25/kg CIF Valencia for 16MT with guaranteed moisture levels below 9.5%. We will generate the Proforma Invoice (PI) with Stripe Escrow deposit details shortly.

* SFA to Guntur Agri Products Ltd [HITL Approved]:
  Dear Sourcing Team,
  We accept ₹200/kg Ex-Works with the 20/80 escrow division model. We will forward the Purchase Order (PO) and dispatch our local cargo surveyor to coordinate your packing schedule.

* FINAL REALIZED MARGIN: 17.3% (Success)
```

### Scenario 2: Premium Basmati Rice (Grains)
*   **Target Country:** Dubai, UAE (Al-Khaleej Foods)
*   **Sourcing Hub:** Karnal, Haryana (Punjab Rice Mills)
*   **Target Margins:** Target: 12% - 15% | Floor: 8%

```
================================────────────────================================
CYCLE 1: SEEDING BASES
================================────────────────================================
* BFA to Al-Khaleej Foods (Dubai):
  Subject: High-Grade 1121 Sella Basmati Rice - FOB Mundra Port
  Dear Sirs, We can supply 50MT (2x20ft FCL) of Premium 1121 Golden Sella Basmati Rice (Average Grain Length 8.35mm+) at $1,150/MT FOB Mundra. Packaging in export-grade 19kg non-woven bags. 

* SFA to Punjab Rice Mills (Haryana):
  Dear Team, We need a wholesale quote for 50MT of 1121 Golden Sella Basmati Rice (moisture < 12.5%, chalky grains < 1%). Please quote FOB Mundra or Ex-Works Haryana. Payment via 100% secure domestic bank LC.

* COUNTERPARTY RESPONSES:
  - Buyer (Dubai): "Your FOB price of $1,150 is above local Middle East market levels. Our target is $1,050/MT FOB Mundra."
  - Supplier (India): "Our price is ₹82,000/MT Ex-Works Karnal. We don't do FOB; you must handle trucking from Karnal to Mundra."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer $1,050 ($52,500) - Supplier ₹82,000 ($49,200 equivalent) - Trucking & Handling ($4,200) = NEGATIVE MARGIN (-1.7%).
  Action: Must push buyer up on volume/packaging concessions and squeeze supplier on Ex-Works pricing.

================================────────────────================================
CYCLE 2: LEVER PLAY & PRESSURE
================================────────────────================================
* BFA to Al-Khaleej Foods (Dubai) [HITL Approved]:
  Dear Sirs, we cannot meet $1,050/MT for 50MT due to raw paddy price increases in Haryana. However, if you increase your order size to 100MT (4 FCLs) and accept standard 50kg PP bags instead of 19kg bags (reducing packaging manufacturing overheads), we can offer a special rate of $1,100/MT FOB Mundra.

* SFA to Punjab Rice Mills (Haryana) [HITL Approved]:
  Dear Team, we are coordinating a 100MT consignment, which will utilize your milling capacity efficiently for a week. Since we are taking over 100% of inland logistics and customs processing at Mundra Port, we require your wholesale Ex-Works rate to drop to ₹76,000/MT.

* COUNTERPARTY RESPONSES:
  - Buyer (Dubai): "100MT in 50kg bags is acceptable. Our final offer is $1,085/MT FOB Mundra."
  - Supplier (India): "For a guaranteed 100MT order, we can drop our Ex-Works price to ₹77,500/MT."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer $1,085 ($108,500 total) - Supplier ₹77,500 ($93,000 equivalent) - Inland transport ($6,500) - Port fees ($1,200) = ~7.1% Net Margin.
  Status: Close to our 8% floor, but we need one more push to cross the 8% target.

================================────────────────================================
CYCLE 3: CONVERGENCE & SIGN-OFF
================================────────────────================================
* BFA to Al-Khaleej Foods (Dubai) [HITL Approved]:
  Dear Sirs, we can meet $1,085/MT FOB Mundra for 100MT, provided the payment is funded via a confirmed Irrevocable Letter of Credit (LC) at Sight, opened through a Tier-1 Dubai bank within 5 business days. Please confirm your acceptance.

* SFA to Punjab Rice Mills (Haryana) [HITL Approved]:
  Dear Team, we accept ₹77,500/MT Ex-Works for 100MT. We require the milling batch to be fully completed within 14 days, with a ₹2,000/day penalty for dispatch delays to protect our vessel booking.

* COUNTERPARTY RESPONSES:
  - Buyer (Dubai): "Confirmed. LC draft will be shared by Monday."
  - Supplier (India): "Accepted. Milling schedule booked."

* FINAL REALIZED MARGIN: 8.4% (Approved)
```

### Scenario 3: Organic Turmeric Powder (Health Food)
*   **Target Country:** USA (BioOrganic Imports Inc.)
*   **Sourcing Hub:** Erode, Tamil Nadu (Erode Organic Farms)
*   **Target Margins:** Target: 12% - 15% | Floor: 8%

```
================================────────────────================================
CYCLE 1: SEEDING BASES
================================────────────────================================
* BFA to BioOrganic Imports Inc. (USA):
  Subject: Certified Organic Turmeric Powder (5% Curcumin) - CIF New York
  Dear Procurement, We can offer 20MT of USDA-Certified Organic Turmeric Powder with guaranteed 5% minimum curcumin content at $2.95/kg CIF New York, delivery ready within 30 days.

* SFA to Erode Organic Farms (Tamil Nadu):
  Dear Team, We require 20MT of USDA-Certified Organic Turmeric Powder (Curcumin min 5%, lead/metal free). Please quote your best price Ex-Works Erode, including organic transaction certificate charges.

* COUNTERPARTY RESPONSES:
  - Buyer (USA): "We require USDA NOP organic certificates. Our target price is $2.60/kg CIF New York."
  - Supplier (India): "Organic certified stock with 5% curcumin is in high demand. Our price is ₹190/kg Ex-Works Erode."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer $2.60 ($52,000) - Supplier ₹190 ($45,600 equivalent) - Ocean freight & insurance ($5,500) = NEGATIVE MARGIN (-1.7%).
  Action: We must trade off the curcumin percentage or charge premium for 5% curcumin.

================================────────────────================================
CYCLE 2: LEVER PLAY & PRESSURE
================================────────────────================================
* BFA to BioOrganic Imports Inc. (USA) [HITL Approved]:
  Dear Team, a 5% Curcumin level represents a premium extraction yield. If you can accept a 4.5% Curcumin level (which is fully compliant with standard USDA organic imports), we can meet your target of $2.60/kg CIF New York. If you strictly require the premium 5% Curcumin level, our absolute floor is $2.80/kg CIF New York.

* SFA to Erode Organic Farms (Tamil Nadu) [HITL Approved]:
  Dear Team, we have other sourcing options in Sangli, Maharashtra. To finalize this transaction with Erode, we need you to include the NOP Organic Transaction Certificates in your quotation at no extra cost, and drop your wholesale price to ₹172/kg Ex-Works.

* COUNTERPARTY RESPONSES:
  - Buyer (USA): "We require the premium 5% Curcumin for our nutraceutical extraction line. We can increase our price to $2.78/kg CIF New York."
  - Supplier (India): "We can provide 5% Curcumin with free USDA certificates at ₹178/kg Ex-Works if you pay a 30% advance deposit."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer $2.78/kg ($55,600 total) - Supplier ₹178/kg ($42,720 equivalent) - Logistics/QC ($5,200) = ~13.8% margin.
  Status: Margin is well within our 12%-15% sweet spot. Proceed to lock in.

================================────────────────================================
CYCLE 3: CONVERGENCE & SIGN-OFF
================================────────────────================================
* BFA to BioOrganic Imports Inc. (USA) [HITL Approved]:
  Dear Team, we agree to supply the premium 5% Curcumin Organic Turmeric Powder at $2.78/kg CIF New York. We will issue the Proforma Invoice with details of the independent SGS testing protocols.

* SFA to Erode Organic Farms (Tamil Nadu) [HITL Approved]:
  Dear Team, we accept ₹178/kg Ex-Works with a 30% advance deposit. The remaining 70% will be released instantly upon SGS lab clearance showing 5% curcumin and nil metal contaminants.

* FINAL REALIZED MARGIN: 13.8% (Success)
```

### Scenario 4: Coir Pith Blocks (Horticulture / Agriculture)
*   **Target Country:** Netherlands (GreenGrow Substrates)
*   **Sourcing Hub:** Pollachi, Tamil Nadu (Pollachi Coir Industries)
*   **Target Margins:** Target: 12% - 15% | Floor: 8%

```
================================────────────────================================
CYCLE 1: SEEDING BASES
================================────────────────================================
* BFA to GreenGrow Substrates (Netherlands):
  Subject: 5kg Coir Pith Blocks (Horticultural Grade) - CIF Rotterdam
  Dear Sirs, We can supply 24MT (1x40ft HC Container) of washed, low-EC 5kg Coir Pith Blocks at EUR 410/MT CIF Rotterdam. Shipment ready within 18 days.

* SFA to Pollachi Coir Industries (Tamil Nadu):
  Dear Team, We need a quote for 24MT of washed Coir Pith Blocks (5kg, EC < 0.5 mS/cm, Expansion 15L/kg+). Please quote FOB Tuticorin Port.

* COUNTERPARTY RESPONSES:
  - Buyer (Netherlands): "We are getting offers at EUR 380/MT CIF Rotterdam. Can you match this?"
  - Supplier (India): "Washed low-EC blocks are ₹24,000/MT FOB Tuticorin. We need 15 days for desalination processing."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer EUR 380 ($10,032 total) - Supplier ₹24,000 ($7,920 equivalent) - Ocean freight Tuticorin to Rotterdam ($1,800) = ~3.1% margin.
  Action: Margin is far below our 8% floor. Must reduce freight costs or negotiate supplier down.

================================────────────────================================
CYCLE 2: LEVER PLAY & PRESSURE
================================────────────────================================
* BFA to GreenGrow Substrates (Netherlands) [HITL Approved]:
  Dear Team, our blocks guarantee an expansion ratio of 15L/kg, which is 15% higher than the cheaper alternatives in the market, saving you raw material handling costs. We cannot offer EUR 380/MT, but we can finalize at EUR 395/MT CIF Rotterdam if we book the container for next month's shipping slot (longer lead time allows us to secure lower ocean freight rates).

* SFA to Pollachi Coir Industries (Tamil Nadu) [HITL Approved]:
  Dear Team, your price of ₹24,000/MT FOB is high compared to Salem-based coir mills. We can offer you a continuous monthly booking of 2 containers (48MT) if you reduce the price to ₹21,500/MT FOB Tuticorin.

* COUNTERPARTY RESPONSES:
  - Buyer (Netherlands): "Longer lead time of 30 days is acceptable. We agree to EUR 395/MT CIF Rotterdam."
  - Supplier (India): "For a recurring monthly contract, we agree to ₹22,000/MT FOB Tuticorin."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer EUR 395 ($10,428) - Supplier ₹22,000 ($7,260 equivalent) - Low-season freight ($1,400) = ~16.9% margin.
  Status: Well above our target range. Close the deal.

================================────────────────================================
CYCLE 3: CONVERGENCE & SIGN-OFF
================================────────────────================================
* BFA to GreenGrow Substrates (Netherlands) [HITL Approved]:
  Dear Team, we accept EUR 395/MT CIF Rotterdam with a 30-day delivery window. We will issue the Proforma Invoice with the escrow allocation instructions.

* SFA to Pollachi Coir Industries (Tamil Nadu) [HITL Approved]:
  Dear Team, we accept ₹22,000/MT FOB Tuticorin on a monthly recurring contract. First Purchase Order (PO) is being dispatched for signature.

* FINAL REALIZED MARGIN: 16.9% (Success)
```

### Scenario 5: Combed Cotton Yarn (Textiles)
*   **Target Country:** Turkey (Levant Spinning Corp)
*   **Sourcing Hub:** Coimbatore, Tamil Nadu (Salem Textile Spinners)
*   **Target Margins:** Target: 12% - 15% | Floor: 8%

```
================================────────────────================================
CYCLE 1: SEEDING BASES
================================────────────────================================
* BFA to Levant Spinning Corp (Turkey):
  Subject: 30s Ne Combed Cotton Yarn for Weaving - CIF Izmir
  Dear Sirs, We can supply 20MT of Premium 30s Ne Combed Cotton Yarn (UT5 registered, contamination-free) at $3.20/kg CIF Izmir, shipment within 25 days.

* SFA to Salem Textile Spinners (Tamil Nadu):
  Dear Team, We require 20MT of combed cotton yarn (30s Ne weaving yarn, Uster 5% standards). Please quote your best price Ex-Works.

* COUNTERPARTY RESPONSES:
  - Buyer (Turkey): "Our maximum target is $2.90/kg CIF Izmir. We need 60 days credit term."
  - Supplier (India): "We quote ₹218/kg Ex-Works. Payment must be 100% advance before loading."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer $2.90 ($58,000) - Supplier ₹218 ($52,320 equivalent) - Shipping to Izmir ($3,200) = ~4.2% margin, plus a high-risk credit request.
  Action: Red flag on credit request. We must enforce 100% Cash-in-Escrow and negotiate yarn parameters to save cost.

================================────────────────================================
CYCLE 2: LEVER PLAY & PRESSURE
================================────────────────================================
* BFA to Levant Spinning Corp (Turkey) [HITL Approved]:
  Dear Sirs, as we are a zero-credit merchant clearing customs at preferential rates, we do not extend 60-day credit terms. All orders are secured via 100% bank escrow lock before dispatch. In exchange for your cash-in-escrow commitment, we can lower our unit price to $3.05/kg CIF Izmir.

* SFA to Salem Textile Spinners (Tamil Nadu) [HITL Approved]:
  Dear Team, we cannot pay 100% cash advance upfront before production starts. We can pay a 10% advance deposit to secure the spinning schedule, with the remaining 90% paid on a Bank Letter of Intent, released the minute the transport container clears our domestic customs export gate. In return, we expect your price to match the Coimbatore benchmark of ₹198/kg.

* COUNTERPARTY RESPONSES:
  - Buyer (Turkey): "We accept 100% Escrow funding if the price is fixed at $3.00/kg CIF Izmir."
  - Supplier (India): "We can compromise at ₹204/kg Ex-Works with your 10% advance + 90% customs-release payment scheme."

* ORCHESTRATOR ANALYSIS:
  Current Math: Buyer $3.00/kg ($60,000 total) - Supplier ₹204/kg ($48,960 equivalent) - Shipping & port charges ($3,400) = ~12.7% margin.
  Status: Meets margin guidelines. Zero financial credit exposure achieved.

================================────────────────================================
CYCLE 3: CONVERGENCE & SIGN-OFF
================================────────────────================================
* BFA to Levant Spinning Corp (Turkey) [HITL Approved]:
  Dear Sirs, we agree to your price of $3.00/kg CIF Izmir on a 100% Escrow deposit model. We will forward the Proforma Invoice and escrow details today.

* SFA to Salem Textile Spinners (Tamil Nadu) [HITL Approved]:
  Dear Team, we accept ₹204/kg Ex-Works on the 10/90 customs release model. We will forward the Purchase Order (PO) to lock in the production timeline.

* FINAL REALIZED MARGIN: 12.7% (Success)
```


---

## Appendix A: Implementation Roadmap

> Module path: `backend/agents/negotiation_agent/`

```
  [Phase 1: DB & State Setup] ──► [Phase 2: pgvector RAG Index] ──► [Phase 3: LangGraph Agent Wiring] ──► [Phase 4: Guards & Inbox]
```

### Phase 1: Database & Negotiation State Setup (Week 1)
* Create PostgreSQL schemas: `negotiation_sessions` (session state, parties, parameters) and `negotiation_rounds` (per-round bids, counters, reasoning, margin snapshots).
* Configure MongoDB-backed session locks (shared TTL cache) to track active round counts per `trade_match_id`.
* Implement `max_rounds` (default: 5) convergence boundary at the database tier.
* Define `CostEnvelope` and `StartNegotiationRequest` Pydantic models in `agents/negotiation_agent/schemas.py`.

### Phase 2: pgvector RAG Pipeline (Week 2)
* Populate the PostgreSQL `embeddings` store with chunked trade vectors derived from PDFs and product playbooks.
* Write the lookup utility to retrieve trade rules based on HS code and query parameters.
* Set up retrieval evaluation tests to ensure 100% accurate contextual injection (e.g., moisture cap under 9.5% for Ground Pepper).

### Phase 3: LangGraph Graph Topology & Tool Wiring (Week 3)
* Establish the cycle path layout for `BuyerFacingAgent` and `SupplierFacingAgent` nodes in LangGraph.
* Integrate tool bindings to query live currencies (USD ⇄ INR) and fetch freight shipping spot rates from Cogoport interfaces.
* Wire HITL approval gate node: intercepts before outbound messages, emits `approval.requested` if `hitl_required`.
* Build response parsers returning `NegotiationResponse` contracts.

### Phase 4: Deterministic Guardrails & Inbox Monitor (Week 4)
* Develop `verify_payout_and_margin()` to assert net margins ≥ 8% including `cost_envelope` (travel + QC + doc costs).
* Build inbox monitor service: IMAP/API polling, attachment extraction (PDF/Excel), quote parsing into structured round data.
* Set up exception triggers: emit `negotiation.failed` if rounds exceed `max_rounds` without convergence.
* Write integration tests (Pytest) mocking counter-responses to verify happy-path and escalation scenarios.

---

## Appendix B: Match Engine Interaction Diagram

```
[Phase 1] Match Engine runs ONCE ──► CMS ≥ 75? ──► [Phase 2] Negotiation Agent loop runs internally 
                                                            │ (checks local Python margin guards,
                                                            │  NOT the Match Engine, on each round)
                                                            ▼
                                                   Terms Converged! → emits `negotiation.agreed`
                                                            │
                                                            ▼
                                                   [Phase 3] Write final terms to DB 
                                                   & invoke Match Engine one last time to compute 
                                                   the final Realized CMS against finalized pricing.
                                                            │
                                                            ▼
                                                   DealMaker service picks up `negotiation.agreed`
                                                   and issues PI (buyer) + PO (supplier).
```
