# PortLess Production Task List

Created: 2026-07-17 · Source analysis: [`PRODUCTION_READINESS_GAPS.md`](PRODUCTION_READINESS_GAPS.md)

This is the **execution backlog**: every task needed to run PortLess in the real
market — connecting real buyers, sellers, QC agencies, freight/customs partners
and money custody — earning margin continuously, with minimal human
intervention, surviving every exception and negative scenario. Work top-down;
each phase unlocks the next. Status legend: `[x]` done · `[~]` partial · `[ ]` open.

---

## ⛔ PHASE 0 — Security emergency (do this TODAY, before anything else)

- [ ] **SEC-1 — Rotate the leaked MongoDB Atlas credential.** `backend/.env`
  containing the live `MONGO_URI` (username + password to the Atlas cluster)
  is **committed to git and pushed to `github.com/RouteToRich/portless`
  (origin/main)**. Anyone with repo access — and everyone, if the repo is or
  ever becomes public — can read/write the production database.
  1. In MongoDB Atlas: create a new database user + password; delete the old
     user; restrict network access (IP allowlist) while at it.
  2. `git rm --cached backend/.env` and commit, so the file stops being tracked
     (it is already gitignored going forward).
  3. Purge it from history (`git filter-repo --path backend/.env --invert-paths`
     or BFG) and force-push, or accept that the old (now-rotated) secret stays
     in history.
  4. Audit the cluster for unexpected access/collections.
- [ ] **SEC-2 — Secret scan the whole history** (gitleaks/trufflehog) for any
  other committed keys (Groq, Tavily, Google Drive refresh token, SMTP app
  password). Rotate anything found.
- [ ] **SEC-3 — Move runtime secrets out of files** — deploy with a secret
  manager (Doppler/Vault/cloud-native) or environment injection; `.env` files
  remain for local dev only.

---

## ✅ Recently completed (2026-07-16 → 17) — context for what follows

- [x] Top-5 buyer-selectable QC providers (SGS, QIMA, BV, Intertek, TÜV SÜD) with
  catalog + cost-quote endpoints, per-provider key placeholders, HMAC webhook schemes.
- [x] QC cost quoted into the negotiation margin floor from round 0; re-quoted on buyer opt-in.
- [x] Buyer's QC choice captured from email (classifier + name scan); several named
  acceptable agencies → cheapest among them; never opted → cheapest at booking.
- [x] Freight quoted into the margin floor (`/shield-link/freight/quote` → `logistics_cost`).
- [x] Agreement completeness checklist — no `AGREED` until QC choice + quantity received;
  agent emails the buyer for missing items instead of closing.
- [x] Deal keeps `opportunity_id`, full `match_results`, winner + CMS after the
  opportunity leaves the radar; Deal Board shows CMS + matched count (depth gap → UI-1).

---

## PHASE 1 — Go-live blockers (no real-money trade before ALL of these)

### Config & persistence

- [ ] **P1-1 Production config gate** — startup readiness check that **refuses to boot
  in production profile** if any of: Mongo unreachable, any `*_WEBHOOK_SECRET` unset,
  email in log-only mode, `TRADE_MIN_MOCK_MODE`/`AUTO_FLOW_ENABLED` on, document store
  on local-disk fallback, LLM gateway keyless. Add `ENV_PROFILE=dev|staging|production`.
- [ ] **P1-2 Durable Mongo everywhere** — single production cluster + per-service
  collections verified for all 13 stateful modules; in-memory fallback allowed only in dev/CI.
  Fix the known **Motor/event-loop fragility** seen when tests run against live Mongo
  (client bound to a dead loop) — create clients per-loop or at app lifespan.
- [ ] **P1-3 Backups & restore drill** — Atlas continuous backup + a tested restore runbook.

### Money custody (the earning path itself)

- [ ] **P1-4 Neutral escrow provider onboarding** — Tazapay / Escrow.com KYB, sandbox,
  webhook secrets, corridor coverage (India → target markets), fee schedule confirmed.
  The current Stripe-hold path keeps buyer money in PortLess's own account — not
  acceptable to real buyers and a regulatory risk.
- [ ] **P1-5 Auto-wire the money chain** — `PI_ACCEPTED` → escrow create;
  `funded` webhook → `ESCROW_FUNDED` → PO gate; `qc.passed` + customs proof →
  tranche release; `CLEARED` → payout gate; refund → `REFUNDED`. All idempotent,
  all signature-verified, all replay-protected (timestamp window + event-id dedupe).
