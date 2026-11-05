# Trade-Min — Workflow & Implementation Reference

> **Module:** `services/trade_min/` · **Port:** 8002 · **Prefix:** `/trade-min`
> **Role:** Phase-1 **opportunity discovery** — scans global trade signals and
> produces enriched, scored export opportunities that seed deals.
> **Status:** ✅ Full · **Persistence:** MongoDB (`TRADE_MONGO_URI`) + in-memory · **Scheduler:** APScheduler

---

## 1. Purpose

- Continuously detect **who is buying what** using multi-tier demand signals
  (historical trend → buying-cycle pattern → live RFQ → disruption news).
- Enrich each opportunity (product, buyer, tariff gate, candidate suppliers) and
  emit `opportunity.detected`.
- Provide the `MatchEngineSeed` that the Deals service uses to create a deal.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| `deals.service` | `get_match_seed(opportunity_id)`, `mark_opportunity_converted()` |
| `api_gateway` | `POST /opportunities/scan`, `GET /opportunities` |
| **APScheduler** (self) | Nightly scan + reorder-window cron jobs |

| Invokes (downstream) | Why |
|---|---|
| `shared/connectors` via `registry.get_connector()` | Trade intel (Comtrade, Volza, MacMap…), demand signals (Alibaba RFQ, EU TED, GDELT), regulatory |

## 3. Workflow

```
POST /trade-min/scan   (or nightly cron)
  Phase 1  Historical demand      ← Comtrade / trade-intel connectors
  Phase 2  Live demand signals    ← Alibaba RFQ · EU TED · GDELT
  Phase 3  Tariff gate            ← MacMap / tariff source
  Phase 4  Supplier discovery     ← IndiaMART / directories
  Phase 5  Enrich + score urgency → EnrichedOpportunity[]  → opportunity.detected
  (phases run concurrently; missing keys → graceful empty/stub results)

Scheduler:
  scheduler_nightly_cron   → full market scan
  scheduler_reorder_cron   → buying-cycle "due to reorder" pass
```

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/trade-min/health` | Health |
| `POST` | `/trade-min/scan` | Run the discovery pipeline → opportunities |
| `GET` | `/trade-min/opportunities` | List enriched opportunities |
| `GET` | `/trade-min/opportunities/{id}` | Fetch one |
| `GET` | `/trade-min/…` | Match-seed / detail read (see router) |
| `POST` | `/trade-min/…` | Manual/seed op (see router) |

## 5. Completion status

| Feature | Status |
|---|---|
| 5-phase concurrent pipeline | ✅ |
| APScheduler nightly + reorder crons | ✅ |
| Graceful connector stubs (missing keys) | ✅ |
| MongoDB + in-memory + response cache | ✅ |
| `get_match_seed` / `mark_opportunity_converted` for Deals | ✅ |

## 6. Waiting for configuration

Everything degrades gracefully; set keys to go live:

| Var | Unlocks |
|---|---|
| `COMTRADE_SUBSCRIPTION_KEY` | Historical demand (free/open works too) |
| `VOLZA_API_KEY` | Named-buyer discovery (paid; free alternatives exist) |
| `ALIBABA_APP_KEY` / `_SECRET`, `INDIAMART_API_KEY` / `_MOBILE` | Live RFQ + supplier directories |
| `MACMAP_API_KEY` | Tariff gate |
| `API_SETU_KEY` | Regulatory enrichment |
| `TRADE_MONGO_URI` | Durable storage |
| `SCHEDULER_NIGHTLY_CRON`, `SCHEDULER_REORDER_CRON` | Cron cadence |

## 7. Nice-to-have / pending

- **Named-buyer discovery** is the genuine paid gap (Volza-class) — see
  `workflow.md` → "Trade-Min Cost-Reduction" for free-first replacements.
- GDELT network path is often blocked in restricted environments.
- Swap `registry.get_connector` stubs for live sources incrementally.

## 8. Files

`schemas.py` (EnrichedOpportunity, MatchEngineSeed) · `service.py` (pipeline +
scheduler jobs) ·
`repository.py` · `router.py` · `client.py` (`TradeMinClient`) ·
`config.py` · `main.py` (APScheduler lifespan).
