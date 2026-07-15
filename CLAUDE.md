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
| `shared/bus/publisher.py` | `EventPublisher` protocol + `NoopPublisher` — swap-in point for NATS/Kafka |
| `shared/connectors/` | External API adapters (all implement `quote/book/status/webhook_verify`) |
| `shared/connectors/shields.py` | SGS, Cogoport, Freightos, ICEGATE — graceful stub when API key absent |
| `shared/connectors/payments.py` | Stripe Treasury, Payoneer — graceful stub when API key absent |
| `shared/llm_gateway/` | Resilient LLM client with Groq fallback chain + MongoDB call tracking |
| `shared/rag/` | MongoDB Atlas vector store + RAG chunking pipeline |
| `shared/auth/` | `AuthMiddleware` (JWT bearer enforcement + non-Portless allowlist), `dependencies.py` (`require_org_scope`, `require_portless`), `blacklist.py` (logout/token revocation), `security.py` (hash/verify/`create_access_token`) |
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

On callback failure → `deal.exception` emitted with `action_required: manual_retry_or_intervention`.

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

`shared/bus/publisher.py` holds a process-global `NoopPublisher`. Services call `get_publisher().publish(DomainEvent(...))` after committing state. Replace with NATS/Kafka publisher to go event-driven without touching service code.

### LLM gateway

`shared/llm_gateway/` wraps the Negotiation Agent's LLM calls. Primary model via `LLM_PRIMARY_CONNECTOR` / `LLM_PRIMARY_API_KEY`; falls through Groq chain (`qwen/qwen3-32b` → `qwen/qwen3.6-27b` → `openai/gpt-oss-safeguard-20b`) if absent.

### RAG pipeline

`shared/rag/` uses MongoDB Atlas `$vectorSearch`. Embeddings use `text-embedding-3-small` (OpenAI) when `RAG_EMBEDDING_API_KEY` is set; falls back to `LocalHashEmbeddings`. Create the vector index once via `ensure_vector_index()` before running RAG queries. Retrieval is hybrid: vector + keyword (BM25) + optional graph (Neo4j, `RAG_GRAPH_RETRIEVAL_ENABLED`) + optional live web search (Tavily, `RAG_WEB_SEARCH_ENABLED`) — all merged via reciprocal rank fusion before reranking and the prompt-injection safety filter. The Negotiation Agent's `market_intel.py` reuses this same web-search adapter and safety filter for its own per-round market-context fetch (see `agents/negotiation_agent/workflow_negotiation_agent.md` §9).

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
| `negotiation_agent` | ✅ Full | LangGraph 3-node, RAG (with `NEGOTIATION_RAG_ENABLED` escape hatch + timeout); drafts + HITL-gates emails, delegates SMTP to `email_gateway`; optional live web-search market intel (`NEGOTIATION_MARKET_SEARCH_ENABLED`) |
| `dealmaker` | ✅ Full | PI/PO, Jinja2 HTML, SHA-256, margin guard, idempotency |
| `docugen` | ✅ Full | CI/PL/CoO/SDF/Shipping Bill templates, ICEGATE stub |
| `shield_link` | ✅ Full | QC + freight + CHA; HMAC webhooks; graceful stubs |
| `escrow_guard` | ✅ Full | Stripe hold + Payoneer payout; HMAC webhooks; graceful stubs |
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

# Physical inspection + logistics (shield-link)
SGS_API_KEY
SGS_WEBHOOK_SECRET
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

# HITL UI integration
HITL_NOTIFY_WEBHOOK_URL     # optional — POST notifications to UI backend
HITL_NOTIFY_WEBHOOK_SECRET  # HMAC signing key for webhook

# Exception Resolver (MongoDB)
EXCEPTION_RESOLVER_MONGO_URI

# Auth (auth service + shared/auth/middleware.py)
AUTH_PORTLESS_EMAILS        # comma-separated allowlist — who counts as Portless platform staff
AUTH_MONGO_URI               # optional override; falls back to shared MONGO_URI

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
`POST /trade-min/scan` → `opportunity.detected` → `POST /deals` (with `opportunity_id`) → Deals service seeds from Trade-Min + scores via Match Engine → deal at `MATCHING`. If `AUTO_FLOW_ENABLED=true` (dev/test only), this also fires a background task that drives the deal through the rest of the flows below automatically, stopping at each HITL gate for a real operator decision.

**Quote intake → Negotiation start:**
`POST /sourcing/quotes` → GSTN/DGFT/MCA/Udyam verified in parallel → Match Engine re-scores → if SOURCE: patches deal financials + transitions to `NEGOTIATING`.

**Negotiation → PI issuance:**
`POST /negotiation-agent/negotiations/{id}/round` × N rounds until `AGREED` → `POST /dealmaker/pi` → deal `PI_SENT`.

**PI accepted → Escrow:**
`POST /dealmaker/pi/{id}/accept` → deal `PI_ACCEPTED` → `POST /escrow-guard/holds` → Stripe PaymentIntent created → buyer pays → `POST /escrow-guard/webhooks/escrow-funded` → deal `ESCROW_FUNDED`.

**Escrow funded → PO → QC:**
`POST /dealmaker/po` (margin guard: ≥8%) → deal `PO_ISSUED` → `POST /shield-link/qc` → SGS booked → deal `SUPPLIER_DISPATCH_READY` → SGS webhook → deal `QC_PASSED` or `QC_FAILED`.

**QC passed → Freight → Cleared → Paid:**
`POST /shield-link/freight` → deal `IN_TRANSIT` → freight webhook (ARRIVED) → deal `CLEARED` → `POST /escrow-guard/payouts` → Payoneer payout → payout webhook (COMPLETED) → deal `PAID`.

**Document generation (parallel to freight):**
`POST /docugen/documents` (CI, PL, CoO, SDF, Shipping Bill) → `POST /docugen/customs/file` → ICEGATE Shipping Bill + LEO.

**HITL gate (any service, any point):**
Service calls `POST /hitl-approval/approvals` with `action` + `callback_payload` + `autonomy_level`.
- L0/L1: UI notified via SSE stream + webhook; operator resolves at `GET /hitl-approval/approvals/{id}`.
- On APPROVE: saga callback fires the stored `callback_payload` at the target service.
- On REJECT: `deal.exception` emitted; Exception Resolver handles next step.
