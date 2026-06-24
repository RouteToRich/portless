# PortLess — FastAPI Modular Monolith Skeleton

> Zero-Inventory Autonomous Merchant Exporter Platform.
> Scaffold derived from [`needed/portless_master_specification.md`](needed/portless_master_specification.md).

This repository starts as a **modular monolith**: every domain capability is either a
self-contained **service** module or an **agent** module. Services handle deterministic
business logic; agents handle reasoning-heavy tasks with LLM, RAG, and tool use.

Both services and agents have their own FastAPI app, router, schemas, service layer,
and REST client. Modules **never import each other's internals** — they talk over
**REST** through the typed clients in each module's `client.py`.

Because the boundaries and the transport are already REST, promoting any module
to a standalone **microservice** is just a matter of deploying it on its own and
pointing the client base URL at the new host. No code rewrite required.

## Architecture at a glance

```
Client / UI ──► api_gateway ──REST──► deals ──REST──► match_engine
                    │                   │                 │
                    ├──REST──► services: trade_min, sourcing, dealmaker,
                    │          shield_link, docugen, escrow_guard,
                    │          hitl_approval, exception_resolver
                    │
                    └──REST──► agents: negotiation_agent
```

| Module | Type | Responsibility | Default Port |
|---|---|---|---|
| `api_gateway` | service | Sync edge: auth, validation, webhook ingest, idempotency, request fan-out | 8000 |
| `deals` | service | Deal lifecycle + state machine (source of truth) | 8001 |
| `trade_min` | service | Opportunity discovery & arbitrage ranking | 8002 |
| `match_engine` | service | Composite Match Score (CMS) | 8003 |
| `sourcing` | service | RFQ dispatch & quote intake | 8004 |
| `negotiation_agent` | **agent** | Intelligent multi-round negotiation (RAG + internet + inbox + memory + HITL) | 8005 |
| `dealmaker` | service | PI / PO generation | 8006 |
| `shield_link` | service | QC / freight / CHA booking | 8007 |
| `docugen` | service | Document generation + customs filing | 8008 |
| `escrow_guard` | service | Escrow holds & milestone payouts | 8009 |
| `hitl_approval` | service | Human-in-the-loop approval service | 8010 |
| `exception_resolver` | service | Exception classification & remediation | 8011 |

## Layout

```
portless/
├─ shared/                  # cross-cutting, importable by all modules
│  ├─ domain/               # enums, DTOs, event catalog, state machine
│  ├─ clients/              # base async REST client
│  ├─ bus/                  # event-publisher seam (REST today → bus tomorrow)
│  └─ connectors/           # external adapters (quote/book/status) + registry
├─ agents/                  # AI agent modules (LLM reasoning + RAG + tools)
│  └─ negotiation_agent/    # intelligent multi-round negotiation agent
│     ├─ main.py            # standalone FastAPI app
│     ├─ router.py          # HTTP routes
│     ├─ schemas.py         # request/response Pydantic models
│     ├─ service.py         # agent logic (RAG, inbox monitor, memory, HITL)
│     └─ client.py          # REST client used by other modules
├─ services/                # deterministic service modules
│  └─ <service>/
│     ├─ main.py            # standalone FastAPI app (run as a microservice)
│     ├─ router.py          # HTTP routes
│     ├─ schemas.py         # request/response Pydantic models
│     ├─ service.py         # business logic (skeleton — not implemented)
│     └─ client.py          # REST client used by other modules
├─ tests/                   # smoke + state-machine tests
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
| **Tools** | Internal APIs, DB, event bus | LLM + RAG (pgvector) + internet + email/inbox + HITL |
| **Guardrails** | Input validation (Pydantic) | Policy guards + autonomy levels + HITL escalation |
| **Examples** | deals, match_engine, escrow_guard | negotiation_agent (future: exception_resolver_agent) |

## Run a single service (microservice mode)

```bash
pip install -r requirements.txt
uvicorn services.deals.main:app --reload --port 8001
```

## Run everything (monolith mode, separate processes)

```bash
docker compose up --build
```

Each module gets its own container and port, yet they all share one repo and the
`shared/` package. This is the "modular monolith → microservices" path.

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite smoke-tests that every service app boots with a healthy `/health`
endpoint and validates the deal state machine.

> **Status:** Skeleton only. Business logic in every `service.py` raises
> `NotImplementedError` and is marked with `TODO`. No persistence, bus, or
> external integration is wired up yet.