- [ ] **P1-6 Double-entry ledger** — every hold/release/refund/payout/fee/FX row;
  continuous invariant check (held = released + refunded + outstanding); auto-freeze
  money actions on reconciliation mismatch.
- [ ] **P1-7 Complete the cost model in pricing** — margin formula now covers
  freight + QC, but **escrow fees, payment/FX spread, insurance, duties/taxes and
  a risk reserve are still missing**. Add them to `CostEnvelope`/quote seeding so a
  deal can never be agreed at a real-world loss.

### Communication & identity

- [ ] **P1-8 Production email domain + deliverability** — own domain, SPF/DKIM/DMARC,
  transactional provider (SES/SendGrid/Postmark) instead of a personal Gmail app
  password; per-deal reply-to addresses (`deal-<id>@…`) so inbound mapping stops
  depending on free-text matching.
- [ ] **P1-9 Implement SendGrid ECDSA webhook verification** (currently placeholder)
  and Mailgun signing check; reject unsigned inbound in production.
- [ ] **P1-10 Legal entity + terms** — merchant-exporter model, IEC/GST/AD code,
  buyer T&Cs, supplier T&Cs, dispute/cancellation policy, privacy policy. Without
  signed terms, the CRF/escrow chain has no contractual force.

### Access control

- [ ] **P1-11 Roles + four-eyes** — operator roles per action class; two-person
  approval for payout, refund, margin-below-policy, deal value > threshold; MFA for
  approver accounts; full approval audit export.
- [ ] **P1-12 Rate limiting + CORS lockdown** on auth, gateway, webhooks, negotiation,
  email endpoints.

---

## PHASE 2 — Proof & document integrity (trust chain for money release)

- [ ] **P2-1 Real ICEGATE/CHA filing** — replace stub; verify Shipping Bill + LEO at
  source; store document hashes.
- [ ] **P2-2 Digital CRF verification** — beyond webhook HMAC: issuer signature/QR/
  portal check, archive report PDF + photos on the TPO (QIMA report API first —
  the only public one).
- [ ] **P2-3 Cross-document consistency check before any release** — PI ↔ PO ↔ CI ↔
  PL ↔ BL ↔ Shipping Bill ↔ QC report: quantities, HS code, parties, ports,
  container/seal numbers must match or the deal freezes.
- [ ] **P2-4 Tamper evidence** — immutable hash + timestamp + signer on every document
  version; Google Drive is primary store — kill the local-disk fallback in production.
- [ ] **P2-5 Evidence pack generator** — one click: PI, PO, email thread, QC report +
  photos, BL, shipping bill, ledger extract, provider callbacks — for any dispute.

---

## PHASE 3 — Automation backbone (earning with less human intervention)

- [~] **P3-1 Real event bus** — *all six continuation legs done 2026-07-18*:
  `InProcessEventBus` (`shared/bus/publisher.py`) wired in `app.py`;
  `shared/orchestration/continuations.py` subscribes (each idempotent,
  deal-state guarded, `deal.exception` on failure; `EVENT_CONTINUATION_ENABLED`):
  - `NEGOTIATION_AGREED` → issue PI (deal → PI_SENT)
  - `PI_ACCEPTED` → open neutral escrow sized from the PI, carrying the
    buyer-opted QC provider (deal → ESCROW_PENDING; funding stays a buyer action)
  - `ESCROW_FUNDED` → create `ISSUE_PO` HITL gate (operator still approves)
  - `PO_ISSUED` → book QC with the buyer-opted inspector
  - `QC_PASSED` → create `BOOK_FREIGHT` HITL gate
  - `CLEARED` → create `RELEASE_SUPPLIER_PAYOUT` HITL gate
  Money/commitment steps remain HITL-gated — continuations create the gates,
  never approve them. **Pending sub-tasks:**
  - [ ] **P3-1a Durable event log / transactional outbox** — the in-process bus
    loses a trigger if the process dies between the state change and the handler
    running; persist events (Mongo `events` collection) with an outbox relay +
    replay of unhandled events at startup, so continuations survive restarts.
  - [ ] **P3-1b Failure-path handlers** — subscribe `QC_FAILED`,
    `NEGOTIATION_FAILED` (and later `PAYOUT_FAILED`/refund events) to the
    Exception Resolver (`raise_exception`) so negative outcomes get triaged
    automatically instead of parking the deal.
  - [ ] **P3-1c Document-generation leg** — on `QC_PASSED` (parallel to the
    freight gate), generate CI/PL/CoO/SDF/Shipping Bill via DocuGen and file
    CHA, replacing the auto-flow-only document step.
  - [ ] **P3-1d Handler retry / dead-letter** — a failed continuation currently
    only emits `deal.exception`; add bounded retry with backoff and a
    dead-letter record with a one-click "re-run continuation" operator action.
  - [ ] **P3-1e Payout-completed leg** — on `PAYOUT_COMPLETED`, verify deal
    reached `PAID`, close out the ledger entry, and archive the deal evidence
    pack (depends on P1-6 ledger, P2-5 evidence pack).
