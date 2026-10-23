# PortLess — "Completely Automatic Merchant Trading" Gap Analysis

Date: 2026-10-19 · Updated: 2026-10-23 (§4.4 — identification on own Apify actors, money chain) · Scope: `backend/` (all services + `shared/orchestration/`), `ui/`
Companion docs: `PRODUCTION_TRACKER.md` (execution backlog), `CLAUDE.md` (architecture), `backend/workflow.md`

---

## 1. Verdict in one paragraph

The implementation is architecturally sound — a modular monolith with a code-enforced deal
state machine, durable NATS event bus, typed service boundaries, idempotent writes, HITL
gates with saga callbacks, and graceful connector stubs — and before today it could already
drive a deal from `NEGOTIATION_AGREED` to `PAID` with an operator approving three gates.
It was **not** "completely automatic". Walking every stage of the trade loop (section 3)
found eleven places where the deal parked silently or needed a human to copy data between
screens: nobody sent the RFQ, nobody emailed the PI or the escrow link to the buyer, nobody
emailed the PO to the supplier, every negotiation email was hard-coded to wait for a human,
a failed QC or failed negotiation went nowhere, documents and customs were never generated
in event mode, escrow tranches were never released, and the Autonomy Settings page saved
nothing. All eleven code gaps are now closed (section 4) and covered by 77 new tests; the
full suite is 747 passed with the same 9 pre-existing environment-dependent failures as
before. What remains (section 5) is mostly **not code**: a neutral escrow provider contract,
QC/freight/customs API credentials, a real email domain, and live buyer-price data. With
those in place and the autonomy policy set to L2, a deal runs from opportunity scan to
supplier payout with a human touching only money movement (capped at L1 by design).

---

## 2. What "completely automatic" means here

A merchant exporter's deal has seven actors: the scanner, the supplier, the buyer, the
inspector, the forwarder, customs, and the money. "Completely automatic" means PortLess
performs every action that is PortLess's to perform, *asks* each external actor for the one
thing only they can do (quote, accept, pay, inspect, ship, clear), *detects* their answer
without a human re-typing it, and *escalates* — rather than stalls — whenever an answer
does not come. Humans remain in the loop only where the operator chooses (autonomy policy)
or where money moves (hard ceiling).

Three design rules were kept throughout:

1. **Events move deals, not pollers.** Every leg is a durable-bus subscriber in
   `shared/orchestration/continuations.py`; the dev-only `auto_flow.py` is untouched and
   still defers to / is deferred to correctly.
2. **The pipeline creates gates, never approves them.** How autonomous the system is lives
   in one place — `autonomy_policy.py` — not scattered as `autonomy_level="L0"` literals.
3. **Counterparty actions stay counterparty actions.** Nothing fakes a buyer acceptance or
   a funding webhook; the system makes those actions *possible* (sends the PI, sends the
   payment link, reads the reply) and *noticed* (reminders, SLA exceptions).

---

## 3. Stage-by-stage audit of the trade loop

Legend — **Before**: state on 2026-10-19 before this work. **Now**: after this work.
`AUTO` = runs with no human · `GATE(Lx)` = HITL approval created at policy default `Lx`
(operator can set L2 to automate) · `EXT` = waits on an external party · `MANUAL` = still
needs an operator.

