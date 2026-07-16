# PortLess Production Readiness Gaps

Created: 2026-07-16 · Updated: 2026-07-17

Purpose: turn PortLess from a strong demo/local system into a production market-trading business that can discover opportunities, negotiate, collect buyer money, execute shipment, pay suppliers, and earn margin with low human intervention and controlled risk.

> The actionable execution backlog derived from this analysis lives in
> [`PRODUCTION_TASK_LIST.md`](PRODUCTION_TASK_LIST.md) — work from that file;
> this one is the reference analysis.

## Recently Closed Gaps (2026-07-16 → 17)

These items from the original list are now implemented and tested (571-test suite green):

- **Multi-provider QC ecosystem** — top-5 buyer-selectable inspectors (SGS, QIMA, Bureau Veritas, Intertek, TÜV SÜD) in `shared/connectors/qc_providers.py`, separated from freight connectors; per-provider API-key/HMAC-secret placeholders; catalog (`GET /shield-link/qc/providers`) and cost quote (`POST /shield-link/qc/quote`) endpoints.
- **QC cost priced into negotiation** — `cost_envelope.qc_cost` is quoted (not zero/static) before the first CIF offer and re-quoted when the buyer opts a provider.
- **Buyer QC opt-in captured from email** — intent classifier extracts named-acceptable agencies (array, rejection-aware) + deterministic name scan; single choice honored; several named → cheapest among them; never opted → cheapest catalog provider at booking (`SHIELD_LINK_QC_FALLBACK_STRATEGY`).
- **Freight cost priced into negotiation** — `POST /shield-link/freight/quote` at negotiation start feeds `logistics_cost` into every margin/floor/acceptance check (was hard-coded 0 → margins were overstated by the entire freight cost, ~$114/MT on the stub lane).
- **Agreement completeness checklist** — `NEGOTIATION_AGREED` is withheld until required info is received (buyer's QC inspector choice, confirmed quantity); the buyer agent drafts an email requesting missing items instead of closing; operator override bypasses (`NEGOTIATION_AGREEMENT_CHECKLIST_ENABLED`).
- **First buyer email content rules** — first outreach lists the supported QC agencies and asks the buyer to opt; inspection cost never presented as a separate charge.

## Current Readiness Summary

PortLess already has the core workflow modeled: opportunity discovery, deal creation, match scoring, sourcing, negotiation, PI/PO, QC/freight/customs, escrow, HITL approvals, email processing, exception handling, auth, and UI pages. The biggest remaining gaps are production integration, automation, money-flow safety, live third-party reliability, and operational controls.

Do not treat "all modules implemented" as "production ready." Many paths still degrade to stubs, log-only sends, in-memory storage, manual operator steps, disabled webhooks, placeholder verification, or best-effort transitions.

## Immediate Answers From Code Review

### Opportunity removed after deal starts

Status: mostly implemented — verified in code 2026-07-17.

When a deal is created from an opportunity, the Deals service stores `opportunity_id`, `match_results` (full Match-Engine score breakdown for every candidate), `winning_supplier_id`, `winning_cms`, `buyer_target_price_usd`, and seeded buyer/supplier parties on the `DealRecord` (`services/deals/schemas.py`). The UI Deal Board (`ui/src/pages/DealBoard.tsx`) shows the CMS chip + matched-supplier count per deal card and a Match (CMS) column in the table view; Deal Detail (`ui/src/pages/DealDetail.tsx`) shows the origin opportunity plus the sourcing match table. Converted opportunities correctly leave the active radar while their matching data survives on the deal.

Remaining gaps (tracked in P4 and PRODUCTION_TASK_LIST.md § UI):

- Deal Board shows only the summary (CMS score + count). The full matching breakdown a user needs — per-candidate scores, why the winner was selected, buyer target price vs supplier FOB, tariff impact, data freshness — is only partially on Deal Detail and absent from the board. Add an expandable "Matching" panel or drill-down from the board card.
- If the opportunity store is reset (in-memory mode), the origin opportunity card disappears; `match_results` still survive on the deal. Production Mongo persistence for Trade-Min and Deals makes converted opportunity records durable — snapshot the opportunity core fields onto the deal at conversion time so the deal never depends on the opportunity store at all.

### Initial buyer email

Status: generated, not hardcoded — with one hardcoded fallback path.

The first buyer negotiation message is drafted by the Negotiation Agent LLM using `BUYER_AGENT_SYSTEM_PROMPT`. The prompt has a specific "FIRST OUTREACH" rule (guideline 1a): introduce PortLess, state the opening CIF offer plainly, highlight 2–3 value points, **list the supported QC inspection agencies and invite the buyer to opt for one**, and close with a call to action — never referencing prior discussions. Later-round rules (1b/1c) remind an un-opted buyer once, confirm a chosen inspector, and — when the agreement checklist finds missing information — request each missing item explicitly without declaring the deal closed. The subject line is deterministic (`Trade Proposal - HS <code> Supply Partnership`); the body is LLM-generated and always passes the L0 HITL approval gate, where operators approve, reject, or modify before dispatch.

Two caveats:

- **LLM-failure fallback IS hardcoded**: if the gateway is exhausted/unparseable twice, the round falls back to a one-line template ("Thank you for your offer. We propose USD X/unit CIF."). Acceptable as a last resort because it is still HITL-gated, but the operator should recognize it as a degraded draft — label fallback drafts in the approval UI.
- Because the body is LLM-generated, production needs prompt regression tests (no supplier-price leakage, CIF offer present, QC options listed, no invented certifications), prompt/version tracking per sent email, and an operator edit policy before increasing autonomy.

## P0 - Must Fix Before Any Real Money Trade

### Production configuration and persistence

- [ ] Configure durable MongoDB for every stateful module: auth, deals, trade_min, sourcing, negotiation_agent, email_processor, dealmaker, shield_link, docugen, escrow_guard, hitl_approval, exception_resolver, RAG.
- [ ] Disable or clearly isolate all in-memory fallbacks in production. In-memory state will lose deals, approvals, emails, escrows, and documents on restart.
- [ ] Disable mock modes and demo seed data in production (`TRADE_MIN_MOCK_MODE`, UI mocks, negotiation mock seed, connector stubs).
- [ ] Add a startup "production readiness check" endpoint that fails if any required key, DB, webhook secret, or document store is missing.
- [ ] Add environment validation by profile: `dev`, `staging`, `production`.

### Real opportunity and supplier data

- [ ] Obtain and configure live data APIs: Comtrade/open trade data, Volza-class buyer data or free alternatives, Alibaba RFQ / IndiaMART, MacMap tariff source, API Setu.
- [ ] Replace graceful connector stubs with live connectors incrementally and show per-source quality/confidence in the UI.
- [ ] Add freshness SLAs for opportunity data: scan time, source, validity window, price date, tariff date.
- [ ] Add duplicate opportunity detection so the same buyer demand is not converted into multiple live deals.
- [ ] Add "opportunity stale" guard before deal creation when buyer signal or seller quote is too old.

### Money custody and escrow

- [ ] Move real trades to neutral escrow providers, not the legacy Stripe hold path inside PortLess's account.
- [ ] Complete live Tazapay / Escrow.com onboarding, sandbox testing, KYB, webhook secrets, payout limits, corridor coverage, and fee confirmation.
- [ ] Wire `PI_ACCEPTED` to escrow creation automatically after buyer choices are known.
- [ ] Wire `qc.passed` and customs/export proof to escrow tranche releases automatically.
- [ ] Wire refund to deal `REFUNDED` transition.
- [ ] Implement supplier payout from released escrow tranches, not manual/legacy payout assumptions.
- [ ] Enforce ledger invariant continuously: held = released + refunded + outstanding, and released = supplier_paid + PortLess_margin + fees.
- [ ] Add double-entry ledger/audit table for every money movement, fee, FX conversion, refund, payout, and adjustment.

### Third-party proof and document authenticity

- [ ] Replace DocuGen ICEGATE stub with real ICEGATE/CHA filing integration.
- [ ] Verify customs proof at source: Shipping Bill, Let Export Order, BL/eBL, carrier event, and document hashes.
- [ ] Implement digital CRF verification beyond webhook HMAC: issuer signature, QR/portal verification, report hash, PDF/photo archival.
- [ ] Add dual-document cross-check before money release: PI, PO, invoice, packing list, BL, shipping bill, QC report, quantities, HS code, buyer/supplier names, port, container/seal.
- [ ] Add document tamper evidence: immutable hashes, timestamps, signer identity, storage URI, and change history.

### Webhook and API security

- [ ] Set every production webhook secret. Current behavior accepts some callbacks when secrets are missing.
- [ ] Implement SendGrid ECDSA verification. It is currently a placeholder.
- [ ] Verify API gateway webhook ingress: signature validation, idempotency keys, dedupe, and route-to-owning-service.
- [ ] Add request rate limiting at auth, gateway, webhooks, negotiation, and email endpoints.
- [ ] Add replay protection for all provider webhooks using timestamp tolerance and event IDs.
- [ ] Add allowlisted origins only; lock down CORS for production.

### Human approval and authorization

- [ ] Add role/permission model for HITL approvals: who can approve email, PO, freight, customs, payout, refund, exception resolution.
- [ ] Add four-eyes approval for money movement, refund, provider change, margin-below-policy, and large deal value.
- [ ] Add per-action autonomy policy: what may be L0, L1, L2, L3 by deal value, counterparty risk, margin, country, and product.
- [ ] Add audit export for all approvals and operator edits.

## P1 - Automation Needed To Reduce Human Intervention

### Event bus and orchestration

- [x] ~~Replace no-op publisher with in-process async event bus~~ — done 2026-07-17 (`shared/bus/publisher.py` `InProcessEventBus`, wired in `app.py`; `EVENT_CONTINUATION_ENABLED` escape hatch).
- [~] Subscribe handlers for deal events — done 2026-07-18 for the six core legs (agreed→PI, PI accepted→escrow, funded→PO gate, PO→QC booking, QC passed→freight gate, cleared→payout gate; all in `shared/orchestration/continuations.py`). Still open: negotiation failed, QC failed, customs filed, payout completed, refund completed, document generation.
- [x] ~~Make every event handler idempotent and state-machine guarded~~ — done for all six handlers (idempotency keys + deal-status validation; HITL gates stay human-approved).
- [ ] Add durable event log collection with event_id, deal_id, topic, payload, result, retries, and error — **the in-process bus loses a trigger if the process dies between state change and handler; this is the outbox upgrade.**
- [ ] Move long-running connector calls to retryable jobs with backoff and dead-letter queue.

### Negotiation automation

- [ ] Add regression tests for first buyer email body: no supplier price leakage, includes CIF offer, lists QC providers, asks buyer to choose inspector, avoids false claims.
- [ ] Add prompt/version tracking for every generated outbound email.
- [ ] Add LLM output validation: JSON schema, price floor, no internal cost/margin leakage, no invented certifications.
- [ ] Use policy deadline/date fields to adjust urgency and escalation.
- [ ] Decide whether operator override can explicitly close a negotiation or must always satisfy convergence guard.
- [ ] Add WhatsApp/Telegram only after email flow is production-stable.

### Email reliability

- [ ] Replace 15-minute IMAP polling with IMAP IDLE or provider webhooks for near-real-time replies.
- [ ] Add outbound email retry and dead-letter queue.
- [ ] Add bounce/delivery-failure ingestion and mark email history failed.
- [ ] Add attachment support for outbound PI, PO, QC report, document packs.
- [ ] Strip quoted reply chains before price extraction to avoid using old quoted prices.
- [ ] Add multi-currency and unit normalization in inbound price extraction.
- [ ] Parse PDF/Excel attachments for quoted prices.
- [ ] Add operator endpoints for inbound email reject, reassign, retry, and ambiguous mapping resolution.

### Sourcing and supplier selection

- [ ] Add multi-supplier RFQ fan-out and automatic best-quote selection.
- [ ] Add retry/backoff for GSTN, DGFT, MCA, Udyam, VIES, and supplier directory failures.
- [ ] Surface each verification result in UI with evidence timestamp and source.
- [ ] Block supplier selection when verification source is degraded unless operator explicitly overrides.
- [ ] Add supplier blacklist/denylist and repeat performance score.

### Logistics automation

- [x] ~~Quote freight into negotiation pricing~~ — done 2026-07-17 (`POST /shield-link/freight/quote` feeds `logistics_cost`); comparison below still open.
- [ ] Add real freight rate comparison across Cogoport, Freightos, SeaRates, carrier-direct APIs (quote endpoint exists for the first two; add multi-provider fan-out + compare).
- [ ] Auto-select freight by price, ETA, reliability, route risk, and buyer preference.
- [ ] Add Vizion or Terminal49 container visibility connector and normalized DCSA milestone events.
- [ ] Add container loading check inspection type for stuffing photos, seal number, and packaging evidence.
- [ ] Add ETA exception handling for delay, transshipment, rolled container, customs hold, demurrage, and lost document.

## P2 - Exception and Negative Scenario Coverage

### Opportunity and market-data failures

- [ ] Opportunity is created from stale buyer import data or old RFQ.
- [ ] Same opportunity is discovered from multiple sources and duplicate deals are created.
- [ ] Buyer signal is fake, spam, scraped incorrectly, or belongs to a non-buying entity.
- [ ] Buyer demand exists but target price is outdated due to commodity price movement.
- [ ] HS code is wrongly classified, causing wrong tariff, documentation, or inspection requirements.
- [ ] Destination country has a temporary import ban, quota, sanctions restriction, or certificate requirement.
- [ ] Seller price source is old, estimated, or not valid for the requested quantity/specification.
- [ ] Tariff API fails open and opportunity margin is overstated.
- [ ] Currency conversion source is unavailable or returns an outlier rate.
- [~] Opportunity margin is positive before freight/QC/escrow/FX fees but negative after all real costs. *(Partially handled 2026-07-17: freight + QC now inside the negotiation margin floor from round 0; escrow fees, FX spread, insurance, and duty are still not in the formula.)*

### Negotiation and communication failures

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

### Sourcing and supplier failures

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

### Contract, PI, PO, and pricing failures

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

### Escrow, payment, refund, and payout failures

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

### QC, inspection, and quality failures

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

### Freight, shipment, customs, and delivery failures

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

### Document and evidence failures

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

### Security, fraud, abuse, and access failures

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

### System, concurrency, and data-integrity failures

- [ ] Process restarts after provider call succeeds but before local state update.
- [ ] Process restarts after local `PENDING` record but before provider call.
- [ ] Two operators approve the same HITL action concurrently.
- [ ] Two event handlers transition the same deal at the same time.
- [ ] Same opportunity is converted by two requests concurrently.
- [ ] Duplicate idempotency key is reused for a different action.
- [ ] Mongo write succeeds but event publish fails.
- [ ] Event publish succeeds but subscriber fails.
- [ ] Background watcher auto-resolves while operator is manually resolving.
- [ ] State machine rejects valid recovery path after a partial failure.
- [ ] Clock skew causes token expiry, webhook replay window, or SLA timers to behave incorrectly.
- [ ] RAG/LLM provider outage causes fallback content that is too generic for a regulated product.
- [ ] Health check passes while critical downstream connector is degraded.

### Regulatory, legal, and business failures

- [ ] Product is restricted, dual-use, food/pharma/chemical regulated, or needs special export license.
- [ ] Buyer/supplier/country appears on sanctions or denied-party list after deal starts.
- [ ] Buyer requires documentary LC, insurance, or certificates PortLess cannot provide.
- [ ] Contract terms do not clearly define inspection standard, dispute window, title transfer, risk transfer, or governing law.
- [ ] Insurance claim is required for damaged/lost cargo but insurance was not purchased.
- [ ] Tax/GST/export incentive treatment is wrong.
- [ ] Platform revenue is booked before collectability is certain.
- [ ] Deal is profitable gross but cash-flow negative due to tranche timing.
- [ ] Tariff or export restriction changes mid-deal.

### Required exception workflows

- [ ] Build scenario-specific exception playbooks with owner, SLA, safe default, and recovery action.
- [ ] Add auto-freeze rules: freeze payout on document mismatch, QC fail, buyer dispute, webhook signature failure, ledger mismatch, or margin breach.
- [ ] Add "safe cancellation" flow: notify parties, refund remaining funds, cancel freight/QC, archive records.
- [ ] Add "manual retry" UI for failed connector calls with idempotency key reuse.
- [ ] Add customer-facing status explanations for buyer/seller without exposing internal margin or supplier price.
- [ ] Add exception severity levels: info, warning, blocked, frozen, financial-risk, legal-risk.
- [ ] Add exception owner routing: negotiation, sourcing, finance, logistics, documents, compliance, engineering.
- [ ] Add SLA timers and escalation ladder for every exception type.
- [ ] Add automatic "freeze deal" action that blocks email sends, PO issuance, freight booking, escrow release, payout, and document filing.
- [ ] Add automatic "freeze money" action that blocks only tranche release/refund/payout while allowing documents and communication.
- [ ] Add recovery checklist per frozen state with required evidence to unfreeze.
- [ ] Add idempotent compensating actions: cancel QC booking, cancel freight, void PI/PO, refund escrow, reverse local release marker, mark payout disputed.
- [ ] Add exception timeline on Deal Detail showing trigger, evidence, owner, SLA, actions taken, and resolution.
- [ ] Add one-click evidence pack generation for disputes: PI, PO, email thread, QC report, photos, BL, shipping bill, ledger, provider callbacks.
- [ ] Add "reprocess from checkpoint" for stuck deals: rerun from last safe status without duplicating provider calls.
- [ ] Add root-cause taxonomy so repeated failures become product tasks, not one-off operator notes.
- [ ] Add post-resolution audit: who resolved, why, financial impact, customer impact, and prevention task.

## P3 - Production Operations

### Observability

- [ ] Add correlation IDs across request, event, approval, email, provider call, and deal log.
- [ ] Add structured logs and dashboards by deal_id and event_id.
- [ ] Add metrics: opportunity conversion, email send/reply latency, negotiation rounds, match score distribution, gross margin, failed connector calls, approval wait time, escrow funding lag, QC pass rate, shipment delay rate.
- [ ] Add alerts for: webhook verification disabled, DB fallback active, email log-only mode, pending approval backlog, failed money movement, unreconciled ledger, stale deal, stuck status.

### Testing and release

- [ ] Add end-to-end staging test with live sandbox providers where available.
- [ ] Add contract tests for every connector.
- [ ] Add load/smoke tests for scan, create deal, negotiate, issue PI/PO, book QC/freight, escrow release.
- [ ] Add chaos tests for provider timeout, duplicate webhook, restart mid-call, partial DB write.
- [ ] Add UI tests for Deal Board, Deal Detail, approvals, org user view, document downloads, payments tab.
- [ ] Add production deploy checklist and rollback plan.

### Compliance and legal

- [ ] Confirm merchant-exporter legal model, invoice flow, tax obligations, GST/export compliance, IEC, and liability terms.
- [ ] Draft buyer terms, supplier terms, cancellation terms, dispute policy, privacy policy, and data processing terms.
- [ ] Confirm sanctions/AML/KYB responsibilities with escrow provider and add denylist screening if not fully covered.
- [ ] Confirm insurance needs: cargo insurance, trade credit insurance, professional liability, cyber.
- [ ] Confirm product-specific compliance for restricted/regulated HS codes.

## P4 - UI and Product Gaps

- [ ] Show full matching info on Deal Board and Deal Detail, including scored suppliers, why winner was selected, source confidence, buyer price, supplier FOB, tariff impact, and stale-data warnings.
- [ ] Add buyer-facing portal beyond read-only deals: escrow funding link, PI acceptance, QC provider selection, shipment tracking, document pack, dispute/accept buttons.
- [ ] Add supplier-facing portal: RFQ response, PO acceptance, production milestones, document upload, payout status.
- [ ] Add operation dashboard: stuck deals, margin risk, pending approvals, provider outages, funding delays, shipment delays.
- [ ] Add money dashboard: expected margin vs realized margin, fees, FX, escrow releases, supplier payouts, refunds, reconciliation.
- [ ] Add per-deal timeline with all events, approvals, emails, documents, provider callbacks, and money movements.
- [ ] Add "why this action is blocked" explanations for operators.

## Nice-To-Have Revenue Features

- [ ] Auto-rank opportunities by expected net profit after all fees, freight, QC, escrow, FX, tariff, and risk reserve.
- [ ] Add repeat-buyer reorder prediction and auto-follow-up campaigns.
- [ ] Add preferred supplier marketplace with performance history.
- [ ] Add dynamic margin strategy: higher margin for high risk, lower margin for repeat buyers.
- [ ] Add trade credit insurance quoting for higher-value deals.
- [ ] Add bank LC option for large enterprise buyers.
- [ ] Add automatic quote expiration and revalidation.
- [ ] Add corridor playbooks by country/product with required documents, mandated PSI agencies, customs risks, and common buyer objections.
- [ ] Add CRM-style buyer/supplier profiles and relationship history.
- [ ] Add revenue forecast and cash requirement forecast by active deal stage.

## Suggested Execution Order

1. Production config gate: no mocks, no in-memory, no missing webhook secrets, no log-only email in production.
2. Secure email + negotiation: first buyer/supplier outreach tests, SendGrid/Mailgun verification, retry/dead-letter, inbound operator tools.
3. Neutral escrow path: provider onboarding, PI accepted -> escrow open, funded -> PO, QC/customs -> tranche release, refund -> deal refunded.
4. Third-party proof: real QC report archival/verification, real customs filing, document cross-check.
5. Event/retry backbone: event bus, event log, retryable jobs, idempotent handlers.
6. Live supplier/buyer data: real connector rollout with confidence and freshness guards.
7. Buyer/supplier portals and reduced operator workload.
8. Advanced optimization: freight comparison, shipment visibility, dynamic pricing, insurance, analytics.

## Definition Of Production Ready

PortLess is production ready only when:

- A real buyer can accept terms and fund neutral escrow.
- A real supplier can be verified, contracted, and paid from released funds.
- Every money movement is reconciled and reversible where legally possible.
- Every third-party callback is authenticated, idempotent, and auditable.
- A deal can survive process restarts without losing state.
- No live path depends on mock data, in-memory repositories, log-only sends, or placeholder verification.
- Exceptions freeze risky actions and give operators a clear recovery path.
- The system can prove why it made each decision and what evidence was used.
- PortLess margin is protected after freight, QC, escrow fees, FX, tariffs, refunds, and delays.
