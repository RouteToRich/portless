# PortLess Production Tracker

Created: 2026-07-16 · Updated: 2026-07-17 · **Single source of truth** — merged
from the former `PRODUCTION_READINESS_GAPS.md` (reference analysis) and
`PRODUCTION_TASK_LIST.md` (execution backlog), so completion is tracked in one file.

Purpose: turn PortLess from a strong demo/local system into a production
market-trading business that discovers opportunities, negotiates, collects buyer
money, executes shipment, pays suppliers, and earns margin with low human
intervention and controlled risk.

**How to use:** work the phases top-down; each phase unlocks the next.
Status legend: `[x]` done · `[~]` partial · `[ ]` open.
Appendix A holds the full negative-scenario inventory (~150 items) that Phase 4
executes as workstreams. Appendix B holds reference answers from code review.

Do not treat "all modules implemented" as "production ready." Many paths still
degrade to stubs, log-only sends, in-memory storage, manual operator steps,
disabled webhooks, placeholder verification, or best-effort transitions.

---

## ⛔ PHASE 0 — Security emergency (do this TODAY, before anything else)

- [~] **SEC-1 — Rotate the leaked MongoDB Atlas credential.** `backend/.env`
  containing the live `MONGO_URI` (username + password to the Atlas cluster)
  was **committed and pushed to `github.com/RouteToRich/portless` (origin/main)**.
  *Update 2026-07-17: `backend/.env` is no longer tracked in the current index
  (verified via `git ls-files`), but the secret remains in git history.* Still open:
  1. In MongoDB Atlas: create a new database user + password; delete the old
     user; restrict network access (IP allowlist) while at it.
  2. Purge the file from history (`git filter-repo --path backend/.env
     --invert-paths` or BFG) and force-push, or accept that the old
     (now-rotated) secret stays in history.
  3. Audit the cluster for unexpected access/collections.
- [ ] **SEC-2 — Secret scan the whole history** (gitleaks/trufflehog) for any
  other committed keys (Groq, Tavily, Google Drive refresh token, SMTP app
  password). Rotate anything found.
- [ ] **SEC-3 — Move runtime secrets out of files** — deploy with a secret
  manager (Doppler/Vault/cloud-native) or environment injection; `.env` files
  remain for local dev only. NATS `*.creds` files are gitignored (2026-07-17) —
  never commit or share them; revoke the Synadia user if one ever leaks.

---

## 🏢 PHASE B — Company formation & compliance setup (India, non-technical)

Everything here is paperwork, not code — but Phase 1 is blocked without it:
escrow KYB (P1-4) needs a legal entity, the email domain (P1-8) needs a
registered business to look credible, exports legally require IEC + AD code +
GST, and PortLess's own sourcing pipeline verifies suppliers via GSTN/Udyam/IEC —
so PortLess itself must be verifiable the same way. Costs are indicative
2026 figures; confirm with a CA.

### Entity & identity

- [ ] **B-1 Choose the business entity.** For a single founder the realistic
  options:
  | Option | Cost / time | Pros | Cons |
  |---|---|---|---|
  | Sole proprietorship | ~₹0 (GST in trade name is the "registration") · days | Fastest, cheapest, minimal compliance | No limited liability; weakest for escrow KYB / foreign buyer trust |
  | **OPC (One Person Company)** ← recommended | ~₹6–15k incl. DSC/professional fees · 1–2 weeks | Limited liability, real company credibility for Tazapay/Escrow.com KYB and buyer T&Cs, still single-owner | ROC annual filings, slightly more accounting |
  | Pvt Ltd | similar cost, needs 2 directors/shareholders | Best for future investors/partners | Overkill for one person today; can convert OPC later |
  Recommendation: **OPC** — international buyers and escrow providers take a
  registered company far more seriously, and liability isolation matters when
  real cargo money flows.
- [ ] **B-2 Incorporate** (if OPC/Pvt Ltd): DSC (digital signature, ~₹1–2k) →
  name reservation + SPICe+ on mca.gov.in (PAN + TAN issued with incorporation) →
  certificate of incorporation. Appoint a CA for this — it also sets up B-10.
- [ ] **B-3 Business current account** in the entity's name (pick a bank with a
  strong trade/forex desk — ICICI/HDFC/Axis — because it becomes your
  **Authorized Dealer (AD) bank** for export proceeds; see B-7).

### Registrations (the export stack)

- [ ] **B-4 Udyam (MSME) registration** — free, instant-ish, online at
  udyamregistration.gov.in (Aadhaar + PAN). Benefits: collateral-free credit
  schemes, interest subvention, priority-sector lending, and government-tender
  advantages. Note: PortLess's sourcing pipeline verifies counterparties via
  Udyam through API Setu — being registered makes PortLess itself verifiable.
- [ ] **B-5 GST registration** — free, gst.gov.in (CA fee ~₹1–3k if outsourced).
  Exports are **zero-rated**: also file the **LUT (Form RFD-11)** — renewed
  every financial year — so exports ship without paying IGST upfront; claim
  input-tax-credit refunds on inputs.
- [ ] **B-6 IEC (Import Export Code)** — dgft.gov.in, ₹500, usually same-day.
  This IS the "EXIM license" for general exports — legally mandatory. Link it
  to the GSTN + PAN. (The app verifies supplier IECs via DGFT — same registry.)
- [ ] **B-6a Export-policy check per product (ITC-HS Schedule 2)** — before
  committing to any HS code, verify its export policy status on DGFT:
  **Free** (IEC alone suffices), **Restricted** (needs a separate DGFT export
  authorization per product — apply before quoting buyers), or **Prohibited**
  (walk away). Also check product-specific mandates (e.g. APEDA registration
  for agri, FSSAI for food, drug licenses for pharma) and the destination
  country's import-side requirements. Wire the same check into Trade-Min's
  opportunity screening later (O-9 / X-1) so the scanner never surfaces a
  restricted HS code as an opportunity.
- [ ] **B-7 AD Code registration** — get the 14-digit AD code letter from your
  bank (B-3) and register it at every port of shipment on ICEGATE. Without
  this, shipping bills cannot be filed and export proceeds cannot be realized
  (FEMA/EDPMS). Also register on **icegate.gov.in** as an exporter (B-8 below
  and the P2-1 ICEGATE integration both need this account).
- [ ] **B-8 RCMC** — Registration-Cum-Membership Certificate from the relevant
  Export Promotion Council: **FIEO** for multi-product merchant exporters
  (~₹6–15k/yr) or the product council (e.g. APEDA for agri) once the product
  focus is clear. Required for most export incentive schemes (RoDTEP etc.).
- [ ] **B-9 Domain + professional email** *(unblocks P1-8)* — register the
  domain (`portless.io` is already assumed in `.env` —
  `PORTLESS_EMAIL_DOMAIN` / `PORTLESS_TRADE_ADDRESS`; ~₹1–4k/yr for .io, or a
  cheaper .in): registrar with Cloudflare DNS. Then business mailboxes:
  **Zoho Mail** (free tier for 5 users on a custom domain, or ~₹99/user/mo) or
  Google Workspace (~₹160+/user/mo). Configure **SPF + DKIM + DMARC** on the
  domain, then point the app's SMTP/IMAP at it — replacing the personal Gmail
  app password. Transactional sending (SES/SendGrid) rides the same domain.

### Ongoing compliance & protection

- [ ] **B-10 CA + compliance calendar** — GSTR-1/GSTR-3B (monthly or QRMP
  quarterly), annual GSTR-9, ITR, ROC annual filings (AOC-4, MGT-7A for OPC),
  LUT renewal each April, e-BRC/EDPMS export-realization tracking with the AD
  bank. Budget ~₹2–5k/mo for a CA on retainer.
