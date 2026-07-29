# PortLess — Technical Reference & Zero-Cost Setup Guide

> **What this is:** a single technical reference for the PortLess backend — the
> tech stack, every service and its status, all enums and event topics, the
> databases used, the full third-party API registry (free vs paid), and a
> **step-by-step guide to run the entire platform at $0** (including free cloud
> options on Azure for personal use).
>
> **The headline:** PortLess is designed to run **end-to-end for free**. Every
> external dependency degrades gracefully — no MongoDB (in-memory), no API keys
> (stubbed connectors), no OpenAI (local embeddings), no Gmail (log-only email),
> and a **free Groq key ships as the default LLM**. You can clone, `pip install`,
> and run a full deal pipeline without spending a cent or signing up for anything.

---

## Table of Contents

- [1. At a glance](#1-at-a-glance)
- [2. Tech stack](#2-tech-stack)
- [3. Services, status & ports](#3-services-status--ports)
- [4. Databases used](#4-databases-used)
- [5. Enum reference](#5-enum-reference)
- [6. Domain event topics](#6-domain-event-topics)
- [7. Deal state machine](#7-deal-state-machine)
- [8. Third-party APIs — free vs paid](#8-third-party-apis--free-vs-paid)
- [9. Running at $0 — the default (local)](#9-running-at-0--the-default-local)
- [10. Free cloud hosting (Azure, personal use)](#10-free-cloud-hosting-azure-personal-use)
- [11. Environment variables reference](#11-environment-variables-reference)
- [12. Security notes](#12-security-notes)
- [13. Commands cheat-sheet](#13-commands-cheat-sheet)

---

## 1. At a glance

| Attribute | Value |
|---|---|
| **What it is** | Zero-inventory autonomous merchant-exporter platform — finds a buyer + supplier, verifies both, negotiates, ships, and settles payment automatically |
| **Architecture** | Modular monolith → microservices. Every module is a self-contained FastAPI app; runs combined on one port (dev) or one-container-per-service (prod) |
| **Language / runtime** | Python **≥ 3.11** |
| **Web framework** | FastAPI + Uvicorn (ASGI) |
| **Primary datastore** | MongoDB (Motor async) — **in-memory fallback** when no URI is set |
| **LLM** | Groq (free default key) → optional Anthropic Claude / OpenAI primary; LangChain + LangGraph agents |
| **Inter-service transport** | Typed REST clients today (`BaseServiceClient`); event catalog ready for a NATS/Kafka bus swap |
| **Default cost to run** | **$0** — stubs + in-memory + free Groq + local embeddings + log-only email |
| **Container** | `python:3.11-slim`, one image, `SERVICE_MODULE`/`PORT` env-driven |

---

## 2. Tech stack

### Core (required)

| Layer | Library | Version | License | Cost |
|---|---|---|---|---|
| Web framework | `fastapi` | ≥ 0.111 | MIT | Free |
| ASGI server | `uvicorn[standard]` | ≥ 0.30 | BSD | Free |
| Validation / models | `pydantic` | ≥ 2.7 | MIT | Free |
| Settings | `pydantic-settings` | ≥ 2.3 | MIT | Free |
| HTTP client | `httpx` | ≥ 0.27 | BSD | Free |
| Env loading | `python-dotenv` | ≥ 1.0 | BSD | Free |
| Auth tokens | `PyJWT` | ≥ 2.8 | MIT | Free |

### Data & scheduling

| Purpose | Library | Version | Notes |
|---|---|---|---|
| MongoDB async driver | `motor` | ≥ 3.3 | Every service's Mongo repo; optional (in-memory fallback) |
| Postgres async driver | `asyncpg` | ≥ 0.29 | **Listed but unused** by current services (all persistence is MongoDB/Motor); safe to drop unless you add a Postgres feature |
| Scheduler | `apscheduler` | ≥ 3.10 | Trade-Min nightly/reorder cron jobs (`AsyncIOScheduler`) |
| Templating | `jinja2` | ≥ 3.1 | DealMaker PI/PO + DocuGen document HTML |

### AI / LLM

| Purpose | Library | Version | Notes |
|---|---|---|---|
| LLM framework | `langchain` (+ `-openai`, `-anthropic`, `-groq`) | ≥ 0.2 | Model connectors for the gateway |
| Agent graphs | `langgraph` | ≥ 0.1 | Negotiation agent (3-node) + Exception Resolver (2-node) |

### Email transport (Email Gateway)

| Purpose | Library | Version | Notes |
|---|---|---|---|
| Async IMAP | `aioimaplib` | ≥ 0.9 | Inbound poll (fetch UNSEEN) |
| Async SMTP | `aiosmtplib` | ≥ 3.0 | Outbound send (Gmail STARTTLS); only imported when Gmail is configured |

### Dev / tooling

| Purpose | Tool | Notes |
|---|---|---|
| Tests | `pytest` (+ `httpx` test client) | `asyncio_mode = auto`; 492-test suite |
| Lint | `ruff` | line-length 100, target `py311` |
| Container | Docker (`python:3.11-slim`) + docker-compose | one image, per-service env |
| Build metadata | `pyproject.toml` (setuptools) | `requires-python >= 3.11` |

---

## 3. Services, status & ports

All modules are ✅ implemented. Combined monolith runs on **8081**; each service
also runs standalone on its own port.

| Port | Service | Prefix | Status | Responsibility |
|---|---|---|---|---|
| 8000 | `api_gateway` | `/` | ✅ | Edge routing / gateway |
| 8001 | `deals` | `/deals` | ✅ | Deal lifecycle state machine + orchestration |
| 8002 | `trade_min` | `/trade-min` | ✅ | Phase-1 opportunity discovery (5-phase pipeline + scheduler) |
| 8003 | `match_engine` | `/match-engine` | ✅ | Deterministic Composite Match Score (CMS) |
| 8004 | `sourcing` | `/sourcing` | ✅ | RFQ dispatch + GSTN/DGFT/MCA/Udyam verification |
| 8005 | `negotiation_agent` | `/negotiation-agent` | ✅ | LangGraph 3-node price negotiation; drafts + HITL-gates emails |
| 8006 | `dealmaker` | `/dealmaker` | ✅ | PI / PO issuance, margin guard, idempotency |
| 8007 | `shield_link` | `/shield-link` | ✅ | QC + freight + CHA; HMAC webhooks |
| 8008 | `docugen` | `/docugen` | ✅ | CI/PL/CoO/SDF/Shipping-Bill + ICEGATE filing |
| 8009 | `escrow_guard` | `/escrow-guard` | ✅ | Stripe hold + Payoneer payout; HMAC webhooks |
| 8010 | `hitl_approval` | `/hitl-approval` | ✅ | L0–L3 gates; SSE stream; webhook notify; saga callbacks; timeout watcher |
| 8011 | `exception_resolver` | `/exception-resolver` | ✅ | LangGraph LLM agent; AUTO_FIX/ESCALATE/CANCEL/MONITOR |
| 8012 | `email_gateway` | `/email-gateway` | ✅ | Email **transport**: IMAP fetch (auto) + SMTP send (API) |
| 8013 | `email_processor` | `/email-processor` | ✅ | Inbound **processing**: match to live negotiation, extract price, persist, advance |
| — | `auth` | — | ✅ | JWT users + admin-provisioned organizations (Portless-allowlist admin UI; shared MongoDB `portless` db, `users`/`organizations` collections + in-memory fallback), wired in `app.py` lifespan |

> **Docker-compose gap:** `docker-compose.yml` defines services only through
> `exception-resolver` (8011). `email_gateway` (8012) and `email_processor`
> (8013) have Makefile targets but **no compose block yet** — add them if you
> deploy via compose.

### Cross-cutting (`shared/`)

| Package | Purpose |
|---|---|
| `shared/domain/` | `DealStatus`, `EventTopic`, `DomainEvent`, state machine + `can_transition()` |
| `shared/clients/base.py` | `BaseServiceClient` async httpx wrapper |
| `shared/config.py` | `Settings` — service-registry URLs (8000–8013) + cross-cutting env |
| `shared/connectors/` | External adapters (`shields.py`, `payments.py`, `trade_intel.py`, …) — **graceful stubs** when keys absent |
| `shared/bus/publisher.py` | `NoopPublisher` — swap-in point for NATS/Kafka |
| `shared/llm_gateway/` | Resilient LLM client (Groq fallback chain + Mongo call tracking) |
| `shared/rag/` | MongoDB Atlas `$vectorSearch` store + chunking pipeline |

---

## 4. Databases used

**Primary: MongoDB (via `motor`).** Each service uses its **own logical
database**; all fall back to an **in-memory store** when their `*_MONGO_URI` is
unset — so the whole platform runs with **no database at all** in dev/CI.

| Store | Env var | Default DB name | Falls back to |
|---|---|---|---|
| Trade-Min | `TRADE_MONGO_URI` | — | in-memory |
| Deals | `DEALS_MONGO_URI` | — | in-memory |
| Sourcing | `SOURCING_MONGO_URI` | — | in-memory |
| Negotiation + email history | `NEGOTIATION_MONGO_URI` | negotiation | in-memory |
| DealMaker | `DEALMAKER_MONGO_URI` | — | in-memory |
| DocuGen | `DOCUGEN_MONGO_URI` | — | in-memory |
| Shield-Link | `SHIELD_LINK_MONGO_URI` | — | in-memory |
| Escrow-Guard | `ESCROW_GUARD_MONGO_URI` | — | in-memory |
| HITL Approval | `HITL_APPROVAL_MONGO_URI` | — | in-memory |
| Email Processor | `EMAIL_PROCESSOR_MONGO_URI` | email_processor | in-memory |
| Exception Resolver | `EXCEPTION_RESOLVER_MONGO_URI` | — | in-memory |
| Auth (users) | `AUTH_MONGO_URI` (falls back to shared `MONGO_URI`) | portless (shared) | in-memory |
| RAG vectors | `RAG_MONGO_URI` | rag | LocalHash / degraded |
| LLM call tracking | `LLM_MONGO_URI` | llm_gateway | disabled (no tracking) |
| Shared cache (API responses + session locks) | shared cache config | — | in-memory (no Redis needed) |

**Special stores**
- **Vector database:** MongoDB **Atlas Vector Search** (`$vectorSearch`), index
  `vector_index`, field `embedding`, **1536 dims** (`text-embedding-3-small`),
  cosine similarity. Created by `ensure_indexes()` (run from the RAG router's
  `startup()` hook) or once via the Atlas UI.
- **Cache / locks:** a shared MongoDB collection doubles as the API-response
  cache and the negotiation **session lock** — no Redis required.
- **Postgres:** `asyncpg` is declared in `requirements.txt` but **no current
  service uses it**; persistence is MongoDB end-to-end.

> Because the Email Gateway is **stateless transport**, it has no database.
> Persistence for inbound email lives in the Email Processor.

---

## 5. Enum reference

### `shared/domain/enums.py`

**`DealStatus`** (19 states — the single source of truth):
`DRAFT`, `MATCHING`, `NEGOTIATING`, `NEGOTIATION_AGREED`, `NEGOTIATION_FAILED`,
`PI_SENT`, `PI_ACCEPTED`, `ESCROW_PENDING`, `ESCROW_FUNDED`, `PO_ISSUED`,
`SUPPLIER_DISPATCH_READY`, `QC_PASSED`, `QC_FAILED`, `IN_TRANSIT`, `CLEARED`,
`PAID`, `DISPUTE`, `CANCELLED`, `REFUNDED`

| Enum | Values |
|---|---|
| `PartyRole` | `buyer`, `supplier` |
| `AutonomyLevel` | `L0` (always ask), `L1` (ask, auto-approve on timeout), `L2` (auto, notify after), `L3` (fully autonomous, log only) |
| `ThirdPartyType` | `qc`, `freight`, `cha` |
| `DocumentType` | `PI`, `CI`, `PL`, `CoO`, `SDF`, `SHIPPING_BILL` |
| `ApprovalAction` | `RELEASE_SUPPLIER_PAYOUT`, `BOOK_FREIGHT`, `ISSUE_PO`, `FILE_CUSTOMS`, `RESOLVE_EXCEPTION`, `SEND_NEGOTIATION_EMAIL` |

### Negotiation Agent (`agents/negotiation_agent/schemas.py`)

| Enum / literal | Values |
|---|---|
| `NegotiationStatus` | `STARTED`, `ROUND`, `AGREED`, `FAILED`, `HITL_PENDING`, `ESCALATED` |
| `AgentRouting` | `CALL_BUYER_AGENT`, `CALL_SUPPLIER_AGENT`, `HITL_APPROVAL`, `NEGOTIATION_AGREED`, `NEGOTIATION_FAILED` |
| `Party` | `buyer`, `supplier` |
| `Channel` | `email`, `portal`, `whatsapp`, `telegram` |

### Email history (`agents/negotiation_agent/email_history.py`)

| Enum | Values |
|---|---|
| `EmailDirection` | `outbound`, `inbound` |
| `EmailStatus` | `draft`, `pending_approval`, `sent`, `modified_sent`, `rejected`, `failed`, `received` |

### Email Gateway (`services/email_gateway/schemas.py`)

| Enum | Values |
|---|---|
| `SendOutcome` | `sent`, `logged` |

### Email Processor (`services/email_processor/schemas.py`)

| Enum | Values |
|---|---|
| `InboundEmailSource` | `imap`, `sendgrid_webhook`, `mailgun_webhook`, `manual` |
| `MappingConfidence` | `header`, `subject_token`, `manual_override`, `ambiguous`, `unmatched` |
| `InboundEmailStatus` | `received`, `mapped`, `processed`, `needs_operator`, `ambiguous`, `unmatched`, `rejected` |

### Exception Resolver (`services/exception_resolver/schemas.py`)

| Enum | Values |
|---|---|
| `ExceptionCategory` | `QC_FAILED`, `FREIGHT_DELAY`, `PAYMENT_FAILED`, `NEGOTIATION_FAILED`, `CUSTOMS_REJECTED`, `DOCUMENT_ERROR`, `CONNECTOR_ERROR`, `MARGIN_BREACH`, `APPROVAL_REJECTED`, `CALLBACK_FAILED`, `UNKNOWN` |
| `ExceptionSeverity` | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `ExceptionStatus` | `OPEN`, `ANALYZING`, `REMEDIATING`, `RESOLVED`, `ESCALATED`, `UNRESOLVABLE` |
| `RemediationDecision` | `AUTO_FIX`, `ESCALATE`, `CANCEL`, `MONITOR` |

---

## 6. Domain event topics

`shared/domain/events.py` — `EventTopic` (carried over REST today, bus-ready):

`opportunity.detected`, `deal.created`, `match.scored`, `quote.requested`,
`quote.received`, `negotiation.started`, `negotiation.round`,
`negotiation.agreed`, `negotiation.failed`, `pi.sent`, `pi.accepted`,
`escrow.funded`, `po.issued`, `qc.booked`, `qc.passed`, `qc.failed`,
`freight.booked`, `freight.milestone`, `customs.filed`, `customs.cleared`,
`payout.requested`, `payout.completed`, `approval.requested`,
`approval.resolved`, `deal.exception`

Envelope: `DomainEvent { event_id, topic, deal_id?, occurred_at, payload }`.
Published today via `NoopPublisher` — swap in NATS/Kafka without touching callers.

---

## 7. Deal state machine

`shared/domain/state_machine.py` → `DEAL_TRANSITIONS` + `can_transition(from, to)`.

```
DRAFT → MATCHING → NEGOTIATING ⇄ NEGOTIATION_AGREED
                        │  └────► NEGOTIATION_FAILED → MATCHING | CANCELLED
                        ▼
NEGOTIATION_AGREED → PI_SENT → PI_ACCEPTED → ESCROW_PENDING → ESCROW_FUNDED
   → PO_ISSUED → SUPPLIER_DISPATCH_READY → QC_PASSED | QC_FAILED
QC_PASSED → IN_TRANSIT → CLEARED → PAID
QC_FAILED → DISPUTE → CANCELLED | REFUNDED

Terminal: PAID, CANCELLED, REFUNDED, NEGOTIATION_FAILED
```

Every service coerces the string to `DealStatus(value)` **before** calling
`can_transition()` — passing a raw string/mock causes a silent dict miss.

---

## 8. Third-party APIs — free vs paid

> Legend — **$0 path:** how to use this integration at zero cost.

### AI / LLM / embeddings

| Integration | Used by | Paid? | $0 path |
|---|---|---|---|
| **Groq LLM** (`qwen3-32b` → `qwen3.6-27b` → `gpt-oss-safeguard-20b`) | negotiation, exception resolver | Free tier | **Default key ships active**; free tier is generous. Keep as-is |
| Anthropic Claude (optional primary) | negotiation, exception resolver | Paid | Leave `LLM_PRIMARY_API_KEY` empty → chain starts at free Groq |
| OpenAI embeddings (`text-embedding-3-small`, 1536-d) | RAG | Paid (~$0.02/1M) | Set `RAG_EMBEDDING_PROVIDER=local` → `LocalHashEmbeddings` (offline, $0) |

### Data & persistence

| Integration | Used by | Paid? | $0 path |
|---|---|---|---|
| MongoDB Atlas | all services + RAG vectors + cache | Free **M0** tier | M0 = 512 MB shared, free forever, supports `$vectorSearch` |
| (persistence off) | all services | — | Leave every `*_MONGO_URI` empty → in-memory (fully functional, non-durable) |

### Trade intelligence & demand (Phase 1/2)

| Integration | Paid? | $0 path |
|---|---|---|
| UN Comtrade | Free (open) | Use directly (rate-limited) |
| Volza / Trademo / ImportGenius | Paid | Replace with free alternatives (see `workflow.md` → "Trade-Min Cost-Reduction"); or run stub |
| Alibaba RFQ, EU TED, GDELT | Free / open | Use directly |
| MacMap (tariffs) | Free-ish | Free alternatives documented; or stub |
| IndiaMART | Paid/keyed | Stub when key absent |

### Regulatory / verification (India + EU)

| Integration | Paid? | $0 path |
|---|---|---|
| GSTN / MCA / Udyam (via **API Setu**) | Free (govt, keyed) | Free API-Setu key; else connector returns graceful result |
| DGFT | Free/open | Use directly |
| VIES (EU VAT) | Free (open) | No key needed |
| Allianz Trade (credit) | Paid | Optional; skip for personal use |

### Execution: QC, freight, customs, payments

| Integration | Used by | Paid? | $0 path |
|---|---|---|---|
| SGS / Intertek (QC) | shield_link | Per-inspection | **Stub** (deal advances) when `SGS_API_KEY` absent |
| Cogoport / Freightos (freight) | shield_link | Per-booking | **Stub** when keys absent |
| ICEGATE (customs) | docugen, shield_link | Free/govt | **Stub** when key absent |
| Stripe Treasury (escrow) | escrow_guard | Per-transaction | **Stub**; or Stripe **test mode** (free sandbox) |
| Payoneer (payout) | escrow_guard | Per-payout | **Stub**; or sandbox |

### Email

| Integration | Used by | Paid? | $0 path |
|---|---|---|---|
| Gmail IMAP + SMTP | email_gateway | **Free** | Gmail + a free **App Password** |
| SendGrid / Mailgun inbound | email_processor | Free tiers | Optional; Gmail alone is enough |
| (email off) | — | — | Leave `IMAP_USER` + `GMAIL_SENDER_ADDRESS` empty → poll disabled + log-only send |

---

## 9. Running at $0 — the default (local)

**Everything below is free and needs no accounts.** This is the fastest path.

```bash
# 1. Clone + install (Python ≥ 3.11)
cd backend
pip install -r requirements.txt
pip install -r requirements-dev.txt      # for tests

# 2. (Optional) copy env — you can skip this entirely for the $0 default
cp .env.example .env                      # then leave the paid keys blank

# 3. Run the whole platform on one port
uvicorn app:app --reload --port 8081
#    Swagger UI: http://localhost:8081/docs

# 4. Verify
pytest -q                                 # full suite
pytest tests/test_health.py -q            # every service boots + /health 200
```

**Why this costs nothing:**

| Concern | Default behaviour at $0 |
|---|---|
| Database | No `*_MONGO_URI` → **in-memory** stores (full functionality, non-durable) |
| LLM | **Free Groq key ships as default** → negotiation + exception agents work |
| Embeddings / RAG | Set `RAG_EMBEDDING_PROVIDER=local` → `LocalHashEmbeddings`, no OpenAI |
| QC / freight / customs / payments | Connectors return **realistic stubs** → deals reach `PAID` |
| Regulatory / trade data | Free/open APIs, or graceful empty results |
| Email | No IMAP/SMTP creds → polling disabled + **log-only** send |

**Add durability for free (optional):** create a **MongoDB Atlas M0** cluster
(free forever), then set the `*_MONGO_URI` vars you care about. M0 supports
`$vectorSearch`, so RAG works too. This is the only "cloud signup" needed for a
persistent-but-free setup, and it's cloud-agnostic.

---

## 10. Free cloud hosting (Azure, personal use)

For a **personal-use** deployment you can host the whole thing on free tiers.
Recommended mapping (verify current free-tier terms — cloud offers change):

| Component | $0 local default | Azure free option | Notes |
|---|---|---|---|
| **Compute** (the monolith) | `uvicorn` locally | **Azure Container Apps** (monthly free grant: ~180k vCPU-s + 360k GiB-s + 2M req) **or** App Service **F1** (free) | Container Apps suits the single Docker image; F1 sleeps/limits CPU |
| **Database** (Mongo) | in-memory | **MongoDB Atlas M0** (free, any cloud) *or* **Azure Cosmos DB for MongoDB (vCore) free tier** | Atlas M0 is drop-in (code targets Atlas `$vectorSearch`); Cosmos vCore also does vector search but verify index syntax |
| **Vector search** (RAG) | `LocalHashEmbeddings` | Atlas M0 Vector Search (free) | Keep embeddings local for $0; Atlas M0 hosts the vectors |
| **LLM** | Groq free key | Keep **Groq** (free) *or* **GitHub Models** free tier | Azure OpenAI needs approval + is paid — avoid for $0 |
| **Scheduler / cron** | in-process APScheduler | Runs inside the same container (free) *or* Azure Functions timer (1M free exec/mo) | No extra service needed — APScheduler is in-process |
| **Email** | log-only | **Gmail** free + App Password | Simplest $0 email; Azure Communication Services email is pay-go |
| **Secrets** | `.env` | Container Apps secrets / App settings (free) | Or Azure Key Vault (tiny cost) |
| **Static/UI** (if added) | — | **Azure Static Web Apps** free tier | For a future frontend |

### Zero-cost Azure deployment steps (personal)

1. **Create a free Azure account** (12 months of popular services + always-free
   services + a starter credit). No charge if you stay on free tiers.
2. **Database — MongoDB Atlas M0** (recommended, cloud-agnostic, free forever):
   create a free M0 cluster, add a DB user + IP allow-list, copy the SRV URI,
   and create the Atlas **Vector Search** index (`vector_index`, field
   `embedding`, 1536 dims, cosine). *(Azure-native alternative: Cosmos DB for
   MongoDB vCore free tier.)*
3. **Build the image** from the provided `Dockerfile` (`python:3.11-slim`).
   The image is env-driven via `SERVICE_MODULE` + `PORT`; for the monolith set
   `SERVICE_MODULE=app:app` and `PORT=8081`.
4. **Deploy to Azure Container Apps** (consumption plan → stays in the free
   monthly grant for a low-traffic personal app). Set **min replicas = 0** so it
   scales to zero (no cost) when idle.
5. **Configure app settings / secrets** (Container Apps → Secrets): set only the
   free keys — `RAG_EMBEDDING_PROVIDER=local`, the Atlas `*_MONGO_URI`s you want
   durable, `LLM_GROQ_API_KEY` (or keep the default), and — if you want live
   email — `IMAP_USER` / `IMAP_PASSWORD` / `GMAIL_SENDER_ADDRESS` /
   `GMAIL_APP_PASSWORD`. Leave every paid key **blank** → stubs stay active.
6. **Keep LLM on Groq** (free). Do **not** enable Azure OpenAI / Anthropic for a
   $0 build.
7. **Scheduler needs nothing extra** — APScheduler runs inside the container.
8. **Verify** by hitting `/{service}/health` and `/docs` on the Container App URL.

> **Cost-guardrails:** scale-to-zero (min replicas 0), stay on Atlas **M0**, keep
> **all** paid connector keys blank (stubs), embeddings **local**, LLM on
> **Groq**. That combination is genuinely $0 for personal traffic.

### Non-Azure free alternatives (if you prefer)

- **Compute always-free VMs:** Oracle Cloud Always-Free (Arm Ampere) or Google
  Cloud `e2-micro` — run the monolith 24/7 for free.
- **Compute PaaS free:** Render / Railway / Fly.io free allowances, Hugging Face
  Spaces (for the FastAPI app).
- **DB:** MongoDB Atlas M0 works with any of the above unchanged.

---

## 11. Environment variables reference

**None are required for the $0 local run.** Set only what you want to enable.

```bash
# ── Persistence (all optional → in-memory when unset) ─────────────────
TRADE_MONGO_URI= DEALS_MONGO_URI= SOURCING_MONGO_URI= NEGOTIATION_MONGO_URI=
DEALMAKER_MONGO_URI= DOCUGEN_MONGO_URI= SHIELD_LINK_MONGO_URI=
ESCROW_GUARD_MONGO_URI= HITL_APPROVAL_MONGO_URI= EMAIL_PROCESSOR_MONGO_URI=
EXCEPTION_RESOLVER_MONGO_URI= AUTH_MONGO_URI= RAG_MONGO_URI= LLM_MONGO_URI=

# ── LLM (free by default) ─────────────────────────────────────────────
LLM_GROQ_API_KEY=            # default key active; override with your own free key
LLM_PRIMARY_API_KEY=         # leave blank → skip paid Claude/OpenAI primary

# ── RAG / embeddings (free by default) ────────────────────────────────
RAG_EMBEDDING_PROVIDER=local # "local" = $0 offline; "openai" = paid quality
RAG_EMBEDDING_API_KEY=       # only if provider=openai

# ── Email transport (Email Gateway) — leave blank to disable ──────────
IMAP_HOST=imap.gmail.com
IMAP_USER=                   # empty → IMAP polling disabled
IMAP_PASSWORD=               # Gmail App Password
IMAP_POLL_INTERVAL_SECONDS=900
GMAIL_SENDER_ADDRESS=        # empty → outbound is log-only
GMAIL_APP_PASSWORD=
FORWARD_VIA_HTTP=false       # gateway → processor forward mode

# ── Email processing (Email Processor) ────────────────────────────────
EMAIL_PROCESSOR_MONGO_URI=
PORTLESS_TRADE_ADDRESS=portless-trade@portless.io
SENDGRID_WEBHOOK_PUBLIC_KEY= MAILGUN_SIGNING_KEY=

# ── Execution connectors — leave blank → graceful stubs ───────────────
SGS_API_KEY= COGOPORT_API_KEY= FREIGHTOS_API_KEY= ICEGATE_API_KEY=
STRIPE_SECRET_KEY= PAYONEER_CLIENT_ID= PAYONEER_CLIENT_SECRET=
# Webhook HMAC secrets (set in production):
SGS_WEBHOOK_SECRET= COGOPORT_WEBHOOK_SECRET= FREIGHTOS_WEBHOOK_SECRET=
STRIPE_WEBHOOK_SECRET= PAYONEER_WEBHOOK_SECRET=

# ── Regulatory / trade (free where possible) ──────────────────────────
API_SETU_KEY= COMTRADE_SUBSCRIPTION_KEY= MACMAP_API_KEY=
```

See `.env.example` for the fully-commented list.

---

## 12. Security notes

- **Hardcoded default MongoDB Atlas credential.** `shared/rag/src/config.py`
  ships a **real SRV connection string with an embedded username/password** as
  the default `RAG_MONGO_URI` (and it's referenced from `llm_gateway` too). For
  any real deployment: **rotate that database user's password immediately**, and
  move the URI to an env var / secret (`RAG_MONGO_URI`) instead of a code
  default. Never commit live credentials.
- **Webhook verification.** All provider webhooks use HMAC-SHA256 (Mailgun,
  Stripe, SGS, Cogoport, Freightos, Payoneer, HITL). If a `*_WEBHOOK_SECRET` is
  unset the connector **logs a warning and accepts all** — set every secret in
  production. SendGrid ECDSA verification is a **placeholder** (accepts without
  verifying) until `ecdsa` is wired.
- **Outbound email HITL gate.** No negotiation email is sent without an L0
  approval — safe by default.
- **Secrets management (cloud):** use Container Apps secrets / Key Vault; keep
  `.env` out of version control.

---

## 13. Commands cheat-sheet

```bash
# Install
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run combined monolith (recommended for local / $0)
uvicorn app:app --reload --port 8081        # http://localhost:8081/docs

# Run a single service (see Makefile for all)
make deals            # 8001
make email-gateway    # 8012  (IMAP fetch + SMTP send)
make email-processor  # 8013  (inbound matching + persistence)

# Docker (one container per service)
make up               # docker compose up --build
make down

# Tests
pytest -q                              # full suite
pytest tests/test_health.py -q         # smoke: every service boots + /health 200
pytest tests/test_email_gateway.py tests/test_email_processor.py -q

# Check third-party API connectivity
python scripts/check_trade_api_connections.py
```

---

> **Bottom line for personal use:** run the monolith on an always-free compute
> (Azure Container Apps scaled-to-zero, or a free VM), point persistence at
> **MongoDB Atlas M0**, keep **embeddings local**, keep the **LLM on Groq**, use
> **Gmail** for email, and leave **every paid connector key blank** so the stubs
> stay active. Total recurring cost: **$0.**
