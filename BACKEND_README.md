# PortLess backend — FastAPI modular monolith

The backend is a **modular monolith**: every capability is a self-contained module with
its own FastAPI app, router, schemas, service layer, repository and typed REST client.
In development all modules mount on one app (`app.py`, port 8081) and call each other's
`service.py` in-process; in production each module can run on its own port and the same
calls go through its `client.py` over REST. Promoting a module to a microservice is a
deployment decision, not a rewrite.

The functional walkthrough lives in [workflow.md](workflow.md); the engineering reference
(every env var, flow and invariant) in [CLAUDE.md](CLAUDE.md). This file is the map.

## Modules

| Module | Type | Responsibility | Port |
|---|---|---|---|
| `services/auth` | service | Identity (`/identity/*`): sign-in is auth-service's via the RouteToRich gateway; PortLess keeps organizations + the Portless-staff allowlist; `shared/auth/middleware.py` checks every token | monolith only |
| `services/api_gateway` | service | Edge proxy for microservice mode: auth, validation, webhook ingest, idempotency, fan-out | 8000 |
| `services/deals` | service | Deal record + state machine (source of truth), parties, buyer candidates, timeline, evidence pack | 8001 |
| `services/trade_min` | service | Opportunity discovery (5 phases, own Apify actors + free APIs), catalogue / corridors, scheduler | 8002 |
| `services/match_engine` | service | Composite Match Score (SOURCE / NEGOTIATE / REJECT) | 8003 |
| `services/sourcing` | service | RFQ dispatch (API + `[RFQ:…]` e-mail), quote intake, GSTN / DGFT / MCA / Udyam verification | 8004 |
| `agents/negotiation_agent` | agent | LangGraph negotiation: static trade context + optional market intel, HITL-gated e-mails, intent classifier, agreement gate | 8005 |
| `services/dealmaker` | service | Proforma Invoice / Purchase Order (Jinja2 + SHA-256, Google Drive), margin guard | 8006 |
| `services/shield_link` | service | QC booking (5 inspectors), freight, CHA / ICEGATE, carrier milestones incl. `DELIVERED`, tracking poll | 8007 |
| `services/docugen` | service | CI / PL / CoO / SDF / Shipping Bill rendering, customs filing | 8008 |
| `services/escrow_guard` | service | Neutral staged escrow, funding + shortfall, ordered tranche release, acceptance window, payouts, ledger, journal | 8009 |
| `services/hitl_approval` | service | Gates L0–L3 with timeouts and value thresholds, four-eyes, SSE / webhook / Telegram, saga callbacks, digest | 8010 |
| `services/exception_resolver` | agent | Playbooks first, then a LangGraph LLM plan: AUTO_FIX / ESCALATE / CANCEL / MONITOR | 8011 |
| `services/email_gateway` | service | IMAP poll + SMTP send transport | 8012 |
| `services/email_processor` | service | Maps every inbound mail to a deal stage (outreach reply, quote, counter-offer, PI acceptance, delivery acceptance) | 8013 |

## Shared packages

```
shared/
├─ domain/            enums, DealStatus state machine, EventTopic catalogue, Party model
├─ bus/               EventPublisher: in-process bus or NATS JetStream (durable, replayed)
├─ orchestration/
│  ├─ continuations.py  event subscribers that move a deal to its next step (the automation backbone)
│  ├─ autonomy_policy.py gate policy v2: level, timeout, on-timeout default, value threshold, money ceiling
│  ├─ outreach.py        buyer outreach funnel (introductions, replies, follow-ups, next candidate)
│  ├─ enrichment.py      contact finder → e-mail check → sanctions screening onto the deal parties
│  ├─ notifications.py   templated counterparty e-mails (PI, funding, PO, QC, shipment, window, settlement…)
│  ├─ deal_watcher.py    SLA sweep: reminders, tracking poll, window fallback, stall exceptions
│  ├─ playbooks.py       deterministic remediations (re-inspection, shortfall, cancel-unfunded, next buyer)
│  ├─ evidence.py        deal timeline + evidence pack (JSON / HTML, SHA-256)
│  ├─ digest.py          daily operator digest (Telegram / e-mail)
│  └─ auto_flow.py       dev-only poller that fakes buyer actions (off by default)
├─ connectors/        quote / book / status / webhook_verify adapters + registry: Apify actors (8 slots),
│                     inspectors, Cogoport / Freightos, ICEGATE, Tazapay / Escrow.com, Stripe, Payoneer,
│                     Google Drive, trade intelligence, regulatory APIs — stubs when keys are absent
├─ catalogue.py       product lines / corridors behind GET/PUT /trade-min/catalogue
├─ readiness.py       production readiness gate (GET /health/readiness)
├─ telegram.py        operator push
├─ auth/              AuthMiddleware, auth-service token check (identity.py), org scoping
├─ llm_gateway/       LLM client → shared LLM Gateway (no provider keys held) + call tracking
└─ cache/             shared Mongo-backed TTL cache (raw third-party responses)
```

Every service module follows the same layout: `main.py` (standalone app + lifespan),
`router.py`, `schemas.py`, `service.py`, `repository.py` (Protocol + in-memory fallback +
Mongo), `client.py`, `config.py`, and a `workflow_<name>.md` with that module's detail.

## Run

```bash
pip install -r requirements.txt -r requirements-dev.txt

uvicorn app:app --reload --port 8081          # everything on one port, Swagger at /docs
make deals                                    # one module standalone (see Makefile for all ports)
docker compose up --build                     # one container per module
```

Configuration is `.env` next to this file (copy `.env.example`). With nothing set the
whole pipeline still runs: in-memory stores, stub connectors, log-only e-mail, in-process
bus. `GET /health/readiness` lists what is stubbed.

## Tests

```bash
python -m pytest tests --rootdir=.                 # full suite, ~7 min
python -m pytest tests/test_health.py -q           # every module boots, /health 200
python -m pytest tests/test_escrow_money_chain.py tests/test_money_continuations.py -q
python -m pytest tests/test_gate_policy_v2.py tests/test_batch_b.py tests/test_batch_c.py -q
```

Run from the repository root with `--rootdir=.` so the asyncio auto mode in
`pyproject.toml` applies. Baseline: 885 pass; 9 tests need a live LLM key or MongoDB
(`tests/llm_gateway/*`, `test_llm_response_cache.py`). Unit tests
never start a paid Apify run (`tests/conftest.py` pins stub mode).

## Scripts

| Script | Purpose |
|---|---|
| `scripts/check_trade_api_connections.py` | Connectivity check of every third-party API with the configured keys |
| `scripts/google_drive_authorize.py` | One-time OAuth consent for the Google Drive document store |