- [ ] **B-11 ECGC cover** — export credit insurance for buyer-default risk
  (ties O-8); plus a cargo-insurance broker relationship (Phase 7 table).
- [ ] **B-12 Trademark "PortLess"** (optional, ~₹4.5k govt fee + attorney) —
  worth filing in class 35/36/39 before the brand appears on invoices and
  buyer portals.

---

## 🚀 PHASE D — Deployment (single-user production, keep it cheap)

The stack is already cloud-hosted where it matters — MongoDB Atlas (data),
Synadia Cloud (event bus), Google Drive (documents) — so production compute is
just **one small box for the FastAPI monolith + static hosting for the UI**.
For a single-user application, do NOT build Kubernetes/microservice
infrastructure (O-5 stays "monolith" until real load says otherwise).

Recommended shape (≈ ₹500–900/mo total):

- [ ] **D-1 Compute: one small VPS** — Hetzner CX22 (~€4.5/mo) / DigitalOcean /
  AWS Lightsail, Ubuntu LTS. Run the existing Docker setup (`Dockerfile` +
  `docker-compose.yml`) or plain `uvicorn app:app` under systemd. Alternative
  if no server admin is wanted: Render/Railway starter (~$7/mo) — less control,
  zero maintenance. (Oracle Cloud's always-free ARM VM is a ₹0 option if
  availability cooperates.)
- [ ] **D-2 DNS + TLS** — Cloudflare DNS on the B-9 domain:
  `app.<domain>` → UI, `api.<domain>` → VPS. TLS via **Caddy** (simplest
  auto-HTTPS reverse proxy) or nginx + certbot in front of uvicorn.
- [ ] **D-3 UI hosting** — build the Vite UI → **Cloudflare Pages** (free) with
  `/api` pointed at `api.<domain>`; or serve the static build from the same
  VPS behind Caddy if one box is preferred.
- [ ] **D-4 Secrets on the host** — env vars injected via systemd
  `EnvironmentFile`/compose env or Doppler (SEC-3); upload the NATS `.creds`
  file out-of-band (scp), never through git. Set `ENV_PROFILE=production` once
  P1-1 exists; **`TRADE_MIN_MOCK_MODE` and `AUTO_FLOW_ENABLED` must be off**.
- [ ] **D-5 Inbound reachability** — production webhooks (Stripe/provider
  callbacks, SendGrid inbound) need the public `api.<domain>`; register the
  URLs + secrets with each provider (P1-9). IMAP polling keeps working
  meanwhile.
- [ ] **D-6 Backups + monitoring** — Atlas backup is on (P1-3); add VPS
  snapshots (Hetzner ~20% surcharge), UptimeRobot/Better Stack free tier
  pinging `/health`, and log rotation. Alert to email/Telegram.
- [ ] **D-7 Deploy procedure** — simplest reliable loop:
  `git push` → GitHub Actions SSH deploy (or a one-line
  `git pull && docker compose up -d --build` runbook). Tag releases; keep
  a rollback = previous image. Document it in a `DEPLOY.md` runbook.
- [ ] **D-8 OS hardening basics** — SSH keys only, ufw allowing 80/443/22,
  fail2ban, unattended-upgrades. One hour of setup, permanent benefit.

---

## 📣 PHASE M — Marketing & go-to-market (non-technical)

A merchant exporter lives on trust signals: buyers who receive a PortLess
email/PI will Google the company before replying, and suppliers will check
whether the buyer of their goods is real. Everything below is cheap and
single-founder-sized; it rides on the Phase B domain (B-9) and Phase D
hosting.

### Presence (do before the first outreach email goes out)

- [ ] **M-1 Company website** on the B-9 domain — a simple static marketing
  site (5–6 pages: home, how it works for buyers, how it works for suppliers,
  products/corridors, about + registrations, contact). Host it free on
  Cloudflare Pages next to the app (D-3); `www.<domain>` for the site,
  `app.<domain>` for the platform. Show the trust artifacts prominently:
  IEC number, GSTIN, Udyam number, RCMC membership, escrow-protected payment
  flow, named QC partners (SGS/QIMA/BV/Intertek/TÜV SÜD). A buyer checking
  the domain of the email that just landed must find a credible exporter.
- [ ] **M-2 Google Business Profile + map listing** for the registered address;
  consistent NAP (name/address/phone) everywhere.
- [ ] **M-3 LinkedIn company page + founder profile** — buyers' procurement
  people check LinkedIn first; post corridor/product content occasionally.
- [ ] **M-4 Email trust polish** — professional signature (logo, registrations,
  website), branded PI/PO templates already exist; verify the outbound domain
  isn't on blocklists (mail-tester.com score ≥ 9) after B-9/P1-8 DNS setup.
- [ ] **M-5 WhatsApp Business account** on a dedicated number — Indian
  suppliers negotiate on WhatsApp; even before the P3-10 channel integration,
  the human fallback needs a business-verified identity.

### Discovery & lead generation (feeds Trade-Min's pipeline)

- [ ] **M-6 B2B marketplace listings** — IndiaMART seller profile, TradeIndia,
  Alibaba (free tier first; Gold Supplier ~$2–4k/yr only once volume
  justifies), ExportersIndia. These double as inbound lead sources alongside
  the connectors PortLess already scans.
- [ ] **M-7 FIEO / EPC directory listing** — comes with B-8 RCMC membership;
  buyers browsing council directories are pre-qualified.
- [ ] **M-8 Product catalog + pitch assets** — one-page company profile PDF,
  product spec sheets per HS code (grades, packaging, MOQ, certifications),
  and a short "how escrow + third-party QC protects you" explainer for buyer
  objection handling — the negotiation agent's emails can link to these.
- [ ] **M-9 Content/SEO starter** — a handful of corridor pages
  ("<product> exporter India to <country>") targeting the exact searches
  import buyers make; publish the QC/escrow process as content. Low volume,
  high intent.
- [ ] **M-10 Buyer references & testimonials** — after the first 2–3 completed
  deals, collect written references and display them (with permission);
  nothing converts hesitant importers better.

### Later (only once deals flow)

- [ ] **M-11 Trade fairs / buyer-seller meets** — EPC-subsidized international
  fairs (MSME/FIEO reimburse part of the cost for Udyam-registered exporters —
  another B-4 benefit).
- [ ] **M-12 Paid acquisition experiments** — Google Ads on corridor keywords,
  LinkedIn outreach tooling — only after M-9 pages exist to land on and the
  funnel metrics (O-2) can attribute results.

---

## ✅ Recently completed (2026-07-16 → 17) — context for what follows

- [x] Top-5 buyer-selectable QC providers (SGS, QIMA, BV, Intertek, TÜV SÜD) in
  `shared/connectors/qc_providers.py` with catalog (`GET /shield-link/qc/providers`)
  + cost-quote (`POST /shield-link/qc/quote`) endpoints, per-provider API-key/HMAC
  placeholders.
- [x] QC cost quoted into the negotiation margin floor from round 0
  (`cost_envelope.qc_cost`); re-quoted when the buyer opts a provider.
- [x] Buyer's QC choice captured from email (LLM intent classifier + deterministic
  name scan, rejection-aware); one named agency honored; several named → cheapest
  among them; never opted → fallback at booking (`SHIELD_LINK_QC_FALLBACK_STRATEGY`).
- [x] Freight quoted into the margin floor (`POST /shield-link/freight/quote` →
  `logistics_cost`) — was hard-coded 0, overstating margin by the entire freight
  cost (~$114/MT on the stub lane).
