# Deals — Workflow & Implementation Reference

> **Module:** `services/deals/` · **Port:** 8001 · **Prefix:** `/deals`
> **Role:** The **deal lifecycle state machine** and orchestration hub — the single
> source of truth for a deal's `status`. Every execution service transitions the
> deal through this module.
> **Status:** ✅ Full · **Persistence:** MongoDB (`DEALS_MONGO_URI`) + in-memory fallback

---

## 1. Purpose

- Own the canonical `DealRecord` and its `status` (19 states, see `shared/domain`).
- Enforce the deal state machine — every transition is checked with
  `can_transition(from, to)` before it is persisted; illegal transitions are rejected.
- Orchestrate deal creation: seed financials from Trade-Min and score via Match Engine.
- Keep an append-only `status_history` audit trail.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| `api_gateway` | CRUD + status/financials via HTTP |
| `sourcing` | `get_deal`, `update_financials`, `update_status` → `NEGOTIATING` |
| `dealmaker` | `get_deal`, `update_status` → `PI_SENT` / `PI_ACCEPTED` / `PO_ISSUED` |
| `shield_link` | `update_status` → `SUPPLIER_DISPATCH_READY` / `QC_*` / `IN_TRANSIT` / `CLEARED` |
| `escrow_guard` | `update_status` → `ESCROW_FUNDED` / `PAID` |
| `docugen` | `get_deal` (reads deal data for documents) |

| Invokes (downstream) | Why |
|---|---|
| `trade_min.service.get_match_seed()` / `mark_opportunity_converted()` | Seed a deal from a detected opportunity |
| `match_engine.service.score_match()` | Compute initial CMS at creation |
| `shared.domain.state_machine.can_transition()` | Guard every transition |
| `shared.orchestration.auto_flow.start()` | Fire-and-forget auto-continuation task (dev/test only — see §3a) |

*(Monolith uses in-process imports; microservice mode swaps for `DealsClient`.)*

## 3. Workflow

```
POST /deals {opportunity_id}
  1. get_match_seed(opportunity_id)      ← Trade-Min
  2. score_match(seed + supplier)        ← Match Engine  → CMS
  3. persist DealRecord (status=DRAFT → MATCHING), status_history seeded
  4. mark_opportunity_converted()        ← Trade-Min (best-effort)

PATCH /deals/{id}/status {target_status}
  → DealStatus(current); can_transition(current, target)?  reject if false
  → mark_status(): $set status + $push status_history
```

### 3a. Auto-Flow orchestrator (`shared/orchestration/auto_flow.py`, dev/test only)

Gated by `AUTO_FLOW_ENABLED` (`shared/config.py`, default `False`). When on,
`create_deal()` fires a background `asyncio.create_task` for any deal that
reaches `MATCHING` via an opportunity — it drives the deal through
sourcing → negotiation → PI → escrow funding using the **real service-layer
functions** (no HTTP, no business-logic mocking). It stops and
creates a normal HITL approval (at the deal's own `autonomy_profile`, usually
L0) before `ISSUE_PO`, `BOOK_FREIGHT`, and `RELEASE_SUPPLIER_PAYOUT` — an
operator resolves these the same way as any real HITL deployment. It polls
for negotiation convergence rather than self-driving further rounds; real
back-and-forth still happens over actual email or a manual "Run Round".
Escrow-funded / QC-pass / freight-cleared webhooks are simulated a few
seconds after each booking call, since the third-party connectors that would
normally fire them are stubbed. This is an in-memory task, not a durable
job — a server restart silently kills any in-flight run.

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/deals/health` | Health | Public |
| `GET` | `/deals` | List — scoped to the caller's organization (all orgs for Portless staff); filter: status, limit | Portless + non-Portless |
| `POST` | `/deals` | Create + seed + score | Portless only |
| `GET` | `/deals/{deal_id}` | Fetch one (404 if absent) | Portless only |
| `PATCH` | `/deals/{deal_id}/status` | Guarded transition | Portless only |
| `PATCH` | `/deals/{deal_id}/financials` | Patch margin / target price / winner / CMS | Portless only |
| `GET` | `/deals/{deal_id}/match-scores` | Match history for the deal | Portless only |

`GET /deals` is the *only* endpoint in this table (besides health) a
non-Portless caller can reach at all — `shared/auth/middleware.py`'s
non-Portless allowlist 403s everything else app-wide, before the request
even reaches this router's own `require_org_scope`/`_org_deal` checks. The
Ops Console mirrors this: a non-Portless login renders
`portless-ui/src/pages/OrgDeals.tsx` (deal_id, hs_code, status only — no drill-in),
never `DealBoard.tsx` or `DealDetail.tsx`.

## 5. Data & persistence

`DealRecord` keyed by `deal_id`; Mongo indexes: `deal_id` (unique), `operator_id`,
`org_id`, `status`, `opportunity_id` (sparse), `created_at desc`. `mark_status()`
is an atomic `$set` + `$push status_history`.

`org_id` ties every deal to the creating user's organization — assigned by a
Portless admin via the Organizations admin UI, not derived from the user's
email (see `services/auth/organization.py`) — and is stamped server-side,
never client-supplied. `GET /deals` and `GET /deals/{id}` are scoped to
`org_id == caller's org` except for Portless
platform staff (`is_portless` JWT claim), who see every organization's deals.

## 6. Completion status

| Feature | Status |
|---|---|
| State machine + `can_transition` guard | ✅ |
| Create → seed (Trade-Min) → score (Match Engine) | ✅ |
| `update_status` / `update_financials` / history | ✅ |
| Mongo + in-memory repos | ✅ |
| Correlation back-refs (`opportunity_id`) | ✅ (no negotiation_id/email field — matching goes via the negotiation session) |

## 7. Waiting for configuration

- `DEALS_MONGO_URI` — set for durable storage; unset → in-memory.
- No external API keys required (this service is pure orchestration).
- `AUTO_FLOW_ENABLED` — off by default; turns on the dev/test auto-continuation
  task described in §3a. Not intended for production (autonomy/HITL gates
  still apply, but the background driver itself is a dev/test convenience).

## 8. Nice-to-have / pending

- Dedicated `tests/test_deals.py` (no unit test file today).
- Auto-transition to `NEGOTIATION_AGREED/FAILED` currently driven by callers; a
  subscriber on `negotiation.agreed/failed` events would decouple it (needs a
  real event bus — `NoopPublisher` today).
- Optimistic-concurrency / version field for high-contention updates.

## 9. Files

`schemas.py` (DealRecord + requests) · `service.py` (create/get/update + guard) ·
`repository.py` (InMemory + Mongo) · `router.py` · `client.py` (`DealsClient`) ·
`config.py` · `main.py`. Dev/test auto-continuation driver lives outside this
module at `shared/orchestration/auto_flow.py` (see §3a).
