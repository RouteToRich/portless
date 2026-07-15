# PortLess — FastAPI Modular Monolith

> Zero-Inventory Autonomous Merchant Exporter Platform.

This repository is a **modular monolith**: every domain capability is either a
self-contained **service** module or an **agent** module. Services handle deterministic
business logic; agents handle reasoning-heavy tasks with LLM, RAG, and tool use.

Both services and agents have their own FastAPI app, router, schemas, service layer,
and REST client. Modules **never import each other's internals** directly over HTTP —
in the combined monolith they call each other's `service.py` in-process; in
standalone microservice mode they go through each module's typed `client.py` over REST.

Because the boundaries and the transport are already REST-shaped, promoting any module
to a standalone **microservice** is just a matter of deploying it on its own and
pointing the client base URL at the new host. No code rewrite required.

> **Status:** All modules below are fully implemented — see
> [`CLAUDE.md`](../CLAUDE.md)'s Implementation Status table and each module's
> own `workflow_<name>.md` for a detailed feature-by-feature audit. This file
> is a map, not the source of truth for what's done.

## Architecture at a glance

```
Client / UI ──► auth (JWT + orgs) ──► api_gateway ──REST──► deals ──REST──► match_engine
                                          │                   │                 │
                                          ├──REST──► services: trade_min, sourcing, dealmaker,
                                          │          shield_link, docugen, escrow_guard,
                                          │          hitl_approval, exception_resolver,
                                          │          email_gateway, email_processor
                                          │
                                          └──REST──► agents: negotiation_agent
```

`shared/auth/middleware.py` gates every request app-wide before it reaches a
router. `shared/orchestration/auto_flow.py` is an optional dev/test-only
background driver (off by default) that auto-continues a deal through the
flow above, still stopping at every HITL gate for a real operator decision.

| Module | Type | Responsibility | Default Port |
|---|---|---|---|
| `auth` | service | Register/login/JWT, organizations, non-Portless allowlist | mounted in `app.py` only (no standalone port) |
| `api_gateway` | service | Sync edge: auth, validation, webhook ingest, idempotency, request fan-out | 8000 |
| `deals` | service | Deal lifecycle + state machine (source of truth) | 8001 |
| `trade_min` | service | Opportunity discovery & arbitrage ranking | 8002 |
| `match_engine` | service | Composite Match Score (CMS) | 8003 |
| `sourcing` | service | RFQ dispatch & quote intake | 8004 |
| `negotiation_agent` | **agent** | Intelligent multi-round negotiation (RAG + live web search + inbox + memory + HITL) | 8005 |
| `dealmaker` | service | PI / PO generation | 8006 |
| `shield_link` | service | QC / freight / CHA booking | 8007 |
| `docugen` | service | Document generation + customs filing | 8008 |
| `escrow_guard` | service | Escrow holds & milestone payouts | 8009 |
| `hitl_approval` | service | Human-in-the-loop approval service (SSE + webhook) | 8010 |
| `exception_resolver` | **agent** | LangGraph exception classification & remediation | 8011 |
| `email_gateway` | service | IMAP fetch + SMTP send transport | 8012 |
| `email_processor` | service | Inbound email matching + persistence | 8013 |

## Layout

```
portless/
├─ shared/                  # cross-cutting, importable by all modules
│  ├─ domain/               # enums, DTOs, event catalog, state machine
│  ├─ clients/              # base async REST client
│  ├─ bus/                  # event-publisher seam (REST today → bus tomorrow)
│  ├─ connectors/           # external adapters (quote/book/status) + registry
│  ├─ auth/                 # AuthMiddleware, JWT security, org-scope deps, revocation blacklist
│  ├─ orchestration/        # auto_flow.py — optional dev/test auto-continuation driver
│  ├─ llm_gateway/          # resilient LLM client (Claude/OpenAI primary → Groq fallback chain)
│  ├─ rag/                  # MongoDB Atlas vector store + hybrid (vector/keyword/graph/web) RAG
│  └─ cache/                # shared MongoDB-backed TTL cache
├─ agents/                  # AI agent modules (LLM reasoning + RAG + tools)
│  ├─ negotiation_agent/    # intelligent multi-round negotiation agent (LangGraph)
│  └─ exception_resolver's LangGraph agent lives under services/exception_resolver/
├─ services/                # deterministic + agent-adjacent service modules
│  └─ <service>/
│     ├─ main.py            # standalone FastAPI app (run as a microservice)
│     ├─ router.py          # HTTP routes
│     ├─ schemas.py         # request/response Pydantic models
│     ├─ service.py         # business logic
│     ├─ repository.py      # Protocol + InMemoryFallback + MongoRepository
│     └─ client.py          # REST client used by other modules
├─ mocked/                  # realistic JSON fixtures for every service/state (UI dev without backend)
├─ tests/                   # 492 tests: smoke, state-machine, per-service, RAG backends, auth, market-intel...
├─ scripts/                 # check_trade_api_connections.py, e2e_mock_flow.py
├─ requirements.txt
├─ requirements-dev.txt
├─ pyproject.toml
├─ docker-compose.yml       # one container per module
├─ .env.example
└─ Makefile
```

### Services vs Agents

| Concern | `services/` | `agents/` |
|---|---|---|
| **Logic type** | Deterministic business rules, CRUD, sagas | LLM reasoning, RAG retrieval, autonomous decisions |
| **State** | Stateless or simple DB state | Persisted conversation memory, RAG context |
| **Tools** | Internal APIs, DB, event bus | LLM Gateway + RAG (Atlas vector/keyword/graph/web) + email inbox + HITL |
| **Guardrails** | Input validation (Pydantic) | Policy guards + autonomy levels + HITL escalation |
| **Examples** | deals, match_engine, escrow_guard | negotiation_agent; exception_resolver (LangGraph, lives under `services/`) |

## Run a single service (microservice mode)

```bash
pip install -r requirements.txt
uvicorn services.deals.main:app --reload --port 8001
# or: make deals   (see Makefile for every module's target/port)
```

## Run everything

```bash
# All services in one process (recommended for local dev)
uvicorn app:app --reload --port 8081

# Or one container per module
docker compose up --build
```

Each module gets its own container and port in Docker mode, yet they all share one repo and the
`shared/` package. This is the "modular monolith → microservices" path.

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite (492 tests) covers: every service boots with a healthy `/health`
endpoint, the deal state machine, per-service business logic (dealmaker,
docugen, shield-link, escrow-guard, hitl-approval, exception-resolver,
match-engine, negotiation agent + its market-intel leg), auth/org scoping,
trade-min caching, and the RAG backends.
