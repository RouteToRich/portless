# PortLess — End-to-End Workflow (as built)

> **Who is this for?** Anyone who needs to know what happens to a trade from the first
> scan to the last payout: operators deciding how much autonomy to grant, engineers
> changing a leg, partners auditing a deal. Each stage says what happens, which event
> starts it, which gate (if any) guards it, which API call does the work, and what a
> counterparty sees. Everything here is implemented; where a step depends on a credential
> or an external actor that is not yet in place, it says so.
>
> Engineering reference: [`../CLAUDE.md`](../CLAUDE.md). Open items: [`../PRODUCTION_TRACKER.md`](../PRODUCTION_TRACKER.md).
> Diagram: [`../needed/portless_complete_automation_flow.excalidraw`](../needed/portless_complete_automation_flow.excalidraw).
> Per-module detail: each module's `workflow_<name>.md`.

---

## Contents

1. [The idea in one paragraph](#1-the-idea-in-one-paragraph)
2. [Who does what](#2-who-does-what)
3. [The deal state machine](#3-the-deal-state-machine)
4. [How a step triggers the next one](#4-how-a-step-triggers-the-next-one)
5. [Gates and autonomy](#5-gates-and-autonomy)
6. [Stage 0 — Catalogue and scan](#6-stage-0--catalogue-and-scan)
7. [Stage 1 — Deal creation and counterparty identification](#7-stage-1--deal-creation-and-counterparty-identification)
8. [Stage 2 — Buyer outreach funnel](#8-stage-2--buyer-outreach-funnel)
9. [Stage 3 — Sourcing the supplier](#9-stage-3--sourcing-the-supplier)
10. [Stage 4 — Negotiation to agreement](#10-stage-4--negotiation-to-agreement)
11. [Stage 5 — Proforma Invoice and escrow funding](#11-stage-5--proforma-invoice-and-escrow-funding)
12. [Stage 6 — Purchase Order and inspection](#12-stage-6--purchase-order-and-inspection)
13. [Stage 7 — Freight, customs, clearance](#13-stage-7--freight-customs-clearance)
14. [Stage 8 — Delivery, acceptance, settlement, PAID](#14-stage-8--delivery-acceptance-settlement-paid)
15. [The money chain — invariants](#15-the-money-chain--invariants)
16. [E-mail in, e-mail out](#16-e-mail-in-e-mail-out)
17. [When nothing happens — the deal watcher](#17-when-nothing-happens--the-deal-watcher)
18. [When something breaks — exceptions and playbooks](#18-when-something-breaks--exceptions-and-playbooks)
19. [What the operator sees](#19-what-the-operator-sees)
20. [Books, timeline, evidence](#20-books-timeline-evidence)
21. [Running the whole loop locally](#21-running-the-whole-loop-locally)
22. [Configuration cheat-sheet](#22-configuration-cheat-sheet)

---

## 1. The idea in one paragraph

PortLess is a merchant exporter with no warehouse. It finds a product that a foreign
buyer is importing at one price and an Indian manufacturer is selling at a lower one,
contracts both sides in its own name, has the buyer pay into a neutral escrow, has an
independent inspector confirm the goods before they ship, books the ship and the customs
filing, and releases the escrow in tranches against each proof. The supplier is paid from
what the escrow has released; the difference is PortLess's margin. Software runs the loop;
a human decides only where money or PortLess's commitment is at stake, and even those
decisions can be delegated per action, per amount, per deal.

## 2. Who does what

| Actor | Does | Through |
|---|---|---|
| **PortLess automation** | scans, identifies, screens, writes and sends e-mails, drafts and sends PI/PO, opens escrow, books QC/freight/customs, renders documents, releases tranches against proof, pays the supplier, raises exceptions | events + continuations (`shared/orchestration/continuations.py`) |
| **Operator** | sets autonomy per action, approves or rejects gates, edits drafts, adds buyers or suppliers, records a delivery, reads the digest | UI (Approvals Inbox, Deal Detail, Autonomy Settings), Telegram, e-mail |
| **Buyer** | replies to outreach, negotiates, accepts the PI, funds the escrow, accepts the goods or complains | e-mail only (every mail carries a token) + the escrow provider's checkout |
| **Supplier** | quotes, negotiates, acknowledges the PO, readies the lot for inspection, ships | e-mail only |
| **Providers** | inspector (SGS / QIMA / BV / Intertek / TÜV), forwarder (Cogoport / Freightos), customs (ICEGATE), escrow (Tazapay / Escrow.com), payout rail (Payoneer), carrier tracking | HMAC-signed webhooks + the own Apify actors |

## 3. The deal state machine

`shared/domain/state_machine.py` is the single source of truth; every transition is
validated with `can_transition()` and recorded in `status_history`.

```
DRAFT → MATCHING → NEGOTIATING ↔ NEGOTIATING_AGREED → NEGOTIATION_FAILED
NEGOTIATING_AGREED → PI_SENT → PI_ACCEPTED → ESCROW_PENDING → ESCROW_FUNDED
  → PO_ISSUED → SUPPLIER_DISPATCH_READY → QC_PASSED | QC_FAILED
QC_PASSED → IN_TRANSIT → CLEARED → PAID
QC_FAILED → DISPUTE → CANCELLED | REFUNDED | SUPPLIER_DISPATCH_READY   (re-inspection playbook)
PI_SENT | PI_ACCEPTED | ESCROW_PENDING → CANCELLED                     (unfunded-deal playbook)
Terminal: PAID, CANCELLED, REFUNDED, NEGOTIATION_FAILED
```

Two things are not states but matter as much: the **escrow** (`PENDING → PARTIALLY_FUNDED
→ FUNDED → RELEASED / REFUNDED`, with four tranches) and the **negotiation session**
(`STARTED → ROUND … → HITL_PENDING → AGREED / FAILED`).

## 4. How a step triggers the next one

Every service publishes a `DomainEvent` after it commits (`deal.created`,
`quote.received`, `negotiation.agreed`, `pi.sent`, `escrow.funded`, `qc.passed`,
`freight.milestone`, `escrow.tranche_released`, `payout.completed`, `deal.exception` …).
The continuations module subscribes once in the monolith and reacts: it validates the
deal is in the expected state (so a duplicate event is a no-op), calls the next service
in-process, creates the gate when one is needed, sends the counterparty notice, and on
failure publishes `deal.exception` instead of dying quietly. With a Synadia `*.creds`
file the bus is NATS JetStream and events survive a restart; otherwise it is in-process
and the deal watcher catches whatever a crash lost.

## 5. Gates and autonomy

A gate is a HITL approval (`POST /hitl-approval/approvals`) with an action, a summary,
the callback payload that APPROVE will execute, and an autonomy level resolved by
`shared/orchestration/autonomy_policy.py`:

| Level | Behaviour |
|---|---|
| **L0** | waits for a human, no timeout |
| **L1** | waits; after the action's timeout it auto-resolves with the action's default (negotiation e-mails 4 h → APPROVE; money and everything else 24 h → REJECT) |
| **L2 / L3** | approved immediately, the callback runs, the operator is informed |

Resolution order: saved override (Autonomy Settings page, Mongo) → `AUTONOMY_<ACTION>`
env → the deal's `autonomy_profile` (PO / freight / customs / tranche / payout only) →
built-in default. Defaults are L2 for `DISPATCH_RFQ`, `SEND_DEAL_NOTIFICATION` and
`SEND_BUYER_OUTREACH`, L0 for everything else.

Three rules sit on top of the policy:

- **Money ceiling.** `RELEASE_ESCROW_TRANCHE`, `RELEASE_SUPPLIER_PAYOUT` and
  `CONFIRM_BUYER_ACCEPTANCE` never resolve above `AUTONOMY_MONEY_CEILING` (default L1).
- **Value thresholds with preconditions.** `AUTONOMY_<ACTION>_AUTO_APPROVE_BELOW_USD` or
  `AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD` makes a gate L2 while the amount is under the
  threshold **and** the leg's preconditions hold — a tranche needs its proof references and
  a reconciled ledger, a payout needs a reconciled ledger. The approval carries the flag
  `auto_approved_below_value_threshold`.
- **Four-eyes.** Above `FOUR_EYES_THRESHOLD_USD` a money gate needs two different operators;
  the first APPROVE is recorded and the gate stays pending; L2/L3 are overridden to L0.

Operators edit all of this at `GET/PUT /hitl-approval/autonomy-policy` (levels, timeouts,
on-timeout defaults, thresholds, the global money threshold).

Soft rejects (declining a draft, skipping a buyer, "not an acceptance") leave the deal
where it is; every other REJECT raises `deal.exception`.

---

## 6. Stage 0 — Catalogue and scan

**What happens.** The operator tells PortLess what it sells and where
(`PUT /trade-min/catalogue`: product lines with HS code, plain name, bill-of-lading words,
destination markets, seller profile, typical lot). A scan (`POST /trade-min/scan`, the
nightly scheduler, or the UI button) runs product × market through five phases: historical
demand and the market's unit value (UN Comtrade via `export-market-finder-ai` →
`price_benchmark_usd_per_mt`), live demand and named buyers (`alibaba-rfq-leads-ai`
everywhere, `us-importer-finder-ai` for the US, EU TED, GDELT disruption), tariff gate
(MacMap), supplier discovery (`india-supplier-finder-ai`, IndiaMART, TradeIndia) and
scoring. The result is an `EnrichedOpportunity` with `top_buyers` (name, country, e-mail
when known, fit score, pitch angle, quantity) and `top_sellers` (price per MT, MOQ, GSTIN,
contact), ranked by arbitrage and urgency.

**Guards.** Without a catalogue the built-in sample codes and markets run.
`APIFY_MAX_RUNS_PER_DAY` caps actor runs across scans, enrichment and tracking;
`TRADE_MIN_MOCK_MODE=true` replaces every live source with curated scenarios.
`GET /trade-min/sources` shows what is live, stub or mock.

**Event.** `opportunity.detected`.

## 7. Stage 1 — Deal creation and counterparty identification

**What happens.** A deal is created from the opportunity (`POST /deals` with
`opportunity_id`, the Radar's "Start deal", or automatically when
`AUTO_DEAL_FROM_OPPORTUNITY_ENABLED=true` and the rank / confidence thresholds pass). The
Deals service seeds the buyer target price (benchmark), the quantity from the buyer lead,
the **buyer candidates** (every identified buyer, in fit order), the supplier shortlist
and the parties; the deal is `MATCHING`.

`deal.created` then runs enrichment on the parties: `company-contact-finder-ai` (website,
e-mail, phone), `email-list-qualifier-ai` for unverified addresses, `counterparty-check-ai`
(OFAC / UN / EU / UK sanctions, LEI, EU VIES). Results land on the deal `Party`
(`email_status`, `sanctions_status`, `contact_source`). A `block` verdict raises
`deal.exception` and forces the RFQ gate to L0. `PATCH /deals/{id}/parties/{role}` lets
an operator correct a contact (never overwritten later); `POST /deals/{id}/enrich` re-runs
it.

**Gates.** None yet. **Events.** `deal.created`, `counterparty.screened`.

## 8. Stage 2 — Buyer outreach funnel

**Why.** A scan gives buyers who import the product, not buyers who asked PortLess for an
offer. Pitching a counter-price to a cold importer is how deals die. So before any
negotiation, PortLess introduces itself.

**What happens.** `deal.created` also starts `outreach.start_outreach()`: the first
`OUTREACH_PARALLEL` (2) pending candidates with an address get a templated introduction —
the actor's pitch angle, the catalogue product, the escrow + third-party inspection story,
a clear ask (grade, quantity, port), an opt-out line — behind a `SEND_BUYER_OUTREACH`
gate (L2 by default: sent immediately through the e-mail gateway). The subject carries
`[BUY:<deal_id>:<candidate_id>]`.

The reply comes back through the Email Processor (mapper step 2a) and
`outreach.handle_reply()` classifies it:

| Reply | Result |
|---|---|
| interested ("send your price", "yes, 20 MT CIF Hamburg") | candidate `selected`; the buyer party gets the real address (`contact_source=outreach:*`); the parked negotiation opens or is re-addressed |
| declined ("not interested", "remove us") | candidate `declined`; the next pending candidate is contacted |
| unclear | `PARSE_INBOUND_EMAIL` approval — a human reads it and marks interested / skips |

Silence: the deal watcher sends one follow-up after `OUTREACH_FOLLOWUP_DAYS` (3) and marks
`no_reply` after `OUTREACH_NO_REPLY_DAYS` (7) more, then moves on. When every candidate
declined or stayed silent, `deal.exception` (`OUTREACH:exhausted`) asks the operator for
new buyers or a cancellation.

**Rule.** `on_negotiation_started` waits while the funnel is open (`waiting_for_buyer`):
no buyer is ever sent a counter-offer before saying they want one. Deals created by an
operator without candidates, or whose buyer party was set by hand, skip the funnel.

**Operator controls.** `POST /deals/{id}/outreach/start`,
`POST /deals/{id}/buyer-candidates` (add a lead), `POST /deals/{id}/buyer-candidates/{cid}/skip`.
Deal Detail shows the funnel with status, times and reply previews.

## 9. Stage 3 — Sourcing the supplier

**What happens.** In parallel with the funnel, the `DISPATCH_RFQ` gate (L2) sends RFQs to
the top-3 supplier shortlist: suppliers with an e-mail get it **by e-mail** with an
`[RFQ:<rfq_id>]` subject token, others through the connector. A supplier's reply is mapped
by the token → `sourcing.receive_quote_by_email()` (sender matched to the dispatched
address, GSTIN / IEC read from the body) → the same path as `POST /sourcing/quotes`:
GSTN / DGFT / MCA / Udyam verified in parallel, the Match Engine re-scores
(`POST /match-engine/score`: price corridor, verification, distance to port, business age,
Udyam, shipment history).

**Decision.** `SOURCE` with a verified supplier (GST and IEC both checked and passed)
whose price is inside the buyer's corridor → `CONFIRM_SUPPLIER_SELECTION` resolves at L2
(`SOURCING_AUTO_SOURCE_VERIFIED`, flag `auto_sourced_verified_in_corridor`): the deal's
financials are patched and it moves to `NEGOTIATING`. Anything else → the gate waits at
L0 with risk flags (forced L0 when verification failed). `negotiation.failed` later →
`deal.exception` + a re-match gate for the next-best supplier.

**Events.** `rfq.dispatched`, `quote.received`, `deal.status_changed` (NEGOTIATING).

## 10. Stage 4 — Negotiation to agreement

**What happens.** `negotiation.started` opens a session once an interested buyer exists
(`POST /negotiation-agent/negotiations`): buyer target, supplier ask, cost envelope (QC
cost of the buyer-opted inspector, freight, fees), margin floor, policy (max rounds,
concession steps). The LangGraph agent drafts each e-mail with static product trade
context and optional live market intel (Tavily). Every outbound e-mail is a
`SEND_NEGOTIATION_EMAIL` gate (L0 by default; at L1 it goes out after 4 h unless an
operator edits or rejects it; the first mail stays L0 while `HITL_ON_FIRST_OUTBOUND`
holds). Replies come back by token (`[NEG:<id>]`), the price is extracted, the LLM intent
classifier plus a deterministic price-consistency check decide counter / accept / reject,
and `POST /negotiation-agent/negotiations/{id}/round` runs the next round.

**Agreement.** Acceptance parks the session at `HITL_PENDING` behind
`CONFIRM_NEGOTIATION_AGREEMENT` (L0) — the only path to `AGREED`. REJECT reopens the
round (soft). `negotiation.agreed` → buyer screening re-checked when older than 30 days:
`clear` / `unchecked` → PI; `review` → `CONFIRM_COUNTERPARTY_RISK` gate; `block` → no PI,
`deal.exception`.

**Failure.** Max rounds or a walk-away → `NEGOTIATION_FAILED` → the playbook contacts the
next buyer candidate (or asks the operator when the funnel is exhausted) and proposes the
next supplier.

## 11. Stage 5 — Proforma Invoice and escrow funding

**PI.** `POST /dealmaker/pi` renders the PI (Jinja2, SHA-256, Google Drive) with the
agreed price, the deal quantity, payment terms derived from the escrow tranche schedule,
the buyer's chosen inspector and escrow provider, and a validity. Deal `PI_SENT`;
`pi.sent` e-mails it to the buyer (`SEND_DEAL_NOTIFICATION`, same thread).

**Acceptance.** The buyer's unconditional "we accept" → `CONFIRM_PI_ACCEPTANCE` gate →
`POST /dealmaker/pi/{id}/accept` → `PI_ACCEPTED`; anything conditional →
`PARSE_INBOUND_EMAIL`. The portal / API path is the same call.

**Escrow.** `pi.accepted` → `POST /escrow-guard/escrows`: a neutral staged escrow with the
buyer's provider (Tazapay default, Escrow.com), sized from the PI, provider fee % recorded,
four tranches (30 % on PO issued, 40 % on QC passed, 20 % on cleared, 10 % on accepted).
Deal `ESCROW_PENDING`; funding instructions (amount, checkout link, schedule) go to the
buyer.

**Funding.** `POST /escrow-guard/webhooks/escrow-funded` (HMAC-verified, replay-safe on
`event_id`): the full amount → `FUNDED`, deal `ESCROW_FUNDED`. Less than the PI →
`PARTIALLY_FUNDED` with `shortfall`, the deal stays `ESCROW_PENDING`, the buyer gets a
shortfall e-mail with the exact missing amount, `deal.exception` records it; the next
webhook that completes the total finishes funding. No PO is ever issued against money that
is not there.

**Waiting.** The deal watcher reminds the buyer (`DEAL_PI_REMINDER_AFTER_HOURS`,
`DEAL_ESCROW_REMINDER_AFTER_HOURS`, max `DEAL_REMINDER_MAX`); PI expiry or an unfunded
escrow raise `deal.exception`; after `ESCROW_FUNDING_GRACE_DAYS` (14) the resolver proposes
the `cancel_unfunded` playbook (refund any partial funds, cancel, notify both parties) for
the operator to approve.

## 12. Stage 6 — Purchase Order and inspection

**PO.** `escrow.funded` → `ISSUE_PO` gate → `POST /dealmaker/po` (margin guard ≥ 8 % after
all costs) → `PO_ISSUED`; the PO is e-mailed to the supplier; the **T1** tranche gate
(`RELEASE_ESCROW_TRANCHE`, money) is created with the PO as proof.

**QC.** `po.issued` → `POST /shield-link/qc` books the buyer-opted inspector (cheapest
catalogue inspector when the buyer never chose) → `SUPPLIER_DISPATCH_READY`; the booking is
e-mailed to the supplier. The watcher reminds the supplier when the PO is not acknowledged
(`DEAL_PO_ACK_REMINDER_AFTER_HOURS`) and when the inspection does not happen
(`DEAL_QC_READINESS_REMINDER_AFTER_HOURS`).

**Result.** The inspector's webhook (`POST /shield-link/qc/webhook`, HMAC):
- `PASS` → `QC_PASSED`: the document pack (CI, PL, CoO, SDF, Shipping Bill) is rendered,
  the **T2** tranche gate is created with the Clean Report of Findings as proof, and the
  `BOOK_FREIGHT` gate opens.
- `FAIL` → `QC_FAILED` → `DISPUTE` + `deal.exception`. The resolver's `QC_FAILED`
  playbook proposes a **re-inspection** (`RESOLVE_EXCEPTION`, playbook `reinspect`) while
  failed inspections are within `QC_REINSPECTION_MAX` (1): APPROVE moves the deal
  `DISPUTE → SUPPLIER_DISPATCH_READY`, clears any escrow dispute flag, books a fresh
  inspection and sends the supplier a rework notice. Past the limit the operator chooses
  refund (`POST /escrow-guard/escrows/{id}/refund` → `REFUNDED`) or a new supplier.

## 13. Stage 7 — Freight, customs, clearance

**Freight.** `BOOK_FREIGHT` gate → `POST /shield-link/freight` (Cogoport / Freightos quote
and booking; vessel, voyage, container, ETD/ETA) → `IN_TRANSIT`; `freight.booked` creates
the `FILE_CUSTOMS` gate with the document ids attached and e-mails the shipment details to
the buyer.

**Customs.** `FILE_CUSTOMS` → `shield_link.file_cha()` → ICEGATE Shipping Bill + Let Export
Order.

**Milestones.** Carrier milestones arrive three ways and all go through the same
`_apply_milestone`: the forwarder's webhook (`POST /shield-link/freight/webhook`, HMAC),
the carrier-tracking actor poll (`POST /shield-link/deals/{id}/track`, also run by the
watcher every `TRACKING_POLL_HOURS` for `IN_TRANSIT` deals) and an operator entry
(`POST /shield-link/freight/{ref}/milestone`). `ARRIVED` / `CLEARED` move the deal to
`CLEARED`; `customs.cleared` creates the **T3** tranche gate (BL + Shipping Bill + LEO as
proof), e-mails the arrival notice, and opens the `RELEASE_SUPPLIER_PAYOUT` gate for an
amount **capped at the net funds already released from escrow** (the PO value travels as
`total_owed`).

## 14. Stage 8 — Delivery, acceptance, settlement, PAID

**Delivery opens the window.** The `DELIVERED` milestone → `freight.milestone` →
`escrow_guard.open_inspection_window()`: the buyer's acceptance window
(`inspection_window_hours`, 72 h) starts and `escrow.inspection_opened` e-mails the buyer
what to do (accept, or report a problem). If delivery is recorded before T3 released, the
window opens on that release. Without any delivery signal the watcher opens it
`INSPECTION_WINDOW_FALLBACK_DAYS` (21) after clearance. `INSPECTION_WINDOW_TRIGGER=proof`
restores the earlier behaviour (window on the last proof tranche).

**Acceptance.** "Goods received in good order, we accept" → `CONFIRM_BUYER_ACCEPTANCE` gate
(money) → `POST /escrow-guard/escrows/{id}/accept` → **T4** released → escrow `RELEASED`,
`escrow.settled`. Silence until the deadline → the escrow watcher auto-releases (silence is
consent). A complaint → `POST /escrow-guard/escrows/{id}/dispute` blocks every release and
raises `deal.exception`; `/clear-dispute` lifts it once resolved, `/refund` returns the
undisbursed balance.

**Supplier paid, deal PAID.** Every tranche release re-gates the supplier payout with the
newly available funds; `escrow.settled` gates the final balance. `POST /escrow-guard/payouts`
→ Payoneer → `POST /escrow-guard/webhooks/payout` (COMPLETED). The deal becomes `PAID`
only when completed payouts reach the PO value; a partial payout leaves it `CLEARED` with
`supplier_balance_due` on the ledger. On `PAID` both parties get the completion e-mail and
`realized_margin_usd` (net received − payouts) is written on the deal.

## 15. The money chain — invariants

1. PortLess never holds the principal: it sits with the escrow provider.
2. No Purchase Order against an unfunded or partially funded escrow.
3. Tranches release **in schedule order**, each against an independent proof (PO, Clean
   Report of Findings, BL + LEO, buyer acceptance), never while the deal is in
   `DISPUTE` / `CANCELLED` / `REFUNDED` / `QC_FAILED`, never above the principal.
4. PortLess cannot self-release the final tranche: only the buyer's acceptance or a lapsed
   window that opened on delivery.
5. Supplier payouts never exceed the net funds released (principal released − provider
   fees); the deal is `PAID` only when the supplier has the full PO value.
6. Webhooks are HMAC-verified and replay-safe; every provider event id is applied once.
7. Every deal has a ledger (`GET /escrow-guard/deals/{id}/ledger`: principal, funded,
   shortfall, released, fees, net, payouts, available, supplier owed and balance, realised
   margin, reconciliation issues) and a double-entry journal; the escrow watcher raises
   `deal.exception` once per reconciliation breach.
8. Money gates are capped at `AUTONOMY_MONEY_CEILING`, auto-approve under a value threshold
   only with proof and a reconciled ledger, and need two approvers above
   `FOUR_EYES_THRESHOLD_USD`.

## 16. E-mail in, e-mail out

**Out.** Every counterparty e-mail is drafted by a template or the negotiation agent,
recorded in the e-mail history, gated by policy, and sent by the e-mail gateway
(Gmail SMTP; log-only without credentials). Templates: PI issued, escrow funding, funding
shortfall, PO issued, inspection booked, shipment, arrival, acceptance window open,
settlement, completion, reminders, QC rework notice, cancellation, buyer outreach and
follow-up.

**In.** The gateway polls IMAP (or SendGrid / Mailgun webhooks post) into the Email
Processor, whose mapper reads headers and subject tokens and routes:

| Token / state | Handler |
|---|---|
| `[BUY:deal:candidate]` | outreach reply → interested / declined / unclear |
| `[RFQ:id]` | supplier quote → verification → scoring |
| `[NEG:id]`, session live | negotiation counter-offer → next round |
| `[NEG:id]`, session agreed, deal `PI_SENT` | PI acceptance → `CONFIRM_PI_ACCEPTANCE` |
| `[NEG:id]`, deal `CLEARED` / `PAID`, buyer | goods acceptance → `CONFIRM_BUYER_ACCEPTANCE`; complaint → dispute |
| unmatched / ambiguous | stored; `PARSE_INBOUND_EMAIL` for a human |

Idempotent on `Message-ID`; a manual submission endpoint exists for pasted mail.

## 17. When nothing happens — the deal watcher

`shared/orchestration/deal_watcher.py` sweeps every active deal each
`DEAL_WATCHER_INTERVAL_SECONDS`:

| State | Action |
|---|---|
| `PI_SENT` | buyer reminders; PI expiry → `deal.exception` |
| `ESCROW_PENDING` | buyer reminders (shortfall-aware); unfunded → `deal.exception` |
| `PO_ISSUED` / `SUPPLIER_DISPATCH_READY` | supplier reminders; overdue inspection → `deal.exception` |
| `IN_TRANSIT` | carrier-tracking poll every `TRACKING_POLL_HOURS` |
| `CLEARED` | acceptance-window fallback after `INSPECTION_WINDOW_FALLBACK_DAYS` |
| `MATCHING` / `NEGOTIATING` with an open funnel | outreach follow-ups; not counted as a stall |
| anything else unchanged `DEAL_STALL_SLA_HOURS` | `deal.exception` (`<STATUS>:stalled`) |

The negotiation agent has its own stale-session watcher; the escrow service has its own
window / reconciliation watcher.

## 18. When something breaks — exceptions and playbooks

Every `deal.exception` (a failed leg, a rejected gate, a watcher finding, a provider
dispute) is persisted `OPEN` by the Exception Resolver, then handled:

1. **Playbook fast path** (`shared/orchestration/playbooks.py`, no LLM): `QC_FAILED` →
   re-inspection proposal (or refund / replace supplier past the limit); `PAYMENT_FAILED` →
   shortfall reminder (auto), monitor inside the grace period, `cancel_unfunded` proposal
   after it; `NEGOTIATION_FAILED` → next buyer candidate (auto) or operator when exhausted;
   `DOCUMENT_ERROR` / `CONNECTOR_ERROR` → automatic retry.
2. **LLM plan** for everything else: category, severity, root cause, steps, decision
   (`AUTO_FIX` / `ESCALATE` / `CANCEL` / `MONITOR`).
3. Money-touching or deal-closing remediations run only through a `RESOLVE_EXCEPTION`
   approval whose payload names the playbook; the summary says what APPROVE will do.

Status lifecycle: `OPEN → ANALYZING → REMEDIATING → RESOLVED | ESCALATED | UNRESOLVABLE`.

## 19. What the operator sees

- **Approvals Inbox** — every waiting gate with its payload (the full draft e-mail, the
  tranche proof, the payout cap), risk flags, timeout countdown, four-eyes progress; live
  through SSE (`GET /hitl-approval/stream`).
- **Telegram** — every gate that waits for a human and every countersign request
  (`HITL_TELEGRAM_BOT_TOKEN` / `HITL_TELEGRAM_CHAT_ID`), with the deep link.
- **Daily digest** — gates by action and age, deals per stage, stalls, buyers awaited,
  open exceptions, money in escrow / released / paid / owed, Apify spend
  (`GET /hitl-approval/digest`, Telegram and/or `DIGEST_EMAIL_TO` at `DIGEST_HOUR_UTC`).
- **Deal Detail** — parties with enrichment, the buyer funnel, negotiation thread,
  logistics with milestones and tracking, documents, escrow with tranches and window,
  ledger and journal, timeline.
- **System Health** — `GET /health/readiness`: every stub / fallback as an ok / warn / fail
  check; `ENV_PROFILE=production` refuses to boot while a check fails.

## 20. Books, timeline, evidence

- `GET /escrow-guard/deals/{id}/journal` (`?format=csv`; `/journals` for all deals): the
  double-entry view derived from the hold and payouts — escrow funded, PO value (COGS /
  supplier payable), each tranche as net cash + provider fee + recognised revenue, refunds,
  supplier payouts — with a trial-balance check against the ledger.
- `GET /deals/{id}/timeline`: every dated fact from every service (status changes,
  outreach, RFQs and quotes, rounds, e-mails, gates and signatures, PI/PO, documents,
  orders and milestones, escrow events, payouts, exceptions), merged and sorted.
- `GET /deals/{id}/evidence-pack?format=json|html`: deal, parties, PI/PO, document hashes,
  orders, ledger + journal, approvals, exceptions, correspondence (bodies as SHA-256),
  timeline — and a SHA-256 over the whole pack. What a bank, insurer, auditor or dispute
  resolver asks for.

## 21. Running the whole loop locally

```bash
AUTH_PORTLESS_EMAILS=e2e-test@portless.local TRADE_MIN_MOCK_MODE=true \
  uvicorn app:app --port 8081
python scripts/e2e_money_chain.py
```

The script scans a mocked corridor, creates the deal, checks enrichment and the RFQ
gate, forces the negotiation to agreement, issues the PI, opens the escrow, funds it short
and then in full (replaying a webhook on the way), approves the PO and T1, passes QC,
approves T2 and freight, files customs, clears the cargo, approves T3, confirms the
window is still closed, records `DELIVERED`, sees the window open, takes the capped
supplier payout, accepts the goods as the buyer, settles the escrow, pays the balance and
finishes with the deal `PAID` and a reconciled ledger. `scripts/e2e_mock_flow.py` runs the
three scenario opportunities (strong, marginal, disrupted) through sourcing and
negotiation.

## 22. Configuration cheat-sheet

| Area | Variables |
|---|---|
| Persistence / bus | `MONGO_URI`, `MONGO_DB_NAME`, `EVENT_BUS_BACKEND`, `NATS_CREDS_FILE` |
| Identification | `APIFY_TOKEN`, `APIFY_ACTOR_OWNER`, `APIFY_MAX_RUNS_PER_DAY`, `APIFY_MAX_ITEMS`, `ENRICHMENT_ON_DEAL_CREATED`, catalogue via API |
| Outreach | `OUTREACH_ENABLED`, `OUTREACH_PARALLEL`, `OUTREACH_FOLLOWUP_DAYS`, `OUTREACH_NO_REPLY_DAYS` |
| Sourcing | `SOURCING_AUTO_SOURCE_VERIFIED`, `API_SETU_KEY` |
| Gates | `AUTONOMY_<ACTION>`, `_TIMEOUT_SECONDS`, `_ON_TIMEOUT`, `_AUTO_APPROVE_BELOW_USD`, `AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD`, `AUTONOMY_MONEY_CEILING`, `FOUR_EYES_THRESHOLD_USD`, `HITL_ON_FIRST_OUTBOUND` |
| Money | Tazapay / Escrow.com keys and webhook secrets, `PAYONEER_*`, `STRIPE_*`, `INSPECTION_WINDOW_TRIGGER`, `INSPECTION_WINDOW_FALLBACK_DAYS`, `ESCROW_FUNDING_GRACE_DAYS` |
| Operations | inspector keys (`SGS_*`, `QIMA_*`, `BUREAU_VERITAS_*`, `INTERTEK_*`, `TUV_SUD_*`), `COGOPORT_*`, `FREIGHTOS_*`, `ICEGATE_API_KEY`, `TRACKING_POLL_HOURS`, `QC_REINSPECTION_MAX` |
| Watcher | `DEAL_WATCHER_ENABLED`, `DEAL_*_REMINDER_AFTER_HOURS`, `DEAL_REMINDER_MAX`, `DEAL_STALL_SLA_HOURS` |
| Communication | `GMAIL_SENDER_ADDRESS`, `GMAIL_APP_PASSWORD`, `IMAP_*`, `PORTLESS_EMAIL_DOMAIN`, `DEAL_NOTIFICATIONS_ENABLED`, `HITL_TELEGRAM_*`, `DAILY_DIGEST_ENABLED`, `DIGEST_HOUR_UTC`, `DIGEST_EMAIL_TO`, `HITL_NOTIFY_WEBHOOK_URL` |
| Exceptions | `EXCEPTION_PLAYBOOKS_ENABLED` |
| LLM / market intel | LLM Gateway credentials, `NEGOTIATION_MARKET_SEARCH_ENABLED`, `TAVILY_API_KEY` |
| Readiness | `ENV_PROFILE`, `READINESS_ENFORCE` |

Full list with defaults and comments: `.env.example`.