- [x] Agreement completeness checklist — no `NEGOTIATION_AGREED` until QC choice +
  quantity received; the agent emails the buyer for missing items instead of
  closing; operator override bypasses (`NEGOTIATION_AGREEMENT_CHECKLIST_ENABLED`).
- [x] First buyer email content rules — first outreach lists supported QC agencies
  and asks the buyer to opt; inspection cost never presented as a separate charge.
- [x] Deal keeps `opportunity_id`, full `match_results`, winner + CMS after the
  opportunity leaves the radar; Deal Board shows CMS + matched count (depth gap → UI-1).
- [x] **Durable event bus over NATS JetStream (Synadia Cloud)** — 2026-07-17:
  `shared/bus/nats_bus.py` + `shared/bus/factory.py`; `EVENT_BUS_BACKEND`
  auto-selects NATS when a `*.creds` file is present (in-process fallback
  otherwise, loudly logged). One durable pull consumer per topic → events
  published while the monolith is down are **replayed on next boot**. Handler
  failure → NAK retry with backoff (5s→60s, ×`NATS_MAX_DELIVER`), then TERM +
  dead-letter `deal.exception`; poison messages TERMed; `publish()` never
  raises (broker outage degrades to in-process delivery). Standalone service
  mode publishes durably too (`consume=False` in every service lifespan).
  Verified live: round-trip on Synadia + full monolith boot with 7 durable consumers.
- [x] `DEAL_EXCEPTION` → Exception Resolver continuation (2026-07-17) — emitted
  exceptions now actually reach `raise_exception()`; loop-safe (resolver's own
  lifecycle echoes carry `exception_id` and are skipped).

---

## PHASE 1 — Go-live blockers (no real-money trade before ALL of these)

### Config & persistence

- [ ] **P1-1 Production config gate** — startup readiness check that **refuses to
  boot in production profile** if any of: Mongo unreachable, any `*_WEBHOOK_SECRET`
  unset, email in log-only mode, `TRADE_MIN_MOCK_MODE`/`AUTO_FLOW_ENABLED` on,
  document store on local-disk fallback, LLM gateway keyless, event bus on
  in-process fallback. Add `ENV_PROFILE=dev|staging|production` with
  environment validation by profile.
- [ ] **P1-2 Durable Mongo everywhere** — single production cluster + per-service
  collections verified for all 13 stateful modules (auth, deals, trade_min,
  sourcing, negotiation_agent, email_processor, dealmaker, shield_link, docugen,
  escrow_guard, hitl_approval, exception_resolver, RAG); in-memory fallback
  allowed only in dev/CI. Fix the known **Motor/event-loop fragility** seen when
  tests run against live Mongo (client bound to a dead loop) — create clients
  per-loop or at app lifespan.
- [ ] **P1-3 Backups & restore drill** — Atlas continuous backup + a tested
  restore runbook.

### Real opportunity and supplier data

- [ ] **P1-D1** Obtain and configure live data APIs: Comtrade/open trade data,
  Volza-class buyer data or free alternatives, Alibaba RFQ / IndiaMART, MacMap
  tariff source, API Setu.
- [ ] **P1-D2** Replace graceful connector stubs with live connectors incrementally;
  show per-source quality/confidence in the UI.
- [ ] **P1-D3** Freshness SLAs for opportunity data: scan time, source, validity
  window, price date, tariff date.
- [ ] **P1-D4** Duplicate opportunity detection so the same buyer demand is not
  converted into multiple live deals.
- [ ] **P1-D5** "Opportunity stale" guard before deal creation when buyer signal
  or seller quote is too old.

### Money custody (the earning path itself)

- [ ] **P1-4 Neutral escrow provider onboarding** — Tazapay / Escrow.com KYB,
  sandbox, webhook secrets, corridor coverage (India → target markets), payout
  limits, fee schedule confirmed. The current Stripe-hold path keeps buyer money
  in PortLess's own account — not acceptable to real buyers and a regulatory risk.
- [~] **P1-5 Auto-wire the money chain** — `PI_ACCEPTED` → escrow create;
  `funded` webhook → `ESCROW_FUNDED` → PO gate; `CLEARED` → payout gate are
  event-driven today (durable NATS continuations). Still open: `qc.passed` +
  customs proof → **tranche release** automation, refund → `REFUNDED`
  transition, supplier payout from released escrow tranches (not legacy payout
  assumptions), replay protection (timestamp window + event-id dedupe) on every
  money webhook.
- [ ] **P1-6 Double-entry ledger** — every hold/release/refund/payout/fee/FX row;
  continuous invariant check (held = released + refunded + outstanding, and
  released = supplier_paid + PortLess_margin + fees); auto-freeze money actions
  on reconciliation mismatch.
- [ ] **P1-7 Complete the cost model in pricing** — margin formula now covers
  freight + QC, but **escrow fees, payment/FX spread, insurance, duties/taxes and
  a risk reserve are still missing**. Add them to `CostEnvelope`/quote seeding so
  a deal can never be agreed at a real-world loss.

### Communication & identity

- [ ] **P1-8 Production email domain + deliverability** — own domain,
  SPF/DKIM/DMARC, transactional provider (SES/SendGrid/Postmark) instead of a
  personal Gmail app password; per-deal reply-to addresses (`deal-<id>@…`) so
  inbound mapping stops depending on free-text matching. *Domain purchase +
  mailboxes + DNS records are B-9 (Phase B); this task is the app-side switch.*
- [ ] **P1-9 Webhook verification hardening** — implement SendGrid ECDSA
  verification (currently placeholder) and Mailgun signing check; reject unsigned
  inbound in production; set **every** production webhook secret (current
  behavior accepts some callbacks when secrets are missing); verify API-gateway
  webhook ingress (signature validation, idempotency keys, dedupe,
  route-to-owning-service); replay protection for all provider webhooks
  (timestamp tolerance + event IDs).
- [ ] **P1-10 Legal entity + terms** — *entity, MSME/GST/IEC/AD-code
  registrations now tracked step-by-step in Phase B (B-1…B-8).* Remaining here:
  buyer T&Cs, supplier T&Cs, dispute/cancellation policy, privacy policy.
  Without signed terms, the CRF/escrow chain has no contractual force.

### Access control

- [ ] **P1-11 Roles + four-eyes** — role/permission model for HITL approvals (who
  can approve email, PO, freight, customs, payout, refund, exception resolution);
  two-person approval for payout, refund, provider change, margin-below-policy,
  deal value > threshold; MFA for approver accounts; audit export for all
  approvals and operator edits.
- [ ] **P1-12 Rate limiting + CORS lockdown** on auth, gateway, webhooks,
  negotiation, email endpoints; allowlisted origins only in production.

---

## PHASE 2 — Proof & document integrity (trust chain for money release)

- [ ] **P2-1 Real ICEGATE/CHA filing** — replace the DocuGen ICEGATE stub; verify
  customs proof at source (Shipping Bill, Let Export Order, BL/eBL, carrier
  event) and store document hashes.
- [ ] **P2-2 Digital CRF verification** — beyond webhook HMAC: issuer
  signature/QR/portal check, report hash, archive report PDF + photos on the TPO
  (QIMA report API first — the only public one).
- [ ] **P2-3 Cross-document consistency check before any release** — PI ↔ PO ↔ CI
  ↔ PL ↔ BL ↔ Shipping Bill ↔ QC report: quantities, HS code, buyer/supplier
  names, ports, container/seal numbers must match or the deal freezes.
- [ ] **P2-4 Tamper evidence** — immutable hash + timestamp + signer identity +
  storage URI + change history on every document version; Google Drive is the
  primary store — kill the local-disk fallback in production.
