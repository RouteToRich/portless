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

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/deals/health` | Health |
| `GET` | `/deals` | List (filter: status, operator_id, limit) |
| `POST` | `/deals` | Create + seed + score |
| `GET` | `/deals/{deal_id}` | Fetch one (404 if absent) |
| `PATCH` | `/deals/{deal_id}/status` | Guarded transition |
| `PATCH` | `/deals/{deal_id}/financials` | Patch margin / target price / winner / CMS |
| `GET` | `/deals/{deal_id}/match-scores` | Match history for the deal |

## 5. Data & persistence

`DealRecord` keyed by `deal_id`; Mongo indexes: `deal_id` (unique), `operator_id`,
`status`, `opportunity_id` (sparse), `created_at desc`. `mark_status()` is an
atomic `$set` + `$push status_history`.

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

## 8. Nice-to-have / pending

- Dedicated `tests/test_deals.py` (no unit test file today).
- Auto-transition to `NEGOTIATION_AGREED/FAILED` currently driven by callers; a
  subscriber on `negotiation.agreed/failed` events would decouple it (needs a
  real event bus — `NoopPublisher` today).
- Optimistic-concurrency / version field for high-contention updates.

## 9. Files

`schemas.py` (DealRecord + requests) · `service.py` (create/get/update + guard) ·
`repository.py` (InMemory + Mongo) · `router.py` · `client.py` (`DealsClient`) ·
`config.py` · `main.py`.