- [ ] **P3-2 Retry + dead-letter for connector calls** — exponential backoff jobs;
  operator "manual retry" UI reusing idempotency keys; today a `ConnectorError`
  just surfaces and the deal stalls.
- [~] **P3-3 Near-real-time email + extraction safety** — *price plausibility guard
  done 2026-07-17*: an extracted counter-price outside ~3× of the negotiation
  corridor never becomes the working bid/ask and flags the round for HITL
  (`guards.is_price_plausible`, `NEGOTIATION_PRICE_BAND_FACTOR`; operator override
  bypasses) — directly prevents the real $102,123.71-vs-$1,255 incident. Still
  open: IMAP IDLE / inbound webhooks (15-min polling), outbound retry + bounce
  ingestion, PDF/Excel attachment price parsing, multi-currency/unit normalization.
- [~] **P3-4 Negotiation guardrails for autonomy** — *done 2026-07-17*: LLM-fallback
  template drafts and drafts with unresolved `[placeholders]` are labeled on the
  HITL approval (risk flags `degraded_llm_fallback` / `unresolved_placeholders` +
  "⚠ edit before sending" summary prefix); factual-grounding prompt rules (no
  invented product names/certifications, no bracketed placeholders, HS-code
  grounding). Still open: prompt regression suite in CI, prompt+version stamping
  per sent email, detecting non-price conditions in acceptance replies (delivery
  deadline, packaging, incoterm change).
- [ ] **P3-5 Sourcing fan-out** — multi-supplier RFQ dispatch, auto best-quote selection,
  verification retry/backoff, supplier denylist + performance score; block selection on
  degraded verification without explicit override.
- [ ] **P3-6 Freight comparison + booking automation** — fan out quote to Cogoport +
  Freightos (+ SeaRates later), auto-select by price/ETA/reliability, quote-expiry
  revalidation before booking.
- [ ] **P3-7 Shipment visibility** — Vizion or Terminal49 connector pushing normalized
  DCSA milestones into `freight/webhook`; ETA-exception detection (rolled container,
  transshipment delay, customs hold, demurrage).
- [ ] **P3-8 Container Loading Check** — CLC inspection type on the QC path (stuffing
  photos + seal numbers) so visual evidence covers the shipping leg.
- [ ] **P3-9 Autonomy policy engine** — per-action L0–L3 levels driven by deal value,
  margin headroom, counterparty history, corridor risk — this is what actually reduces
  human intervention safely over time.

---

## PHASE 4 — Exception & negative-scenario coverage

The full scenario inventory (~150 items) lives in `PRODUCTION_READINESS_GAPS.md` §P2.
Execute it as ten workstreams, each delivering: detection → auto-freeze rule →
operator playbook (owner, SLA, safe default) → compensating action → test.

- [ ] **X-1 Market-data failures** — stale/duplicate/fake opportunities, wrong HS code,
  tariff API fail-open, FX outliers. Deliver: freshness SLAs, duplicate detection,
  stale-data guard before deal creation.
- [ ] **X-2 Negotiation failures** — out-of-order replies, unmapped senders, quoted-price
  extraction errors, phishing on a valid token, LLM bad output, operator mis-edit.
  Deliver: session locks (exist) + reply-token addressing + extraction confidence gates.
- [ ] **X-3 Supplier failures** — post-match verification failure, bank-account change
  after PO (fraud signal → auto-freeze), cancellation after escrow funded, capacity miss.
- [ ] **X-4 Contract/pricing failures** — PI accepted after validity expiry, PI/PO field
  drift, margin guard bypass when buyer price missing, FX move breaking margin.
- [ ] **X-5 Escrow/payout failures** — partial/wrong-amount funding, provider outage
  mid-release, release-succeeded-locally-failed (and inverse), chargebacks, payout
  sanctions blocks. Deliver: reconciliation job + freeze-money action.
- [ ] **X-6 QC failures** — provider unavailable in supplier city, inspector no-show,
  ambiguous/conditional report, webhook-says-pass-PDF-says-fail, report/deal mismatch,
  buyer disputes after pass, buyer changes provider after PI (re-quote + PI amendment flow).
