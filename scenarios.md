# Scenarios — Deep Walkthroughs

Step-by-step scenario write-ups: exact status transitions, which function/endpoint
fires them, and what changes on screen in the Ops Console at each step. Companion
to `workflow.md` (endpoint-by-endpoint reference) and `layman_workflow.md`
(plain-English narrative) — this file is the "trace one deal through the system"
view.

---

## Scenario 1 — Opportunity scan to negotiation AGREED

> Last reviewed: 2026-07-14.

Everything upstream of `AGREED`: how an opportunity is found, how it becomes a
deal, how a supplier is sourced, and — in detail — every status a negotiation
session moves through before it converges. Continues in **Scenario 2** below.

### 1. Trade-Min scan → opportunities discovered

`POST /trade-min/scan` (or the nightly/reorder APScheduler cron) runs the
5-phase pipeline in `services/trade_min/service.py`:

```
Phase 1  Historical demand      ← Comtrade / trade-intel connectors
Phase 2  Live demand signals    ← Alibaba RFQ · EU TED · GDELT
Phase 3  Tariff gate            ← MacMap / tariff source
Phase 4  Supplier discovery     ← IndiaMART / directories
Phase 5  Enrich + score urgency → EnrichedOpportunity[] → opportunity.detected
```

No deal exists yet — this only produces `EnrichedOpportunity` records
(`status=ACTIVE|MARGINAL|EXPIRED|CONVERTED`, `arbitrage_rank`,
`seller_candidates[]`, `buyer_signals[]`).

- **UI:** **Opportunity Radar** page. Clicking **"Scan New"** calls
  `useScanOpportunities` → `POST /trade-min/scan`, then a success toast
  ("Scan complete"). The summary cards (Total Opportunities, Top HS Code, Avg
  Arbitrage Rank) recompute from the refreshed list. The table renders one row
  per opportunity: HS Code, Market, Source badge, an `ArbitrageBar` (colored
  green ≥7, amber ≥4, red below), a **Top Seller** cell (cheapest
  `estimated_fob_price_usd` from `seller_candidates`, "+N more" if there are
  others), and a **Buyer Signals** cell (first `buyer_signals` entry's
  name/country, "+N more"). Each row ends with a **Start Deal** button — unless
  `opp.status === "CONVERTED"`, in which case it shows a **"Deal Started"**
  badge instead.

### 2. Deal created + auto-matched → `MATCHING`

Operator clicks **Start Deal** → `POST /deals {operator_id, hs_code, market,
opportunity_id}` → `deals/service.py::create_deal()`:

1. Fetches the `MatchEngineSeed` from Trade-Min (`get_match_seed`).
2. Scores up to 3 top-seller candidates **concurrently**
   (`match_engine.service.score_match()` — CMS = 0.5·margin + 0.3·proximity +
   0.2·reliability, gated to 0 on any failed regulatory check; `SOURCE` at
   CMS ≥ ~75).
3. Picks the highest-scoring `SOURCE` candidate as `winning_supplier_id` /
   `winning_cms` (may be `None` if nobody clears the bar — the deal still
   proceeds, just without a winner yet).
4. **Sets `status = MATCHING` unconditionally once a seed exists** — this
   happens even if no candidate scored `SOURCE` (see `create_deal`'s comment:
   "always move to MATCHING once seeded"). `DRAFT` is only used when there's
   no `opportunity_id` at all (a manually-created deal).
5. Marks the opportunity `CONVERTED` and emits `deal.created`.

**Deal: (new) → `MATCHING`**
- **UI:** toast "Deal created — Deal `<id>` started"; the Opportunity Radar
  row for that opportunity flips to the **"Deal Started"** badge (its
  `useCreateDeal` mutation also invalidates the `opportunities` query, since
  the source opportunity flips server-side to `CONVERTED`); the new deal
  appears on **Deal Board** in the **Active** Kanban column with
  `DealPipeline` sitting at "Matching."
