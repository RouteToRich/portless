# Sourcing — Workflow & Implementation Reference

> **Module:** `services/sourcing/` · **Port:** 8004 · **Prefix:** `/sourcing`
> **Role:** Phase-2 **supplier sourcing** — dispatch RFQs, verify candidate
> factories against government registries, re-score, and move the deal into
> negotiation.
> **Status:** ✅ Full · **Persistence:** MongoDB (`SOURCING_MONGO_URI`) + in-memory

---

## 1. Purpose

- Send RFQs to candidate suppliers and record their quotes.
- Run parallel **government background checks** (GSTN · DGFT · MCA · Udyam, plus
  EU VIES where relevant) — a failed check is a hard gate.
- Re-score the verified pair via Match Engine; on `SOURCE`, patch deal financials
  and transition `MATCHING → NEGOTIATING`.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| `api_gateway` | RFQ / quote / verify endpoints |
| External supplier / portal | Quote submission (`POST /sourcing/quotes`) |

| Invokes (downstream) | Why |
|---|---|
| `shared/connectors` via `get_connector()` | RFQ dispatch + regulatory verification (API Setu, DGFT, VIES…) |
| `match_engine.service.score_match()` | Re-score with verified supplier data |
| `deals.service.get_deal` / `update_financials` / `update_status` | Patch financials + transition → `NEGOTIATING` (best-effort) |

## 3. Workflow

```
POST /sourcing/rfq        → dispatch RFQ to candidate(s) via connector

POST /sourcing/quotes     (supplier replies)
  1. persist QuoteRecord
  2. verify supplier  ── GSTN · DGFT · MCA · Udyam (parallel) ── any FAIL ⇒ gate
  3. score_match(seed + verification)          ← Match Engine
  4. if decision == SOURCE:
        update_financials(winning supplier, CMS, target price)   ← Deals
        update_status(MATCHING → NEGOTIATING)                    ← Deals (best-effort)
```

The deal transition is **best-effort**: if it fails the quote is still persisted.

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/sourcing/health` | Health |
| `POST` | `/sourcing/rfq` | Dispatch an RFQ |
| `GET` | `/sourcing/rfqs/{id}` · `/sourcing/deals/{deal_id}/rfqs` | RFQ reads |
| `POST` | `/sourcing/quotes` | Intake quote → verify → re-score → transition |
| `GET` | `/sourcing/quotes/{id}` · `/sourcing/deals/{deal_id}/quotes` | Quote reads |
| `POST` | `/sourcing/verify/{supplier_id}` | Run verification on demand |

## 5. Completion status

| Feature | Status |
|---|---|
| RFQ dispatch + quote intake | ✅ |
| Parallel GSTN/DGFT/MCA/Udyam verification | ✅ |
| Match-Engine re-score + deal patch → NEGOTIATING | ✅ |
| Regulatory response caching | ✅ |
| Mongo + in-memory repos | ✅ |

## 6. Waiting for configuration

| Var | Unlocks |
|---|---|
| `API_SETU_KEY` (+ `API_SETU_CLIENT_ID`) | GSTN / MCA / Udyam live verification |
| `SOURCING_MONGO_URI` | Durable RFQ/quote storage |
| (VIES) | No key needed — open EU endpoint |

Missing keys → connectors return graceful results (verification degrades, not crashes).

## 7. Nice-to-have / pending

- Dedicated `tests/test_sourcing.py` (verification parallelism + re-score path).
- Multi-supplier RFQ fan-out with automatic best-quote selection.
- Retry/backoff on flaky government endpoints; surface per-check status to the UI.

## 8. Files

`schemas.py` (RfqRecord, QuoteRecord, VerificationResult) · `service.py` (RFQ +
verify + re-score + transition) · `repository.py` · `router.py` · `client.py`
(`SourcingClient`) · `config.py` · `main.py`.