- [ ] **X-7 Freight/customs failures** — vessel cutoff missed, rolled container, seal
  mismatch, ICEGATE rejection, destination hold, buyer refuses delivery/goes silent
  after arrival.
- [ ] **X-8 Document failures** — stale data rendered, upload failure, wrong-org access,
  cross-document mismatch, missing product-specific certificates (CoO, phyto, fumigation).
- [ ] **X-9 Security/fraud** — replayed webhooks, prompt injection via inbound email into
  RAG/drafting context, stolen operator token, org-scoping leaks.
- [ ] **X-10 Concurrency/integrity** — crash between provider call and DB write (both
  orders), double approval, concurrent transitions, Mongo-write-ok-event-publish-fail.
  Deliver: outbox pattern + "reprocess from checkpoint" for stuck deals.

Cross-cutting deliverables for all workstreams:

- [ ] **X-11 Freeze framework** — `freeze deal` (blocks sends/PO/booking/release/filing)
  and `freeze money` (blocks only release/refund/payout) as first-class deal flags with
  unfreeze checklists.
- [ ] **X-12 Compensating actions** — idempotent cancel-QC, cancel-freight, void-PI/PO,
  refund-escrow, reverse-local-release-marker, mark-payout-disputed.
- [ ] **X-13 Exception timeline on Deal Detail** — trigger, evidence, owner, SLA,
  actions, resolution; root-cause taxonomy feeding this backlog.

---

## PHASE 5 — Operations

- [ ] **O-1 Correlation IDs** across request → event → approval → email → provider call.
- [ ] **O-2 Metrics + alerting** — funnel (opportunities → deals → agreed → funded →
  shipped → paid), margin realized vs expected, connector failure rates, approval
  backlog, escrow funding lag; alert on: verification-disabled, DB-fallback-active,
  email-log-only, unreconciled ledger, stuck deal > SLA.
- [ ] **O-3 Test hardening** — fix the 4 pre-existing failing tests
  (`test_trade_min_cache` ×3, `llm_gateway test_retry_defaults` — env-dependent
  assertions); make the suite pass against live-Mongo config (loop-safe clients);
  contract tests per connector; chaos tests (timeout, duplicate webhook, restart mid-call).
- [ ] **O-4 Staging environment** with provider sandboxes; deploy checklist + rollback.
- [ ] **O-5 Runtime topology decision** — monolith (8081) vs per-service containers for
  production; health checks that fail when a critical downstream is degraded, not
  just when the process is up.

---

## PHASE 6 — UI / product gaps

- [ ] **UI-1 Matching info on Deal Board** *(user requirement — the opportunity leaves
  the list once a deal starts, so the deal must carry the story)*: expandable
  "Matching" panel per deal card/row showing — origin opportunity snapshot, every
  scored candidate with CMS breakdown, why the winner won, buyer target price vs
  supplier FOB, tariff impact, data freshness/confidence. Backend already stores all
  of it (`match_results`); also **snapshot opportunity core fields onto the deal at
  conversion** so the board never depends on the opportunity store.
- [ ] **UI-2 Buyer portal** — PI acceptance, escrow funding link, QC provider selection
  (same top-5 catalog the emails offer), shipment tracking, document pack, dispute/accept.
- [ ] **UI-3 Supplier portal** — RFQ response, PO acceptance, production milestones,
  document upload, payout status.
- [ ] **UI-4 Operations dashboard** — stuck deals, pending approvals, margin-at-risk,
  provider outages, funding delays.
- [ ] **UI-5 Money dashboard** — expected vs realized margin per deal, fees, FX,
  releases, payouts, refunds, reconciliation status.
- [ ] **UI-6 "Why is this blocked"** explanations wherever an action is gated
  (checklist, freeze, margin, verification).

---

## PHASE 7 — Third-party onboarding tracker (code is ready; contracts are not)

Connector code + env placeholders exist for everything below; each row needs a
commercial step before it turns real. Indicative costs from the 2026 research in
`backend/services/shield_link/workflow_shield_link.md` §10.

