# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

All commands run from `backend/`:

```bash
# Install
pip install -r requirements.txt
pip install -r requirements-dev.txt   # adds pytest + httpx test client

# Dev server — all services on one port (recommended for local work)
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
# auth has no standalone Makefile target — it's mounted only in the combined
# monolith (app.py, port 8081); services/api_gateway/ is the standalone edge
# service (make gateway) that fronts everything else in microservice mode.

# Docker — one container per service
make up    # docker compose up --build
make down

# Tests
pytest -q                                 # full suite (492 pass)
pytest tests/test_health.py -q            # smoke: every service boots + /health 200
pytest tests/test_state_machine.py        # deal state machine unit tests
pytest tests/test_match_engine.py         # CMS formula unit tests
pytest tests/test_dealmaker.py            # PI / PO issuance, margin guard
pytest tests/test_docugen.py              # document rendering + customs filing
pytest tests/test_shield_link.py          # QC, freight, CHA, webhooks
pytest tests/test_escrow_guard.py         # escrow hold, funded webhook, payout
pytest tests/test_hitl_approval.py        # HITL gate logic, SSE, callbacks
pytest tests/test_exception_resolver.py  # LLM agent, all categories, AUTO_FIX/ESCALATE paths
pytest tests/test_apify_connector.py      # PortLess Apify actors: normalisers, run flow, Trade-Min wiring
pytest tests/test_enrichment.py           # counterparty contact + sanctions enrichment, party PATCH
pytest tests/test_escrow_money_chain.py   # shortfall, replay, tranche order, fees, settlement, payout cap, PAID rule
pytest tests/test_money_continuations.py  # screening gate, capped/balance payouts, tranche/settled legs, RFQ by e-mail
pytest tests/test_readiness.py            # production readiness gate
pytest -k "test_name" -q                  # single test by name

# API connectivity check (trade-min third-party APIs)
python scripts/check_trade_api_connections.py
```

---

## Architecture

### Modular monolith → microservices path

Every module (`services/<name>/` or `agents/<name>/`) is a **self-contained FastAPI app** with its own `main.py`, `router.py`, `schemas.py`, `service.py`, and `client.py`. Modules communicate exclusively via the typed REST clients in each module's `client.py`. In dev, all routers mount on `app.py` (port 8081); in production each module runs on its own port (see `Makefile`).

In the combined monolith (`app.py`) services call each other's `service.py` directly via in-process imports to avoid HTTP round-trips. In production microservice mode these calls go via each module's `client.py` over HTTP.

### `shared/` — cross-cutting, importable by all modules

| Package | Purpose |
|---|---|
| `shared/domain/enums.py` | `DealStatus`, `ApprovalAction`, `DocumentType`, `AutonomyLevel` enums |
| `shared/domain/state_machine.py` | `DEAL_TRANSITIONS` dict + `can_transition(from, to) → bool` |
| `shared/domain/events.py` | `EventTopic` enum + `DomainEvent` envelope |
| `shared/clients/base.py` | `BaseServiceClient` — async httpx wrapper; all service clients extend this |
| `shared/config.py` | `Settings` (pydantic-settings) — service registry URLs + cross-cutting env vars |
| `shared/bus/` | `EventPublisher` protocol + three backends: `NoopPublisher`, `InProcessEventBus`, `NatsJetStreamBus` (durable, Synadia Cloud). `factory.init_event_bus()` selects via `EVENT_BUS_BACKEND` (auto → NATS when a `*.creds` file is present) |
| `shared/connectors/` | External API adapters (all implement `quote/book/status/webhook_verify`) |
| `shared/connectors/qc_providers.py` | Buyer-selectable QC inspectors — SGS, QIMA, Bureau Veritas, Intertek, TÜV SÜD — plus `QC_PROVIDER_CATALOG` metadata and alias resolution; graceful stub when API key absent |
| `shared/connectors/shields.py` | Freight + customs: Cogoport, Freightos, ICEGATE — graceful stub when API key absent (re-exports the QC symbols for back-compat) |
| `shared/connectors/payments.py` | Stripe Treasury, Payoneer — graceful stub when API key absent |
| `shared/connectors/apify.py` | **PortLess-owned Apify actors** (`rich_minds` account, built with `/actor-pipeline`): `us-importer-finder-ai`, `alibaba-rfq-leads-ai`, `export-market-finder-ai`, `india-supplier-finder-ai`, `company-contact-finder-ai`, `counterparty-check-ai`, `email-list-qualifier-ai`. One base (`run` → wait → dataset) + one normaliser per actor onto `BuyerSignal` / `SellerCandidate` / `Party` shapes. Empty result (never raises) without `APIFY_TOKEN`; `tests/conftest.py` pins stub mode so unit tests never start a paid run |
| `shared/readiness.py` | Production readiness gate: every stub / fallback as a named ok/warn/fail check (`GET /health/readiness`); `ENV_PROFILE=production` refuses to boot while a check fails |
| `shared/orchestration/enrichment.py` | Counterparty identification: contact finder → e-mail check → sanctions / LEI / VIES screening on the Apify actors; results land on the deal `Party` (`email`, `email_status`, `sanctions_status` …). Runs at `DEAL_CREATED`, re-screens the buyer before the PI, and on demand via `POST /deals/{id}/enrich` |
| `shared/llm_gateway/` | Resilient LLM client with Groq fallback chain + MongoDB call tracking |
| `shared/rag/` | MongoDB Atlas vector store + RAG chunking pipeline |
| `shared/auth/` | `AuthMiddleware` (JWT bearer enforcement + non-Portless allowlist), `dependencies.py` (`require_org_scope`, `require_portless`), `blacklist.py` (logout/token revocation), `security.py` (hash/verify/`create_access_token`) |
| `shared/orchestration/continuations.py` | **Production automation backbone** — event subscribers that move a deal to its next step (opportunity → deal, RFQ, negotiation start, PI, escrow, PO/QC/freight/customs/tranche/payout gates, document pack, counterparty notifications, failure triage). Registered on the bus in the monolith only |
| `shared/orchestration/autonomy_policy.py` | Per-action L0–L3 resolution for every HITL gate (override store → env → deal profile → default; money ceiling). Backs `GET/PUT /hitl-approval/autonomy-policy` |
| `shared/orchestration/notifications.py` | Templated counterparty emails (PI, escrow funding, PO, QC booking, shipment, arrival, completion, reminders) recorded in the negotiation email history and gated by `SEND_DEAL_NOTIFICATION` |
| `shared/orchestration/deal_watcher.py` | SLA sweep: buyer reminders for `PI_SENT` / `ESCROW_PENDING`, `deal.exception` on PI expiry, unfunded escrow, or any status stalled past `DEAL_STALL_SLA_HOURS` |
| `shared/orchestration/auto_flow.py` | Dev/test-only auto-continuation task that drives an opportunity-seeded deal through sourcing → negotiation → PI → escrow using real service calls, gated on HITL approvals for PO/freight/payout. Off by default (`AUTO_FLOW_ENABLED`) |