| # | Stage | Trigger | Before | Gap found | Now |
|---|---|---|---|---|---|
| 1 | Nightly scan → `opportunity.detected` | APScheduler | AUTO | — | AUTO |
| 2 | Opportunity → deal (`MATCHING`) | operator "Start Deal" | MANUAL | nothing subscribed to `opportunity.detected`; only the UI button created deals | `on_opportunity_detected` → `create_deal` (opt-in `AUTO_DEAL_FROM_OPPORTUNITY_ENABLED`; rank/confidence thresholds; simulated-source and duplicate guards). Default stays MANUAL on purpose until live demand data exists (P1-D1). |
| 3 | RFQ to suppliers | — | MANUAL (`POST /sourcing/rfq` by hand) | deal reached `MATCHING` and nothing asked a supplier anything | `on_deal_created` → `DISPATCH_RFQ` GATE(L2) → `dispatch_rfq()` to top-3 (SOURCE matches first, else opportunity seller candidates). Missing buyer target price derived as 1.25× cheapest FOB, persisted, logged as HEURISTIC. |
| 4 | Supplier quote → `NEGOTIATING` | `POST /sourcing/quotes` | AUTO only if Match Engine says SOURCE; otherwise parked forever | reliability leg defaults to 0 for any supplier without MCA/Udyam history → CMS < 75 → DISCARD → deal stuck in `MATCHING` with no signal | `on_quote_received` → `CONFIRM_SUPPLIER_SELECTION` GATE(L0; forced L0 + risk flags when GST/IEC verification failed) → callback records the match (`deals.record_match_result`) and moves the deal to `NEGOTIATING`. Supplier quote *intake by email* is still MANUAL (see 5.A-1). |
| 5 | Negotiation session opens (round 0 draft) | `negotiation.started` | AUTO | — | AUTO |
| 6 | Negotiation emails out | each round | GATE hard-coded **L0** regardless of deal profile | the single biggest "not automatic" point: every email, every round, waits for a click | GATE(policy) — `SEND_NEGOTIATION_EMAIL` via `resolve_level`. First outbound stays L0 while `hitl_on_first_outbound` is on; degraded/placeholder drafts always L0. |
| 7 | Replies in → next round | IMAP poll → Email Processor → `run_round` | AUTO (15-min poll) | — | AUTO. (IMAP IDLE / inbound webhooks still open — P3-3.) |
| 8 | Agreement confirmation | LLM intent + price guard | GATE hard-coded L0 | same as 6 | GATE(policy) — `CONFIRM_NEGOTIATION_AGREEMENT`. |
| 9 | Negotiation fails | stale watcher / max rounds | deal → `NEGOTIATION_FAILED`, nothing else | no exception, no re-match; deal dead silently | `on_negotiation_failed` → `deal.exception` (resolver) + `CONFIRM_SUPPLIER_SELECTION` re-match GATE for the next-best SOURCE candidate (callback: `NEGOTIATION_FAILED → MATCHING → NEGOTIATING`, new session opens with the new supplier). |
| 10 | PI issued | `negotiation.agreed` | AUTO | — | AUTO |
| 11 | PI delivered to buyer | — | **never** | PI rendered to Drive/disk and nobody told the buyer | `on_pi_sent` → templated PI email (`SEND_DEAL_NOTIFICATION`, default L2) in the same `[NEG:<id>]` thread. |
| 12 | Buyer accepts PI | `POST /dealmaker/pi/{id}/accept` | MANUAL (operator clicks on buyer's behalf) | buyer's email reply mapped to a terminal (AGREED) session → `UNMATCHED`; also `PARSE_INBOUND_EMAIL` was not a valid `ApprovalAction`, so *every* priceless inbound silently produced no approval | Email Processor now maps post-agreement replies; unconditional acceptance (`guards.is_explicit_acceptance`) → `CONFIRM_PI_ACCEPTANCE` GATE(L0) → `accept_pi()`; other replies → `PARSE_INBOUND_EMAIL` (now valid). EXT: buyer must reply. |
| 13 | Escrow opened | `pi.accepted` | AUTO | — | AUTO |
| 14 | Buyer told how to fund | — | **never** | `checkout_url` sat in Mongo | funding-instructions email (amount, provider, link, tranche schedule) after `create_escrow`. EXT: buyer pays. |
| 15 | Escrow funded | provider webhook | AUTO | — | AUTO |
| 16 | PO issued | `ISSUE_PO` gate → `issue_po` | GATE(deal profile) | — | GATE(policy, follows deal profile) |
| 17 | PO delivered to supplier | — | **never** | | `on_po_issued` → PO email to supplier. |
| 18 | Escrow tranche T1 (30 % on PO) | — | **never** (`release_tranche` existed, nothing called it) | money stuck in escrow until manual API call | `RELEASE_ESCROW_TRANCHE` GATE(L0, money ceiling) with PO id as proof. |
| 19 | QC booked | `po.issued` | AUTO | — | AUTO + booking email to supplier (`on_qc_booked`). |
| 20 | QC result | inspector webhook | AUTO | — | AUTO. EXT. |
| 21 | QC passed → documents | — | **only in dev auto-flow** | P3-1c open | CI/PL/CoO/SDF/Shipping Bill rendered in `on_qc_passed` (idempotent per deal × type). |
| 22 | Tranche T2 (40 % on QC) | — | never | | GATE(L0, money ceiling) with CRF hash + report URL as proof. |
| 23 | Freight booked | `BOOK_FREIGHT` gate | GATE(deal profile) | — | GATE(policy) + shipment email to buyer. |
| 24 | QC failed | inspector webhook | deal → `QC_FAILED`, nothing else | no DISPUTE, no exception, no refund path | `on_qc_failed` → `DISPUTE` + `deal.exception` (resolver escalates). `refund()` now transitions `DISPUTE → REFUNDED`. |
| 25 | Customs filing | — | **only in dev auto-flow** | `FILE_CUSTOMS` action + `file_cha()` existed; nothing created the gate | `on_freight_booked` → `FILE_CUSTOMS` GATE(policy) with rendered document ids attached. |
| 26 | Cargo cleared | freight webhook | AUTO | — | AUTO + arrival email to buyer. EXT. |
| 27 | Tranche T3 (20 % on clearance) | — | never | | GATE(L0, money ceiling) with shipping-bill refs as proof. |
| 28 | Supplier payout | `RELEASE_SUPPLIER_PAYOUT` gate | GATE(deal profile) | — | GATE(policy, money ceiling). |
| 29 | Paid → close-out | `payout.completed` | deal → `PAID`, realized margin patched | no completion notice (P3-1e) | `on_payout_completed` → completion emails to both parties. Ledger close/evidence pack still open (needs P1-6/P2-5). |
| 30 | Buyer silent (PI not accepted / escrow unfunded) | — | **nothing**; PI `expires_at` never acted on | deal could sit in `PI_SENT` forever | `deal_watcher.py`: reminders every 48 h (max 2), then `deal.exception` on PI expiry / unfunded escrow; any status stalled > 7 days → exception. |
| 31 | Autonomy Settings page | UI | saved to local state only ("backend not implemented") | | `GET/PUT /hitl-approval/autonomy-policy`, Mongo-persisted; page loads/saves real policy and shows where each level comes from. |

**Reading the table:** rows 2, 4, 12 and the three tranche rows are the places where a
human is still the default. All of them are policy-switchable (2 by config, the rest by the
autonomy policy) except money movement, which is capped at L1 — "ask, auto-approve on
timeout only if you explicitly configure `default_on_timeout=APPROVE`" — until the ledger
and four-eyes work in `PRODUCTION_TRACKER.md` P1-6 / P1-11 exists.

---

## 4. What was built (2026-10-19)

### 4.1 New modules (`backend/shared/orchestration/`)

| File | Responsibility |
|---|---|
| `autonomy_policy.py` | `resolve_level(action, deal)` — override store (Mongo `autonomy_policy` collection, in-memory fallback) → `AUTONOMY_<ACTION>` env → deal `autonomy_profile` (only for `ISSUE_PO`, `BOOK_FREIGHT`, `FILE_CUSTOMS`, `RELEASE_SUPPLIER_PAYOUT`, `RELEASE_ESCROW_TRANCHE`) → built-in default. `AUTONOMY_MONEY_CEILING` (default `L1`) clamps the two money-moving actions. `effective_policy()` powers the settings UI. |
| `notifications.py` | Nine templated, deterministic counterparty emails (`pi_issued`, `escrow_opened`, `po_issued`, `qc_booked`, `freight_booked`, `cargo_cleared`, `deal_completed`, `pi_reminder`, `escrow_reminder`). Each is recorded as an `EmailHistoryRecord` on the deal's negotiation (visible in the Negotiation Inbox), gated by `SEND_DEAL_NOTIFICATION`, and sent through the existing `send_approved_email` → Email Gateway path, so replies thread back. Idempotent on `(kind, deal, suffix)`. Never raises. |
| `deal_watcher.py` | Background sweep (default every 10 min): `PI_SENT` → reminders / PI-expiry exception; `ESCROW_PENDING` → reminders / unfunded exception; any other non-terminal status → stall exception past `DEAL_STALL_SLA_HOURS`. `NEGOTIATING` is left to the negotiation agent's own stale watcher. |

### 4.2 Modified modules

| File | Change |
|---|---|
| `shared/domain/enums.py` | `ApprovalAction` += `DISPATCH_RFQ`, `CONFIRM_SUPPLIER_SELECTION`, `CONFIRM_PI_ACCEPTANCE`, `RELEASE_ESCROW_TRANCHE`, `SEND_DEAL_NOTIFICATION`, `PARSE_INBOUND_EMAIL` |
| `shared/orchestration/continuations.py` | 9 new legs (`on_opportunity_detected`, `on_deal_created`, `on_quote_received`, `on_negotiation_failed`, `on_pi_sent`, `on_qc_booked`, `on_qc_failed`, `on_freight_booked`, `on_payout_completed`); `on_pi_accepted`, `on_po_issued`, `on_qc_passed`, `on_customs_cleared` extended with notifications / documents / tranche gates; every gate level from the policy; secondary effects wrapped in `_best_effort` so they never block the primary step |
| `services/hitl_approval/callbacks.py` | 6 new approve callbacks, soft-reject map now owned here (`SOFT_REJECT_ACTIONS`); `RELEASE_ESCROW_TRANCHE` is a hard reject |
| `services/hitl_approval/router.py` | `GET/PUT /hitl-approval/autonomy-policy` |
| `services/hitl_approval/service.py` | soft-reject set imported from callbacks |
| `agents/negotiation_agent/service.py` | `_email_gate_level()` / `_resolve_gate_level()` replace the two hard-coded `"L0"` literals |
| `services/email_processor/mapper.py`, `service.py`, `schemas.py` | post-agreement mapping (`MappingResult.session_status`), `_handle_post_agreement_reply`, `_create_pi_acceptance_hitl` |
| `services/deals/service.py` | `record_match_result()` (upsert supplier into `match_results`, optionally make winner, fill buyer target if empty) |
| `services/escrow_guard/service.py` | `refund()` → `DISPUTE → REFUNDED` |
| `shared/config.py` | `auto_deal_*` settings |
| `app.py` | loads the policy at startup; starts/stops the deal watcher |
| `ui/src/pages/AutonomySettings.tsx`, `ui/src/api/endpoints.ts` | page wired to the backend; shows effective level + source, money badge, per-deal-profile badge, unsaved-change state |
| `CLAUDE.md`, `PRODUCTION_TRACKER.md`, `backend/.env.example` | documentation of all of the above |

### 4.3 Tests

| File | Covers |
|---|---|
| `tests/test_autonomy_policy.py` (17) | defaults, env, runtime override + clear, deal profile, money ceiling, `effective_policy` sources, every `ApprovalAction` has a class |
| `tests/test_notifications.py` (9) | draft → gate → auto-send (log-only SMTP) → `SENT`; idempotency; L0 keeps pending; no session/address; disabled; templates |
| `tests/test_automation_continuations.py` (35) | each new leg, gate ordering (freight/payout gate still last so existing assertions hold), money ceiling on tranche gates, opportunity guards, registration, all six callbacks incl. the `NEGOTIATION_FAILED → MATCHING → NEGOTIATING` re-match |
| `tests/test_email_processor_post_agreement.py` (7) | acceptance → `CONFIRM_PI_ACCEPTANCE`; conditional / wrong-stage / supplier replies → operator; FAILED stays unmatched; live session still runs a round; `PARSE_INBOUND_EMAIL` regression |
| `tests/test_deal_watcher.py` (10) | reminder cadence + cap, PI expiry, missing records, escrow unfunded, generic stall, skips, disabled, fault isolation |

Baseline before: 670 passed / 9 failed. After: 747 passed / 9 failed — the same nine
(`tests/llm_gateway/test_config.py` ×3, `tests/llm_gateway/test_fallback.py` ×3,
`tests/test_llm_response_cache.py` ×2, `tests/test_rag_backends.py` ×1), all asserting on
live `.env` keys or a reachable Mongo and already listed under O-3 in the tracker.

### 4.4 Second pass (2026-10-23) — identification on PortLess's own actors, money chain without gaps

| Area | What changed | Closes |
|---|---|---|
| Buyer / seller identification | `shared/connectors/apify.py` — the PortLess-generated Apify actors (`rich_minds`) are the named-buyer, demand-benchmark, supplier, contact, screening and e-mail-check sources (details: `portless_apify_actors.md` §8). Trade-Min Phases 1/2/4 call them next to the free APIs; `GET /trade-min/sources` reports what is live. Seller prices normalised to USD/MT. | A-2 (real buyer price per market), §5.B "live buyer data" (per corridor), P1-D2 |
| Counterparty enrichment | `shared/orchestration/enrichment.py` + `Party` contact/screening fields; runs at `deal.created`, before the PI (`CONFIRM_COUNTERPARTY_RISK` on review, PI withheld on block), on demand (`POST /deals/{id}/enrich`); operator corrections via `PATCH /deals/{id}/parties/{role}`. | E-2/E-3/E-9/E-10; the `@portless.placeholder` first e-mail |
| RFQ by e-mail | `sourcing._dispatch_by_email` (`[RFQ:<id>]`) + Email Processor `_handle_rfq_reply` → `receive_quote_by_email`. | A-1 |
| Escrow funding | provider total below the PI → `PARTIALLY_FUNDED`, deal stays `ESCROW_PENDING`, shortfall e-mail + exception; replay-safe webhooks (`event_id`), Stripe signature window. | P1-5 (replay), P1-9 (partial) |
| Tranche release | strictly in schedule order; blocked in `DISPUTE`/`CANCELLED`/`REFUNDED`/`QC_FAILED`; provider fee accrued; `escrow.tranche_released` / `escrow.settled` events; `refund` refuses when nothing is left. | A-3 (one model: the tranche ledger), §5.C partial |
| Supplier payout | gate amount capped at `available_for_payout` (net released − payouts), `total_owed` = PO value; `PAID` only when the supplier is paid in full; balance payout follows the final tranche; a payout never flips a staged escrow; realised margin = net received − payouts. | A-3 (the 90 % released vs 92 % owed cash gap) |
| T4 handshake | `inspection_window_open` e-mail; "we accept the goods" → `CONFIRM_BUYER_ACCEPTANCE` gate → `accept_escrow`; silence still auto-releases. | A-4 |
| Ledger | `GET /escrow-guard/deals/{id}/ledger` + `/ledgers`: every number the UI shows + reconciliation incl. `payouts ≤ net received`. | towards P1-6 |
| Readiness | `shared/readiness.py`, `GET /health/readiness`; `ENV_PROFILE=production` refuses to boot on a failing check. | §5.C "production config gate", P1-1 |
| UI | Payments tab (escrow / tranches / ledger / payouts / accept / dispute / refund), Overview party contact + screening + edit + enrich, Radar sources + simulated badge + benchmark, Money page ledgers, System Health readiness, HITL money details. | UI-1 (simulated badge) |

Still manual / external after this pass: generating `india-supplier-finder-ai`;
an Apify plan that may run the Store actors live; the commercial prerequisites
in §5.B; four-eyes + double-entry ledger (§5.C) before the money ceiling moves.

---

## 5. Remaining gaps

### 5.A Code gaps (automation still incomplete)

| # | Gap | Why it matters | Suggested fix | Tracker ref |
|---|---|---|---|---|
| A-1 ✅ 2026-10-23 | **Supplier quote intake by email.** RFQs go out through the IndiaMART/TradeIndia connectors (portal fallback otherwise); a supplier's emailed quote is not mapped to the RFQ and must be entered via `POST /sourcing/quotes`. | Stage 4 is the last stage where an operator re-types counterparty data. | Send RFQs by email too (reuse `notifications.py` with an `[RFQ:<rfq_id>]` subject token), add an RFQ branch to the Email Processor mapper, extract price/quantity/GSTIN with the existing extractor + LLM, call `receive_quote`. | P3-5 |
| A-2 ✅ 2026-10-23 (markets the export-market actor covers) | **Buyer price heuristic.** None of the live connectors populate `BuyerSignal.unit_price_usd`, so the 1.25× FOB heuristic (pre-existing in `auto_flow`, now also in the RFQ leg, loudly logged) silently defines the margin corridor. | Margin guard, CMS, and the negotiation corridor are all anchored on a made-up number. | Buyer target from a real source (Volza/ImportGenius manifests, buyer RFQ price, or operator input at deal start); refuse `AUTO_DEAL_FROM_OPPORTUNITY` when no real price exists. | P1-D1, P1-7 |
| A-3 ✅ 2026-10-23 | **Legacy vs neutral escrow models coexist.** `request_payout()` (Stripe hold + Payoneer) marks the whole hold `RELEASED`; `release_tranche()` (Tazapay/Escrow.com staged) tracks per-tranche. Both are reachable; the continuation opens the neutral one, the payout gate still uses the legacy function. | Ledger can read released = 100 % while tranches say 90 %. | Decide the production model (tracker says neutral), route `RELEASE_SUPPLIER_PAYOUT` through the tranche ledger, and build the double-entry ledger. | P1-4, P1-6 |
| A-4 ✅ 2026-10-23 | **T4 (buyer acceptance) release** relies on the buyer calling `accept_escrow` or the inspection window lapsing. No notification tells the buyer the window is open. | Final 10 % and PortLess margin wait on silence. | Add `inspection_window_open` notification when T3 releases. | — |
| A-5 | **Deal watcher dedupes exceptions in-process only.** After a restart a still-stuck deal is reported once more. | Duplicate exceptions in the resolver after redeploys. | Persist `(deal, kind)` on the deal record or check the resolver for an open exception with the same stage. | X-10 |
| A-6 | **Continuation consumers run only in the monolith.** Standalone service mode publishes durably but nothing consumes. | Per-service deployment loses automation. | Dedicated consumer worker (documented in O-5). | O-5 |
| A-7 | **Outbox gap.** Mongo write and event publish are not atomic (pre-existing). | A crash between them loses the trigger; the deal watcher now catches the *stall* 7 days later, not the lost event itself. | Transactional outbox. | P3-1a |
| A-8 | **Email transport is poll-based (15 min) and single-mailbox.** | Latency per round; buyer/supplier replies share one inbox. | IMAP IDLE or SendGrid/Mailgun inbound webhooks (verification code exists as placeholders). | P3-3, P1-9 |
| A-9 | **No attachments on outbound notifications.** The PI/PO email carries a Drive link when the document store is Drive, otherwise just the data. | Buyers expect a PDF. | Add attachment support to `SendEmailRequest`/sender; PDF rendering. | P3-3 |
| A-10 | **Exception Resolver auto-fix coverage** is `DOCUMENT_ERROR` and `CONNECTOR_ERROR` only; `QC_FAILED`, `NEGOTIATION_FAILED`, `PAYMENT_FAILED` always escalate. | Every negative path still ends at a human. | Add playbooks per category (re-inspection booking, re-match — now one gate away —, payout retry). | P4 |

### 5.B External / commercial prerequisites (block "real" automation regardless of code)

| Prerequisite | Without it | Tracker ref |
|---|---|---|
| Neutral escrow provider (Tazapay / Escrow.com) KYB + sandbox + webhook secrets | `create_escrow` returns a stub; `checkout_url` in the buyer email is fake; nothing can be funded | P1-4 |
| QC provider API keys (QIMA first — the only public API) | bookings are stubs; QC webhook never arrives; the pipeline parks at `SUPPLIER_DISPATCH_READY` until the deal watcher raises a stall | Phase 7 |
| Freight (Cogoport / Freightos) keys | booking stub; no milestone webhook → never `CLEARED` | Phase 7 |
| CHA / ICEGATE registration (needs IEC + AD code) | `file_cha` stub LEO | B-6, B-7, P2-1 |
| Own email domain + SPF/DKIM/DMARC + transactional provider | notifications leave from a personal Gmail app password; deliverability and trust are poor; per-deal reply-to impossible | B-9, P1-8 |
| Live buyer data (Comtrade/Census keys at minimum, manifest data ideally) | opportunities fall back to SHA-256 "simulated" numbers (blocked from auto-deal by design) | P1-D1a |
| Legal entity + IEC + GST + bank AD code | PI/PO carry placeholder entity details; exports cannot legally ship | Phase B |

### 5.C Control gaps to close before raising the money ceiling

- Double-entry ledger with invariant checks and auto-freeze (P1-6).
- Four-eyes approval for payout / tranche release above a value threshold, MFA for
  approvers (P1-11). The money ceiling is the stop-gap.
- Production config gate that refuses to boot with stub connectors, log-only email, or
  in-process bus (P1-1). Today the pipeline would happily "complete" a deal against stubs.
- Replay protection and mandatory secrets on every money webhook (P1-9).
- Cross-document consistency check before T2/T3 release (P2-3) — the tranche gates attach
  proof refs but nothing verifies PI ↔ PO ↔ CI ↔ CRF quantities yet.

### 5.D Known limitations of today's additions

- `PI_SENT`, `PO_ISSUED`, `QC_PASSED` and `CUSTOMS_CLEARED` arrive twice (owning service
  emit + Deals status emitter). All legs are idempotent (approval idempotency keys,
  notification ids, deal-status guards), but the second firing costs a few lookups.
- Document generation at `QC_PASSED` uses deal defaults for packaging/weights/vessel
  fields; the Shipping Bill is re-rendered at `freight.booked` only if missing, so vessel
  data entered later does not back-fill earlier documents (P2-3 / X-8 territory).
- `CONFIRM_SUPPLIER_SELECTION` on a supplier not scored by the Match Engine stores a
  `MatchResult` with zero component scores and the quote price as FOB — enough to open the
  negotiation corridor, not a real CMS.
- The opportunity → deal leg stamps `AUTO_DEAL_ORG_ID` (default `portless`); multi-tenant
  deployments must set it per tenant or keep the leg off.

### 5.E Lower-cost buyer and supplier data — Apify actors

Moved to [`portless_apify_actors.md`](portless_apify_actors.md): core data sources that
replace paid connectors (C-1 to C-6), actors needed to close automation gaps (E-1 to
E-11), limits, recommended use, how they connect through one `ApifyActorConnector`, and
the questions for the actor generator. Verified on the Apify Store 2026-10-22.

---

## 6. Operating recipe — run one deal hands-off (dev, stubs)

```
# backend/.env
EVENT_CONTINUATION_ENABLED=true
DEAL_NOTIFICATIONS_ENABLED=true
DEAL_WATCHER_ENABLED=true
AUTONOMY_SEND_NEGOTIATION_EMAIL=L2
AUTONOMY_CONFIRM_NEGOTIATION_AGREEMENT=L2
AUTONOMY_CONFIRM_PI_ACCEPTANCE=L2
AUTONOMY_CONFIRM_SUPPLIER_SELECTION=L2
AUTONOMY_ISSUE_PO=L2
AUTONOMY_BOOK_FREIGHT=L2
AUTONOMY_FILE_CUSTOMS=L2
HITL_ON_FIRST_OUTBOUND=false          # otherwise round-0 email still waits for a click
# money stays at the ceiling (L1) — approvals auto-REJECT on timeout unless default_on_timeout=APPROVE
```

Event chain you will see in the logs (stubs fire no webhooks, so steps marked ⏸ need the
`e2e_mock_flow.py`-style webhook calls or real providers):

```
opportunity.detected ─(AUTO_DEAL_* on)─▶ deal.created(MATCHING)
  ─▶ DISPATCH_RFQ (L2) ─▶ quote.received ─▶ NEGOTIATING ─▶ negotiation.started
  ─▶ round-0 drafts (L2 → sent) ─▶ replies via IMAP ─▶ rounds ─▶ CONFIRM_NEGOTIATION_AGREEMENT (L2)
  ─▶ negotiation.agreed ─▶ PI issued ─▶ pi.sent ─▶ PI email to buyer
  ⏸ buyer replies "we accept" ─▶ CONFIRM_PI_ACCEPTANCE (L2) ─▶ pi.accepted ─▶ escrow opened + funding email
  ⏸ escrow-funded webhook ─▶ ISSUE_PO (L2) ─▶ po.issued ─▶ PO email + T1 tranche gate (L1) ─▶ QC booked + email
  ⏸ QC PASS webhook ─▶ documents rendered + T2 gate (L1) + BOOK_FREIGHT (L2) ─▶ freight.booked
  ─▶ FILE_CUSTOMS (L2) + shipment email ⏸ ARRIVED webhook ─▶ CLEARED ─▶ T3 gate (L1) + arrival email
  ─▶ RELEASE_SUPPLIER_PAYOUT (L1) ⏸ payout COMPLETED webhook ─▶ PAID ─▶ completion emails
```

Every ⏸ is an external actor; the deal watcher turns a missing one into reminders and then
a `deal.exception` instead of silence.

---

## 7. Recommended order of work

1. **Commercial unblockers first** (5.B): escrow KYB, QIMA account, email domain — the
   code is waiting on them, not the reverse.
2. **Supplier quote by email** (A-1) — the last manual data-entry stage.
3. **Real buyer price** (A-2) before enabling `AUTO_DEAL_FROM_OPPORTUNITY_ENABLED` —
   cheapest route is the ImportYeti / Alibaba RFQ Apify actors in 5.E.
4. **Ledger + four-eyes** (5.C) — then, and only then, raise `AUTONOMY_MONEY_CEILING`.
5. **Resolver playbooks** (A-10) so negative paths self-heal where safe.
6. Outbox + consumer worker (A-6, A-7) when moving off the single-process monolith.

---

## Appendix A — New HITL actions

| Action | Created by | Callback | Reject |
|---|---|---|---|
| `DISPATCH_RFQ` | `on_deal_created` | `sourcing.dispatch_rfq` | soft |
| `CONFIRM_SUPPLIER_SELECTION` | `on_quote_received`, `on_negotiation_failed` | `deals.record_match_result` + status → `NEGOTIATING` | soft |
| `CONFIRM_PI_ACCEPTANCE` | Email Processor (post-agreement reply) | `dealmaker.accept_pi` | soft |
| `RELEASE_ESCROW_TRANCHE` | `on_po_issued` / `on_qc_passed` / `on_customs_cleared` | `escrow_guard.release_tranche` | **hard** |
| `SEND_DEAL_NOTIFICATION` | `notifications.notify_party` | `negotiation_agent.send_approved_email` | soft |
| `PARSE_INBOUND_EMAIL` | Email Processor | no-op | soft |

## Appendix B — Event → handler map (`register_continuations`)

`OPPORTUNITY_DETECTED`, `DEAL_CREATED`, `QUOTE_RECEIVED`, `NEGOTIATION_STARTED`,
`NEGOTIATION_FAILED`, `NEGOTIATION_AGREED`, `PI_SENT`, `PI_ACCEPTED`, `ESCROW_FUNDED`,
`PO_ISSUED`, `QC_BOOKED`, `QC_PASSED`, `QC_FAILED`, `FREIGHT_BOOKED`, `CUSTOMS_CLEARED`,
`PAYOUT_COMPLETED`, `DEAL_EXCEPTION` — one durable NATS consumer each in the monolith.

## Appendix C — New environment variables

`AUTONOMY_<ACTION>`, `AUTONOMY_MONEY_CEILING`, `DEAL_NOTIFICATIONS_ENABLED`,
`DEAL_WATCHER_ENABLED`, `DEAL_WATCHER_INTERVAL_SECONDS`, `DEAL_PI_REMINDER_AFTER_HOURS`,
`DEAL_ESCROW_REMINDER_AFTER_HOURS`, `DEAL_REMINDER_MAX`, `DEAL_STALL_SLA_HOURS`,
`AUTO_DEAL_FROM_OPPORTUNITY_ENABLED`, `AUTO_DEAL_MIN_ARBITRAGE_RANK`,
`AUTO_DEAL_MIN_CONFIDENCE`, `AUTO_DEAL_OPERATOR_ID`, `AUTO_DEAL_ORG_ID`,
`AUTO_DEAL_AUTONOMY_PROFILE`. All documented with defaults in `backend/.env.example`.
