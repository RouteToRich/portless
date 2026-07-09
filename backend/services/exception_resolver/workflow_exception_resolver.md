# Exception Resolver — Workflow & Implementation Reference

> **Module:** `services/exception_resolver/` · **Port:** 8011 · **Prefix:** `/exception-resolver`
> **Role:** The platform's **self-healing agent** — a LangGraph LLM agent that
> classifies any failure and chooses a remediation (auto-fix, escalate, cancel,
> or monitor).
> **Status:** ✅ Full · **Persistence:** MongoDB (`EXCEPTION_RESOLVER_MONGO_URI`) + in-memory

---

## 1. Purpose

- Ingest a `deal.exception` (from any service), classify it into one of 10
  categories, build a remediation plan, and act — always producing a result.
- Keep the pipeline resilient: a failure never dies silently; it becomes a typed,
  auditable exception with a decided next step.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| Any service that fails | `raise_exception()` with a `deal.exception` context |
| `hitl_approval.callbacks` | On saga-callback failure → `deal.exception` routed here |
| `api_gateway` | Read `GET /exceptions/{id}` |

| Invokes (downstream) | Why |
|---|---|
| `shared/llm_gateway` (`LLMClient`) | Classify + plan (Groq fallback chain) |
| `services.docugen.service` | `AUTO_FIX` for `DOCUMENT_ERROR` → regenerate document |
| `services.hitl_approval.service.create_approval` | `ESCALATE`/`CANCEL` → `RESOLVE_EXCEPTION` gate |
| event bus | Emits `deal.exception` (OPEN + final status) |

## 3. Workflow (LangGraph 2-node: analyze → execute)

```
raise_exception(context)  → persist OPEN record (crash-safe)  → status=ANALYZING
  analyze:  LLM classifies → {category, severity, decision, plan (JSON)}
  execute:  dispatch on decision → status=REMEDIATING → final:
     AUTO_FIX  → _AUTO_FIX_DISPATCH[category](record)   (e.g. DocuGen regen) → RESOLVED
                 (falls through to ESCALATE if no handler)
     ESCALATE  → hitl_approval.create_approval(RESOLVE_EXCEPTION) → ESCALATED
     CANCEL    → create approval / cancel path                    → ESCALATED
     MONITOR   → log + RESOLVED (no human)

LLM chain exhausted → conservative UNKNOWN/HIGH/ESCALATE (never raises)
Agent crash OR HITL creation fails → UNRESOLVABLE (manual intervention)
```

**Categories:** `QC_FAILED`, `FREIGHT_DELAY`, `PAYMENT_FAILED`,
`NEGOTIATION_FAILED`, `CUSTOMS_REJECTED`, `DOCUMENT_ERROR`, `CONNECTOR_ERROR`,
`MARGIN_BREACH`, `APPROVAL_REJECTED`, `CALLBACK_FAILED`, `UNKNOWN`.

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/exception-resolver/health` | Health |
| `POST` | `/exception-resolver/exceptions` | Raise → analyze → remediate |
| `GET` | `/exception-resolver/exceptions/{id}` | Fetch one |
| `GET` | `/exception-resolver/exceptions` | List (filter) |

## 5. Completion status

| Feature | Status |
|---|---|
| LangGraph analyze → execute | ✅ |
| 10-category classification + JSON plan | ✅ |
| AUTO_FIX / ESCALATE / CANCEL / MONITOR dispatch | ✅ |
| HITL saga (`RESOLVE_EXCEPTION`) | ✅ |
| Crash-safe OPEN record + LLM-exhausted fallback | ✅ |
| `tests/test_exception_resolver.py` (all categories, paths) | ✅ |

## 6. Waiting for configuration

| Var | Effect |
|---|---|
| `LLM_GROQ_API_KEY` | Classification LLM (**default key active** → free) |
| `LLM_PRIMARY_API_KEY` | Optional higher-tier primary (Claude/OpenAI) |
| `EXCEPTION_RESOLVER_MONGO_URI` | Durable exception storage; unset → in-memory |

## 7. Nice-to-have / pending

- More `AUTO_FIX` handlers (currently `DOCUMENT_ERROR` via DocuGen; add
  `CONNECTOR_ERROR` retry, `FREIGHT_DELAY` rebooking, etc.).
- Learning loop: feed resolved outcomes back into the prompt/RAG.
- Real event-bus subscription (today `deal.exception` is emitted via `NoopPublisher`).

## 8. Files

`schemas.py` (categories/severity/status/decision enums, records) · `agent.py`
(LangGraph nodes + AUTO_FIX dispatch) · `service.py` (raise/get/list) ·
`repository.py` · `router.py` · `client.py` (`ExceptionResolverClient`) ·
`config.py` · `main.py`.