### Service structure pattern

```
services/<name>/
  config.py     # pydantic-settings subclass; reads env vars with service prefix
  main.py       # create_app() → FastAPI with asynccontextmanager lifespan
  router.py     # APIRouter with prefix="/<name>"; maps HTTP → service layer
  schemas.py    # Pydantic request/response models (no business logic)
  service.py    # All business logic; raises typed exceptions caught by router
  repository.py # Protocol + InMemoryFallback + MongoRepository
  client.py     # BaseServiceClient subclass; used by OTHER services to call this one
```

### HITL Approval — additional modules

```
services/hitl_approval/
  notification.py  # SSE fan-out (asyncio.Queue per client) + webhook POST
  callbacks.py     # Saga-continuation callbacks: ApprovalAction → downstream service
```

### Deal state machine

`shared/domain/state_machine.py` defines `DEAL_TRANSITIONS` and `can_transition(from, to) → bool`. Every service that transitions deal status coerces the string value first: `DealStatus(deal_status)` then calls `can_transition()`. This is critical — passing a raw string or mock object causes a silent dict miss.

```
DRAFT → MATCHING → NEGOTIATING ↔ NEGOTIATING_AGREED → NEGOTIATION_FAILED
NEGOTIATION_AGREED → PI_SENT → PI_ACCEPTED → ESCROW_PENDING → ESCROW_FUNDED
  → PO_ISSUED → SUPPLIER_DISPATCH_READY → QC_PASSED | QC_FAILED
QC_PASSED → IN_TRANSIT → CLEARED → PAID
QC_FAILED → DISPUTE → CANCELLED | REFUNDED
Terminal: PAID, CANCELLED, REFUNDED, NEGOTIATION_FAILED
```

### Repository pattern

Each service implements a `Protocol` with two concrete backends:
- `InMemoryFallbackRepository` — activated when `*_MONGO_URI` env var is absent; in-process only.
- `MongoXxxRepository` — Motor + Atlas; activated when URI is set.

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

**Graceful degradation:** connectors check for their API key at call-time. If absent they return a realistic stub response (deal pipeline advances in dev/CI). Only real HTTP failures raise `ConnectorError`. Never raise in a stub — the stub must always return a dict.

### Webhook verification

All provider webhooks use HMAC-SHA256:
- SGS: `x-sgs-signature` header
- Cogoport/Freightos: `x-cogoport-signature` / `x-freightos-signature`
- Stripe: `stripe-signature` with `t=timestamp,v1=<hex>` format (parsed manually)
- Payoneer: `x-payoneer-signature`
- HITL webhook: `x-portless-signature`

If the webhook secret env var is not set, the connector logs a warning and returns `True` (accept all). In production set all `*_WEBHOOK_SECRET` vars.

### HITL notification to UI

`services/hitl_approval/notification.py` manages two delivery channels:

1. **SSE stream** — `GET /hitl-approval/stream` streams `text/event-stream`. One `asyncio.Queue` per connected client; last 50 events buffered for replay on reconnect. Since the browser's `EventSource` API can't set an `Authorization` header, `AuthMiddleware` accepts the bearer token as a `?token=` query param for this one path only (`_QUERY_TOKEN_PATH_SUFFIXES`) — every other endpoint still requires the header.
2. **Webhook POST** — when `HITL_NOTIFY_WEBHOOK_URL` is set, a HMAC-signed JSON POST fires on every approval created/resolved.