- [ ] **P2-5 Evidence pack generator** — one click: PI, PO, email thread, QC
  report + photos, BL, shipping bill, ledger extract, provider callbacks — for
  any dispute.

---

## PHASE 3 — Automation backbone (earning with less human intervention)

- [~] **P3-1 Real event bus** — **upgraded to durable NATS JetStream 2026-07-17**
  (`shared/bus/nats_bus.py`, `EVENT_BUS_BACKEND` selection, Synadia Cloud free
  tier; in-process fallback). All seven continuation legs live in
  `shared/orchestration/continuations.py` (each idempotent, deal-state guarded,
  `deal.exception` on failure; `EVENT_CONTINUATION_ENABLED` escape hatch):
  - `NEGOTIATION_AGREED` → issue PI (deal → PI_SENT)
  - `PI_ACCEPTED` → open neutral escrow sized from the PI, carrying the
    buyer-opted QC provider (deal → ESCROW_PENDING; funding stays a buyer action)
  - `ESCROW_FUNDED` → create `ISSUE_PO` HITL gate (operator still approves)
  - `PO_ISSUED` → book QC with the buyer-opted inspector
  - `QC_PASSED` → create `BOOK_FREIGHT` HITL gate
  - `CLEARED` → create `RELEASE_SUPPLIER_PAYOUT` HITL gate
  - `DEAL_EXCEPTION` → Exception Resolver `raise_exception()` (loop-safe)

  Money/commitment steps remain HITL-gated — continuations create the gates,
  never approve them. **Sub-task status:**
  - [~] **P3-1a Durable event log / transactional outbox** — *broker-side
    durability done*: JetStream persists events and durable consumers replay
    them after a crash/restart, so "process dies between state change and
    handler" no longer loses the trigger. *Still open*: the Mongo write and the
    publish are not atomic — a crash between DB commit and publish still loses
    the event; add a transactional outbox (Mongo `events` collection + relay)
    for exactly-that window, plus an inspectable event-log view.
  - [~] **P3-1b Failure-path handlers** — *`DEAL_EXCEPTION` → Exception Resolver
    wired 2026-07-17.* Still open: subscribe `QC_FAILED` and
    `NEGOTIATION_FAILED` (and later `PAYOUT_FAILED`/refund events) so negative
    outcomes are triaged automatically instead of parking the deal.
  - [ ] **P3-1c Document-generation leg** — on `QC_PASSED` (parallel to the
    freight gate), generate CI/PL/CoO/SDF/Shipping Bill via DocuGen and file
    CHA, replacing the auto-flow-only document step.
  - [~] **P3-1d Handler retry / dead-letter** — *bus-level done 2026-07-17*:
    failed continuation handlers are NAK-redelivered with backoff and, after
    `NATS_MAX_DELIVER` attempts, TERMed with a dead-letter `deal.exception`
    (which now reaches the resolver). Still open: operator "re-run
    continuation" one-click action on the dead-letter record.
  - [ ] **P3-1e Payout-completed leg** — on `PAYOUT_COMPLETED`, verify deal
    reached `PAID`, close out the ledger entry, archive the deal evidence pack
    (depends on P1-6 ledger, P2-5 evidence pack).
- [ ] **P3-2 Retry + dead-letter for direct connector calls** — the event legs
  retry now, but a `ConnectorError` on the direct REST path still just surfaces
  and stalls the deal; add exponential-backoff jobs + operator "manual retry" UI
  reusing idempotency keys.
- [~] **P3-3 Near-real-time email + extraction safety** — *price plausibility
  guard done 2026-07-17*: an extracted counter-price outside ~3× of the
  negotiation corridor never becomes the working bid/ask and flags the round for
  HITL (`guards.is_price_plausible`, `NEGOTIATION_PRICE_BAND_FACTOR`; operator
  override bypasses) — directly prevents the real $102,123.71-vs-$1,255
  incident. Still open: IMAP IDLE / inbound webhooks (15-min polling today),
  outbound retry + bounce/delivery-failure ingestion, attachment support for
  outbound PI/PO/document packs, quoted-reply-chain stripping before price
  extraction, PDF/Excel attachment price parsing, multi-currency/unit
  normalization, operator endpoints for inbound reject/reassign/retry/ambiguous
  mapping resolution.
- [~] **P3-4 Negotiation guardrails for autonomy** — *done 2026-07-17*:
  LLM-fallback template drafts and drafts with unresolved `[placeholders]` are
  labeled on the HITL approval (risk flags `degraded_llm_fallback` /
  `unresolved_placeholders` + "⚠ edit before sending" summary prefix);
  factual-grounding prompt rules (no invented product names/certifications, no
  bracketed placeholders, HS-code grounding). Still open: prompt regression
  suite in CI (no supplier-price leakage, CIF offer present, QC options listed,
  no invented certifications), prompt+version stamping per sent email, LLM
  output validation (JSON schema, price floor, no internal cost/margin leakage),
  detecting non-price conditions in acceptance replies (delivery deadline,
  packaging, incoterm change), policy deadline/date-driven urgency and
  escalation, decide whether operator override can explicitly close a
  negotiation or must always satisfy the convergence guard.
- [ ] **P3-5 Sourcing fan-out** — multi-supplier RFQ dispatch, auto best-quote
  selection, retry/backoff for GSTN/DGFT/MCA/Udyam/VIES failures, surface each
  verification result in UI with evidence timestamp + source, supplier
  denylist + repeat performance score; block selection on degraded verification
  without explicit operator override.
- [ ] **P3-6 Freight comparison + booking automation** — fan out quote to
  Cogoport + Freightos (+ SeaRates/carrier-direct later), auto-select by
  price/ETA/reliability/route risk/buyer preference, quote-expiry revalidation
  before booking.
- [ ] **P3-7 Shipment visibility** — Vizion or Terminal49 connector pushing
  normalized DCSA milestones into `freight/webhook`; ETA-exception detection
  (rolled container, transshipment delay, customs hold, demurrage, lost document).
- [ ] **P3-8 Container Loading Check** — CLC inspection type on the QC path
  (stuffing photos + seal numbers + packaging evidence) so visual evidence
  covers the shipping leg.
- [ ] **P3-9 Autonomy policy engine** — per-action L0–L3 levels driven by deal
  value, margin headroom, counterparty history, corridor risk, country, product —
  this is what actually reduces human intervention safely over time.
- [ ] **P3-10 WhatsApp/Telegram channels** — only after email flow is
  production-stable.

---

## PHASE 4 — Exception & negative-scenario coverage

The full scenario inventory (~150 items) lives in **Appendix A** below. Execute
it as ten workstreams, each delivering: detection → auto-freeze rule → operator
playbook (owner, SLA, safe default) → compensating action → test.

- [ ] **X-1 Market-data failures** — stale/duplicate/fake opportunities, wrong HS
  code, tariff API fail-open, FX outliers. Deliver: freshness SLAs, duplicate
  detection, stale-data guard before deal creation.
- [ ] **X-2 Negotiation failures** — out-of-order replies, unmapped senders,
  quoted-price extraction errors, phishing on a valid token, LLM bad output,
  operator mis-edit. Deliver: session locks (exist) + reply-token addressing +
  extraction confidence gates.
- [ ] **X-3 Supplier failures** — post-match verification failure, bank-account
  change after PO (fraud signal → auto-freeze), cancellation after escrow funded,
  capacity miss.
- [ ] **X-4 Contract/pricing failures** — PI accepted after validity expiry,
  PI/PO field drift, margin guard bypass when buyer price missing, FX move
  breaking margin.