- If `AUTO_FLOW_ENABLED=true` (dev/test only), `create_deal` also fires
  `shared/orchestration/auto_flow.start(deal_id)` here — a background task
  that drives steps 3–4 below (and everything in Scenario 2) using the real
  service calls, still pausing at every HITL gate.

### 3. Sourcing: RFQ → quote → re-score → `NEGOTIATING`

This is a separate, explicit phase — nothing about reaching `MATCHING` auto-
dispatches an RFQ.

```
POST /sourcing/rfq        → dispatch RFQ to the candidate supplier(s)
POST /sourcing/quotes     (supplier replies with a quote)
  1. persist QuoteRecord
  2. verify supplier — GSTN · DGFT · MCA · Udyam (parallel) — any FAIL ⇒ gate
  3. score_match(seed + verification)          ← Match Engine (re-score)
  4. if decision == SOURCE:
       update_financials(winning supplier, CMS, target price)  ← Deals
       update_status(MATCHING → NEGOTIATING)                   ← Deals (best-effort)
```

**Deal: `MATCHING` → `NEGOTIATING`**
- **UI gap, same shape as Scenario 2's callout:** there is no `SourcingApi` in
  `portless-ui/src/api/endpoints.ts` at all — dispatching an RFQ or submitting a quote
  isn't reachable from the Ops Console today. This step happens via a direct
  API call (Swagger `/docs`) or via Auto-Flow, which calls
  `sourcing.dispatch_rfq` + `sourcing.receive_quote` directly and, if the
  re-scored CMS still lands under the `SOURCE` threshold (common for thin/
  mocked test data, since a fresh mocked supplier's reliability component
  defaults to 0), nudges the transition through anyway the way an operator
  confirming a quote in the UI would.
- Once `NEGOTIATING` is reached: `DealPipeline` advances to "Negotiating";
  Kanban card stays in the **Active** column; `DealDetail`'s Negotiation tab
  is now ready to show a session once one starts.

### 4. Negotiation — full status walkthrough

`POST /negotiation-agent/negotiations` (`start_negotiation()`) with
`{deal_id, trade_match_id, buyer_id, seller_id, product_code,
buyer_country_code, buyer_target_price, supplier_quote_price, cost_envelope,
policy, batna, buyer_email?, seller_email?}`.

#### 4a. Session start

- Loads static trade context once (`trade_context_for_product(hs_code)` —
  product playbook and concession patterns).
- If `NEGOTIATION_MARKET_SEARCH_ENABLED=true`, concurrently fetches fresh
  buyer + supplier live web-search market color (`market_intel.fetch_both()`)
  — this one re-fetches **every round**, unlike trade context which is loaded
  once and reused.
- Creates `NegotiationSession` — `status=STARTED`, `round_no=0`,
  `current_bid=buyer_target_price`, `current_ask=supplier_quote_price`.
- LangGraph fires with `is_first_round=True`.

#### 4b. `NegotiationSession.status` reference

| Status | Meaning | Terminal? |
|---|---|---|
| `STARTED` | Session created, first graph pass running | No |
| `ROUND` | Active round in progress, waiting for the next reply | No |
| `HITL_PENDING` | An email draft is awaiting operator approval/reject/modify | No |
| `AGREED` | Convergence reached, `agreed_price` set | **Yes** |
| `FAILED` | Max rounds exhausted without convergence | **Yes** |
| `ESCALATED` | Operator escalated beyond normal HITL handling | **Yes** |

#### 4c. Every round follows the same deterministic-guard-first pattern

The **Orchestrator** node evaluates guards in priority order before ever
asking the LLM to route:

```
gap ≤ concession_step  AND  margin ≥ floor?      → AGREED (stop, deterministic)
round_no ≥ max_rounds  AND  not converged?        → FAILED (stop)
margin < floor + hitl_margin_warning_pct?         → still drafts one message, hitl_required=True
is_first_round AND hitl_on_first_outbound?        → still drafts one message, hitl_required=True
otherwise                                          → LLM decides CALL_BUYER_AGENT or CALL_SUPPLIER_AGENT
```

