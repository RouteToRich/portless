# PortLess — FastAPI Modular Monolith Skeleton

> Zero-Inventory Autonomous Merchant Exporter Platform.
> Scaffold derived from [`needed/portless_master_specification.md`](needed/portless_master_specification.md).

This repository starts as a **modular monolith**: every domain capability is a
self-contained service module with its own FastAPI app, router, schemas, service
layer, and REST client. Services **never import each other's internals** — they
talk over **REST** through the typed clients in each module's `client.py`.

Because the boundaries and the transport are already REST, promoting any module
to a standalone **microservice** is just a matter of deploying it on its own and
pointing the client base URL at the new host. No code rewrite required.

## Architecture at a glance

```
Client / UI ──► api_gateway ──REST──► deals ──REST──► match_engine
                    │                   │                 │
                    └──REST──► trade_min, sourcing, negotiator, dealmaker,
                              shield_link, docugen, escrow_guard,
                              hitl_approval, exception_resolver
```

| Module | Responsibility | Default Port |
|---|---|---|
| `api_gateway` | Sync edge: auth, validation, webhook ingest, idempotency, request fan-out | 8000 |
| `deals` | Deal lifecycle + state machine (source of truth) | 8001 |
| `trade_min` | Opportunity discovery & arbitrage ranking | 8002 |
| `match_engine` | Composite Match Score (CMS) | 8003 |
| `sourcing` | RFQ dispatch & quote intake | 8004 |
| `negotiator` | Bounded buyer/supplier negotiation loop | 8005 |
| `dealmaker` | PI / PO generation | 8006 |
| `shield_link` | QC / freight / CHA booking | 8007 |
| `docugen` | Document generation + customs filing | 8008 |
| `escrow_guard` | Escrow holds & milestone payouts | 8009 |
| `hitl_approval` | Human-in-the-loop approval service | 8010 |
| `exception_resolver` | Exception classification & remediation | 8011 |

## Layout

```
portless/
├─ shared/                  # cross-cutting, importable by all services
│  ├─ domain/               # enums, DTOs, event catalog, state machine
│  ├─ clients/              # base async REST client
│  ├─ bus/                  # event-publisher seam (REST today → bus tomorrow)
│  └─ connectors/           # external adapters (quote/book/status) + registry
├─ services/                # one folder per service module
│  └─ <service>/
│     ├─ main.py            # standalone FastAPI app (run as a microservice)
│     ├─ router.py          # HTTP routes
│     ├─ schemas.py         # request/response Pydantic models
│     ├─ service.py         # business logic (skeleton — not implemented)
│     └─ client.py          # REST client used by other services
├─ tests/                   # smoke + state-machine tests
├─ requirements.txt
├─ requirements-dev.txt
├─ pyproject.toml
├─ docker-compose.yml       # one container per service
├─ .env.example
└─ Makefile
```

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
