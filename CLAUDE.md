# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

All commands run from the repository root:

```bash
# Install
pip install -r requirements.txt
pip install -r requirements-dev.txt   # adds pytest + httpx test client

# Dev server â€” all services on one port (recommended for local work)
uvicorn app:app --reload --port 8081
# Swagger UI: http://localhost:8081/docs

# Run a single service in isolation (see Makefile for full list)
uvicorn services.deals.main:app --reload --port 8001
make gateway        # port 8000
make deals          # port 8001
make trade-min      # port 8002
make match-engine   # port 8003
make sourcing       # port 8004
make negotiation-agent  # port 8005
make dealmaker      # port 8006
make docugen        # port 8008
make shield-link        # port 8007
make escrow-guard       # port 8009
make hitl-approval      # port 8010
make exception-resolver # port 8011
make email-gateway      # port 8012  (IMAP fetch + SMTP send transport)
make email-processor    # port 8013  (inbound matching + persistence)
make qc-sandbox         # port 8014  (QIMA API contract + SGS / BV portal simulator; tests and demos only)
make escrow-sandbox     # port 8015  (Tazapay API v3 + Escrow.com API simulator; tests and demos only)
make carrier-sandbox    # port 8016  (Maersk booking / Track & Trace / eBL / VGM simulator; tests and demos only)
# auth has no standalone Makefile target â€” it's mounted only in the combined
# monolith (app.py, port 8081); services/api_gateway/ is the standalone edge
# service (make gateway) that fronts everything else in microservice mode.

# Docker â€” one container per service
make up    # docker compose up --build
make down

# Tests
pytest -q                                 # full suite (4536 pass, 1 skip, ~6 min)
pytest -q --cov=agents --cov=services --cov=shared --cov=app --cov-branch --cov-report=term  # 95.46% line + branch (2026-11-09)
pytest tests/test_health.py -q            # smoke: every service boots + /health 200
pytest tests/test_state_machine.py        # deal state machine unit tests
pytest tests/test_match_engine.py         # CMS formula unit tests
pytest tests/test_dealmaker.py            # PI / PO issuance, margin guard
pytest tests/test_docugen.py              # document rendering + customs filing
pytest tests/test_trade_documents.py      # all 13 documents: real deal facts, DRAFT on a missing fact, buyer / supplier never see each other, issued along the trade
pytest tests/test_shield_link.py          # QC, freight, CHA, webhooks
pytest tests/test_qc_inspection.py        # QIMA API (sandbox contract), SGS / BV portal, goods-ready date, poll / webhook / operator status, reports, reschedule
pytest tests/test_packing.py              # buyer's packing: read from replies, asked before agreement, priced, passed to supplier / PI / PO / QIMA, staff endpoint
pytest tests/test_escrow_guard.py         # escrow hold, funded webhook, payout
pytest tests/test_hitl_approval.py        # HITL gate logic, SSE, callbacks
pytest tests/test_exception_resolver.py  # LLM agent, all categories, AUTO_FIX/ESCALATE paths
pytest tests/test_apify_connector.py      # PortLess Apify actors: normalisers, run flow, Trade-Min wiring
pytest tests/test_enrichment.py           # counterparty contact + sanctions enrichment, party PATCH
pytest tests/test_escrow_money_chain.py   # shortfall, replay, tranche order, fees, settlement, payout cap, PAID rule
pytest tests/test_money_continuations.py  # screening gate, capped/balance payouts, tranche/settled legs, RFQ by e-mail
pytest tests/test_supplier_retention.py   # supplier holdback until buyer acceptance: refusal, ledger, gate flags, off switch
pytest tests/test_supplier_commitment.py  # supplier confirms before the PI, standby supplier on decline / silence, late reply from a replaced supplier, PO ack before QC, indicative range
pytest tests/test_trade_costs.py         # cost sheet (location → port, FOB vs EXW), margin on the supplier's price, ledger costs, journal, market check, price basis
pytest tests/test_approval_gate_enforcement.py # money routes need their approved gate, hand-set statuses, relayed acceptance
pytest tests/test_automation.py            # Automation mode: isolation, catalogue parsing, run and clear rules
pytest tests/test_platform_controls.py     # Kill Switch, refund gate, proven approvers, PI validity, PO margin, customs status, PI port, escrow wording, no-price acceptance
pytest tests/test_readiness.py            # production readiness gate
pytest tests/test_gate_policy_v2.py       # gate timeouts / on-timeout defaults / value thresholds, auto-source, delivery replies, supplier reminders
pytest tests/test_batch_b.py              # buyer outreach funnel, catalogue, carrier tracking + delivery window, resolver playbooks, Apify run budget
pytest tests/test_batch_c.py              # four-eyes approvals, double-entry journal, timeline + evidence pack, Telegram + digest
pytest tests/test_negotiation_drafting.py # price bands + margin guarantee, briefing stages, draft hygiene, RFQ / outreach templates
pytest tests/test_company_profile.py      # signature, configured contact lines, no invented track record, intro + RFQ
pytest tests/test_email_quality_fixes.py  # false target / market claims removed, counter price read (not our echo), no double buyer mail, notice wording
pytest tests/test_negotiation_routing.py  # right e-mail to the right party: send preflight, sender verification, RFQ/outreach matching, memory scoping
pytest tests/test_status_notices.py      # status e-mails: logged_only / skipped / failed delivery, new notices, correspondence log
pytest tests/test_compliance.py          # trade compliance: HS policy / destination / screening / related parties / prices / payer, loading + seal, holds
pytest tests/test_compliance_edges.py     # every seed entry, country list, limit and hold rule
pytest tests/test_compliance_money_block.py  # a block-level hold stops every money path; audited false-alarm clearance
pytest tests/test_state_machine_exhaustive.py  # every status pair against a hand-written table
pytest tests/test_escrow_http_and_providers.py # escrow routes, webhook signatures, Stripe / Payoneer / Tazapay / Escrow.com
pytest tests/test_maersk_freight.py       # Maersk connector contract, booking → VGM gate → B/L gate → voyage → delivery on the simulator, freight status, DUPRO / PSI / PEO phases, trade-brief notes, order-confirmed e-mails
pytest tests/test_escrow_providers.py      # buyer's escrow choice, Tazapay / Escrow.com on the simulator: open, pay, submit → provider pays out, dispute, staff relay, proof link, deal status
pytest tests/test_deals_http_and_callbacks.py  # tenancy, deal routes, every saga callback
pytest tests/test_continuations_safety.py # kill switch and replay on every event handler
pytest tests/test_repositories_contract.py # every repository: in-memory and Mongo obey one contract; backend selection
pytest tests/test_routes_and_clients.py   # every route's status codes; every REST client's calls
pytest tests/test_connectors_and_imap.py  # freight, customs, regulatory connectors; IMAP poller; LLM tracking
pytest tests/test_trade_data_and_infra.py # trade-data connectors, Google Drive, NATS, cache, knowledge memory
pytest tests/test_trade_min_pipeline.py   # five scan phases, directory, reply extraction, HITL notifications
pytest tests/test_services_failure_paths.py  # DealMaker, Deals, Email Processor failure paths
pytest tests/test_negotiation_service_paths.py # start / resume / claim, QC choice, gates, stale watcher
pytest tests/test_startup_and_lifespans.py # one failing part never stops boot or shutdown; standalone apps
pytest tests/test_auto_flow_and_agent.py  # dev auto-flow resume, e-mail templates, Exception Resolver agent
pytest tests/test_final_gaps.py           # Apify runs, LLM gateway client, knowledge, catalogue, price book, mapping, autonomy policy
pytest tests/test_docs_orders_playbooks.py # DocuGen storage, Shield-Link failures, playbooks, Sourcing helpers
pytest tests/test_small_modules.py        # API gateway, QC provider HTTP, market intel, sign-in, e-mail history
pytest -k "test_name" -q                  # single test by name

# API connectivity check (trade-min third-party APIs)
python scripts/check_trade_api_connections.py
```

`tests/factories.py` has `make(Model, **fields)`: a valid Pydantic record with every other required field filled. Never call `asyncio.run()` in a test: it clears the event loop older tests rely on. Mongo in tests is `mongomock_motor`; outbound HTTP is `httpx.MockTransport`.

**Known gaps** (no failing test; each needs a decision):
- `DgftConnector.book` (denied-entity check) is a stub that always answers not denied; it needs a real copy of the DGFT list.

---

## Architecture

### Modular monolith â†’ microservices path

Every module (`services/<name>/` or `agents/<name>/`) is a **self-contained FastAPI app** with its own `main.py`, `router.py`, `schemas.py`, `service.py`, and `client.py`. Modules communicate exclusively via the typed REST clients in each module's `client.py`. In dev, all routers mount on `app.py` (port 8081); in production each module runs on its own port (see `Makefile`).

In the combined monolith (`app.py`) services call each other's `service.py` directly via in-process imports to avoid HTTP round-trips. In production microservice mode these calls go via each module's `client.py` over HTTP.

### `shared/` â€” cross-cutting, importable by all modules