| Provider | For | Commercial step | Approx cost | Env keys |
|---|---|---|---|---|
| QIMA | QC (most API-native) | Self-serve client account → API keys | ~$309/man-day, API free | `QIMA_API_KEY` + secret |
| SGS India | QC (max acceptance) | Key-account agreement (sales) | ~$300–500/man-day | `SGS_API_KEY` + secret |
| Bureau Veritas / Intertek / TÜV SÜD | QC alternates | Enterprise agreements | $300–600/man-day | `BUREAU_VERITAS_*`, `INTERTEK_*`, `TUV_SUD_*` |
| Cogoport | Freight booking (India-first) | Partner API agreement | free platform; margin in rate | `COGOPORT_API_KEY` + secret |
| Freightos | Freight quotes/booking | Developer-portal onboarding | SaaS ~$100–500/mo | `FREIGHTOS_API_KEY` + secret |
| Vizion or Terminal49 | Container visibility | API contract / free tier | ~$2–5/container or freemium | (new) |
| Tazapay / Escrow.com | Money custody | KYB + sandbox + contract | per-transaction fee ~1–3% | (new) |
| Payoneer | Supplier payout | Partner onboarding | per-payout fee | `PAYONEER_*` |
| ICEGATE (via CHA) | Customs filing | Registered CHA appointment | ₹2,000–6,000/Shipping Bill | `ICEGATE_API_KEY` |
| API Setu | GSTN/DGFT/MCA/Udyam verification | Developer account + product approval | free/nominal | `API_SETU_KEY` |
| Comtrade/Census/WITS, IndiaMART, Alibaba RFQ | Opportunity data | Free/paid API keys | free tiers exist | various |
| Transactional email (SES/SendGrid) | Deliverable outreach | Domain + account | ~$0–100/mo | SMTP/webhook keys |
| Cargo insurance provider | Transit risk | Broker relationship | ~0.3–0.5% of cargo value | (new) |

---

## Nice-to-have revenue features (after the above earns reliably)

- [ ] Auto-rank opportunities by **expected net profit after all costs + risk reserve**
  (the full cost model from P1-7 makes this possible).
- [ ] Repeat-buyer reorder prediction + automated follow-up campaigns.
- [ ] Dynamic margin strategy (risk-based up, loyalty-based down).
- [ ] Preferred-supplier marketplace with performance history.
- [ ] Trade-credit insurance quoting; LC option for enterprise buyers.
- [ ] Corridor playbooks (mandated PSI agencies, documents, customs risks per country/product).
- [ ] CRM-style buyer/supplier relationship history.
- [ ] Revenue + cash-flow forecast by deal stage (tranche timing means profitable
  deals can still be cash-negative mid-flight).
- [ ] WhatsApp/Telegram negotiation channels (only after email is production-stable).

---

## Known open problems in today's code (found during implementation, be honest about these)

1. `backend/.env` with live Mongo credentials is committed & pushed (→ SEC-1).
2. Freight-quote failure silently reverts `logistics_cost` to 0 — margin then
   overstates by the whole freight cost (loud log only; consider blocking agreement
   instead in production profile).
3. Escrow fees / FX spread / insurance / duties still missing from the margin formula (P1-7).
4. Webhook connectors accept-all when secrets are unset; SendGrid verification is a placeholder (P1-9).
5. Outbound email is log-only without SMTP config; inbound is 15-min IMAP polling (P3-3).
6. ~~LLM-failure fallback drafts not labeled~~ — fixed 2026-07-17 (risk flags + summary prefix on the approval).
7. ~~Event publisher is a no-op~~ — fixed 2026-07-18: in-process bus + all six
   pipeline continuation legs (AGREED→PI, PI accepted→escrow, funded→PO gate,
   PO→QC, QC passed→freight gate, cleared→payout gate). Remaining: durable
   outbox so triggers survive process restarts (P3-1).
8. 4 pre-existing test failures on this machine (env-dependent) + Motor/event-loop
   fragility when the suite runs against live Mongo (O-3, P1-2).
9. Deal-status transitions from webhooks are best-effort (logged, not retried) —
   a failed transition leaves deal state behind reality (P3-2).
10. Buyer "acceptance" carrying new non-price conditions (deadline, packaging,
    incoterm) is not detected — only price and QC-agency changes are (P3-4).
11. ~~Mis-extracted email price can become an agreement~~ — fixed 2026-07-17
    (price plausibility band guard in `run_round`).

---

## Definition of "earning in production"

A real buyer funds neutral escrow for terms negotiated by the agent; a verified
supplier ships goods that passed a buyer-chosen inspector's photo-documented QC;
customs and freight complete with verified documents; escrow releases tranches
against verified proof; the supplier is paid; PortLess's margin lands after every
fee, and the ledger reconciles to the cent — with the operator only ever touching
HITL gates and exceptions, and every exception having a playbook instead of a
panic.