HITL service also runs a **background timeout watcher** (`run_timeout_watcher()`) that polls every 30s and auto-resolves expired L1 approvals with `default_on_timeout`.

### HITL autonomy levels

| Level | Behavior |
|-------|----------|
| L0 | Always ask — PENDING until human resolves; no timeout |
| L1 | Ask with timeout — PENDING; auto-resolves after `expires_at` |
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
| `RESOLVE_EXCEPTION` | Logs + emits `deal.exception` (exception_resolver stub) |
| `SEND_NEGOTIATION_EMAIL` | `negotiation_agent.service.send_approved_email()`; soft REJECT → `reject_email()` |
| `CONFIRM_NEGOTIATION_AGREEMENT` | `negotiation_agent.service.finalize_agreement()` — the ONLY path to negotiation `AGREED`; soft REJECT → `reject_agreement()` reopens the session at `ROUND` |
| `DISPATCH_RFQ` | `sourcing.service.dispatch_rfq()` (deal reached MATCHING) |
| `CONFIRM_SUPPLIER_SELECTION` | `deals.service.record_match_result()` + transition to `NEGOTIATING` (via `MATCHING` when re-matching after `NEGOTIATION_FAILED`) |
| `CONFIRM_PI_ACCEPTANCE` | `dealmaker.service.accept_pi()` — buyer's email reply read as PI acceptance |
| `RELEASE_ESCROW_TRANCHE` | `escrow_guard.service.release_tranche()` — staged tranche (T1 PO_ISSUED / T2 QC_PASSED / T3 CLEARED), strictly in schedule order; money-moving, hard REJECT |
| `CONFIRM_COUNTERPARTY_RISK` | `continuations.issue_pi_for_deal()` — buyer screening returned `review`; APPROVE issues the PI the AGREED leg held back; hard REJECT (`block` never reaches a gate — it raises `deal.exception` and withholds the PI) |
| `CONFIRM_BUYER_ACCEPTANCE` | `escrow_guard.service.accept_escrow()` — buyer's post-delivery e-mail confirmed as acceptance → final (T4) tranche; money-moving, soft REJECT keeps the inspection window running |
| `SEND_DEAL_NOTIFICATION` | `negotiation_agent.service.send_approved_email()` — templated counterparty email (`shared/orchestration/notifications.py`) |
| `PARSE_INBOUND_EMAIL` | No-op (informational) — operator acts through the Email Processor manual endpoint |

Soft rejects (`callbacks.SOFT_REJECT_ACTIONS`) log and leave the deal where it is; every other REJECT emits `deal.exception`. On callback failure → `deal.exception` emitted with `action_required: manual_retry_or_intervention`.

### Autonomy policy (`shared/orchestration/autonomy_policy.py`)

Every gate above is created at the level `resolve_level(action, deal)` returns: saved override (`GET/PUT /hitl-approval/autonomy-policy`, the Autonomy Settings page; Mongo-persisted) → `AUTONOMY_<ACTION>` env → the deal's `autonomy_profile` (only for PO / freight / customs / payout / tranche) → built-in default (`DISPATCH_RFQ` and `SEND_DEAL_NOTIFICATION` L2, everything else L0). Money-moving actions are clamped to `AUTONOMY_MONEY_CEILING` (default L1). Two overrides no policy lifts: degraded/placeholder negotiation drafts stay L0, and the first outbound negotiation email stays L0 while `hitl_on_first_outbound` is on. Setting every action to L2 runs a deal end-to-end with no operator click; money release still stops at L1.

### Exception Resolver Agent

`services/exception_resolver/` is a **LangGraph-powered agent** — the only module with LLM-driven business logic outside the negotiation agent.

**Agent flow (`agent.py`):**
1. `analyze` node — LLM (via `LLMClient`) classifies exception into one of 10 categories and builds a JSON remediation plan.
2. `execute` node — dispatches on the LLM's `decision`:
   - `AUTO_FIX` → calls `_AUTO_FIX_DISPATCH[category](record)` (e.g. DocuGen regen for `DOCUMENT_ERROR`); falls through to ESCALATE if no handler.
   - `ESCALATE` / `CANCEL` → calls `hitl_approval.service.create_approval()` with `RESOLVE_EXCEPTION` action and full context in `callback_payload`.
   - `MONITOR` → logs + marks RESOLVED, no HITL.

**Exception categories:** `QC_FAILED`, `FREIGHT_DELAY`, `PAYMENT_FAILED`, `NEGOTIATION_FAILED`, `CUSTOMS_REJECTED`, `DOCUMENT_ERROR`, `CONNECTOR_ERROR`, `MARGIN_BREACH`, `APPROVAL_REJECTED`, `CALLBACK_FAILED`, `UNKNOWN`.

**LLM fallback:** if all models exhausted, returns conservative `UNKNOWN/HIGH/ESCALATE` analysis without raising — the pipeline always produces a result.

### Exception Resolver — key invariants