| Package | Purpose |
|---|---|
| `shared/domain/enums.py` | `DealStatus`, `ApprovalAction`, `DocumentType`, `AutonomyLevel` enums |
| `shared/domain/state_machine.py` | `DEAL_TRANSITIONS` dict + `can_transition(from, to) â†’ bool` |
| `shared/domain/events.py` | `EventTopic` enum + `DomainEvent` envelope |
| `shared/clients/base.py` | `BaseServiceClient` â€” async httpx wrapper; all service clients extend this |
| `shared/config.py` | `Settings` (pydantic-settings) â€” service registry URLs + cross-cutting env vars |
| `shared/bus/` | `EventPublisher` protocol + three backends: `NoopPublisher`, `InProcessEventBus`, `NatsJetStreamBus` (durable, Synadia Cloud). `factory.init_event_bus()` selects via `EVENT_BUS_BACKEND` (auto â†’ NATS when a `*.creds` file is present) |
| `shared/connectors/` | External API adapters (all implement `quote/book/status/webhook_verify`) |
| `shared/connectors/qc_providers.py` | Buyer-selectable QC inspectors (only **QIMA** enabled today, `QC_PROVIDERS_ENABLED`; `enabled_qc_catalog()` / `bookable_qc_provider()`) — SGS, QIMA, Bureau Veritas, Intertek, TÜV SÜD — plus `QC_PROVIDER_CATALOG` (with `channel`) and alias resolution. **QIMA** (`channel=api`) is the real Public API (developers.qima.com): `POST /auth/v2/token` (MD5 password, `Ai-Api-Access-Token` / `Ai-User-Id` / Bearer / `Referer` headers), `POST /v1.0/inspection`, order detail + report search for status / result (`overrallResult`), `pdf-info` → `file` for the report PDF, `DELETE …/inspection/{id}?reason=`; no webhook, so it is polled. **SGS SMART** and **BV OneSource Connect** have no public API (`channel=portal`): `book` writes the booking request, Shield-Link e-mails it to `<PREFIX>_BOOKING_EMAIL`, results come by signed webhook or staff relay. Intertek / TÜV SÜD keep the generic JSON adapter. Stub when unconfigured. `QC_SANDBOX_PROVIDERS` points QIMA / SGS / BV at `services/qc_sandbox` (in process via ASGI, or `QC_SANDBOX_URL`) — see `docs/integrations/QC_INSPECTION.md` |
| `shared/orchestration/qc_schedule.py` | When the inspection happens: the supplier's goods-ready date (100 % produced, ≥ `QC_MIN_PACKED_PCT` packed), never sooner than `QC_BOOKING_LEAD_BUSINESS_DAYS` working days, weekend → Monday; `parse_ready_date()` reads the date from a supplier e-mail without a model; poll / grace / ship-date settings. `QC_READINESS_DATE_REQUIRED` (default on; pinned off in `tests/conftest.py` and the catalogue run) holds the booking until the date is known: the PO e-mail asks for it, `supplier_commitment` reads it from the PO acknowledgement or a later reply (else `qc_readiness_request`), `POST /shield-link/deals/{id}/qc-readiness` (staff) records a date heard by phone |
| `shared/orchestration/supplier_address.py` + `shared/currency.py` | **The supplier's facility address** (where the inspector goes; a scan knows only the town, which `deals._supplier_contact` carries as a placeholder): asked in the negotiation's `missing_information`, the firm order request and the PO e-mail, read without a model from every verified supplier e-mail (`capture`, labelled address or a line with a PIN code; never over an operator-entered one), `PATCH /deals/{id}/parties/supplier` for staff. `QC_FACILITY_ADDRESS_REQUIRED` (default on; pinned off in `tests/conftest.py` and the catalogue run) holds the QC booking until it is complete (`facility_address_request`; the reply or the staff update books it). **Currency by country**: `party_currency()` — a counterparty is written to in its own currency where PortLess has a rate (India → INR, USD/INR from `market_intel`), else USD; templates print `INR 87,360 (USD 910.00)` plus the rate line (`party_currency` / `fx_rate` in the context — `currency` stays the currency of an amount); the supplier agent gets `write_currency` and `supplier_quoted_in` and mirrors a quotation in the currency it was written in. PO, escrow and payouts stay USD |
| `shared/orchestration/qc_status.py` | **The deal's QC status** (`DealRecord.qc_status` / `qc_status_detail`, `QcStatus` enum): AWAITING_PO_ACK · AWAITING_READY_DATE · NOT_BOOKED · BOOKING_FAILED · REQUESTED · SCHEDULED · INSPECTED · OVERDUE · RESULT_PENDING · REINSPECTION · PASSED · FAILED. `compute()` derives it only from its sources (deal status, supplier commitment, QC orders); `sync()` writes it with the atomic `DealRepository.set_fields` (never a full save) and is called on every deal transition (`deals.update_status`), QC booking (success or failure), provider update (`shield_link.qc.apply_update`), commitment save (PO ack / goods-ready date) and the watcher sweep (OVERDUE). Never raises. The console's Deal Board QC column shows it per deal |
| `shared/orchestration/packing.py` | **The buyer's packing requirements** (`PackingSpec` on the negotiation session: bag type / net weight, liner, marking or private label, pallets, fumigation, container lining, or `standard_ok`). **Asked**: the agreement checklist (`guards.missing_agreement_info`) and briefing withhold the agreement until it is known (`NEGOTIATION_PACKING_REQUIRED`, default on; pinned off in `tests/conftest.py` and the catalogue run); read without a model from the new text of the buyer's reply (`parse_packing`), from its outreach reply, or set by staff (`PUT /deals/{id}/packing`, `GET` shows cost + QC checks). **Passed on**: supplier firm order request (its price must include the packing; its confirmation sets `packing_in_supplier_price`), PI and PO (`packing_spec`), PO e-mail, QC booking (QIMA `unitPacking` / `outerPacking` / `sampleReferenceClientComments`; portal `packing` section) and the booking notice. **Priced**: cost-sheet line `packing` — standard inside FOB, special = `surcharge()` from `TRADE_COST_PACKING_*` / `TRADE_COST_PALLETS_*` / `TRADE_COST_FUMIGATION_*` / `TRADE_COST_CONTAINER_LINING_*` / `TRADE_COST_FLEXIBAG_*` settings paid by PortLess until the supplier confirms it is in its price |
| `shared/connectors/shields.py` | Freight + customs: Cogoport, Freightos, ICEGATE â€” graceful stub when API key absent (re-exports the QC symbols for back-compat) |
| `shared/connectors/payments.py` | Stripe Treasury, Payoneer â€” graceful stub when API key absent |
| `shared/connectors/apify.py` | **PortLess-owned Apify actors** (`rich_minds` account, built with `/actor-pipeline`): `us-importer-finder-ai`, `alibaba-rfq-leads-ai`, `export-market-finder-ai`, `india-supplier-finder-ai`, `company-contact-finder-ai`, `counterparty-check-ai`, `email-list-qualifier-ai`, plus `multi-carrier-tracking-ai` (carrier milestones: one `"NUMBER, carrier, ref"` line per box in `live` mode, container status BOOKED / IN_TRANSIT / ARRIVED / DISCHARGED / DELIVERED / EMPTY_RETURNED mapped onto the shield-link milestones; it looks numbers up through the public carrier Store Actors, which the `rich_minds` plan refuses: the run ends with 0 rows + `OUTPUT.ok: false`, returned as `source_refused` (poll status + readiness `carrier_tracking` warn), never a silent stub). One base (`run` â†’ wait â†’ dataset) + one normaliser per actor onto `BuyerSignal` / `SellerCandidate` / `Party` shapes. Empty result (never raises) without `APIFY_TOKEN`; `APIFY_MAX_RUNS_PER_DAY` is a process-wide daily run budget (`runs_today()` in `describe_actors()`); `tests/conftest.py` pins stub mode so unit tests never start a paid run |
| `shared/catalogue.py` | **Product catalogue / corridors** (`GET/PUT /trade-min/catalogue`, Catalogue page; Mongo `catalogue` collection): `ProductLine` (HS code, name = search keyword, bill-of-lading `products`, `markets`, `seller_profile`, default quantity). With active lines a scan without explicit codes/markets runs product Ã— its markets, Phase 2/4 keywords and the actor inputs come from it, and outreach e-mails name the product. Empty â†’ config defaults |
| `shared/readiness.py` | Production readiness gate: every stub / fallback as a named ok/warn/fail check (`GET /health/readiness`); `ENV_PROFILE=production` refuses to boot while a check fails |
| `shared/orchestration/compliance.py` | **Trade compliance** â€” PortLess is exporter of record, so it answers for what is in the container. **Trade check** at `deal.created` (block â†’ `deal.exception`, no outreach / RFQ) and before the PI (`continuations.pi_after_compliance`): HS export policy (seed of ITC-HS Schedule 2 prohibited / restricted / state-trading items + SCOMET / munitions / possible dual-use; operator confirmations in Mongo `compliance_hs_policy` via `GET/PUT /deals/compliance/hs-policy/{hs}`; never-confirmed code â†’ review once, APPROVE records it Free), destination + party countries (embargoed KP/IR/SY/CU â†’ block, sanctioned / FATF / PK â†’ review), never-screened party (`COMPLIANCE_UNCHECKED_SCREENING`), related buyer/supplier (domain / phone / address / name), DGFT denied / company < 1 year, off-market price vs the price book (this deal excluded) and buyer/supplier spread â†’ block withholds the PI, review â†’ `CONFIRM_TRADE_COMPLIANCE` (L0). **Payer** at `escrow.funded` (`payer_name` / `payer_country` on the webhook must be the buyer). **Shipment check** after QC: the inspector supervises stuffing + seals the container (`LOADING_SUPERVISION` booked with the QC; record via QC webhook loading fields or `POST /shield-link/deals/{id}/loading` â†’ `shipment.loading_recorded`); container + seal present, loaded HS heading = deal, quantity within `COMPLIANCE_QTY_TOLERANCE_PCT` of PO, PI = PO, photos â†’ only then T2 + `BOOK_FREIGHT` are gated; a bill-of-lading seal on a freight milestone (`seal_numbers`) â‰  inspector seal â†’ `deal.exception`. Open findings are **holds** (Mongo `compliance_checks`, `GET /deals/{id}/compliance`): every PO / freight / customs / tranche / payout / acceptance gate of the deal is created at L0 with `compliance:*` flags; APPROVE of such a gate accepts its review flags (`compliance_accept`), never block-level ones. While a block-level hold is open no money leaves: approving a tranche / payout / acceptance gate â†’ 409 (gate stays PENDING), `release_tranche` / `request_payout` / `accept_escrow` refuse, the inspection-window auto-release waits, an unreadable record counts as a block; refunds stay open. `POST /deals/{id}/compliance/holds/seal_mismatch/clear` (staff, reason required, audited in `hold_clearances`) clears a false seal alarm and accepts the seals reported so far; other blocks clear only when the facts change. Seed lists are not the law â€” confirm each HS code against DGFT. Readiness fails in production when switched off; `tests/conftest.py` pins it off for the pipeline tests |
| `shared/orchestration/enrichment.py` | Counterparty identification: contact finder â†’ e-mail check â†’ sanctions / LEI / VIES screening on the Apify actors; results land on the deal `Party` (`email`, `email_status`, `sanctions_status` â€¦). Runs at `DEAL_CREATED`, re-screens the buyer before the PI, and on demand via `POST /deals/{id}/enrich` |
| `shared/llm_gateway/` | Resilient LLM client with Groq fallback chain + MongoDB call tracking |
| `shared/auth/` | Sign-in is **auth-service's**, through the shared RouteToRich API gateway (same as MakeMeRich) â€” PortLess issues no tokens. `identity.py` checks each bearer token with the gateway's `GET /auth/me` (cached 60 s; `AUTH_GATEWAY_URL` â†’ `LLM_GATEWAY_URL`), or believes the gateway's `X-User-*` headers when `AUTH_TRUST_GATEWAY_HEADERS=true`; refuses other applications' tokens when `PORTLESS_ACCOUNT_ID` is set. `AuthMiddleware` (that check + non-Portless allowlist), `dependencies.py` (`AuthUser`, `require_org_scope`, `require_portless`) |
| `shared/orchestration/continuations.py` | **Production automation backbone** â€” event subscribers that move a deal to its next step (opportunity â†’ deal, RFQ, negotiation start, PI, escrow, PO/QC/freight/customs/tranche/payout gates, document pack, counterparty notifications, failure triage). Registered on the bus in the monolith only |
| `shared/orchestration/autonomy_policy.py` | Per-action gate policy **v2**: level L0â€“L3 (override store â†’ env â†’ deal profile â†’ default; money ceiling) **plus** per-action L1 timeout + on-timeout default (negotiation e-mails 4 h â†’ APPROVE, money â†’ REJECT) and a value threshold (`auto_approve_below_usd` per action or `money_auto_approve_below_usd` for all money gates) that drops a gate to L2 only when the amount is under it **and** the caller's preconditions hold (`resolve_gate(action, deal, amount=, conditions_ok=)`). Backs `GET/PUT /hitl-approval/autonomy-policy` |
| `shared/orchestration/kill_switch.py` | **Kill Switch** (`GET/POST /hitl-approval/kill-switch`, staff, reason required, audited in Mongo `platform_controls`; console top bar). While paused: every APPROVE answers 423 (REJECT stays open), no gate auto-approves (L1/L2/L3 are created L0 with flag `platform_paused`), escrow release / acceptance / payout / refund, PO, QC, freight, customs and all e-mail refuse, and the deal, inspection-window and timeout watchers wait. Provider webhooks and replies are still recorded. A store never readable counts as paused |
| `shared/orchestration/outreach.py` | **Buyer outreach funnel**: the buyers a scan identified (`deal.buyer_candidates`) get a templated introduction behind `SEND_BUYER_OUTREACH` (L2) with a `[BUY:<deal>:<candidate>]` subject token; replies are classified interested / declined / unclear; an interested buyer becomes the deal's buyer party and the negotiation opens; declined / silent (`OUTREACH_FOLLOWUP_DAYS`, `OUTREACH_NO_REPLY_DAYS`) â†’ next candidate; exhausted â†’ `deal.exception` (`OUTREACH:exhausted`). `on_negotiation_started` waits while the funnel is open |
| `shared/orchestration/playbooks.py` | Deterministic remediations the Exception Resolver runs before the LLM: QC_FAILED â†’ re-inspection (`DISPUTE â†’ SUPPLIER_DISPATCH_READY`, dispute cleared, fresh QC booking, rework notice; `QC_REINSPECTION_MAX`), PAYMENT_FAILED â†’ shortfall reminder (auto) / `cancel_unfunded` after `ESCROW_FUNDING_GRACE_DAYS` (refund partial funds, CANCELLED, notices), NEGOTIATION_FAILED â†’ next buyer candidate. Money/closing steps run only through the `RESOLVE_EXCEPTION` gate's `playbook` payload |
| `shared/orchestration/evidence.py` | `GET /deals/{id}/timeline` (every dated fact from every service, merged) and `GET /deals/{id}/evidence-pack?format=json|html` (deal, parties, PI/PO, document hashes, orders, ledger + double-entry journal, approvals, exceptions, correspondence, timeline, SHA-256 over the pack) |
| `shared/orchestration/digest.py` + `shared/telegram.py` | Daily operator digest (pending gates by action + age, deals per stage, stalls, open exceptions, money in escrow / released / owed to suppliers, Apify spend) on Telegram (`HITL_TELEGRAM_BOT_TOKEN` / `HITL_TELEGRAM_CHAT_ID`) and/or e-mail (`DIGEST_EMAIL_TO`) at `DIGEST_HOUR_UTC`; `GET /hitl-approval/digest`, `POST /hitl-approval/digest/send`. Telegram also pushes every gate that waits for a human (and countersign requests) |
| `shared/orchestration/matchmaking.py` + `shared/pricebook.py` + `landed_cost.py` + `extraction.py` | **Prospect matching** (MATCHING deals, `MATCHMAKING_ENABLED`, default on): buyer replies are read into a `BuyerRequirement` on the candidate and quotes into a `SupplierOffer` on the deal (LLM gateway, regex fallback); the **price book** (Mongo `price_observations`: quotes, targets, counters, accepted / agreed prices; Comtrade market + India export unit values as prior; 90-day half-life) gives a buyer-side CIF and supplier-side FOB estimate with range; the **landed-cost bridge** turns FOB into the minimum offer that keeps the margin floor after freight / inspection / insurance / escrow fee; every buyer Ã— supplier pair (offers from other prospects of the same HS code included) gets hard filters (MOQ, capacity, grade conflict, verification, timing), P(margin â‰¥ floor), chance of closing and expected value (`GET /deals/{id}/match-board`). `evaluate()` opens `SEND_INDICATIVE_OFFER` (L0 â€” a price range to an interested buyer; accept / counter becomes their price) or, for a firm pair â‰¥ `MATCH_MIN_PROBABILITY`, `CONFIRM_MATCH` (L0 â€” APPROVE sets buyer + supplier parties and moves MATCHING â†’ NEGOTIATING; the deal's other pending `CONFIRM_MATCH` / `SEND_INDICATIVE_OFFER` gates close as superseded — `hitl_approval.withdraw_pending`, outcome `superseded`). With it on, an interested reply or a SOURCE quote no longer jumps the prospect to negotiation; `GET /trade-min/price-book/{hs}` shows the collected prices |
| `shared/knowledge.py` + `shared/orchestration/memory.py` | **Memory in knowledge-service (RouteToRich RAG)** through the API gateway (`<LLM_GATEWAY_URL>/api/knowledge/api/v1`, same route as MakeMeRich, PortLess service-account bearer, collection `KNOWLEDGE_COLLECTION`=portless-trade, vector + graph). Written in the background (never blocks): every delivered outbound e-mail (`email_gateway.sender`) and every inbound one after mapping (`email_processor`), operator notes (`POST /trade-min/knowledge/notes`), catalogue product lines, deal summaries at match confirmed / PI accepted / ended (`deals.update_status`). Read only by LLM steps â€” negotiation drafts (`knowledge_context`), reply reading, exception resolver; never by money decisions. `GET /trade-min/knowledge/search`. Off when the gateway is not configured or `KNOWLEDGE_ENABLED=false` |
| `shared/orchestration/outcomes.py` | **Outcome feedback loop** (Mongo `match_outcomes`): every CONFIRM_MATCH proposal with its prediction â†’ operator decision â†’ signed (PI_ACCEPTED) / failed (NEGOTIATION_FAILED, CANCELLED) â†’ completed / disputed / refunded; indicative offers â†’ accepted / countered / declined. Gives the match board a calibrated chance of closing once `MATCH_CALIBRATION_MIN` (20) matches are resolved, buyer / supplier track-record factors, an offer-price nudge from acceptance rates, and `GET /hitl-approval/learning` (evidence + recommended level per prospect gate â€” shown on Autonomy Settings, never applied automatically) |
| `shared/documents/` | **One design + one source of facts for every trade document** (PI, PO and the 13 DocuGen documents). `render.py`: Jinja environment (service templates, then `templates/_base.html.j2` + `_macros.html.j2` — A4 print layout, key-facts grid, party cards, escrow timeline, amount in words), `render_document()` lists every missing required fact in a DRAFT banner instead of printing a placeholder. `issuer.py`: PortLess as exporter of record from `PORTLESS_*` env (address, IEC, GSTIN, PAN, LUT ARN, AD code, signatory); the services' placeholder address is never printed. `facts.py`: `deal_facts()` — PI price / quantity / incoterm / ports, PO, inspector's loading record (loaded MT, containers, seals), freight booking (vessel, B/L), escrow, payouts, cost sheet (FOB split for SDF / Shipping Bill). Buyer documents never name the supplier or its price; supplier documents never name the buyer |
| `shared/company_profile.py` | **One voice for every counterparty e-mail**: who PortLess is, why buyers / suppliers work with us, how an order works (escrow steps from `escrow_guard.DEFAULT_TRANCHES`), and the signature block. Used by the buyer introduction, the RFQ, the indicative offer, the notices and the negotiation agent (`portless_profile` in its payload; the model writes no sign-off — `with_signature()` strips any and appends the real one). Only true-by-design statements — never add a track record, client count or award; address / phone / website / IEC / GSTIN / signing person come from `PORTLESS_*` env and print only when set |
| `shared/orchestration/notifications.py` | Templated counterparty emails (PI, escrow funding, payment received, PO, QC booking, QC passed / failed, shipment, arrival, goods delivered, escrow stage released, supplier payment sent, refund, settlement, completion, cancellation, reminders) recorded in the negotiation email history and gated by `SEND_DEAL_NOTIFICATION`. Delivery is recorded honestly: `sent` only when SMTP delivered, `logged_only` with no mailbox (`delivery_note`; negotiation logic treats it as issued via `email_history.ISSUED_STATUSES`), `failed` with the transport error, `skipped` when the notice could not be addressed (no thread / no address; retried once it can). `GET /deals/{id}/notifications` is the correspondence log (every e-mail to / from buyer and supplier with its delivery outcome; console Deal page > E-mails) |
| `shared/orchestration/deal_watcher.py` | SLA sweep: buyer reminders for `PI_SENT` / `ESCROW_PENDING`, supplier reminders for `PO_ISSUED` / `SUPPLIER_DISPATCH_READY`, carrier-tracking poll for `IN_TRANSIT` (`TRACKING_POLL_HOURS`), acceptance-window fallback for `CLEARED` (`INSPECTION_WINDOW_FALLBACK_DAYS`), outreach follow-ups, `deal.exception` on PI expiry, unfunded escrow, or any status stalled past `DEAL_STALL_SLA_HOURS` (a MATCHING deal whose buyer funnel is open is not a stall) |
| `shared/orchestration/auto_flow.py` | Dev/test-only auto-continuation task that drives an opportunity-seeded deal through sourcing â†’ negotiation â†’ PI â†’ escrow using real service calls, gated on HITL approvals for PO/freight/payout. Off by default (`AUTO_FLOW_ENABLED`) |

### Service structure pattern

```
services/<name>/
  config.py     # pydantic-settings subclass; reads env vars with service prefix
  main.py       # create_app() â†’ FastAPI with asynccontextmanager lifespan
  router.py     # APIRouter with prefix="/<name>"; maps HTTP â†’ service layer
  schemas.py    # Pydantic request/response models (no business logic)
  service.py    # All business logic; raises typed exceptions caught by router
  repository.py # Protocol + InMemoryFallback + MongoRepository
  client.py     # BaseServiceClient subclass; used by OTHER services to call this one
```

### HITL Approval â€” additional modules

```
services/hitl_approval/
  notification.py  # SSE fan-out (asyncio.Queue per client) + webhook POST
  callbacks.py     # Saga-continuation callbacks: ApprovalAction â†’ downstream service
```

### Deal state machine

`shared/domain/state_machine.py` defines `DEAL_TRANSITIONS` and `can_transition(from, to) â†’ bool`. Every service that transitions deal status coerces the string value first: `DealStatus(deal_status)` then calls `can_transition()`. This is critical â€” passing a raw string or mock object causes a silent dict miss.

```
DRAFT â†’ MATCHING â†’ NEGOTIATING â†” NEGOTIATING_AGREED â†’ NEGOTIATION_FAILED
NEGOTIATION_AGREED â†’ PI_SENT â†’ PI_ACCEPTED â†’ ESCROW_PENDING â†’ ESCROW_FUNDED
  â†’ PO_ISSUED â†’ SUPPLIER_DISPATCH_READY â†’ QC_PASSED | QC_FAILED
QC_PASSED â†’ IN_TRANSIT â†’ CLEARED â†’ PAID
QC_FAILED â†’ DISPUTE â†’ CANCELLED | REFUNDED | SUPPLIER_DISPATCH_READY (re-inspection playbook)
PI_SENT | PI_ACCEPTED | ESCROW_PENDING â†’ CANCELLED (unfunded-deal playbook, operator-gated)
Terminal: PAID, CANCELLED, REFUNDED, NEGOTIATION_FAILED
```

### Repository pattern

Each service implements a `Protocol` with two concrete backends:
- `InMemoryFallbackRepository` â€” activated when `*_MONGO_URI` env var is absent; in-process only.
- `MongoXxxRepository` â€” Motor + Atlas; activated when URI is set.

`init_repository()` selects at startup; `get_repository()` returns the active instance.

**Crash-safety pattern:** all create operations write a `PENDING` record to the repository *before* calling the external connector. A crash during the connector call leaves an audit trace rather than a silent hole.

### Connector pattern

All connectors in `shared/connectors/` follow this interface:
```python
async def quote(request: dict) -> dict   # fetch price / data
async def book(request: dict) -> dict    # place order / send RFQ
async def status(ext_ref: str) -> dict  # poll order status
async def webhook_verify(headers, body) -> bool
```

**Live vs sandbox (QC, freight + customs, escrow + payouts):** one implementation serves the real workflow and automation; `INTEGRATION_MODE` (`shared/integration_mode.py`) is the only difference. `live` (default) — every QC / freight / customs / escrow / payout connector calls the real provider with the configured credentials; a missing key or a `.env` placeholder (`your_..._here`) is sent as-is, the provider refuses it and the step fails visibly (`ConnectorError` → 502 / `deal.exception`), never a made-up success; a portal QC provider (SGS / BV) without a real booking-desk address refuses to book, and a booking e-mail that was not delivered fails the booking. `sandbox` — realistic stubs (plus the QC simulator for `QC_SANDBOX_PROVIDERS`); set only by `services/automation/safety.worker_env()` (`assert_isolated()` refuses anything else) and `tests/conftest.py` (which also blanks `.env` placeholders); readiness `integration_mode` fails in production. Tests of the live HTTP paths set `INTEGRATION_MODE=live` with a mocked transport. Readiness treats a placeholder as unset. Other connectors (trade data, Apify, regulatory) keep the old rule: no key → empty / stub result, never raise in a stub.

### Webhook verification

All provider webhooks use HMAC-SHA256:
- SGS: `x-sgs-signature` header
- Cogoport/Freightos: `x-cogoport-signature` / `x-freightos-signature`
- Stripe: `stripe-signature` with `t=timestamp,v1=<hex>` format (parsed manually)
- Payoneer: `x-payoneer-signature`
- HITL webhook: `x-portless-signature`

On the monolith a provider webhook (`/escrow-guard/webhooks/*`, `/shield-link/{qc,freight}/webhook`, `/email-processor/inbound/{sendgrid,mailgun}`) needs no bearer token: `AuthMiddleware` passes a POST without a valid one as `request.state.anonymous_webhook`, and the route then accepts it only when correctly signed (`shared/webhooks.py`). With that provider's secret unset the connector itself accepts anything, which only a signed-in operator or a standalone service behind the gateway can reach; an anonymous caller is refused. Set every `*_WEBHOOK_SECRET`, `SENDGRID_WEBHOOK_PUBLIC_KEY` / `MAILGUN_SIGNING_KEY` for each provider in use. Mailgun signs only timestamp + token, so an anonymous Mailgun call older than 15 minutes is refused.

### HITL notification to UI

`services/hitl_approval/notification.py` manages two delivery channels:

3. **Telegram push** (`shared/telegram.py`) â€” when `HITL_TELEGRAM_BOT_TOKEN` + `HITL_TELEGRAM_CHAT_ID` are set, every gate that waits for a human (`approval_created` with status PENDING) and every countersign request (`approval_countersign_needed`) is pushed with amount, summary and deep link. Auto-approved gates are not pushed.

**Four-eyes:** a money-moving gate (`RELEASE_ESCROW_TRANCHE`, `RELEASE_SUPPLIER_PAYOUT`, `CONFIRM_BUYER_ACCEPTANCE`) whose `financial_impact_usd` is at or above `FOUR_EYES_THRESHOLD_USD` is created with `required_approvals=2`, is never auto-approved (L2/L3 drop to L0, risk flag `four_eyes_required`), and resolves only when two **different** operators APPROVE (`approvals` lists the signatures; the same operator twice â†’ 409). The approver is the signed-in caller and `resolved_by` in the body is ignored; a standalone service checks the bearer token itself and refuses a two-approver gate without a proven identity (401). One REJECT closes it.

**Gates cannot be walked around over HTTP.** `POST /escrow-guard/escrows/{id}/release`, `.../accept`, `.../refund` and `POST /escrow-guard/payouts` answer 403 unless the body names an `approval_id` that is approved, for that action, and for exactly that money (`hitl_approval.assert_gate_approved`) â€” they exist to retry a gate whose callback failed; the saga itself runs in process. An operator relaying a buyer's acceptance calls `POST /escrow-guard/escrows/{id}/request-acceptance`, which opens the gate. `PATCH /deals/{id}/status` sets only `MATCHING`, `NEGOTIATING`, `NEGOTIATION_FAILED`, `DISPUTE` and `CANCELLED` (not `CANCELLED` while buyer money sits in the escrow); every other status comes from its own gate or provider confirmation.

1. **SSE stream** â€” `GET /hitl-approval/stream` streams `text/event-stream`. One `asyncio.Queue` per connected client; last 50 events buffered for replay on reconnect. Since the browser's `EventSource` API can't set an `Authorization` header, `AuthMiddleware` accepts the bearer token as a `?token=` query param for this one path only (`_QUERY_TOKEN_PATH_SUFFIXES`) â€” every other endpoint still requires the header. The deployed UI opens it against the backend directly (`VITE_SSE_BASE_URL`; the backend's `FRONTEND_URL` must allow that origin) because a serverless gateway cannot hold a stream open.
2. **Webhook POST** â€” when `HITL_NOTIFY_WEBHOOK_URL` is set, a HMAC-signed JSON POST fires on every approval created/resolved.

HITL service also runs a **background timeout watcher** (`run_timeout_watcher()`) that polls every 30s and auto-resolves expired L1 approvals with `default_on_timeout`.

### HITL autonomy levels

| Level | Behavior |
|-------|----------|
| L0 | Always ask â€” PENDING until human resolves; no timeout |
| L1 | Ask with timeout â€” PENDING; auto-resolves after `expires_at` |
| L2 | Auto-approve immediately; notify operator (non-blocking) |
| L3 | Fully autonomous; auto-approve + log warning |

### HITL saga callbacks (`callbacks.py`)

When an approval resolves APPROVE, the `execute_callback()` dispatcher calls into the appropriate service:

| Action | Calls |
|--------|-------|
| `ISSUE_PO` | `dealmaker.service.issue_po()` |
| `BOOK_FREIGHT` | `shield_link.service.book_freight()` |
| `RELEASE_SUPPLIER_PAYOUT` | `escrow_guard.service.request_payout()` |
| `FILE_CUSTOMS` | `shield_link.service.file_cha()` |
| `RESOLVE_EXCEPTION` | Operator acknowledgement, or â€” when the approval carries `playbook` â€” runs it: `reinspect` â†’ `playbooks.book_reinspection()`, `cancel_unfunded` â†’ `playbooks.cancel_unfunded_deal()` |
| `SEND_BUYER_OUTREACH` | `outreach.send_outreach()` â€” templated buyer introduction / follow-up through the e-mail gateway (`[BUY:â€¦]` token); soft REJECT â†’ `skip_candidate()` moves the funnel to the next buyer |
| `SEND_NEGOTIATION_EMAIL` | `negotiation_agent.service.send_approved_email()` (a draft saying escrow money is already there is flagged `premature_funds_claim`, forced L0); soft REJECT â†’ `reject_email()` |
| `CONFIRM_NEGOTIATION_AGREEMENT` | `negotiation_agent.service.finalize_agreement()` â€” the ONLY path to negotiation `AGREED`; soft REJECT â†’ `reject_agreement()` reopens the session at `ROUND` |
| `DISPATCH_RFQ` | `sourcing.service.dispatch_rfq()` (deal reached MATCHING) |
| `CONFIRM_SUPPLIER_SELECTION` | `deals.service.record_match_result()` + transition to `NEGOTIATING` (via `MATCHING` when re-matching after `NEGOTIATION_FAILED`) |
| `CONFIRM_PI_ACCEPTANCE` | `dealmaker.service.accept_pi()` â€” buyer's email reply read as PI acceptance |
| `CONFIRM_SUPPLIER_ORDER` | `supplier_commitment.record_confirmed()` — the supplier's reply to the firm order request read as a confirmation; records the committed price (the PO is written at it) and runs `pi_after_compliance()`; soft REJECT keeps the request open |
| `RELEASE_ESCROW_TRANCHE` | `escrow_guard.service.release_tranche()` â€” staged tranche (T1 PO_ISSUED / T2 QC_PASSED / T3 CLEARED), strictly in schedule order; money-moving, hard REJECT |
| `CONFIRM_TRADE_COMPLIANCE` | `compliance.accept_findings()` (never-confirmed HS code recorded Free) then `continuations.pi_after_compliance()` â€” re-checks, issues the PI; hard REJECT |
| `CONFIRM_COUNTERPARTY_RISK` | `continuations.pi_after_compliance()` â€” buyer screening returned `review`; APPROVE issues the PI the AGREED leg held back (after the trade-compliance check); hard REJECT (`block` never reaches a gate â€” it raises `deal.exception` and withholds the PI) |
| `REFUND_ESCROW` | `escrow_guard.service.refund()`: opened by `POST /escrow-guard/escrows/{id}/request-refund` (reason required); never auto-approved by a value threshold; four-eyes applies; a compliance block does not stop it; soft REJECT |
| `CONFIRM_BUYER_ACCEPTANCE` | `escrow_guard.service.accept_escrow()` â€” buyer's post-delivery e-mail confirmed as acceptance â†’ final (T4) tranche; money-moving, soft REJECT keeps the inspection window running |
| `SEND_DEAL_NOTIFICATION` | `negotiation_agent.service.send_approved_email()` â€” templated counterparty email (`shared/orchestration/notifications.py`) |
| `PARSE_INBOUND_EMAIL` | No-op (informational) â€” operator acts through the Email Processor manual endpoint |

Soft rejects (`callbacks.SOFT_REJECT_ACTIONS`) log and leave the deal where it is; every other REJECT emits `deal.exception`. On callback failure â†’ `deal.exception` emitted with `action_required: manual_retry_or_intervention`.

### Autonomy policy (`shared/orchestration/autonomy_policy.py`)

Every gate above is created from `resolve_gate(action, deal, amount=, conditions_ok=)` (`continuations._create_gate`): the level comes from saved override (`GET/PUT /hitl-approval/autonomy-policy`, the Autonomy Settings page; Mongo-persisted) â†’ `AUTONOMY_<ACTION>` env â†’ the deal's `autonomy_profile` (only for PO / freight / customs / payout / tranche) â†’ built-in default (`DISPATCH_RFQ`, `SEND_DEAL_NOTIFICATION` and `SEND_BUYER_OUTREACH` L2, everything else L0). Money-moving actions are clamped to `AUTONOMY_MONEY_CEILING` (default L1). An L1 gate carries the action's timeout and on-timeout default (`AUTONOMY_<ACTION>_TIMEOUT_SECONDS` / `_ON_TIMEOUT`; negotiation e-mails 4 h â†’ APPROVE, money 24 h â†’ REJECT). A value threshold (`AUTONOMY_<ACTION>_AUTO_APPROVE_BELOW_USD`, or `AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD` for all money gates) makes a gate L2 only while the amount is under it **and** the leg's preconditions hold (tranche: proof refs present + ledger reconciled; payout: reconciled; risk flag `auto_approved_below_value_threshold`). Supplier selection auto-sources (L2, flag `auto_sourced_verified_in_corridor`) when GST + IEC were checked and passed and the quote is inside the buyer's price corridor (`SOURCING_AUTO_SOURCE_VERIFIED`). Overrides no policy lifts: degraded/placeholder negotiation drafts stay L0, the first outbound negotiation email stays L0 while `hitl_on_first_outbound` is on, and `FOUR_EYES_THRESHOLD_USD` forces two approvers. Setting every action to L2 runs a deal end-to-end with no operator click; money release still stops at L1 unless a value threshold is set.

### Exception Resolver Agent

`services/exception_resolver/` is a **LangGraph-powered agent** â€” the only module with LLM-driven business logic outside the negotiation agent.

**Agent flow (`agent.py`):**
0. **Playbook fast path** (`_deterministic_analysis`, `EXCEPTION_PLAYBOOKS_ENABLED`, default on) â€” the recurring failures get a fixed plan without an LLM round-trip, keyed on `context.category` / the watcher's `stage`: `QC_FAILED` â†’ ESCALATE with `playbook=reinspect` while failed inspections â‰¤ `QC_REINSPECTION_MAX`, else CANCEL (refund / replace supplier); `PAYMENT_FAILED` â†’ AUTO_FIX shortfall reminder, MONITOR inside `ESCROW_FUNDING_GRACE_DAYS`, ESCALATE `playbook=cancel_unfunded` after it; `NEGOTIATION_FAILED` â†’ AUTO_FIX next buyer candidate (ESCALATE when the funnel is exhausted); `DOCUMENT_ERROR` / `CONNECTOR_ERROR` â†’ AUTO_FIX. The approval's `callback_payload.playbook` tells `_cb_resolve_exception` what APPROVE runs.
1. `analyze` node â€” otherwise the LLM (via `LLMClient`) classifies the exception into one of 10 categories and builds a JSON remediation plan.
2. `execute` node â€” dispatches on the LLM's `decision`:
   - `AUTO_FIX` â†’ calls `_AUTO_FIX_DISPATCH[category](record)` (e.g. DocuGen regen for `DOCUMENT_ERROR`); falls through to ESCALATE if no handler.
   - `ESCALATE` / `CANCEL` â†’ calls `hitl_approval.service.create_approval()` with `RESOLVE_EXCEPTION` action and full context in `callback_payload`.
   - `MONITOR` â†’ logs + marks RESOLVED, no HITL.

**Exception categories:** `QC_FAILED`, `FREIGHT_DELAY`, `PAYMENT_FAILED`, `NEGOTIATION_FAILED`, `CUSTOMS_REJECTED`, `DOCUMENT_ERROR`, `CONNECTOR_ERROR`, `MARGIN_BREACH`, `APPROVAL_REJECTED`, `CALLBACK_FAILED`, `UNKNOWN`.

**LLM fallback:** if all models are exhausted or the gateway is not configured, returns conservative `UNKNOWN/HIGH/ESCALATE` analysis without raising â€” the pipeline always produces a result.

### Exception Resolver â€” key invariants

- `service.raise_exception()` always persists an `OPEN` record first (crash-safe audit trace).
- Status lifecycle: `OPEN` â†’ `ANALYZING` â†’ `REMEDIATING` â†’ `RESOLVED` | `ESCALATED` | `UNRESOLVABLE`.
- `UNRESOLVABLE` means the agent crashed OR HITL creation failed â€” always requires manual intervention.
- The LLM prompt returns strict JSON; markdown fences are stripped automatically before parsing.

### Right e-mail, right counterparty (`agents/negotiation_agent/email_safety.py`)

Many deals run at once, so every e-mail path checks identity:
- **History** â€” the drafting agents only see `email_history` of their own `negotiation_id`, scoped to their party (buyer agent never sees supplier mail), SENT/RECEIVED only. Knowledge memory reaches a draft only through `knowledge_filter`: this counterparty's own e-mails (`counterparty_address` / company domain), notes about it, product notes â€” never deal summaries (margins), the other side's e-mails, other companies' threads or graph facts.
- **Outbound** â€” `send_approved_email` is idempotent (only DRAFT / PENDING_APPROVAL send) and runs `preflight` right before SMTP: real address; recipient is a current contact of the party the draft was written for (session + deal party + selected candidate) and never the other party; buyer â‰  supplier address; body names nothing of the other side; for agent drafts the session is not FAILED/ESCALATED and the party has not replied (nor a newer e-mail been sent) since drafting. Refusal â†’ status `BLOCKED` + `block_reason` + `EmailBlockedError` (â†’ deal.exception). Draft-time: other side's name / address / domain / price in the body â†’ `counterparty_leak`, no real address â†’ `no_real_address`, both force L0.
- **Inbound** â€” the mapper takes the party from the exact outbound e-mail answered (`EMAIL-â€¦` in In-Reply-To / References), then the address we wrote to, then the folder hint, and sets `sender_check` (`verified` = known address of that party, same company domain allowed, free-mail exact only). Only `verified` / operator override advances a negotiation; `other_party` / `unknown` / no party â†’ `NEEDS_OPERATOR` + `PARSE_INBOUND_EMAIL` (flag `sender_not_verified`), not recorded in the agent's history (there is no default party). RFQ replies (`rfq_sender_supplier`) and outreach replies (`outreach.sender_matches`) use the same rule â€” no domain guess on gmail, no "only one supplier" fallback, a token alone is not enough.

### Automation mode (`services/automation/`)

The console's Automation toggle shows a dashboard of test runs. `POST /automation/runs` starts `worker.py` as its own process, so the running app and the `portless` database are never involved. `safety.worker_env()` blanks everything configured except the Mongo connection and LLM gateway login and forces `MONGO_DB_NAME=portless_automation`; `assert_isolated()` refuses any other database or a real mail, Apify, Telegram or payment credential; the network guard allows only Mongo, the LLM gateway and DNS. E-mail stops at a composed record, and an address not on `.invalid` is refused. Only e-mail drafting reaches the LLM in catalogue runs (classifiers and the resolver use their deterministic fallbacks, as the catalogue was written); the journeys use the LLM throughout, including as the counterparties.

`catalogue.py` reads the Gherkin into cases and iterations; `steps*.py` implement the step vocabulary; `seed.py` builds a deal at any stage. Result statuses: `passed`, `failed`, `not_automated` (a step or fixture has no automation yet, named in the error; never counted as a pass) and `needs_browser` (`@ui` cases: the runner does not drive the console). A case tagged `@journey:UC-nn` is played by that LLM journey and reported under its own TC id too: `TC-E2E-000` → UC-15 (`journeys_full.py`), the whole positive flow from a stored trade-min opportunity to PAID with nothing injected: outreach + RFQ by e-mail, match board, LLM negotiation, Tazapay, QIMA DUPRO / PSI / PEO and Maersk on their simulators (only the GSTN / DGFT registries answer "active"). A gate that a seeded stage would already have is created through the real continuation only before the scenario's first action, so a gate an action should open can never be supplied by the fixture. `POST /automation/clear` deletes only from `portless_automation`: `test_data` keeps results, `everything` does not. Refused on `ENV_PROFILE=production` (start) and during a run (clear). With the toggle on, the console sends every data request through `/automation/view/*` to `view_server.py`, the app on localhost:8082 connected only to `portless_automation`, started on first use. A run first fills `trade_counterparties` with a live Apify scan when the directory is empty (or `scope=live`), then clears its token; test deals use those real companies with `.invalid` mailboxes. `test_data` clear keeps the scanned directory.

### Event publishing

Services call `get_publisher().publish(DomainEvent(...))` after committing state. The backing bus is selected at startup by `shared/bus/factory.init_event_bus()` (`EVENT_BUS_BACKEND`: `auto`/`nats`/`inprocess`/`noop`):

- **NATS JetStream** (`shared/bus/nats_bus.py`) â€” used when a Synadia `*.creds` file is present (auto-discovered in the repository root, or `NATS_CREDS_FILE`). One stream (`PORTLESS_EVENTS`, subjects `portless.<topic>`), one durable pull consumer per subscribed topic, so events survive restarts and replay. At-least-once delivery: handler failure â†’ NAK with backoff; after `NATS_MAX_DELIVER` attempts â†’ TERM + dead-letter `deal.exception`. Poison (unparseable) messages are TERMed. `publish()` never raises â€” broker outage degrades to in-process delivery with a loud log.
- **InProcessEventBus** â€” fallback when no creds; fire-and-forget async fan-out inside one process.

Pipeline continuations (`shared/orchestration/continuations.py`) subscribe in the **monolith only** (`init_event_bus(consume=True)` in `app.py`) â€” they call peer services via in-process imports. Standalone service mode inits publish-only (`consume=False`); published events persist in JetStream until the monolith's durable consumers replay them. `DEAL_EXCEPTION` routes to the Exception Resolver via `on_deal_exception` (resolver's own lifecycle echoes carry `exception_id` and are skipped to prevent loops).

### LLM gateway

`shared/llm_gateway/` wraps every LLM call â€” PortLess holds **no model provider key** (no Groq / OpenAI / Anthropic / Gemini, and no provider SDK in requirements). `LLMClient.chat()` goes to the shared RouteToRich LLM Gateway through the API gateway's `/api/llm` route (`POST /v1/llm`) using the vendored SDK (`remote_sdk.py`, same file as makemerich-backend, built in `remote.py`); the gateway owns model routing, the fallback chain, retries, provider keys and cost metering; `category=` pins its routing category. PortLess's LLM calls run in background handlers with no user request, so instead of forwarding a caller token it signs in as its own auth-service service account (`LLM_GATEWAY_EMAIL` / `_PASSWORD` / `_ACCOUNT_ID` â†’ `POST /auth/login`, bearer cached, refreshed before the 1 h expiry and once on a 401; or a pre-minted `LLM_GATEWAY_TOKEN`). Caller tag `portless.<caller>`. A failed gateway call raises `LLMExhaustedException`; no URL / credential raises `LLMConfigurationError` (callers degrade: template negotiation drafts, resolver escalates). Calls are still tracked in `llm_requests` and the opt-in response cache. `tests/conftest.py` pins `MONGO_URI=""` and `LLM_GATEWAY_URL=""` so the suite never touches the live database or gateway from `.env`.

---

## Implementation status

| Module | Status | Notes |
|---|---|---|
| `auth` | âœ… Full | Identity only (`/identity/me`, organizations, users): local record per auth-service `user_id`, created on first sight (never adopted by email; pre-migration records flagged `legacy`); Portless-staff allowlist; `shared/auth/middleware.py` gates the whole app |
| `api_gateway` | âœ… Full | Edge proxy â€” auth, validation, webhook ingest, idempotency, request fan-out to every other module |
| `trade_min` | âœ… Full | 5-phase concurrent pipeline; MongoDB + APScheduler; operator catalogue / corridors (`GET/PUT /trade-min/catalogue`) drive scan defaults, keywords and actor inputs; buyer / supplier directory (`services/trade_min/directory.py`, Mongo `trade_counterparties`, `GET /trade-min/buyers` Â· `/suppliers`, console page Buyers & Suppliers) keeps every counterparty a scan found even when the corridor yields no opportunity or deal â€” the supplier search runs even for tariff-dropped corridors; once per product per scan a **worldwide** buyer + supplier search (search line without a country; `ScanRequest.worldwide_search`, default on â€” the console's "Worldwide" toggle) adds importers / manufacturers from every country with their own country (ccTLD fallback) â€” directory only, never an opportunity; `?country=IN` (UI "India only") filters; identical concurrent actor calls are coalesced into one run |
| `match_engine` | âœ… Full | Deterministic CMS formula |
| `deals` | âœ… Full | State machine, MongoDB, orchestration; a DRAFT/MATCHING deal is a **prospect** (console Prospects page; Deal Board shows NEGOTIATING onward): no buyer party while the outreach funnel is open (the interested candidate becomes it, `update_party(replace=True)`), no supplier party until the Match Engine's SOURCE winner / a verified winning quote / an operator confirmation (`set_supplier_party`, screens the new supplier); an e-mail the gateway only LOGGED (no mailbox) leaves the candidate `pending` with `send_error` and the RFQ dispatch `success=false`; optional dev/test `AUTO_FLOW_ENABLED` auto-continuation (`shared/orchestration/auto_flow.py`) |
| `sourcing` | âœ… Full | RFQ dispatch + GSTN/DGFT/MCA/Udyam verification |
| `negotiation_agent` | âœ… Full | **Price decided by code, words by the LLM**: `pricing.py` gives every outbound e-mail a band + recommended price (never below the buyer's own offer / above the supplier's own quote; bounds from what the other side itself committed to â€” `buyer_last_offer` / `supplier_last_quote` on the session â€” so any quotable pair clears the margin floor; opening meets a buyer target that clears the target margin, supplier opening `NEGOTIATION_SUPPLIER_OPENING_DISCOUNT_PCT`=4% under its quote; later rounds split the gap; out-of-band model price â†’ corrected + `price_out_of_band` â†’ L0). `briefing.py` gives the agent what the counterparty already told us (outreach reply / `BuyerRequirement`, RFQ `SupplierOffer`), the stage (`cold_introduction` / `first_offer_after_introduction` / `continuing_thread`), the missing facts to ask for, and the PortLess terms it may state (escrow schedule from `escrow_guard.DEFAULT_TRANCHES`); `clean_outbound` strips markdown / "Subject:" lines; product names from `shared/product_label.py`. LangGraph 3-node with static product trade context; drafts + HITL-gates emails, delegates SMTP to `email_gateway`; optional live web-search market intel (`NEGOTIATION_MARKET_SEARCH_ENABLED`); inbound-reply intent via LLM classifier + price-consistency guard; `AGREED` reachable only via the `CONFIRM_NEGOTIATION_AGREEMENT` HITL gate |
| `dealmaker` | âœ… Full | PI/PO, Jinja2 HTML, SHA-256, margin guard, idempotency |
| `docugen` | âœ… Full | 13 documents on one design (`shared/documents`): CI / PL / CoO application / SDF / Shipping Bill checklist, plus Sales Contract, Inspection Order, Shipping Instructions, Insurance Declaration, Delivery & Acceptance Certificate, Remittance Advice, Statement of Account, Credit Note; a missing required fact → `draft` + `draft_reasons` on the record and a DRAFT banner; ICEGATE stub |
| `shield_link` | âœ… Full | QC + freight + CHA; HMAC webhooks; graceful stubs; carrier milestones incl. `DELIVERED` by webhook, operator (`POST /shield-link/freight/{ref}/milestone`) or the tracking-actor poll (`POST /shield-link/deals/{id}/track`); re-inspection bookings; QC lifecycle (`services/shield_link/qc.py`): booking on the supplier's goods-ready date, provider status REQUESTED → CONFIRMED → INSPECTED → REPORT_ISSUED (PASS / FAIL / PENDING) or CANCELLED from the QIMA poll (deal watcher, `QC_POLL_MINUTES`; `POST /shield-link/deals/{id}/qc/poll`), the signed webhook or staff (`POST /shield-link/qc/{ref}/status`); result applied once; report PDFs stored (Drive) with SHA-256 = CRF hash; `GET /shield-link/deals/{id}/qc`, `GET /shield-link/qc/{tpo}/documents/{i}`, `POST /shield-link/qc/{tpo}/reschedule` (cancels at QIMA, rebooks) |
| `escrow_guard` | âœ… Full | Neutral staged escrow (Tazapay / Escrow.com) + legacy Stripe hold + Payoneer payout; funding shortfall â†’ `PARTIALLY_FUNDED`; replay-safe webhooks; ordered tranche release with fees; buyer acceptance window opened by **delivery** (`inspection_window_trigger`, `open_inspection_window`, `/open-window`, `/clear-dispute`); payout capped at net released funds; PAID only when the supplier is paid in full; settlement event; per-deal ledger + `/ledgers`; double-entry journal + CSV (`/deals/{id}/journal`, `/journals`) |
| `hitl_approval` | âœ… Full | L0/L1/L2/L3 gates with per-action timeouts / on-timeout defaults / value thresholds; four-eyes above `FOUR_EYES_THRESHOLD_USD`; SSE stream; webhook + Telegram notify; daily digest; saga callbacks; timeout watcher |
| `automation` | âœ… Full | Console Automation mode (`/automation/*`, staff). Runs `docs/testing/TEST_CASES.md` and 15 real-LLM journeys in a separate worker process (`services/automation/worker.py`) on the `portless_automation` database only; results in its `qa_*` collections. See Automation below |
| `exception_resolver` | âœ… Full | LangGraph agent; LLM classify+plan; AUTO_FIX/ESCALATE/CANCEL/MONITOR; HITL saga; MongoDB |
| `email_gateway` | âœ… Full | **Transport only** â€” single IMAP + SMTP config. Automatic IMAP poll (15 min) forwards inbound mail to `email_processor`; sends outbound via `POST /email-gateway/send` (called from the negotiation flow after HITL approval) |
| `email_processor` | âœ… Full | **Inbound processing only** â€” receives fetched/pushed emails (Gateway forward, SendGrid/Mailgun webhooks, manual), maps to live negotiation, extracts price, persists, triggers `run_round()` |

### Connectors

| Connector | File | Status |
|---|---|---|
| QIMA | `qc_providers.py` | ✅ Real Public API (book / poll / report PDF / cancel) — stub without `QIMA_API_ACCESS_TOKEN` + `QIMA_ACCOUNT` + `QIMA_PASSWORD`; sandbox with `QC_SANDBOX_PROVIDERS` |
| SGS / Bureau Veritas | `qc_providers.py` | ✅ Portal channel (no public API) — booking request to `SGS_BOOKING_EMAIL` / `BUREAU_VERITAS_BOOKING_EMAIL`, result by signed webhook or staff relay |
| Intertek / TÜV SÜD | `qc_providers.py` | ✅ Generic JSON adapter — stub when `<PREFIX>_API_KEY` absent |
| Maersk | `maersk.py` | ✅ Carrier API (default `FREIGHT_PROVIDER`): OAuth client-credentials + `Consumer-Key`; DCSA booking `POST /dcsa/bkg/v2/bookings`, Track & Trace events, eBL get / approve, VGM; webhook `?token=` + re-read; simulator with `CARRIER_SANDBOX_PROVIDERS` — `docs/integrations/MAERSK.md` |
| Cogoport | `shields.py` | âœ… Wired â€” stub when `COGOPORT_API_KEY` absent |
| Freightos | `shields.py` | âœ… Wired â€” stub when `FREIGHTOS_API_KEY` absent |
| ICEGATE | `shields.py` | âœ… Wired â€” stub when `ICEGATE_API_KEY` absent |
| Tazapay | `escrow.py` | ✅ API v3 — `POST /v3/escrow`, `/v3/document` (release documents; Tazapay verifies and pays out), `/v3/refund`, `/v3/escrow/{id}/expire`; webhook `signature` (base64 HMAC over event id + body + created_at); simulator with `ESCROW_SANDBOX_PROVIDERS` — see `docs/integrations/ESCROW_PROVIDERS.md` |
| Escrow.com | `escrow.py` | ✅ API 2017-09-01 — milestone transaction (one item per tranche), `ship` per milestone, buyer accepts on Escrow.com, `cancel`; unsigned webhook → `?token=` + transaction re-read |
| Stripe Treasury | `payments.py` | âœ… Wired â€” stub when `STRIPE_SECRET_KEY` absent |
| Payoneer | `payments.py` | âœ… Wired â€” stub when `PAYONEER_CLIENT_ID` absent |
| Google Drive | `google_drive.py` | âœ… Wired â€” PRIMARY store for generated PI/PO/DocuGen documents; local-disk fallback (loudly logged) when `GOOGLE_DRIVE_REFRESH_TOKEN` absent or an upload fails |
| PortLess Apify actors | `apify.py` | âœ… Wired â€” 8 slots (buyers, demand benchmark, suppliers, contacts, screening, e-mail check, carrier tracking); empty result when `APIFY_TOKEN` absent; daily run budget `APIFY_MAX_RUNS_PER_DAY`. **External actor** (not produced by `/actor-pipeline`'s automatic research loop â€” generate it deliberately with the pipeline skills, row schema on the connector class): `india-supplier-finder-ai`. Carrier tracking runs on `multi-carrier-tracking-ai` (needs a plan that can run public Actors) |

All other connectors (`trade_intel.py`, `demand_signals.py`, `regulatory.py`) make real HTTP calls; missing credentials return empty/error results gracefully.

---

## Environment

Copy `.env.example` to `.env`. Storage and the non-transactional connectors degrade gracefully (in-memory store, empty results) when credentials are absent; QC / freight / escrow / payout keys ship as `your_..._here` placeholders and are always called for real (`INTEGRATION_MODE=live`) — replace a placeholder with the real key to go live. Key vars:

```
# MongoDB â€” ONE connection string + ONE database for the whole app.
# Set MONGO_URI and MONGO_DB_NAME once; every service/module + LLM Gateway +
# shared cache persists into that SAME
# database via its own collection(s) â€” a genuinely single-database deployment,
# not one database per service. Leave everything blank â†’ services fall back to
# in-memory stores (dev/CI only).
MONGO_URI                 # single cluster URL used by every service/module
MONGO_DB_NAME              # single database name (default: portless)
# Optional per-service overrides (each falls back to MONGO_URI / MONGO_DB_NAME
# when unset) â€” only set these if you deliberately want one service on a
# different cluster/database:
#   TRADE_MONGO_URI, DEALS_MONGO_URI, SOURCING_MONGO_URI, NEGOTIATION_MONGO_URI,
#   DEALMAKER_MONGO_URI, DOCUGEN_MONGO_URI, SHIELD_LINK_MONGO_URI,
#   ESCROW_GUARD_MONGO_URI, HITL_APPROVAL_MONGO_URI, EXCEPTION_RESOLVER_MONGO_URI,
#   EMAIL_PROCESSOR_MONGO_URI, LLM_MONGO_URI, CACHE_MONGO_URI
#   (â€¦and the matching <SERVICE>_MONGO_DB_NAME for each)

# LLM (all LLM calls go through shared/llm_gateway; no keys are hardcoded)
LLM_GATEWAY_URL          # shared LLM Gateway via the API gateway (https://api-gateway-two-nu.vercel.app); set â†’ every chat call goes there
LLM_GATEWAY_EMAIL / LLM_GATEWAY_PASSWORD / LLM_GATEWAY_ACCOUNT_ID   # PortLess's auth-service service account (or LLM_GATEWAY_TOKEN)
# No LLM_GROQ_API_KEY / LLM_PRIMARY_* â€” provider keys live in the gateway

# Regulatory (trade-min verification pipeline)
API_SETU_KEY             # GSTN + MCA + Udyam
COMTRADE_SUBSCRIPTION_KEY
VOLZA_API_KEY
ALIBABA_APP_KEY / ALIBABA_APP_SECRET
MACMAP_API_KEY
INDIAMART_API_KEY / INDIAMART_MOBILE

# QC inspection (docs/integrations/QC_INSPECTION.md)
QIMA_API_ACCESS_TOKEN / QIMA_ACCOUNT / QIMA_PASSWORD (or QIMA_PASSWORD_MD5)   # QIMA Public API; QIMA_BASE_URL (default https://ppapi.qima.com), QIMA_REFERER
QIMA_PRODUCT_CATEGORY_ID / _FAMILY_ID / _TYPE_ID   # from GET /parameter/v2/product-tree — required to book; QIMA_SERVICE_TYPE (1 = PSI), QIMA_AQL_*, QIMA_SAMPLE_LEVEL
SGS_BOOKING_EMAIL / BUREAU_VERITAS_BOOKING_EMAIL   # portal providers' booking desks (SGS SMART / BV OneSource Connect have no public API)
QC_READINESS_DATE_REQUIRED (true) / QC_FACILITY_ADDRESS_REQUIRED (true) / QC_BOOKING_LEAD_BUSINESS_DAYS (2) / QC_MIN_PACKED_PCT (80) / QC_POLL_MINUTES (60) / QC_REPORT_GRACE_HOURS (48)
INTEGRATION_MODE             # live (default: real QC / freight / customs / escrow / payout calls) | sandbox (stubs; automation + pytest only)
QC_PROVIDERS_ENABLED         # default qima — the only QC provider booked today; others dormant (buyer choice → QIMA, explicit request → 422); tests/conftest.py pins all
QC_SANDBOX_PROVIDERS / QC_SANDBOX_URL              # provider sandbox — tests + automation only; readiness fails in production while set
# QC inspection providers (shield-link â€” buyer-selectable top-5; each also
# accepts <PREFIX>_BASE_URL). Buyer's opted provider is captured during
# negotiation; its quoted cost feeds cost_envelope.qc_cost in pricing.
SGS_API_KEY / SGS_WEBHOOK_SECRET
QIMA_API_KEY / QIMA_WEBHOOK_SECRET
BUREAU_VERITAS_API_KEY / BUREAU_VERITAS_WEBHOOK_SECRET
INTERTEK_API_KEY / INTERTEK_WEBHOOK_SECRET
TUV_SUD_API_KEY / TUV_SUD_WEBHOOK_SECRET

# Carrier — Maersk (docs/integrations/MAERSK.md)
FREIGHT_PROVIDER (maersk) / FREIGHT_QUOTE_PROVIDER (cogoport) / FREIGHT_POLL_MINUTES (60)
MAERSK_CONSUMER_KEY / _CLIENT_SECRET / _CUSTOMER_CODE / _SERVICE_CONTRACT / _CARRIER_SERVICE_CODE / _WEBHOOK_SECRET / _DEFAULT_EQUIPMENT
CARRIER_SANDBOX_PROVIDERS / CARRIER_SANDBOX_URL   # carrier simulator — automation + tests only
# Inspection phases
QC_DEFAULT_INSPECTION_TYPES (PSI) / QC_DUPRO_PRODUCTION_PCT (30) / QC_DUPRO_DAYS_BEFORE_READY (10) / QIMA_SERVICE_TYPE_DUPRO (2) / QIMA_SERVICE_TYPE_PEO (ask QIMA)

# Freight + customs (shield-link)
COGOPORT_API_KEY
COGOPORT_WEBHOOK_SECRET
FREIGHTOS_API_KEY
FREIGHTOS_WEBHOOK_SECRET
ICEGATE_API_KEY

# Neutral escrow — the buyer chooses (docs/integrations/ESCROW_PROVIDERS.md)
ESCROW_PROVIDERS_ENABLED (tazapay,escrow_com) / ESCROW_DEFAULT_PROVIDER (tazapay) / ESCROW_POLL_MINUTES (30)
TAZAPAY_API_KEY / _API_SECRET / _WEBHOOK_SECRET / _ENV (production|sandbox) / _BASE_URL / _WEBHOOK_URL / _RELEASE_EVENTS
ESCROW_COM_API_KEY / _EMAIL / _WEBHOOK_SECRET / _ENV / _BASE_URL / _INSPECTION_DAYS
PORTLESS_PUBLIC_BASE_URL / ESCROW_PROOF_LINK_SECRET / ESCROW_PROOF_LINK_DAYS   # signed proof-sheet link per stage for the provider
ESCROW_SANDBOX_PROVIDERS / ESCROW_SANDBOX_URL   # provider simulator — automation + tests only

# Payments (escrow-guard)
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
PAYONEER_CLIENT_ID
PAYONEER_CLIENT_SECRET
PAYONEER_WEBHOOK_SECRET

# Documents (dealmaker + docugen) â€” Google Drive is the PRIMARY document store
GOOGLE_DRIVE_CLIENT_ID       # OAuth "Desktop app" client from console.cloud.google.com
GOOGLE_DRIVE_CLIENT_SECRET
GOOGLE_DRIVE_REFRESH_TOKEN   # mint once via: python scripts/google_drive_authorize.py
GOOGLE_DRIVE_ROOT_FOLDER     # default "Portless" â€” docs land in <root>/<deal_id>/<filename>
# Local disk (DEALMAKER_STORAGE_DIR / DOCUGEN_STORAGE_DIR, default
# <OS temp>/portless-docs/<service>) is an emergency fallback only â€” every
# local write logs a loud warning that Drive needs attention.

# PortLess Apify actors (buyer / seller identification + counterparty enrichment)
APIFY_TOKEN                  # Apify API token (auto-discovered from ~/.apify/auth.json in dev unless APIFY_TOKEN_AUTODISCOVER=false)
APIFY_ACTOR_OWNER            # default rich_minds; APIFY_ACTOR_<KEY> overrides one actor (slug / owner~slug / id)
APIFY_ENABLE_AI / APIFY_MAX_ITEMS / APIFY_RUN_TIMEOUT_SECONDS / APIFY_MAX_DISCOVERY_CHARGE_USD / APIFY_WEBHOOK_SECRET
APIFY_MAX_RUNS_PER_DAY       # daily actor-run budget across scans / enrichment / tracking (0 = unlimited)
ENRICHMENT_ON_DEAL_CREATED   # default true â€” contact finder + sanctions screening at deal.created

# Company profile in every e-mail (shared/company_profile.py) — all optional, printed only when set
PORTLESS_LEGAL_NAME / PORTLESS_BRAND_NAME        # default: dealmaker portless_entity_name / "PortLess"
PORTLESS_SENDER_NAME / PORTLESS_SENDER_TITLE     # who signs; else PORTLESS_DESK_NAME (default "Trade Desk")
PORTLESS_WEBSITE / PORTLESS_CONTACT_EMAIL / PORTLESS_PHONE / PORTLESS_ADDRESS / PORTLESS_IEC / PORTLESS_GSTIN

# Buyer outreach funnel (shared/orchestration/outreach.py)
OUTREACH_ENABLED             # default true â€” introduce PortLess to the identified buyers before negotiating
OUTREACH_PARALLEL            # default 2 â€” candidates contacted at a time
OUTREACH_FOLLOWUP_DAYS / OUTREACH_NO_REPLY_DAYS   # default 3 / 7 â€” follow-up, then next candidate

# Delivery â†’ buyer acceptance window (escrow_guard + shield_link + deal watcher)
INSPECTION_WINDOW_TRIGGER    # delivery (default) | proof â€” what starts the final-tranche window
INSPECTION_WINDOW_MILESTONES # default DELIVERED â€” carrier milestones that open the window
INSPECTION_WINDOW_FALLBACK_DAYS  # default 21 â€” open it anyway this long after customs clearance
TRACKING_POLL_HOURS          # default 12 â€” carrier-tracking actor poll for IN_TRANSIT deals
SUPPLIER_RETENTION_ENABLED   # default true â€” hold back the acceptance tranche's share of the supplier's money until the buyer accepts or the window lapses
SUPPLIER_CONFIRMATION_REQUIRED  # default true — the supplier confirms a firm order request before the buyer's PI; decline / silence → next-ranked supplier
SUPPLIER_CONFIRMATION_HOURS     # default 24 — reply window before the standby supplier is asked
PO_ACK_REQUIRED                 # default true — the QC inspection is booked only once the supplier acknowledges the PO
TRADE_COST_INLAND_INR_PER_TONNE_KM (3.5) / TRADE_COST_INLAND_HANDLING_INR_PER_MT (250) / TRADE_COST_INLAND_DEFAULT_KM (600)
TRADE_COST_ORIGIN_HANDLING_USD_PER_CONTAINER (180) / TRADE_COST_CHA_USD_PER_SHIPMENT (150) / TRADE_COST_DOCUMENTS_USD_PER_SHIPMENT (120)
TRADE_COST_CONTINGENCY_PCT (0.5)   # plus LANDED_INSURANCE_PCT (0.5) and the escrow provider fee — all in every price
MARKET_CHECK_ENABLED (true) / MARKET_CHECK_MAX_AGE_HOURS (24)   # pre-trade news check; TAVILY_API_KEY optional (Google News RSS without it)

# Exception playbooks (shared/orchestration/playbooks.py)
EXCEPTION_PLAYBOOKS_ENABLED  # default true â€” deterministic plans before the LLM
QC_REINSPECTION_MAX          # default 1 â€” re-inspections allowed after a failed QC
ESCROW_FUNDING_GRACE_DAYS    # default 14 â€” unfunded this long â†’ cancel_unfunded playbook (operator-gated)

# Trade compliance (shared/orchestration/compliance.py)
COMPLIANCE_ENABLED           # default true â€” HS export policy / destination / parties / prices before outreach and the PI
COMPLIANCE_LOADING_CHECK_REQUIRED  # default true â€” freight + QC tranche wait for the inspector's container + seal record
COMPLIANCE_UNCHECKED_SCREENING     # review (default) | block | allow â€” a never-screened party before the PI
COMPLIANCE_BLOCKED_COUNTRIES / COMPLIANCE_REVIEW_COUNTRIES   # override the embargo / high-risk ISO-2 lists
COMPLIANCE_PRICE_DEVIATION_PCT (40) / COMPLIANCE_MAX_SPREAD_RATIO (1.8) / COMPLIANCE_QTY_TOLERANCE_PCT (5) / COMPLIANCE_MIN_COMPANY_AGE_YEARS (1)

# Controls
FOUR_EYES_THRESHOLD_USD      # money gates at/above it need two distinct approvers (unset = off)
HITL_TELEGRAM_BOT_TOKEN / HITL_TELEGRAM_CHAT_ID   # Telegram push for gates, countersigns, digest
DAILY_DIGEST_ENABLED / DIGEST_HOUR_UTC / DIGEST_EMAIL_TO   # daily operator digest (default on, 06:00 UTC)

# Production readiness gate (shared/readiness.py)
ENV_PROFILE                  # dev | staging | production â€” production refuses to boot while a readiness check fails
READINESS_ENFORCE            # override the boot refusal (never with real money)
STRIPE_WEBHOOK_TOLERANCE_SECONDS  # signed-payload replay window (default 300)

# HITL UI integration
HITL_NOTIFY_WEBHOOK_URL     # optional â€” POST notifications to UI backend
HITL_NOTIFY_WEBHOOK_SECRET  # HMAC signing key for webhook

# Exception Resolver (MongoDB)
EXCEPTION_RESOLVER_MONGO_URI

# Sign-in (auth-service via the RouteToRich gateway; shared/auth/identity.py)
AUTH_GATEWAY_URL            # gateway whose /auth/me checks tokens (default LLM_GATEWAY_URL)
PORTLESS_ACCOUNT_ID         # PortLess's auth-service app account; other apps' tokens â†’ 403 (default LLM_GATEWAY_ACCOUNT_ID)
AUTH_TRUST_GATEWAY_HEADERS  # default false â€” true believes X-User-* headers (only when only the gateway can reach PortLess)
AUTH_INTROSPECTION_CACHE_SECONDS  # default 60 â€” token-check cache (= revocation delay)
FRONTEND_URL                # deployed console origin (CORS for the direct SSE stream)
AUTH_PORTLESS_EMAILS        # comma-separated allowlist â€” who counts as Portless platform staff
AUTH_MONGO_URI               # optional override; falls back to shared MONGO_URI

# Event bus â€” NATS JetStream (Synadia Cloud); falls back to in-process bus
EVENT_BUS_BACKEND            # auto (default) | nats | inprocess | noop
NATS_CREDS_FILE              # Synadia .creds file; auto-discovers *.creds in the repository root if unset
NATS_URL                     # default tls://connect.ngs.global:4222
NATS_STREAM_NAME / NATS_SUBJECT_PREFIX / NATS_ACK_WAIT_SECONDS / NATS_MAX_DELIVER

# Automation (production path â€” see shared/orchestration/)
EVENT_CONTINUATION_ENABLED   # default true â€” event-driven legs that move a deal to its next step
AUTONOMY_<ACTION>            # per-action L0â€“L3 (e.g. AUTONOMY_SEND_NEGOTIATION_EMAIL=L2); also settable from the UI
AUTONOMY_<ACTION>_TIMEOUT_SECONDS / AUTONOMY_<ACTION>_ON_TIMEOUT   # L1 timeout + APPROVE/REJECT default per action
AUTONOMY_<ACTION>_AUTO_APPROVE_BELOW_USD / AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD  # value thresholds (preconditions still apply)
AUTONOMY_MONEY_CEILING       # default L1 â€” hard cap for RELEASE_ESCROW_TRANCHE / RELEASE_SUPPLIER_PAYOUT
SOURCING_AUTO_SOURCE_VERIFIED  # default true â€” verified supplier inside the price corridor is selected without a click
DEAL_NOTIFICATIONS_ENABLED   # default true â€” templated PI / escrow / PO / QC / shipment / arrival / reminder emails to counterparties
DEAL_WATCHER_ENABLED         # default true â€” reminders + stall exceptions (DEAL_PI_REMINDER_AFTER_HOURS, DEAL_ESCROW_REMINDER_AFTER_HOURS, DEAL_PO_ACK_REMINDER_AFTER_HOURS, DEAL_QC_READINESS_REMINDER_AFTER_HOURS, DEAL_REMINDER_MAX, DEAL_STALL_SLA_HOURS, DEAL_WATCHER_INTERVAL_SECONDS)
KNOWLEDGE_ENABLED / KNOWLEDGE_COLLECTION / KNOWLEDGE_GATEWAY_PATH / KNOWLEDGE_TIMEOUT_SECONDS / KNOWLEDGE_ACL  # knowledge-service (RAG) via the gateway; default on when LLM_GATEWAY_URL + service account are set
MATCH_CALIBRATION_MIN        # default 20 â€” resolved matches before the board's chance of closing is calibrated
MATCHMAKING_ENABLED          # default true â€” match board decides the prospect's buyer Ã— supplier (MATCH_MIN_PROBABILITY 0.45, MATCH_OFFER_VALIDITY_DAYS 30)
RFQ_MAX_SUPPLIERS            # default 5 â€” suppliers asked for a quote per prospect
LANDED_FREIGHT_USD_PER_MT[_<CC>] / LANDED_INSURANCE_PCT / LANDED_INSPECTION_USD_PER_SHIPMENT / LANDED_ESCROW_FEE_PCT  # landed-cost assumptions
AUTO_DEAL_FROM_OPPORTUNITY_ENABLED  # default false â€” convert qualifying opportunity.detected events into deals (AUTO_DEAL_MIN_ARBITRAGE_RANK, AUTO_DEAL_MIN_CONFIDENCE, AUTO_DEAL_ORG_ID, AUTO_DEAL_OPERATOR_ID, AUTO_DEAL_AUTONOMY_PROFILE)

# Dev/test-only conveniences (default off; do not enable in production)
AUTO_FLOW_ENABLED            # auto-continue an opportunity-seeded deal through sourcing/negotiation/PI/escrow (still HITL-gated at PO/freight/payout)

# Negotiation Agent â€” optional live market intelligence
NEGOTIATION_MARKET_SEARCH_ENABLED  # default false â€” fetch live Tavily web-search market color before each drafted email
TAVILY_API_KEY               # used by negotiation market intel above
```

Run `python scripts/check_trade_api_connections.py` to test all third-party API connections.

---

## Key cross-module flows

**Opportunity â†’ Deal creation:**
`POST /trade-min/scan` â†’ `opportunity.detected` â†’ `POST /deals` (with `opportunity_id`) â†’ Deals service seeds from Trade-Min + scores via Match Engine â†’ deal at `MATCHING`. With `AUTO_DEAL_FROM_OPPORTUNITY_ENABLED=true` the `on_opportunity_detected` continuation creates the deal itself (thresholds + duplicate guards; a scan builds no opportunity without live demand data and a measured price gap â€” never simulated numbers). If `AUTO_FLOW_ENABLED=true` (dev/test only), creation also fires a background task that drives the deal through the rest of the flows below automatically, stopping at each HITL gate for a real operator decision.

**Buyer / seller identification (Apify actors):**
`POST /trade-min/scan` runs the PortLess actors next to the free APIs: Phase 1 `export-market-finder-ai` (UN Comtrade demand + the market's unit value per MT â†’ `price_benchmark_usd_per_mt`, the real buyer price anchor that replaces the 1.25Ã— FOB heuristic), Phase 2 named buyers and Phase 4 Indian suppliers from **`apify_company_search`** â€” `company-contact-finder-ai` in search mode (`"<product> importers / wholesale distributors, <market>"`, `"<product> exporter / manufacturer, IN"`): real companies with a checked e-mail / phone from their own site; directories, marketplaces and associations dropped; names cleaned from SEO page titles (`_company_name`); skipped when the HS code has no product name (catalogue or `_HS_TO_KEYWORD`), because a bare number searches junk. It replaced `alibaba-rfq-leads-ai`, `us-importer-finder-ai` (both read third-party Store Actors, which the `rich_minds` Apify plan cannot run â†’ 0 rows) and `india-supplier-finder-ai` (not built); those connectors stay registered and `GET /trade-min/sources` shows them as `stub` ("not used"). The plan also refuses Apify's model access, so `APIFY_ENABLE_AI=false`. All seller prices are normalised to USD/MT (`_normalise_price_per_mt`). `GET /trade-min/sources` reports which sources are live/stub/mock. At `deal.created` the enrichment leg runs `company-contact-finder-ai` + `counterparty-check-ai` (+ `email-list-qualifier-ai` for unverified addresses) and writes `email`/`website`/`phone`/`sanctions_status` onto the deal parties, so the negotiation agent's first e-mail goes to a real mailbox; a `block` verdict raises `deal.exception` and forces the RFQ gate to L0. `PATCH /deals/{id}/parties/{role}` lets an operator correct a contact (never overwritten by a later run); `POST /deals/{id}/enrich` re-runs it.

**Buyer outreach funnel (before any negotiation):**
`deal.created` also starts `outreach.start_outreach()`: the first `OUTREACH_PARALLEL` buyer candidates with an address (actor-identified buyers, or added with `POST /deals/{id}/buyer-candidates`) get a templated introduction (actor pitch angle, product from the catalogue, escrow + third-party QC story, clear ask, opt-out) behind `SEND_BUYER_OUTREACH` (L2) with a `[BUY:<deal>:<candidate>]` subject token. The Email Processor maps the reply (`mapper` step 2a) â†’ `outreach.handle_reply()`: interested â†’ candidate `selected`, buyer party set with the real address (`contact_source=outreach:*`), the parked negotiation is opened / re-addressed; declined â†’ next candidate; unclear â†’ `PARSE_INBOUND_EMAIL`. The deal watcher sends one follow-up after `OUTREACH_FOLLOWUP_DAYS`, marks `no_reply` after `OUTREACH_NO_REPLY_DAYS` more and moves on; when every candidate declined or stayed silent â†’ `deal.exception` (`OUTREACH:exhausted`, NEGOTIATION_FAILED playbook â†’ operator). `on_negotiation_started` waits (`waiting_for_buyer`) while the funnel is open, so no buyer is ever pitched a counter-price before saying yes; operator-created deals without candidates are untouched. `POST /deals/{id}/outreach/start` and `.../buyer-candidates/{cid}/skip` drive it by hand.

**Deal at MATCHING â†’ RFQ â†’ Quote intake â†’ Negotiation start:**
`deal.created` (MATCHING) â†’ enrichment (above) â†’ `DISPATCH_RFQ` gate (policy default L2) â†’ `sourcing.dispatch_rfq()` to the top-3 shortlist (SOURCE matches first, else the opportunity's seller candidates; a missing buyer target price is derived as 1.25Ã— the cheapest FOB and logged as a HEURISTIC). Suppliers with an e-mail (actor / contact finder / operator) get the RFQ **by e-mail** with an `[RFQ:<rfq_id>]` subject token; the Email Processor maps the reply (`mapper.rfq_id_from_subject`) â†’ `sourcing.receive_quote_by_email()` (sender matched to the dispatched address, GSTIN/IEC picked from the body) â†’ the normal quote pipeline; no price â†’ `PARSE_INBOUND_EMAIL`. `POST /sourcing/quotes` â†’ GSTN/DGFT/MCA/Udyam verified in parallel â†’ Match Engine re-scores â†’ if SOURCE: patches deal financials + transitions to `NEGOTIATING`; otherwise `quote.received` â†’ `CONFIRM_SUPPLIER_SELECTION` gate (L0; forced L0 with risk flags when GST/IEC verification failed) â†’ APPROVE records the match and moves the deal to `NEGOTIATING`. `negotiation.failed` â†’ `deal.exception` + a re-match `CONFIRM_SUPPLIER_SELECTION` gate for the next-best SOURCE candidate.

**Cost sheet, market check, price basis (`shared/orchestration/trade_costs.py`, `market_check.py`):**
Before the first e-mail (`start_negotiation`) and on every round, `refresh_cost_sheet()` stores on the session every PortLess cost with its source (quote / estimate / setting) and payer: inland transport + origin handling (supplier address → PIN / town / state → usual export port and road km; only PortLess's when the supplier quotes ex-works — `supplier_basis_from()` reads EXW/FOB from its e-mails), export customs agent, export documents, ocean freight, inspection, marine insurance %, escrow fee %, contingency %. Two numbers drive all pricing: `fixed_usd_per_mt` and `pct_of_price` — the buyer band, the supplier ceiling, the orchestrator margin (judged on the supplier's OWN stated price, never our counter), the supplier-switch check and the PO margin guard all use them. The escrow ledger books each cost once incurred (`portless_costs`, actual from the provider order or estimate; `realized_margin` = net − payouts − costs; `margin_before_costs` keeps the old spread) and the journal posts them to `trade_costs` / `service_payables`. The market check (Tavily or Google News RSS + ITC-HS policy, LLM verdict go / caution / avoid, facts kept only with a headline source) runs at negotiation start; `avoid` forces the first e-mail to L0 (`market_check_avoid`). Every drafted price must say what it rests on (`price_basis`, `market_facts` in the payload; a price with no basis words is flagged `price_without_basis`). `GET /deals/{id}/cost-sheet`, `GET /deals/{id}/market-check?refresh=`. `tests/conftest.py` pins `MARKET_CHECK_ENABLED=false`.

**Supplier commitment (`shared/orchestration/supplier_commitment.py`):**
`issue_pi_for_deal` (every path to a PI) first calls `ready_for_pi()`: the supplier gets a templated firm order request (`supplier_order_request`, its own last stated price, never our counter) and the PI waits. Its reply on the negotiation thread is read deterministically in the Email Processor's post-agreement handler: "we confirm" → `CONFIRM_SUPPLIER_ORDER` (L0) → APPROVE issues the PI; a decline, or no reply within `SUPPLIER_CONFIRMATION_HOURS` (deal watcher `sweep()`), sends the request to the next-ranked supplier (SOURCE match results + received quotes) whose own price still clears the margin floor against the agreed buyer price — `set_supplier_party` + the session's seller fields switch to it, so e-mail checks pass; only one supplier holds an open request at a time; none left → `deal.exception` `SUPPLIER:no_confirmation`. A new price or a question → operator; so does a reply from a supplier that no longer holds the request (flag `supplier_replaced`: after a switch its address is still a known supplier address of the thread). After the PO, `on_po_issued` books QC only on the supplier's acknowledgement (e-mail or `POST /deals/{id}/supplier-commitment/po-ack`). `GET /deals/{id}/supplier-commitment`, `.../confirm` (relay a phone confirmation), `.../next` (skip to the next supplier) — staff. The first priced buyer e-mail quotes an indicative range (`pricing.indicative_range`: recommended price down to halfway to the floor); the firm price is the PI. `tests/conftest.py` and the automation catalogue pin both switches off; the LLM journeys run them on.

**Negotiation â†’ PI issuance:**
`POST /negotiation-agent/negotiations/{id}/round` Ã— N rounds. An e-mailed acceptance that names no price accepts our last offer to that party (no offer yet, or not an acceptance: operator). Acceptance is detected by an LLM intent classifier (keyword guard only as gateway-down fallback) + a deterministic price-consistency check, then gated behind a `CONFIRM_NEGOTIATION_AGREEMENT` (L0) HITL approval â€” session parks at `HITL_PENDING`; operator APPROVE â†’ `finalize_agreement()` â†’ `AGREED` â†’ `negotiation.agreed` â†’ the buyer's sanctions screening is (re)checked when older than 30 days: `clear`/`unchecked` â†’ `POST /dealmaker/pi` â†’ deal `PI_SENT`; `review` â†’ `CONFIRM_COUNTERPARTY_RISK` gate (APPROVE issues the PI); `block` â†’ `deal.exception`, no PI. REJECT reopens the negotiation (soft reject, no deal exception).

**PI sent â†’ PI accepted â†’ Escrow:**
`pi.sent` â†’ the PI is emailed to the buyer (`SEND_DEAL_NOTIFICATION`, same `[NEG:<id>]` thread as the negotiation). The buyer's reply maps back through the Email Processor: an unconditional acceptance (`guards.is_explicit_acceptance`) â†’ `CONFIRM_PI_ACCEPTANCE` gate â†’ `accept_pi()`; anything else â†’ `PARSE_INBOUND_EMAIL` approval for a human. An expired PI is refused on both paths (the e-mail goes to a person, flag `pi_expired`) until staff call `POST /dealmaker/pi/{id}/extend` (reason required, audited, document re-rendered). `POST /dealmaker/pi/{id}/accept` (portal/API) remains the other path â†’ deal `PI_ACCEPTED` â†’ `create_escrow()` (neutral staged escrow, sized from the PI, provider fee % recorded) â†’ deal `ESCROW_PENDING` + funding instructions (amount, checkout link, tranche schedule) emailed to the buyer â†’ buyer pays â†’ `POST /escrow-guard/webhooks/escrow-funded` (replay-safe on `event_id`; Stripe signatures also carry a 5-minute replay window) â†’ deal `ESCROW_FUNDED`. A provider total **below the PI** keeps the escrow `PARTIALLY_FUNDED` and the deal in `ESCROW_PENDING` (shortfall e-mail to the buyer + `deal.exception`; the next webhook with the full total completes it) â€” no PO is ever issued against money that is not there. The deal watcher reminds the buyer (max `DEAL_REMINDER_MAX`) and raises `deal.exception` on PI expiry / unfunded escrow.

**Escrow funded â†’ PO â†’ QC:**
`ISSUE_PO` gate â†’ `POST /dealmaker/po` (margin guard: â‰¥8% of the PI's agreed price, else the deal's target price; refused with neither) â†’ deal `PO_ISSUED` â†’ PO emailed to the supplier + `RELEASE_ESCROW_TRANCHE` gate (T1) â†’ `POST /shield-link/qc` â†’ buyer-opted inspector booked â†’ deal `SUPPLIER_DISPATCH_READY` + booking emailed to the supplier â†’ QC webhook â†’ deal `QC_PASSED` (document pack CI/PL/CoO/SDF/Shipping Bill rendered + T2 tranche gate + `BOOK_FREIGHT` gate) or `QC_FAILED` (â†’ deal `DISPUTE` + `deal.exception`; `refund()` then moves `DISPUTE` â†’ `REFUNDED`).

**QC passed â†’ Freight â†’ Customs â†’ Cleared â†’ Paid:**
`BOOK_FREIGHT` gate (destination = the PI's port) â†’ `POST /shield-link/freight` â†’ deal `IN_TRANSIT` â†’ `freight.booked` â†’ `FILE_CUSTOMS` gate (document ids attached) + shipment emailed to the buyer â†’ freight webhook (ARRIVED) â†’ deal `CLEARED` â†’ T3 tranche gate + arrival email + `RELEASE_SUPPLIER_PAYOUT` gate whose amount is **capped at the net funds already released from escrow** (`DealLedger.available_for_payout`; `total_owed` = PO value travels in the payload) â†’ `POST /escrow-guard/payouts` â†’ Payoneer payout â†’ payout webhook (COMPLETED) â†’ deal `PAID` **only once completed payouts reach the PO value**; a partial payout leaves the deal `CLEARED` and `on_payout_completed` gates the balance as `supplier_balance`.

**Escrow providers (`shared/connectors/escrow.py`, `services/escrow_sandbox`, `shared/orchestration/escrow_status.py`, `escrow_proof.py`):**
The buyer chooses Tazapay or Escrow.com: named in a reply (negotiation `detect_escrow_provider_mention` → session + deal `escrow_provider`, source `buyer`), or by staff (`PUT /deals/{id}/escrow-provider`, refused once the escrow is open); resolution request → PI → deal → session → `ESCROW_DEFAULT_PROVIDER`, enabled providers only; the PI prints it, the funding e-mail names it and who releases. **A tranche release only sends the proof**: `release_tranche` → connector `release` answers `SUBMITTED` (Tazapay: documents uploaded with a signed proof-sheet link, Tazapay verifies and pays out; Escrow.com: milestone shipped, the buyer accepts on Escrow.com or its inspection period lapses) → tranche `SUBMITTED` (`escrow.tranche_submitted`) → booked RELEASED (`_apply_tranche_release` / `_apply_final_release`, `escrow.tranche_released`) only when `poll_provider` reads the payout — provider webhook (`/webhooks/tazapay` signed, `/webhooks/escrow-com?token=`; the escrow is always re-read), the escrow watcher every `ESCROW_POLL_MINUTES`, `POST /escrow-guard/escrows/{id}/poll`, or staff relay of a SUBMITTED stage (`…/tranches/{tid}/provider-confirmed`, provider ref + reason, audited). Funding is read the same way (`_apply_funding`); a refused document / rejected milestone → dispute; Escrow.com's final milestone is shipped when the acceptance window opens. Stubs (no `ESCROW_SANDBOX_PROVIDERS`, the pytest default) still release at once. The deal carries `escrow_provider` / `escrow_status` / `escrow_status_detail` for the Deal Board (synced on every escrow event). Not in Tazapay's public v3 docs: a release endpoint and escrow webhook names — releases are read from verified documents / `TAZAPAY_RELEASE_EVENTS`; confirm with Tazapay.

**Carrier shipment (`shared/connectors/maersk.py`, `services/shield_link/freight.py`, `shared/orchestration/freight_status.py`):**
`BOOK_FREIGHT` (after QC + supervised loading) books on Maersk (`FREIGHT_PROVIDER`) with the stuffed containers, the supplier's port → the PI port (UN/LOCODE via `trade_brief.unlocode`) and the trade brief → deal `IN_TRANSIT`. `freight.sync_shipment` (webhook re-read, deal watcher every `FREIGHT_POLL_MINUTES` for IN_TRANSIT / CLEARED, `POST /shield-link/deals/{id}/track`) reads booking + events + eBL: CONFIRMED → containers / vessel / ETA; `SUBMIT_VGM` gate (L0; cargo per box from the inspector's loaded MT + tare; callback `freight.submit_vgm`); draft B/L → `APPROVE_TRANSPORT_DOCUMENT` gate (L0; callback `freight.approve_transport_document`); ISSUED → `freight.transport_document_issued` (buyer e-mail; B/L number joins the T3 proof); ACT DEPA → DEPARTED, ARRI at POD → ARRIVED (deal CLEARED), GTOT at POD / DROP → DELIVERED (acceptance window); REJECTED / CANCELLED → `deal.exception`. Deal `freight_status` / `_detail` for the board; `GET /shield-link/deals/{id}/shipment` for the tracking card. Forwarder bookings (cogoport / freightos) keep the Apify tracking path.

**Inspection phases (`shared/orchestration/inspection_plan.py`):** buyer's DUPRO / PSI / PEO (reply detection or `PUT /deals/{id}/inspection-plan`; deal `inspection_types`, default PSI). DUPRO booked with the PSI (dated inside production, QIMA serviceType 2), PEO booked after freight (customs gate waits for its PASS, `QIMA_SERVICE_TYPE_PEO`); only the PSI moves the deal — DUPRO / PEO emit `qc.phase_result` (`on_qc_phase_result`). Deal `inspections` lists each phase.

**One description for every outside party (`shared/orchestration/trade_brief.py`):** `brief(deal)` + `note(brief, audience)` put deal / PI / PO / product / HS / quantity / route / phase / booking / desk contact into Maersk `remarks` + `references`, QIMA client comments, the SGS / BV request, Tazapay `transaction_description` + metadata, Escrow.com description, every tranche-release note, Payoneer payout description and the ICEGATE filing — a carrier / inspector note has no price or buyer name, an escrow note no supplier, a payout note no buyer or sale price.

**Order confirmed (`notifications.notify_order_confirmed`, at PI accepted after the escrow opens):** one consolidated e-mail per party — buyer: PI, price, escrow provider + stages + who releases, inspection plan, Maersk shipping, documents, next step; supplier: product, quantity, its own FOB price, PO timing, inspection plan at its facility, container stuffing, payment stages, documents needed. Neither names the other side.

**Escrow money chain (`escrow_guard` + `continuations`):**
Tranches (default 30/40/20/10 on PO_ISSUED / QC_PASSED / CLEARED / ACCEPTED) release strictly in schedule order, never while the deal is in `DISPUTE` / `CANCELLED` / `REFUNDED` / `QC_FAILED`, never above the principal; each release accrues the provider fee and emits `escrow.tranche_released`. The buyer's acceptance window for the final tranche opens on **delivery**, not when the goods are still at sea: the carrier's `DELIVERED` milestone (freight webhook, the `multi-carrier-tracking-ai` poll for `IN_TRANSIT` deals every `TRACKING_POLL_HOURS`, or an operator's `POST /shield-link/freight/{ref}/milestone`) â†’ `freight.milestone` â†’ `escrow_guard.open_inspection_window()` (`escrow.inspection_opened` â†’ `inspection_window_open` e-mail; a delivery recorded before the last proof tranche released opens the window on that release; `INSPECTION_WINDOW_FALLBACK_DAYS` after clearance the deal watcher opens it anyway; `inspection_window_trigger=proof` restores the old behaviour). "We accept the goods" â†’ `CONFIRM_BUYER_ACCEPTANCE` gate â†’ `accept_escrow()`; a complaint â†’ `dispute_escrow()` (`/clear-dispute` lifts it, e.g. when a re-inspection is agreed); silence â†’ auto-release after `inspection_window_hours`. The tranche leg also re-gates the next tranche whose trigger status is already reached and tops up the supplier payout. Until the acceptance tranche has released, the supplier's payouts stop short of `retained_until_acceptance` (the same share of the PO value, default 10 %; `SUPPLIER_RETENTION_ENABLED`; enforced by `request_payout`, shown on the ledger, risk flag `supplier_retention_held`), so a deal cannot reach `PAID` before the buyer has accepted or the window has lapsed. When the last tranche leaves the escrow `escrow.settled` fires: the supplier balance is gated, both parties get a settlement notice, and `realized_margin_usd` = net received âˆ’ payouts is patched onto the deal. `GET /escrow-guard/deals/{id}/ledger` (and `/ledgers` for all deals) exposes principal / funded / shortfall / released / fees / net / payouts / available / supplier owed & balance / realised margin with a reconciliation check (`payouts â‰¤ net received` included); the escrow watcher raises `deal.exception` once per breach.

**Books, timeline, evidence:**
`GET /escrow-guard/deals/{id}/journal` (`?format=csv`; `/journals` for all) derives the double-entry journal from the hold and payouts (escrow funded, PO value, tranche net + fee + revenue, refund, supplier payouts) with a trial-balance and escrow-trust check. `GET /deals/{id}/timeline` merges every dated fact of the deal (status changes, outreach, RFQs/quotes, rounds, e-mails, gates and signatures, PI/PO, documents, orders and milestones, escrow events, payouts, exceptions). `GET /deals/{id}/evidence-pack?format=json|html` is the hand-over pack with a SHA-256 over its content.

**Document generation:**
Every document is HTML on one design (`shared/documents`), stored in Google Drive `<GOOGLE_DRIVE_ROOT_FOLDER>/<deal_id>/` (local `<OS temp>/portless-docs/<service>` only as the loud fallback), with its SHA-256 on the record. Issued along the trade, idempotent per deal × type (`continuations._issue_document`, key `auto-doc-<deal>-<TYPE>[-suffix]`):
PI (negotiation agreed; prints the port the buyer named, else "To be confirmed by the buyer") → **Sales Contract** (`pi.accepted`; governing law from `PORTLESS_CONTRACT_LAW`) → PO (escrow funded; port of loading = the supplier's nearest port from the cost sheet) → **Inspection Order** (`qc.booked`) → CI / PL / CoO application / SDF / Shipping Bill checklist (`qc.passed`, re-rendered at `freight.booked` if missing; invoice at the loaded quantity × PI price, FOB = CIF − freight − insurance from the cost sheet) → **Shipping Instructions** + **Insurance Declaration** for CIF / CIP (`freight.booked`) → **Delivery & Acceptance Certificate** (final tranche released: buyer accepted or window lapsed) → **Remittance Advice** (each completed supplier payout) → **Statement of Account** (`escrow.settled`) → **Credit Note** (escrow refund). Customs is filed only at `QC_PASSED` / `IN_TRANSIT` / `CLEARED` → `shield_link.file_cha()` → ICEGATE Shipping Bill + LEO. A document missing a required fact (exporter IEC / GSTIN / LUT / AD code, package count, open policy …) is still issued, marked DRAFT with the reasons on the record; readiness `documents_exporter` fails in production until the exporter facts are set.

**HITL gate (any service, any point):**
Service calls `POST /hitl-approval/approvals` with `action` + `callback_payload` + `autonomy_level`.
- L0/L1: UI notified via SSE stream + webhook; operator resolves at `GET /hitl-approval/approvals/{id}`.
- On APPROVE: saga callback fires the stored `callback_payload` at the target service.
- On REJECT: `deal.exception` emitted; Exception Resolver handles next step.