- [ ] **X-5 Escrow/payout failures** — partial/wrong-amount funding, provider
  outage mid-release, release-succeeded-locally-failed (and inverse),
  chargebacks, payout sanctions blocks. Deliver: reconciliation job +
  freeze-money action.
- [ ] **X-6 QC failures** — provider unavailable in supplier city, inspector
  no-show, ambiguous/conditional report, webhook-says-pass-PDF-says-fail,
  report/deal mismatch, buyer disputes after pass, buyer changes provider after
  PI (re-quote + PI amendment flow).
- [ ] **X-7 Freight/customs failures** — vessel cutoff missed, rolled container,
  seal mismatch, ICEGATE rejection, destination hold, buyer refuses
  delivery/goes silent after arrival.
- [ ] **X-8 Document failures** — stale data rendered, upload failure, wrong-org
  access, cross-document mismatch, missing product-specific certificates (CoO,
  phyto, fumigation).
- [ ] **X-9 Security/fraud** — replayed webhooks, prompt injection via inbound
  email into RAG/drafting context, stolen operator token, org-scoping leaks.
- [ ] **X-10 Concurrency/integrity** — crash between provider call and DB write
  (both orders), double approval, concurrent transitions,
  Mongo-write-ok-event-publish-fail. Deliver: outbox pattern (P3-1a) +
  "reprocess from checkpoint" for stuck deals.

Cross-cutting deliverables for all workstreams:

- [ ] **X-11 Freeze framework** — `freeze deal` (blocks sends/PO/booking/release/
  filing) and `freeze money` (blocks only release/refund/payout) as first-class
  deal flags with unfreeze checklists + required evidence.
- [ ] **X-12 Compensating actions** — idempotent cancel-QC, cancel-freight,
  void-PI/PO, refund-escrow, reverse-local-release-marker, mark-payout-disputed.
- [ ] **X-13 Exception timeline on Deal Detail** — trigger, evidence, owner, SLA,
  actions, resolution; severity levels (info → legal-risk); owner routing
  (negotiation, sourcing, finance, logistics, documents, compliance,
  engineering); SLA timers + escalation ladder; root-cause taxonomy feeding this
  backlog; post-resolution audit (who, why, financial/customer impact,
  prevention task); customer-facing status explanations that never expose
  internal margin or supplier price; "safe cancellation" flow (notify parties,
  refund remaining funds, cancel freight/QC, archive records); one-click
  evidence pack (P2-5); "reprocess from checkpoint" for stuck deals.

---

## PHASE 5 — Operations

- [ ] **O-1 Correlation IDs** across request → event → approval → email →
  provider call → deal log; structured logs + dashboards by deal_id and event_id.
- [ ] **O-2 Metrics + alerting** — funnel (opportunities → deals → agreed →
  funded → shipped → paid), margin realized vs expected, email send/reply
  latency, negotiation rounds, match score distribution, connector failure
  rates, approval backlog/wait time, escrow funding lag, QC pass rate, shipment
  delay rate; alert on: webhook verification disabled, DB-fallback-active,
  email-log-only, event bus on in-process fallback, pending approval backlog,
  failed money movement, unreconciled ledger, stale deal, stuck status > SLA.
- [ ] **O-3 Test hardening** — fix the pre-existing failing tests on this
  machine (order/env-dependent: `test_trade_min` ×4–5 RuntimeErrors,
  `test_trade_min_cache` ×3 — fail whenever `TRADE_MIN_MOCK_MODE=true` in
  `.env`, `llm_gateway test_retry_defaults`); make the suite pass against
  live-Mongo config (loop-safe clients); contract tests per connector; chaos
  tests (provider timeout, duplicate webhook, restart mid-call, partial DB
  write); load/smoke tests for scan → create deal → negotiate → PI/PO →
  QC/freight → escrow release; UI tests (Deal Board, Deal Detail, approvals,
  org user view, document downloads, payments tab).
- [ ] **O-4 Staging environment** with provider sandboxes; end-to-end staging
  test with live sandbox providers; production deploy checklist + rollback plan.
- [ ] **O-5 Runtime topology decision** — monolith (8081) vs per-service
  containers for production; health checks that fail when a critical downstream
  is degraded, not just when the process is up. Note: continuation consumers
  run only in the monolith today; per-service topology needs a consumer worker.

### Compliance and legal (with P1-10)

- [ ] **O-6** Confirm merchant-exporter legal model, invoice flow, tax
  obligations, GST/export compliance, IEC, liability terms.
- [ ] **O-7** Confirm sanctions/AML/KYB responsibilities with the escrow provider;
  add denylist screening if not fully covered.
- [ ] **O-8** Confirm insurance needs: cargo insurance, trade credit insurance,
  professional liability, cyber.
- [ ] **O-9** Confirm product-specific compliance for restricted/regulated HS codes.

---

## PHASE 6 — UI / product gaps

Current pages (`ui/src/pages/`): Login, OpportunityRadar, DealBoard, DealDetail,
ApprovalsInbox, Exceptions, MoneyMargin, AutonomySettings, OrgDeals,
OrganizationsAdmin, SystemHealth, KnowledgeBase. So the Money dashboard (UI-5)
and an Exceptions view (folded into X-13) already exist as pages — the gaps
below are about **depth and finish on existing pages**, buyer/supplier-facing
surfaces that don't exist yet, and overall product polish.

### Depth on existing pages

- [ ] **UI-1 Matching info on Deal Board** *(user requirement — the opportunity
  leaves the list once a deal starts, so the deal must carry the story)*:
  expandable "Matching" panel per deal card/row showing — origin opportunity
  snapshot, every scored candidate with CMS breakdown, why the winner won, buyer
  target price vs supplier FOB, tariff impact, data freshness/confidence.
  Backend already stores all of it (`match_results`); also **snapshot
  opportunity core fields onto the deal at conversion** so the board never
  depends on the opportunity store (in-memory reset today makes the origin
  opportunity card disappear).
- [ ] **UI-4 Operations dashboard depth** — SystemHealth exists; extend/add a
  view for stuck deals (no state change past an SLA), pending-approval age,
  margin-at-risk list, provider outage indicators, funding/shipment delays —
  the "what needs my attention right now" screen for a single operator.
- [ ] **UI-5a Money dashboard: realized vs expected** — MoneyMargin page exists;
  extend it with expected-vs-realized margin per deal, fee/FX/escrow-release
  breakdown, and reconciliation status once the ledger (P1-6) exists to back it.
- [ ] **UI-6 "Why is this blocked"** explanations wherever an action is gated
  (agreement checklist, freeze, margin guard, verification failure) — surface
  the actual reason instead of a generic disabled button.
- [ ] **UI-7 Per-deal timeline** — DealDetail has a payment-timeline section;
  generalize it to every event (state transitions, approvals, emails sent/
  received, documents generated, provider callbacks, money movements) as one
  chronological view — this is also X-13's exception-timeline foundation.
- [ ] **UI-8 Approvals Inbox triage** — group/filter ApprovalsInbox by autonomy
  level, deal value, and age; bulk-view for an operator catching up after time away.

### New surfaces (external users)

- [ ] **UI-2 Buyer portal** — PI acceptance, escrow funding link, QC provider
  selection (same top-5 catalog the emails offer), shipment tracking, document
  pack, dispute/accept buttons. Needs its own auth scope (buyer, not org-staff).
- [ ] **UI-3 Supplier portal** — RFQ response, PO acceptance, production
  milestones, document upload, payout status. Same auth-scope note as UI-2.

### Product polish (make the app *feel* production-grade, not just functional)

- [ ] **UI-9 Responsive/mobile pass** — operator pages are desktop-built today;
  make DealBoard, ApprovalsInbox, and Exceptions usable on a phone so HITL
  approvals can be handled away from a desk (a single-operator system has no
  backup approver — mobile access to approvals directly reduces deal-stall risk).