- `service.raise_exception()` always persists an `OPEN` record first (crash-safe audit trace).
- Status lifecycle: `OPEN` → `ANALYZING` → `REMEDIATING` → `RESOLVED` | `ESCALATED` | `UNRESOLVABLE`.
- `UNRESOLVABLE` means the agent crashed OR HITL creation failed — always requires manual intervention.
- The LLM prompt returns strict JSON; markdown fences are stripped automatically before parsing.

### Mocked responses (`backend/mocked/`)

Complete realistic JSON fixtures for every service, all deal states, and all error scenarios. See `backend/mocked/README.md` for the full index. Use with MSW, json-server, or Storybook to build the UI without the backend running.

### Event publishing

Services call `get_publisher().publish(DomainEvent(...))` after committing state. The backing bus is selected at startup by `shared/bus/factory.init_event_bus()` (`EVENT_BUS_BACKEND`: `auto`/`nats`/`inprocess`/`noop`):

- **NATS JetStream** (`shared/bus/nats_bus.py`) — used when a Synadia `*.creds` file is present (auto-discovered in `backend/`, or `NATS_CREDS_FILE`). One stream (`PORTLESS_EVENTS`, subjects `portless.<topic>`), one durable pull consumer per subscribed topic, so events survive restarts and replay. At-least-once delivery: handler failure → NAK with backoff; after `NATS_MAX_DELIVER` attempts → TERM + dead-letter `deal.exception`. Poison (unparseable) messages are TERMed. `publish()` never raises — broker outage degrades to in-process delivery with a loud log.
- **InProcessEventBus** — fallback when no creds; fire-and-forget async fan-out inside one process.

Pipeline continuations (`shared/orchestration/continuations.py`) subscribe in the **monolith only** (`init_event_bus(consume=True)` in `app.py`) — they call peer services via in-process imports. Standalone service mode inits publish-only (`consume=False`); published events persist in JetStream until the monolith's durable consumers replay them. `DEAL_EXCEPTION` routes to the Exception Resolver via `on_deal_exception` (resolver's own lifecycle echoes carry `exception_id` and are skipped to prevent loops).

### LLM gateway

`shared/llm_gateway/` wraps the Negotiation Agent's LLM calls. Primary model via `LLM_PRIMARY_CONNECTOR` / `LLM_PRIMARY_API_KEY`; falls through Groq chain (`qwen/qwen3-32b` → `qwen/qwen3.6-27b` → `openai/gpt-oss-safeguard-20b`) if absent.

### RAG pipeline

`shared/rag/` uses MongoDB Atlas `$vectorSearch`. Embeddings use `text-embedding-3-small` (OpenAI) when `LLM_EMBEDDING_API_KEY` is set; falls back to `LocalHashEmbeddings`. The vector index is created by `ensure_indexes()` (run at startup from `shared.rag.src.router.startup()`) or manually in the Atlas UI — it must exist before RAG queries return anything. Only `metadata.authorized_users`, `metadata.source`, and `metadata.content_hash` are Atlas pre-filterable; filtering on any other field silently voids the semantic leg. Retrieval is hybrid: vector + keyword (BM25) + optional graph (`RAG_GRAPH_RETRIEVAL_ENABLED`; backends `mongo` — recommended — or `neo4j`, config default `neo4j`) + optional live web search (Tavily, `RAG_WEB_SEARCH_ENABLED`) — all merged via reciprocal rank fusion before reranking and the prompt-injection safety filter. Full reference: `backend/shared/rag/RAG_IMPLEMENTATION_FLOWS.md`. The Negotiation Agent's `market_intel.py` reuses this same web-search adapter and safety filter for its own per-round market-context fetch (see `agents/negotiation_agent/workflow_negotiation_agent.md` §9).

---

## Implementation status

