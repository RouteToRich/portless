# Match Engine — Workflow & Implementation Reference

> **Module:** `services/match_engine/` · **Port:** 8003 · **Prefix:** `/match-engine`
> **Role:** Deterministic **Composite Match Score (CMS)** — scores a buyer↔supplier
> pair 0–100. **No LLM, no external calls** — a pure, testable formula.
> **Status:** ✅ Full · **Persistence:** MongoDB (optional) + in-memory

---

## 1. Purpose

- Compute the CMS from three weighted components and a hard regulatory gate.
- Return a SOURCE / REJECT decision the caller uses to advance or drop a deal.

**Formula:** Profit margin (**50 %**) + Port proximity (**30 %**) + Reliability
(**20 %**). **Gatekeeper:** any failed regulatory check ⇒ score forced to **0**.
Pass threshold ≈ **75**.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| `deals.service.create_deal` | Initial CMS at deal creation |
| `sourcing.service` | Re-score when a verified quote arrives |
| `api_gateway` | `POST /match-engine/score` |

| Invokes (downstream) | — |
|---|---|
| *(none — deterministic, self-contained)* | Pure computation; no connectors, no LLM |

## 3. Workflow

```
POST /match-engine/score {seed + supplier verification}
  → margin score   (arbitrage spread → 0..100, max at ≥30% margin)
  → proximity score (distance to nearest port → 0..100)
  → reliability score (years + shipments + Udyam)
  → if any regulatory gate FAILED → CMS = 0 (REJECT)
  → else CMS = 0.5·margin + 0.3·proximity + 0.2·reliability
  → decision = SOURCE if CMS ≥ threshold else REJECT
  → persist ScoreResponse (keyed by deal_id)
```

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/match-engine/health` | Health |
| `POST` | `/match-engine/score` | Score a pair → CMS + decision |
| `GET` | `/match-engine/scores` | List scores |
| `GET` | `/match-engine/scores/{deal_id}` | Latest score for a deal |

## 5. Completion status

| Feature | Status |
|---|---|
| Deterministic 3-component CMS | ✅ |
| Regulatory gatekeeper (zero-on-fail) | ✅ |
| Score persistence + history | ✅ |
| `tests/test_match_engine.py` | ✅ |

## 6. Waiting for configuration

- `MATCH_ENGINE_MONGO_URI` (optional) — durable score history; unset → in-memory.
- **No external keys** — this service never leaves the process.

## 7. Nice-to-have / pending

- Configurable component weights per commodity / operator policy.
- Explainability payload (per-component breakdown already computable; expose it).
- Live logistics-cost feed to sharpen the proximity component.

## 8. Files

`schemas.py` (ScoreRequest/Response, SupplierVerification) · `service.py`
(formula + gate) · `repository.py` · `router.py` · `client.py`
(`MatchEngineClient`) · `config.py` · `main.py`.