- [ ] **UI-10 Empty/loading/error states audit** — every list/detail view needs
  a real empty state (not a blank screen), skeleton loading (component exists —
  `ui/skeleton.tsx` — confirm it's used everywhere), and a friendly error state
  distinguishing "no data" from "backend unreachable."
- [ ] **UI-11 Notifications** — in-app toast/badge for new HITL approvals and
  exceptions as they arrive (HITL SSE stream already exists server-side —
  `hitl-approval/stream` — wire it to a persistent bell/toast instead of
  requiring the operator to keep ApprovalsInbox open).
- [ ] **UI-12 Global search** — jump to a deal/opportunity/exception by ID or
  buyer/supplier name from anywhere; today each list is its own silo.
- [ ] **UI-13 Design-system consistency pass** — the shadcn-style primitives
  exist (`ui/src/components/ui/*`); audit that every page actually uses them
  consistently (spacing, typography, color for status badges) rather than
  one-off styling per page — do this once real usage has surfaced the rough
  edges, not before.
- [ ] **UI-14 Dark mode** — nice-to-have once the above is solid; check
  whether the primitives already support it via Tailwind before treating this
  as new work.
- [ ] **UI-15 Onboarding / first-run help** — given this is a single-operator
  tool, low priority, but a short "what does each page do" pass helps if anyone
  else (co-founder, hire) ever needs to operate it.

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
| Synadia Cloud (NATS) | Durable event bus | Free tier active (creds in `backend/`) | free; paid for R3 HA | `NATS_*` (auto-discovers `*.creds`) |
| Cargo insurance provider | Transit risk | Broker relationship | ~0.3–0.5% of cargo value | (new) |

---

## Nice-to-have revenue features (after the above earns reliably)

- [ ] Auto-rank opportunities by **expected net profit after all costs + risk
  reserve** (freight, QC, escrow, FX, tariff — the full cost model from P1-7
  makes this possible).
- [ ] Repeat-buyer reorder prediction + automated follow-up campaigns.
- [ ] Dynamic margin strategy (risk-based up, loyalty/repeat-buyer-based down).
- [ ] Preferred-supplier marketplace with performance history.
- [ ] Trade-credit insurance quoting for higher-value deals; bank LC option for
  large enterprise buyers.
- [ ] Automatic quote expiration and revalidation.
- [ ] Corridor playbooks by country/product (mandated PSI agencies, required
  documents, customs risks, common buyer objections).
- [ ] CRM-style buyer/supplier profiles and relationship history.
- [ ] Revenue + cash-flow forecast by active deal stage (tranche timing means
  profitable deals can still be cash-negative mid-flight).

---

## Known open problems in today's code (found during implementation — be honest about these)

1. `backend/.env` with live Mongo credentials was committed & pushed; the file is
   no longer tracked, but rotation + history purge remain (→ SEC-1).
2. Freight-quote failure silently reverts `logistics_cost` to 0 — margin then
   overstates by the whole freight cost (loud log only; consider blocking
   agreement instead in production profile).
3. Escrow fees / FX spread / insurance / duties still missing from the margin
   formula (P1-7).
4. Webhook connectors accept-all when secrets are unset; SendGrid verification is
   a placeholder (P1-9).
5. Outbound email is log-only without SMTP config; inbound is 15-min IMAP
   polling (P3-3).
6. ~~LLM-failure fallback drafts not labeled~~ — fixed 2026-07-17 (risk flags +
   summary prefix on the approval).
7. ~~Event publisher is a no-op~~ / ~~in-process only~~ — fixed 2026-07-17:
   **durable NATS JetStream bus** with replay, retry/backoff, dead-letter
   `deal.exception`, in-process fallback. Remaining: transactional outbox for
   the DB-commit-vs-publish window (P3-1a); consumers run only in the monolith
   (O-5).
8. Pre-existing test failures on this machine (order/env-dependent:
   `test_trade_min` ×4–5, `test_trade_min_cache` ×3 when
   `TRADE_MIN_MOCK_MODE=true`, `llm_gateway test_retry_defaults`) + Motor/
   event-loop fragility against live Mongo (O-3, P1-2).
9. Deal-status transitions from webhooks are best-effort (logged, not retried) —
   a failed transition leaves deal state behind reality (P3-2).
10. Buyer "acceptance" carrying new non-price conditions (deadline, packaging,
    incoterm) is not detected — only price and QC-agency changes are (P3-4).
11. ~~Mis-extracted email price can become an agreement~~ — fixed 2026-07-17
    (price plausibility band guard in `run_round`).

---

## Suggested execution order

0. **Phase B paperwork in parallel from day one** — entity (B-1/B-2), bank
   account, then Udyam → GST → IEC → AD code (each gates the next); domain +
   email (B-9) as soon as the entity name is final. These have days-to-weeks of
   government lead time, so start them before any more code.
1. Phase 0 security, then the production config gate: no mocks, no in-memory, no
   missing webhook secrets, no log-only email in production. Stand up the
   single-VPS deployment (Phase D) so webhooks and domain email have a real
   endpoint.
2. Secure email + negotiation: first buyer/supplier outreach tests,
   SendGrid/Mailgun verification, retry/dead-letter, inbound operator tools.
   **Before the first real outreach email: M-1 website + M-2/M-3 profiles must
   be live** — buyers verify the sender's domain before replying.
3. Neutral escrow path: provider onboarding, PI accepted → escrow open, funded →
   PO, QC/customs → tranche release, refund → deal refunded.
4. Third-party proof: real QC report archival/verification, real customs filing,
   document cross-check.
5. Event/retry backbone: outbox, failure-path legs, retryable connector jobs.
6. Live supplier/buyer data: real connector rollout with confidence and
   freshness guards.
7. Buyer/supplier portals and reduced operator workload.
8. Advanced optimization: freight comparison, shipment visibility, dynamic
   pricing, insurance, analytics.

## Definition of production ready

PortLess is production ready only when:

- A real buyer can accept terms and fund neutral escrow.
- A real supplier can be verified, contracted, and paid from released funds.
- Every money movement is reconciled and reversible where legally possible.
- Every third-party callback is authenticated, idempotent, and auditable.
- A deal can survive process restarts without losing state.
- No live path depends on mock data, in-memory repositories, log-only sends, or
  placeholder verification.
- Exceptions freeze risky actions and give operators a clear recovery path.
- The system can prove why it made each decision and what evidence was used.
- PortLess margin is protected after freight, QC, escrow fees, FX, tariffs,
  refunds, and delays.

## Definition of "earning in production"

A real buyer funds neutral escrow for terms negotiated by the agent; a verified
supplier ships goods that passed a buyer-chosen inspector's photo-documented QC;
customs and freight complete with verified documents; escrow releases tranches
against verified proof; the supplier is paid; PortLess's margin lands after every
fee, and the ledger reconciles to the cent — with the operator only ever touching
HITL gates and exceptions, and every exception having a playbook instead of a
panic.

---
---

# Appendix A — Negative-scenario inventory (~150 items)

Executed via the Phase 4 workstreams (X-1 … X-13). Tick items here as their
detection + freeze + playbook + compensating action + test land.

## Opportunity and market-data failures (→ X-1)

- [ ] Opportunity is created from stale buyer import data or old RFQ.
- [ ] Same opportunity is discovered from multiple sources and duplicate deals are created.
- [ ] Buyer signal is fake, spam, scraped incorrectly, or belongs to a non-buying entity.
- [ ] Buyer demand exists but target price is outdated due to commodity price movement.
- [ ] HS code is wrongly classified, causing wrong tariff, documentation, or inspection requirements.
- [ ] Destination country has a temporary import ban, quota, sanctions restriction, or certificate requirement.
- [ ] Seller price source is old, estimated, or not valid for the requested quantity/specification.
- [ ] Tariff API fails open and opportunity margin is overstated.
- [ ] Currency conversion source is unavailable or returns an outlier rate.
- [~] Opportunity margin is positive before freight/QC/escrow/FX fees but negative after all real costs. *(Partially handled 2026-07-17: freight + QC now inside the negotiation margin floor from round 0; escrow fees, FX spread, insurance, and duty are still not in the formula → P1-7.)*

## Negotiation and communication failures (→ X-2)

- [ ] Buyer accepts price but refuses escrow funding.
- [~] Buyer says "accepted" but adds new conditions, different QC agency, shorter delivery deadline, or different packaging. *(Partially handled 2026-07-17: a different-price "acceptance" is vetoed; a QC-agency choice in the accepting reply is captured and re-priced; the agreement checklist blocks close on missing QC choice/quantity. New delivery-deadline or packaging conditions are still not detected.)*
- [ ] Buyer accepts in a different currency or unit than the quoted CIF price.
- [ ] Buyer negotiates by phone/WhatsApp outside the email thread and system state becomes stale.
- [ ] Buyer asks for supplier identity, factory address, or direct supplier contact before deal closure.
- [ ] Buyer requests samples, lab test reports, certifications, or photos before price acceptance.
- [ ] Buyer asks for credit terms instead of escrow.
- [ ] Buyer requests Incoterm change from CIF to FOB/DDP/EXW after negotiation starts.
- [ ] Buyer replies from an unrecognized email address and the system cannot map the thread.
- [ ] Buyer forwards the email to another colleague who replies without the negotiation token.
- [ ] Supplier replies with a quote in INR/MT, USD/kg, per bag, per container, or tax-exclusive pricing.
- [ ] Supplier quote includes hidden terms: GST extra, transport extra, packaging extra, minimum order, loading charge.
- [ ] Supplier agrees verbally but refuses PO terms.
- [ ] Supplier and buyer both counter at the same time and the negotiation state receives out-of-order replies.
- [ ] Duplicate email reply or provider webhook arrives.
- [ ] Same email maps to multiple negotiations.
- [ ] Email body contains quoted old price and extractor picks the wrong value.
- [ ] Email attachment contains the actual quote but body has no price.
- [ ] Inbound email is phishing/spam but matches a negotiation token.
- [ ] Outbound email bounces, is rate-limited, lands in spam, or SMTP provider silently drops it.
- [ ] LLM returns invalid JSON, unsafe recommendation, leaked supplier price, or invented certification.
- [ ] LLM drafts legally binding language before HITL agreement confirmation.
- [ ] Operator modifies an email and accidentally changes price, incoterm, quantity, or QC terms.
- [ ] Operator approves wrong email/deal.

## Sourcing and supplier failures (→ X-3)

- [ ] Supplier quote changes after negotiation.
- [ ] Supplier fails verification after initial match.
- [ ] Supplier GSTN/DGFT/MCA/Udyam checks are temporarily unavailable.
- [ ] Supplier passes basic verification but appears on denied-party, litigation, insolvency, or adverse-media lists.
- [ ] Supplier cannot meet exact product grade/specification after initially quoting.
- [ ] Supplier cannot meet requested quantity, MOQ, packaging, shelf-life, or labeling requirement.
- [ ] Supplier has capacity but not by the required shipment date.
- [ ] Supplier requires advance payment before escrow tranche is released.
- [ ] Supplier substitutes a different product grade, batch, factory, or packing material.
- [ ] Supplier misses production date.
- [ ] Supplier refuses independent QC or tries to influence inspector.
- [ ] Supplier invoice/PO details mismatch legal entity, GSTIN, IEC, bank account, or address.
- [ ] Supplier asks to change bank account after PO issuance.
- [ ] Supplier cancels after buyer has funded escrow.
- [ ] Multiple suppliers quote; selected supplier later fails and fallback supplier price breaks margin.

## Contract, PI, PO, and pricing failures (→ X-4)

- [ ] PI is issued with missing buyer legal name, address, VAT/tax ID, delivery port, payment terms, QC provider, or escrow provider.
- [ ] Buyer accepts an outdated PI after price validity expired.
- [ ] Buyer accepts PI but changes quantity or destination port.
- [ ] PO is issued before escrow funded or before margin guard passes.
- [ ] PO margin guard skips because buyer target price is missing.
- [ ] PI and PO use inconsistent units, quantities, product description, HS code, shipment date, or incoterm.
- [ ] Escrow/provider fees are omitted from margin calculation.
- [ ] Freight quote expires before booking.
- [ ] QC fee or travel surcharge increases after negotiation.
- [ ] FX rate moves enough to break margin.
- [ ] Platform discount/manual override makes realized margin below policy.
- [ ] Taxes, duties, bank charges, demurrage, detention, or insurance are not included in landed cost.

## Escrow, payment, refund, and payout failures (→ X-5)

- [ ] Buyer funds partial/wrong amount.
- [ ] Buyer funds escrow in wrong currency or via unsupported payment method.
- [ ] Escrow provider rejects buyer KYB/KYC after PI acceptance.
- [ ] Escrow provider rejects PortLess KYB, transaction type, product category, corridor, or payout country.
- [ ] Escrow provider outage during create/fund/release/refund.
- [ ] Escrow provider webhook is delayed, duplicated, unsigned, replayed, or has mismatched amount.
- [ ] Escrow funded status is received before local escrow record is committed.
- [ ] Escrow is funded but deal transition to `ESCROW_FUNDED` fails.
- [ ] Tranche release is requested before required proof is verified.
- [ ] Tranche release succeeds at provider but local DB update fails.
- [ ] Tranche release fails after local DB marked it released.
- [ ] Refund succeeds at provider but deal remains active.
- [ ] Refund fails because some tranches are already released.
- [ ] Buyer chargeback/dispute occurs outside the modeled escrow flow.
- [ ] Payout provider outage after escrow release.
- [ ] Supplier payout succeeds but supplier claims non-receipt.
- [ ] Supplier payout fails due to bank validation, sanctions, account mismatch, or INR compliance checks.
- [ ] Ledger does not reconcile after fee, FX, release, refund, or payout.
- [ ] Released amount exceeds funded amount due to rounding, duplicate release, or currency conversion.

## QC, inspection, and quality failures (→ X-6)

- [ ] Buyer changes QC provider after PI.
- [ ] Buyer disputes after QC pass.
- [ ] Buyer-selected QC provider is unavailable in supplier city or requested inspection date.
- [ ] QC booking succeeds but inspector misses appointment.
- [ ] Supplier refuses inspector entry or goods are not ready.
- [ ] QC fails, partially fails, or report is ambiguous.
- [ ] QC report passes quantity but fails packaging, labeling, moisture, contamination, certificate, or sample test.
- [ ] QC provider issues report late, blocking vessel cutoff.
- [ ] QC webhook says pass but report PDF says conditional/fail.
- [ ] QC report has wrong deal ID, supplier, product, batch, quantity, or container number.
- [ ] QC report is tampered with or cannot be verified on provider portal.
- [ ] Buyer disputes inspector credibility after selecting that provider.
- [ ] CRF exists but photos/videos are missing or insufficient.
- [ ] Container loading check photos show seal/package mismatch after QC pass.

## Freight, shipment, customs, and delivery failures (→ X-7)

- [ ] Buyer goes silent after arrival.
- [ ] Goods pass QC but transit damage occurs.
- [ ] BL/shipping bill data mismatch.
- [ ] Customs filing rejected.
- [ ] Freight provider cancels or misses vessel cutoff.
- [ ] Freight booking succeeds but no container is assigned.
- [ ] Container is rolled to next vessel.
- [ ] Vessel delay breaks buyer delivery deadline.
- [ ] Transshipment delay or route change increases risk/cost.
- [ ] Container seal mismatch at port or arrival.
- [ ] Goods are short-shipped, over-shipped, or loaded in the wrong container.
- [ ] Carrier issues BL late or with incorrect consignee/notify party.
- [ ] Freight invoice includes unplanned charges.
- [ ] CHA requests additional documents after filing.
- [ ] ICEGATE rejects shipping bill due to HS code, invoice value, AD code, port, exporter, or product restriction.
- [ ] Customs examination finds mismatch with invoice/packing list.
- [ ] LEO is delayed beyond shipment cutoff.
- [ ] Destination customs hold, inspection, duty dispute, or demurrage occurs.
- [ ] Buyer refuses delivery or fails to clear goods at destination.
- [ ] POD/delivery proof is missing, late, or disputed.

## Document and evidence failures (→ X-8)

- [ ] Generated document uses stale deal data after a price/quantity/provider change.
- [ ] Document template renders wrong legal terms or missing mandatory fields.
- [ ] Document PDF/HTML upload to Drive/object store fails.
- [ ] Local fallback document storage is used in production.
- [ ] Download/view link exposes document to the wrong organization.
- [ ] Document hash stored before final content is uploaded.
- [ ] Invoice, packing list, CoO, SDF, BL, shipping bill, and QC report do not cross-match.
- [ ] Required certificate of origin, phytosanitary certificate, fumigation certificate, lab report, or product-specific permit is missing.
- [ ] Document correction is needed after escrow proof has already been submitted.
- [ ] Provider evidence API returns unavailable/expired file URLs.

## Security, fraud, abuse, and access failures (→ X-9)

- [ ] Webhook secret is unset and production accepts unsigned provider callbacks.
- [ ] Replay attack repeats a valid `funded`, `qc.passed`, or `release` webhook.
- [ ] Malicious email injects prompt instructions into negotiation/RAG context.
- [ ] Buyer/supplier account is assigned to the wrong organization.
- [ ] Non-PortLess user gains access to another organization's deal/document.
- [ ] PortLess staff token is stolen and used to approve payout/refund.
- [ ] Operator account has no MFA and can approve high-risk actions.
- [ ] Password reset/email verification absence allows stale or unverified accounts.
- [ ] API key leakage allows fake connector calls or document access.
- [ ] PII/commercial data is logged in plaintext.

## System, concurrency, and data-integrity failures (→ X-10)

- [ ] Process restarts after provider call succeeds but before local state update.
- [ ] Process restarts after local `PENDING` record but before provider call.
- [ ] Two operators approve the same HITL action concurrently.
- [ ] Two event handlers transition the same deal at the same time.
- [ ] Same opportunity is converted by two requests concurrently.
- [ ] Duplicate idempotency key is reused for a different action.
- [ ] Mongo write succeeds but event publish fails. *(Narrowed 2026-07-17: publish
  failure now degrades to in-process delivery with loud logs, and broker-side
  events are durable/replayed — the remaining hole is a crash in the instant
  between DB commit and publish → transactional outbox, P3-1a.)*
- [~] Event publish succeeds but subscriber fails. *(Handled at bus level
  2026-07-17: NAK redelivery with backoff, then TERM + dead-letter
  `deal.exception` routed to the Exception Resolver. Remaining: operator
  "re-run continuation" action, P3-1d.)*
- [ ] Background watcher auto-resolves while operator is manually resolving.
- [ ] State machine rejects valid recovery path after a partial failure.
- [ ] Clock skew causes token expiry, webhook replay window, or SLA timers to behave incorrectly.
- [ ] RAG/LLM provider outage causes fallback content that is too generic for a regulated product.
- [ ] Health check passes while critical downstream connector is degraded.

## Regulatory, legal, and business failures (→ X-1/X-4/O-6…O-9)

- [ ] Product is restricted, dual-use, food/pharma/chemical regulated, or needs special export license.
- [ ] Buyer/supplier/country appears on sanctions or denied-party list after deal starts.
- [ ] Buyer requires documentary LC, insurance, or certificates PortLess cannot provide.
- [ ] Contract terms do not clearly define inspection standard, dispute window, title transfer, risk transfer, or governing law.
- [ ] Insurance claim is required for damaged/lost cargo but insurance was not purchased.
- [ ] Tax/GST/export incentive treatment is wrong.
- [ ] Platform revenue is booked before collectability is certain.
- [ ] Deal is profitable gross but cash-flow negative due to tranche timing.
- [ ] Tariff or export restriction changes mid-deal.

---

# Appendix B — Reference answers from code review (2026-07-17)

## Opportunity removed after deal starts

Status: mostly implemented — verified in code 2026-07-17.

When a deal is created from an opportunity, the Deals service stores
`opportunity_id`, `match_results` (full Match-Engine score breakdown for every
candidate), `winning_supplier_id`, `winning_cms`, `buyer_target_price_usd`, and
seeded buyer/supplier parties on the `DealRecord` (`services/deals/schemas.py`).
The UI Deal Board (`ui/src/pages/DealBoard.tsx`) shows the CMS chip +
matched-supplier count per deal card and a Match (CMS) column in the table view;
Deal Detail (`ui/src/pages/DealDetail.tsx`) shows the origin opportunity plus the
sourcing match table. Converted opportunities correctly leave the active radar
while their matching data survives on the deal.

Remaining gaps (tracked as UI-1):

- Deal Board shows only the summary (CMS score + count). The full matching
  breakdown a user needs — per-candidate scores, why the winner was selected,
  buyer target price vs supplier FOB, tariff impact, data freshness — is only
  partially on Deal Detail and absent from the board.
- If the opportunity store is reset (in-memory mode), the origin opportunity
  card disappears; `match_results` still survive on the deal. Snapshot the
  opportunity core fields onto the deal at conversion time so the deal never
  depends on the opportunity store at all.

## Initial buyer email

Status: generated, not hardcoded — with one hardcoded fallback path.

The first buyer negotiation message is drafted by the Negotiation Agent LLM using
`BUYER_AGENT_SYSTEM_PROMPT`. The prompt has a specific "FIRST OUTREACH" rule
(guideline 1a): introduce PortLess, state the opening CIF offer plainly,
highlight 2–3 value points, **list the supported QC inspection agencies and
invite the buyer to opt for one**, and close with a call to action — never
referencing prior discussions. Later-round rules (1b/1c) remind an un-opted buyer
once, confirm a chosen inspector, and — when the agreement checklist finds
missing information — request each missing item explicitly without declaring the
deal closed. The subject line is deterministic (`Trade Proposal - HS <code>
Supply Partnership`); the body is LLM-generated and always passes the L0 HITL
approval gate, where operators approve, reject, or modify before dispatch.

Two caveats:

- **LLM-failure fallback IS hardcoded**: if the gateway is exhausted/unparseable
  twice, the round falls back to a one-line template ("Thank you for your offer.
  We propose USD X/unit CIF."). Acceptable as a last resort because it is still
  HITL-gated — and since 2026-07-17 fallback drafts are labeled with risk flags
  on the approval.
- Because the body is LLM-generated, production needs prompt regression tests
  (no supplier-price leakage, CIF offer present, QC options listed, no invented
  certifications), prompt/version tracking per sent email, and an operator edit
  policy before increasing autonomy (P3-4).