Whichever agent runs drafts **exactly one** outbound message per graph pass
(buyer talks CIF, supplier talks Ex-Works — neither ever sees the other's real
price, PortLess's margin, or the BATNA; the buyer agent only ever receives
`floor_price`, the supplier agent only `implied_max`). The draft is built from:
`trade_context` (once), `market_context` (fresh, if enabled), the full
party-scoped conversation history (so it never re-offers a concession or
contradicts an earlier message), and the inbound reply if any.

**Every single drafted email — not just the first one — is L0 HITL-gated**
(`SEND_NEGOTIATION_EMAIL`, always-ask, never auto-approved) via
`_create_and_gate_email()`. Session → `HITL_PENDING`, `round_no` increments.

- **UI:** a new thread card appears (or updates) in **Negotiation Inbox** —
  amber "Pending Review" badge, an amber `HitlBanner` at the top of the
  conversation ("N email drafts awaiting operator approval"), and the draft
  itself rendered in `EmailThread` with **Approve & Send** / **Modify** /
  **Reject** buttons. The same approval also shows up as a card in the
  **Approvals Inbox** (SSE-pushed in real time). `DealDetail`'s Negotiation
  tab mirrors this via its own `HitlApprovalCard` list.

#### 4d. Operator resolves the email → reply arrives → next round

- **Approve & Send** → `send_approved_email()` → delivered via
  `email_gateway` (real SMTP, or log-only if unconfigured) →
  `EmailHistoryRecord.status = sent`.
- **Modify** → edits body first, then sends → `status = modified_sent`.
- **Reject** → message never sent → `status = rejected`; operator decides
  next action manually.
- A reply then comes in either for real (IMAP poll every 15 min →
  `email_processor` matches it to the live session via In-Reply-To/
  References/`[NEG:uuid]` subject token → extracts the counter-price →
  `RECEIVED` audit record → calls `run_round()`) or manually, via
  **Negotiation Inbox**'s "Submit Inbound Reply" form (party, counter-price,
  optional pasted message) — for when IMAP auto-detection isn't configured.
- `run_round()` re-fetches fresh market intel, rebuilds conversation history,
  and re-invokes the same LangGraph pass. Session cycles `ROUND ↔
  HITL_PENDING` once per round until a terminal guard fires.

#### 4e. Terminal outcomes

**`AGREED`** — `gap ≤ concession_step` and `margin ≥ floor`, purely
deterministic (no LLM). `agreed_price = midpoint(bid, ask)`; Match Engine
recomputes `realized_cms`; `negotiation.agreed` event fires;
`_post_graph_notify()`'s `_sync_deal_status()` **automatically** calls
`deals.update_status()` → **`NEGOTIATING` → `NEGOTIATION_AGREED`** (see
Scenario 2, step 0 — no operator action needed for this specific transition).
- **UI:** thread badge → green "Agreed"; `ThreadHeader` shows the final
  `Agreed` stat; `DealDetail` Negotiation tab session summary → green
  "AGREED"; `DealPipeline` on both `DealDetail` and `DealBoard` advances to
  "Agreed"; Kanban card stays in the **Active** column (which groups
  `DRAFT/MATCHING/NEGOTIATING/NEGOTIATION_AGREED` together).

**`FAILED`** — `round_no ≥ max_rounds` without convergence. Deal transitions
to the terminal `NEGOTIATION_FAILED` status; Exception Resolver may classify
it (`NEGOTIATION_FAILED` category, typically `CANCEL → ESCALATE`).
- **UI:** thread badge → red "Failed"; `DealPipeline` renders the terminal
  fail indicator (red X, "NEGOTIATION FAILED"); the Kanban card moves to the
  **Complete** column — which, somewhat counter-intuitively, groups both
  successful terminal states (`CLEARED`, `PAID`) *and* failure terminal
  states (`DISPUTE`, `CANCELLED`, `REFUNDED`, `NEGOTIATION_FAILED`) together.