| Module | Status | Notes |
|---|---|---|
| `auth` | ✅ Full | JWT register/login/me/logout; organizations + Portless-staff allowlist; `shared/auth/middleware.py` gates the whole app |
| `api_gateway` | ✅ Full | Edge proxy — auth, validation, webhook ingest, idempotency, request fan-out to every other module |
| `trade_min` | ✅ Full | 5-phase concurrent pipeline; MongoDB + APScheduler; `TRADE_MIN_MOCK_MODE` for network-free dev/CI scans |
| `match_engine` | ✅ Full | Deterministic CMS formula |
| `deals` | ✅ Full | State machine, MongoDB, orchestration; optional dev/test `AUTO_FLOW_ENABLED` auto-continuation (`shared/orchestration/auto_flow.py`) |
| `sourcing` | ✅ Full | RFQ dispatch + GSTN/DGFT/MCA/Udyam verification |
| `negotiation_agent` | ✅ Full | LangGraph 3-node, RAG (with `NEGOTIATION_RAG_ENABLED` escape hatch + timeout); drafts + HITL-gates emails, delegates SMTP to `email_gateway`; optional live web-search market intel (`NEGOTIATION_MARKET_SEARCH_ENABLED`); inbound-reply intent via LLM classifier + price-consistency guard; `AGREED` reachable only via the `CONFIRM_NEGOTIATION_AGREEMENT` HITL gate |
| `dealmaker` | ✅ Full | PI/PO, Jinja2 HTML, SHA-256, margin guard, idempotency |
| `docugen` | ✅ Full | CI/PL/CoO/SDF/Shipping Bill templates, ICEGATE stub |
| `shield_link` | ✅ Full | QC + freight + CHA; HMAC webhooks; graceful stubs |
| `escrow_guard` | ✅ Full | Neutral staged escrow (Tazapay / Escrow.com) + legacy Stripe hold + Payoneer payout; funding shortfall → `PARTIALLY_FUNDED`; replay-safe webhooks; ordered tranche release with fees; payout capped at net released funds; PAID only when the supplier is paid in full; settlement event; per-deal ledger + `/ledgers` |
| `hitl_approval` | ✅ Full | L0/L1/L2/L3 gates; SSE stream; webhook notify; saga callbacks; timeout watcher |
| `exception_resolver` | ✅ Full | LangGraph agent; LLM classify+plan; AUTO_FIX/ESCALATE/CANCEL/MONITOR; HITL saga; MongoDB |
| `email_gateway` | ✅ Full | **Transport only** — single IMAP + SMTP config. Automatic IMAP poll (15 min) forwards inbound mail to `email_processor`; sends outbound via `POST /email-gateway/send` (called from the negotiation flow after HITL approval) |
| `email_processor` | ✅ Full | **Inbound processing only** — receives fetched/pushed emails (Gateway forward, SendGrid/Mailgun webhooks, manual), maps to live negotiation, extracts price, persists, triggers `run_round()` |

### Connectors

| Connector | File | Status |
|---|---|---|
| SGS / Intertek | `shields.py` | ✅ Wired — stub when `SGS_API_KEY` absent |
| Cogoport | `shields.py` | ✅ Wired — stub when `COGOPORT_API_KEY` absent |
| Freightos | `shields.py` | ✅ Wired — stub when `FREIGHTOS_API_KEY` absent |
| ICEGATE | `shields.py` | ✅ Wired — stub when `ICEGATE_API_KEY` absent |
| Stripe Treasury | `payments.py` | ✅ Wired — stub when `STRIPE_SECRET_KEY` absent |
| Payoneer | `payments.py` | ✅ Wired — stub when `PAYONEER_CLIENT_ID` absent |
| Google Drive | `google_drive.py` | ✅ Wired — PRIMARY store for generated PI/PO/DocuGen documents; local-disk fallback (loudly logged) when `GOOGLE_DRIVE_REFRESH_TOKEN` absent or an upload fails |
| PortLess Apify actors | `apify.py` | ✅ Wired — 7 actors (buyers, demand benchmark, suppliers, contacts, screening, e-mail check); empty result when `APIFY_TOKEN` absent; `india-supplier-finder-ai` still to be generated with `/actor-pipeline` (row schema on the connector class) |

All other connectors (`trade_intel.py`, `demand_signals.py`, `regulatory.py`) make real HTTP calls; missing credentials return empty/error results gracefully.

---

## Environment

Copy `.env.example` to `.env`. All services degrade gracefully (in-memory store, stub connectors) when credentials are absent. Key vars:

```
# MongoDB — ONE connection string + ONE database for the whole app.
# Set MONGO_URI and MONGO_DB_NAME once; every service/module (all 11 backend
# services + RAG + LLM Gateway + the shared cache) persists into that SAME
# database via its own collection(s) — a genuinely single-database deployment,
# not one database per service. The RAG vector store ($vectorSearch) and GridFS
# file storage (uploaded documents) also live in this same database — MongoDB
# is the only persistence/vector/blob-storage backend in the app; no S3 or other
# object store is used. Leave everything blank → all services fall back to
# in-memory stores (dev/CI only).
MONGO_URI                 # single cluster URL used by every service/module
MONGO_DB_NAME              # single database name (default: portless)
# Optional per-service overrides (each falls back to MONGO_URI / MONGO_DB_NAME
# when unset) — only set these if you deliberately want one service on a
# different cluster/database:
#   TRADE_MONGO_URI, DEALS_MONGO_URI, SOURCING_MONGO_URI, NEGOTIATION_MONGO_URI,
#   DEALMAKER_MONGO_URI, DOCUGEN_MONGO_URI, SHIELD_LINK_MONGO_URI,
#   ESCROW_GUARD_MONGO_URI, HITL_APPROVAL_MONGO_URI, EXCEPTION_RESOLVER_MONGO_URI,
#   EMAIL_PROCESSOR_MONGO_URI, RAG_MONGO_URI, LLM_MONGO_URI, CACHE_MONGO_URI
#   (…and the matching <SERVICE>_MONGO_DB_NAME for each)

# LLM (all LLM calls go through shared/llm_gateway; no keys are hardcoded)
LLM_GROQ_API_KEY         # Groq fallback-chain key — required unless a primary model is set
LLM_PRIMARY_API_KEY      # Claude/OpenAI primary (optional; Groq chain used if absent)

# Regulatory (trade-min verification pipeline)
API_SETU_KEY             # GSTN + MCA + Udyam
COMTRADE_SUBSCRIPTION_KEY
VOLZA_API_KEY
ALIBABA_APP_KEY / ALIBABA_APP_SECRET
MACMAP_API_KEY
INDIAMART_API_KEY / INDIAMART_MOBILE

# QC inspection providers (shield-link — buyer-selectable top-5; each also
# accepts <PREFIX>_BASE_URL). Buyer's opted provider is captured during
# negotiation; its quoted cost feeds cost_envelope.qc_cost in pricing.
SGS_API_KEY / SGS_WEBHOOK_SECRET
QIMA_API_KEY / QIMA_WEBHOOK_SECRET
BUREAU_VERITAS_API_KEY / BUREAU_VERITAS_WEBHOOK_SECRET
INTERTEK_API_KEY / INTERTEK_WEBHOOK_SECRET
TUV_SUD_API_KEY / TUV_SUD_WEBHOOK_SECRET

# Freight + customs (shield-link)
COGOPORT_API_KEY
COGOPORT_WEBHOOK_SECRET
FREIGHTOS_API_KEY
FREIGHTOS_WEBHOOK_SECRET
ICEGATE_API_KEY

# Payments (escrow-guard)
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
PAYONEER_CLIENT_ID
PAYONEER_CLIENT_SECRET
PAYONEER_WEBHOOK_SECRET

# Documents (dealmaker + docugen) — Google Drive is the PRIMARY document store
GOOGLE_DRIVE_CLIENT_ID       # OAuth "Desktop app" client from console.cloud.google.com
GOOGLE_DRIVE_CLIENT_SECRET
GOOGLE_DRIVE_REFRESH_TOKEN   # mint once via: python scripts/google_drive_authorize.py
GOOGLE_DRIVE_ROOT_FOLDER     # default "Portless" — docs land in <root>/<deal_id>/<filename>
# Local disk (DEALMAKER_STORAGE_DIR / DOCUGEN_STORAGE_DIR, default
# <OS temp>/portless-docs/<service>) is an emergency fallback only — every
# local write logs a loud warning that Drive needs attention.

# PortLess Apify actors (buyer / seller identification + counterparty enrichment)
APIFY_TOKEN                  # Apify API token (auto-discovered from ~/.apify/auth.json in dev unless APIFY_TOKEN_AUTODISCOVER=false)
APIFY_ACTOR_OWNER            # default rich_minds; APIFY_ACTOR_<KEY> overrides one actor (slug / owner~slug / id)
APIFY_ENABLE_AI / APIFY_MAX_ITEMS / APIFY_RUN_TIMEOUT_SECONDS / APIFY_MAX_DISCOVERY_CHARGE_USD / APIFY_WEBHOOK_SECRET
ENRICHMENT_ON_DEAL_CREATED   # default true — contact finder + sanctions screening at deal.created

# Production readiness gate (shared/readiness.py)
ENV_PROFILE                  # dev | staging | production — production refuses to boot while a readiness check fails
READINESS_ENFORCE            # override the boot refusal (never with real money)
STRIPE_WEBHOOK_TOLERANCE_SECONDS  # signed-payload replay window (default 300)

# HITL UI integration
HITL_NOTIFY_WEBHOOK_URL     # optional — POST notifications to UI backend
HITL_NOTIFY_WEBHOOK_SECRET  # HMAC signing key for webhook

# Exception Resolver (MongoDB)
EXCEPTION_RESOLVER_MONGO_URI

# Auth (auth service + shared/auth/middleware.py)
AUTH_PORTLESS_EMAILS        # comma-separated allowlist — who counts as Portless platform staff
AUTH_MONGO_URI               # optional override; falls back to shared MONGO_URI

# Event bus — NATS JetStream (Synadia Cloud); falls back to in-process bus
EVENT_BUS_BACKEND            # auto (default) | nats | inprocess | noop
NATS_CREDS_FILE              # Synadia .creds file; auto-discovers backend/*.creds if unset
NATS_URL                     # default tls://connect.ngs.global:4222
NATS_STREAM_NAME / NATS_SUBJECT_PREFIX / NATS_ACK_WAIT_SECONDS / NATS_MAX_DELIVER

# Automation (production path — see shared/orchestration/)
EVENT_CONTINUATION_ENABLED   # default true — event-driven legs that move a deal to its next step
AUTONOMY_<ACTION>            # per-action L0–L3 (e.g. AUTONOMY_SEND_NEGOTIATION_EMAIL=L2); also settable from the UI
AUTONOMY_MONEY_CEILING       # default L1 — hard cap for RELEASE_ESCROW_TRANCHE / RELEASE_SUPPLIER_PAYOUT
DEAL_NOTIFICATIONS_ENABLED   # default true — templated PI / escrow / PO / QC / shipment / arrival / reminder emails to counterparties
DEAL_WATCHER_ENABLED         # default true — reminders + stall exceptions (DEAL_PI_REMINDER_AFTER_HOURS, DEAL_ESCROW_REMINDER_AFTER_HOURS, DEAL_REMINDER_MAX, DEAL_STALL_SLA_HOURS, DEAL_WATCHER_INTERVAL_SECONDS)
AUTO_DEAL_FROM_OPPORTUNITY_ENABLED  # default false — convert qualifying opportunity.detected events into deals (AUTO_DEAL_MIN_ARBITRAGE_RANK, AUTO_DEAL_MIN_CONFIDENCE, AUTO_DEAL_ORG_ID, AUTO_DEAL_OPERATOR_ID, AUTO_DEAL_AUTONOMY_PROFILE)

# Dev/test-only conveniences (default off; do not enable in production)
TRADE_MIN_MOCK_MODE          # bypass every live trade-intelligence connector with curated scenario data
AUTO_FLOW_ENABLED            # auto-continue an opportunity-seeded deal through sourcing/negotiation/PI/escrow (still HITL-gated at PO/freight/payout)

# Negotiation Agent — RAG resilience + live market intelligence
NEGOTIATION_RAG_ENABLED      # default true; false = always use the static playbook stub, skipping shared RAG entirely
RAG_TIMEOUT_SECONDS          # default 15.0 — wall-clock budget for the RAG call before falling back to the stub
NEGOTIATION_MARKET_SEARCH_ENABLED  # default false — fetch live Tavily web-search market color before each drafted email
TAVILY_API_KEY               # shared by RAG's own web-search leg (RAG_WEB_SEARCH_ENABLED) and negotiation market intel above
```