**`ESCALATED`** — an operator-driven escalation beyond the normal HITL
flow (not reached by any automatic guard).

---

## Scenario 2 — From negotiation AGREED to PAID

> Last reviewed: 2026-07-14.

Once a negotiation session reaches `AGREED` (see Scenario 1, §4e), here's
exactly what happens end-to-end — status by status, what triggers each
transition, and what the UI actually shows (and doesn't) at each point.

### 0. AGREED is reached — the one fully automatic step

`run_round()` in `agents/negotiation_agent/service.py` returns
`session.status == "AGREED"`. `_post_graph_notify()`'s `_sync_deal_status()`
immediately calls `deals.update_status()` for you — **no operator action
needed**:

**Deal: `NEGOTIATING` → `NEGOTIATION_AGREED`**

- **UI:** `DealPipeline` (the stepper on `DealDetail` and the mini-bar on
  `DealBoard` cards) advances to "Agreed"; `StatusBadge` turns green
  (`NEGOTIATION_AGREED` is in the `OK` list); the deal's Kanban card is still
  in the **Active** column (that column groups
  `DRAFT/MATCHING/NEGOTIATING/NEGOTIATION_AGREED` together). The Negotiation
  tab's Session Summary badge flips to "AGREED" and shows the final
  `agreed_price`.

Everything after this point is **not automatic** — each step below requires
an explicit API call. That call comes from one of three places: an operator
hitting the real endpoint directly (there's no UI button for it — see the
callout at the end), a script, or the dev/test-only Auto-Flow orchestrator
(`AUTO_FLOW_ENABLED`, `shared/orchestration/auto_flow.py`).

### 1. Proforma Invoice → `PI_SENT`

`POST /dealmaker/pi` (`dealmaker/service.py::issue_pi`) — requires deal in
`NEGOTIATION_AGREED`. Renders `pi.html.j2`, SHA-256-hashes it, stores the PI
record, emits `pi.sent`.

**Deal: `NEGOTIATION_AGREED` → `PI_SENT`**
- **UI:** pipeline advances to "PI Sent"; card moves to the **PI Stage**
  Kanban column. `DealDetail → Documents` tab would show the PI once the UI
  wires `GET /dealmaker/documents/{deal_id}` — currently that tab only calls
  `useDocuments`, which hits DocuGen's CI/PL/etc., not DealMaker's PI/PO, so
  today the PI/PO HTML isn't surfaced in this tab at all, only reachable via
  the API.

### 2. Buyer accepts → `PI_ACCEPTED`

`POST /dealmaker/pi/{id}/accept` — requires PI status `SENT`. Also
best-effort auto-provisions an Organization for the buyer
(`find_or_create_organization_for_deal`) if a `buyer_email` was supplied.

**Deal: `PI_SENT` → `PI_ACCEPTED`**
- **UI:** pipeline → "PI Accepted"; still in the **PI Stage** column.

### 3. Escrow opened → `ESCROW_PENDING` (a real gap, by design)

This is a documented gap in the pipeline itself: **nothing automatically
drives `PI_ACCEPTED → ESCROW_PENDING`** (`shared/orchestration/auto_flow.py`'s
own docstring calls this out as gap #2). Someone has to call
`PATCH /deals/{id}/status {ESCROW_PENDING}` and then
`POST /escrow-guard/holds` (`create_hold` — Stripe/Payoneer PaymentIntent).

*(There's a second, more sophisticated escrow API — `create_escrow`, the
neutral staged-tranche model for Tazapay/Escrow.com — that **does**
auto-transition `PI_ACCEPTED → ESCROW_PENDING` on its own, but the UI's
Payments tab only reads the simple hold model today, so this walkthrough
stays on that path.)*

**Deal: `PI_ACCEPTED` → `ESCROW_PENDING`**
- **UI:** pipeline → "Escrow Pending"; card moves to the **Escrow** column;
  `StatusBadge` turns amber (WARN); Payments tab shows a hold with status
  `PENDING` and the PENDING → FUNDED → RELEASED mini-timeline at step 1.

### 4. Buyer pays → `ESCROW_FUNDED`

Buyer completes payment on the provider's checkout; the provider fires
`POST /escrow-guard/webhooks/escrow-funded` → `handle_escrow_funded_webhook`
verifies the HMAC signature, marks the hold `FUNDED`, transitions the deal,
emits `escrow.funded`. It also reconciles the funded amount against the
requested principal and raises a `deal.exception` on mismatch.

**Deal: `ESCROW_PENDING` → `ESCROW_FUNDED`**
- **UI:** pipeline → "Funded"; `StatusBadge` green; card moves to Escrow
  column's second slot; Payments tab hold badge → `FUNDED`, timeline fills to
  step 2, "Funded At" timestamp appears.

### 5. PO issued to supplier → `PO_ISSUED` — first HITL gate

`POST /dealmaker/po` (`issue_po`) — requires `ESCROW_FUNDED`, and enforces
the **margin guard**:
`net_margin_pct = (buyer_target_price - po_price) / buyer_target_price × 100`
must be ≥ `po_margin_floor_pct` (default 8%), else `MarginGuardError` (422).
This is the first of three actions normally routed through a **HITL
approval** (`ISSUE_PO` → saga callback → `dealmaker.service.issue_po()`),
since it commits real money to the supplier.

**Deal: `ESCROW_FUNDED` → `PO_ISSUED`**
- **UI (while pending):** a new card appears in **Approvals Inbox** in real
  time via the SSE stream (toast + `HitlApprovalCard`), showing the summary
  and `financial_impact_usd`. Operator clicks Approve/Reject.
- **UI (after approve):** pipeline → "PO Issued"; card moves to the
  **Fulfillment** Kanban column; Approvals Inbox row flips to `APPROVED`.

### 6. QC booked → `SUPPLIER_DISPATCH_READY`

`POST /shield-link/qc` (`book_qc`) — requires `PO_ISSUED`. This one is
**not** HITL-gated by default (only PO/freight/payout are in the
saga-callback map) — it's a direct call. SGS/Cogoport is booked (stub if no
API key).

**Deal: `PO_ISSUED` → `SUPPLIER_DISPATCH_READY`**
- **UI:** pipeline → "Dispatch Ready"; `StatusBadge` amber; Logistics tab's
  QC card appears with provider name and "Booked Xm ago".

### 7. QC result webhook → `QC_PASSED` or `QC_FAILED`

SGS's webhook → `handle_qc_result`. This is the first branch point.

**Happy path — `SUPPLIER_DISPATCH_READY` → `QC_PASSED`:**
- **UI:** pipeline → "QC Passed" (green step); Logistics tab QC badge →
  `PASSED` (green), report link appears if provided.

**Failure path — `SUPPLIER_DISPATCH_READY` → `QC_FAILED`:**
- **UI:** `DealPipeline` renders the **terminal/fail indicator** (red X, "QC
  FAILED" label) instead of continuing the stepper; `StatusBadge` red; card
  moves to the **Complete** column (which also holds terminal/failure
  states); Logistics tab QC badge → `FAILED` (red).
- Behind the scenes: this typically raises a `deal.exception` → **Exception
  Resolver** classifies it (`QC_FAILED` category) → usually `ESCALATE` →
  creates a `RESOLVE_EXCEPTION` HITL approval → operator resolves in
  **Exceptions** page / Approvals Inbox → deal may move to `DISPUTE` →
  `CANCELLED` or `REFUNDED` (all terminal).

The rest of this walkthrough continues the happy path.

### 8. Freight booked → `IN_TRANSIT` — second HITL gate

`POST /shield-link/freight` (`book_freight`) — requires `QC_PASSED`. Second
HITL-gated action (`BOOK_FREIGHT` → `shield_link.service.book_freight()`).

In parallel (not gating status): `POST /docugen/documents` generates
CI/PL/CoO/SDF/Shipping Bill, then `POST /docugen/customs/file` → `file_cha`
files with ICEGATE via CHA — this one explicitly "does not require a
specific deal status; can run alongside freight booking."

**Deal: `QC_PASSED` → `IN_TRANSIT`**
- **UI:** another Approvals Inbox card (SSE toast) for `BOOK_FREIGHT`; after
  approval, pipeline → "In Transit"; Logistics tab's freight card appears
  with a progress bar (Booked 25% → In Transit 60% → Arrived 85% → Cleared
  100%); Documents tab populates as CI/PL/CoO/SDF/Shipping Bill records
  complete (each shows a download icon once `file_url` is set).

### 9. Freight milestone webhook → `CLEARED`

Provider webhook with milestone `ARRIVED` or `CLEARED` →
`handle_freight_milestone` transitions the deal.

**Deal: `IN_TRANSIT` → `CLEARED`**
- **UI:** pipeline → "Cleared"; card moves to the **Complete** Kanban
  column; Logistics freight badge → `CLEARED` (success), progress bar hits
  100%.

### 10. Supplier payout requested — third HITL gate

`POST /escrow-guard/payouts` (`request_payout`) — requires `CLEARED` **and**
a `FUNDED` hold on the deal. Third gated action
(`RELEASE_SUPPLIER_PAYOUT` → `escrow_guard.service.request_payout()`). Marks
the hold `RELEASED` and kicks off a Payoneer payout (`PROCESSING`).

- **UI:** third Approvals Inbox card; after approval, Payments tab hold
  timeline advances to step 3 (`RELEASED`), a payout row appears with status
  `PROCESSING`. Deal status itself is unchanged (still `CLEARED`) until the
  payout completes.

### 11. Payout completes → `PAID` (terminal)

Payoneer webhook `COMPLETED` → `handle_payout_webhook` → deal transitions,
emits `payout.completed`.

**Deal: `CLEARED` → `PAID`**
- **UI:** pipeline shows all steps green, ends at "Paid"; `StatusBadge`
  green; card stays in **Complete** column; Payments tab payout row →
  `COMPLETED`; this is a terminal state — no further transitions.

### Two things worth knowing that aren't obvious from the flow diagram

1. **The UI currently can't trigger any of steps 1–10 itself.** Checking
   `portless-ui/src/api/endpoints.ts`, `DealMakerApi`/`EscrowApi`/`ShieldApi`/
   `DocugenApi` only expose **GET** wrappers (`getHolds`, `getQc`,
   `getFreight`, `getDocuments`) — there's no `issuePi`, `acceptPi`,
   `issuePo`, `bookFreight`, `requestPayout`, etc. anywhere in the client.
   So today, driving a deal from `PI_SENT` through `PAID` happens either by
   calling those endpoints directly (Swagger UI at `/docs`), or by turning on
   `AUTO_FLOW_ENABLED` (`shared/orchestration/auto_flow.py`), which does
   exactly the sequence above using the real service calls and still stops
   at the same three HITL gates. The UI's job in that flow is: show the
   pipeline/status live, and let the operator resolve the Approvals Inbox
   cards.
2. **HITL gates aren't enforced by the state machine** — `can_transition()`
   only validates that e.g. `ESCROW_FUNDED → PO_ISSUED` is a legal
   transition, not that a `POST /hitl-approval/approvals` happened first.
   Gating is opt-in by whoever calls `issue_po`/`book_freight`/
   `request_payout` — Auto-Flow always routes through a gate at the deal's
   own `autonomy_profile` (usually L0), which is why those three specific
   actions show up in the Approvals Inbox in practice.