Run `python scripts/check_trade_api_connections.py` to test all third-party API connections.

---

## Key cross-module flows

**Opportunity → Deal creation:**
`POST /trade-min/scan` → `opportunity.detected` → `POST /deals` (with `opportunity_id`) → Deals service seeds from Trade-Min + scores via Match Engine → deal at `MATCHING`. With `AUTO_DEAL_FROM_OPPORTUNITY_ENABLED=true` the `on_opportunity_detected` continuation creates the deal itself (thresholds + simulated-source + duplicate guards). If `AUTO_FLOW_ENABLED=true` (dev/test only), creation also fires a background task that drives the deal through the rest of the flows below automatically, stopping at each HITL gate for a real operator decision.

**Buyer / seller identification (Apify actors):**
`POST /trade-min/scan` runs the PortLess actors next to the free APIs: Phase 1 `export-market-finder-ai` (UN Comtrade demand + the market's unit value per MT → `price_benchmark_usd_per_mt`, the real buyer price anchor that replaces the 1.25× FOB heuristic), Phase 2 `alibaba-rfq-leads-ai` (every market) + `us-importer-finder-ai` (US market; named importers with website/phone/fit score), Phase 4 `india-supplier-finder-ai` (suppliers with price, MOQ, GSTIN, e-mail). All seller prices are normalised to USD/MT (`_normalise_price_per_mt`). `GET /trade-min/sources` reports which sources are live/stub/mock. At `deal.created` the enrichment leg runs `company-contact-finder-ai` + `counterparty-check-ai` (+ `email-list-qualifier-ai` for unverified addresses) and writes `email`/`website`/`phone`/`sanctions_status` onto the deal parties, so the negotiation agent's first e-mail goes to a real mailbox; a `block` verdict raises `deal.exception` and forces the RFQ gate to L0. `PATCH /deals/{id}/parties/{role}` lets an operator correct a contact (never overwritten by a later run); `POST /deals/{id}/enrich` re-runs it.

**Deal at MATCHING → RFQ → Quote intake → Negotiation start:**
`deal.created` (MATCHING) → enrichment (above) → `DISPATCH_RFQ` gate (policy default L2) → `sourcing.dispatch_rfq()` to the top-3 shortlist (SOURCE matches first, else the opportunity's seller candidates; a missing buyer target price is derived as 1.25× the cheapest FOB and logged as a HEURISTIC). Suppliers with an e-mail (actor / contact finder / operator) get the RFQ **by e-mail** with an `[RFQ:<rfq_id>]` subject token; the Email Processor maps the reply (`mapper.rfq_id_from_subject`) → `sourcing.receive_quote_by_email()` (sender matched to the dispatched address, GSTIN/IEC picked from the body) → the normal quote pipeline; no price → `PARSE_INBOUND_EMAIL`. `POST /sourcing/quotes` → GSTN/DGFT/MCA/Udyam verified in parallel → Match Engine re-scores → if SOURCE: patches deal financials + transitions to `NEGOTIATING`; otherwise `quote.received` → `CONFIRM_SUPPLIER_SELECTION` gate (L0; forced L0 with risk flags when GST/IEC verification failed) → APPROVE records the match and moves the deal to `NEGOTIATING`. `negotiation.failed` → `deal.exception` + a re-match `CONFIRM_SUPPLIER_SELECTION` gate for the next-best SOURCE candidate.

**Negotiation → PI issuance:**
`POST /negotiation-agent/negotiations/{id}/round` × N rounds. Acceptance is detected by an LLM intent classifier (keyword guard only as gateway-down fallback) + a deterministic price-consistency check, then gated behind a `CONFIRM_NEGOTIATION_AGREEMENT` (L0) HITL approval — session parks at `HITL_PENDING`; operator APPROVE → `finalize_agreement()` → `AGREED` → `negotiation.agreed` → the buyer's sanctions screening is (re)checked when older than 30 days: `clear`/`unchecked` → `POST /dealmaker/pi` → deal `PI_SENT`; `review` → `CONFIRM_COUNTERPARTY_RISK` gate (APPROVE issues the PI); `block` → `deal.exception`, no PI. REJECT reopens the negotiation (soft reject, no deal exception).

**PI sent → PI accepted → Escrow:**
`pi.sent` → the PI is emailed to the buyer (`SEND_DEAL_NOTIFICATION`, same `[NEG:<id>]` thread as the negotiation). The buyer's reply maps back through the Email Processor: an unconditional acceptance (`guards.is_explicit_acceptance`) → `CONFIRM_PI_ACCEPTANCE` gate → `accept_pi()`; anything else → `PARSE_INBOUND_EMAIL` approval for a human. `POST /dealmaker/pi/{id}/accept` (portal/API) remains the other path → deal `PI_ACCEPTED` → `create_escrow()` (neutral staged escrow, sized from the PI, provider fee % recorded) → deal `ESCROW_PENDING` + funding instructions (amount, checkout link, tranche schedule) emailed to the buyer → buyer pays → `POST /escrow-guard/webhooks/escrow-funded` (replay-safe on `event_id`; Stripe signatures also carry a 5-minute replay window) → deal `ESCROW_FUNDED`. A provider total **below the PI** keeps the escrow `PARTIALLY_FUNDED` and the deal in `ESCROW_PENDING` (shortfall e-mail to the buyer + `deal.exception`; the next webhook with the full total completes it) — no PO is ever issued against money that is not there. The deal watcher reminds the buyer (max `DEAL_REMINDER_MAX`) and raises `deal.exception` on PI expiry / unfunded escrow.

**Escrow funded → PO → QC:**
`ISSUE_PO` gate → `POST /dealmaker/po` (margin guard: ≥8%) → deal `PO_ISSUED` → PO emailed to the supplier + `RELEASE_ESCROW_TRANCHE` gate (T1) → `POST /shield-link/qc` → buyer-opted inspector booked → deal `SUPPLIER_DISPATCH_READY` + booking emailed to the supplier → QC webhook → deal `QC_PASSED` (document pack CI/PL/CoO/SDF/Shipping Bill rendered + T2 tranche gate + `BOOK_FREIGHT` gate) or `QC_FAILED` (→ deal `DISPUTE` + `deal.exception`; `refund()` then moves `DISPUTE` → `REFUNDED`).

**QC passed → Freight → Customs → Cleared → Paid:**
`BOOK_FREIGHT` gate → `POST /shield-link/freight` → deal `IN_TRANSIT` → `freight.booked` → `FILE_CUSTOMS` gate (document ids attached) + shipment emailed to the buyer → freight webhook (ARRIVED) → deal `CLEARED` → T3 tranche gate + arrival email + `RELEASE_SUPPLIER_PAYOUT` gate whose amount is **capped at the net funds already released from escrow** (`DealLedger.available_for_payout`; `total_owed` = PO value travels in the payload) → `POST /escrow-guard/payouts` → Payoneer payout → payout webhook (COMPLETED) → deal `PAID` **only once completed payouts reach the PO value**; a partial payout leaves the deal `CLEARED` and `on_payout_completed` gates the balance as `supplier_balance`.

**Escrow money chain (`escrow_guard` + `continuations`):**
Tranches (default 30/40/20/10 on PO_ISSUED / QC_PASSED / CLEARED / ACCEPTED) release strictly in schedule order, never while the deal is in `DISPUTE` / `CANCELLED` / `REFUNDED` / `QC_FAILED`, never above the principal; each release accrues the provider fee and emits `escrow.tranche_released`. That leg tells the buyer when the acceptance window opens (`inspection_window_open` e-mail; "We accept the goods" → `CONFIRM_BUYER_ACCEPTANCE` gate → `accept_escrow()`; silence → auto-release after `inspection_window_hours`), re-gates the next tranche whose trigger status is already reached, and tops up the supplier payout. When the last tranche leaves the escrow `escrow.settled` fires: the supplier balance is gated, both parties get a settlement notice, and `realized_margin_usd` = net received − payouts is patched onto the deal. `GET /escrow-guard/deals/{id}/ledger` (and `/ledgers` for all deals) exposes principal / funded / shortfall / released / fees / net / payouts / available / supplier owed & balance / realised margin with a reconciliation check (`payouts ≤ net received` included); the escrow watcher raises `deal.exception` once per breach.

**Document generation:**
Rendered automatically at `QC_PASSED` (`POST /docugen/documents` for CI, PL, CoO, SDF, Shipping Bill; re-rendered at `freight.booked` if missing) and attached to the `FILE_CUSTOMS` gate → `shield_link.file_cha()` → ICEGATE Shipping Bill + LEO.

**HITL gate (any service, any point):**
Service calls `POST /hitl-approval/approvals` with `action` + `callback_payload` + `autonomy_level`.
- L0/L1: UI notified via SSE stream + webhook; operator resolves at `GET /hitl-approval/approvals/{id}`.
- On APPROVE: saga callback fires the stored `callback_payload` at the target service.
- On REJECT: `deal.exception` emitted; Exception Resolver handles next step.
