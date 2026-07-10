# LLM Gateway — Workflow & Implementation Reference

> **Module:** `shared/llm_gateway/`
> **Purpose:** one resilient entry point for **chat/completion** LLM calls — automatic model fallback, per-model retry, and per-request tracking.
> **Engine:** LangGraph state machine (`init_request → call_endpoint → handle_outcome`).
> **Last reviewed:** 2026-07-10.
> Legend: ✅ implemented · ⚠️ partial / present-but-off · ❌ stub (raises `NotImplementedError`) · 💡 nice-to-have

---

## 1. Purpose

A single, dependency-injected client (`LLMClient`) that every agent/service uses instead of calling a provider SDK directly. It gives:

- **Resilience** — a fallback chain (primary → Groq chain) with per-model retries and exponential backoff.
- **Observability** — every attempt (success, fallback, error) is persisted by a tracker.
- **Provider independence** — Groq / OpenAI / Anthropic (via LangChain) and any OpenAI-compatible HTTP endpoint, chosen per-endpoint by a `connector` string.

> **Scope note.** Primarily a **chat/completion** gateway. As of 2026-07-10 it also
> provides **embeddings** via `GatewayEmbeddings` (`embeddings.py`) — the single
> place any embedding-provider SDK is instantiated. RAG calls it (passing its
> `RAG_EMBEDDING_*` config); the gateway resolves the provider + key (RAG key →
> gateway primary OpenAI key) and falls back to local hash embeddings offline.

---

## 2. Where it sits (callers ↔ callees)

```
Callers                                   Gateway                Callees (per endpoint.connector)
───────                                   ───────                ────────────────────────────────
negotiation_agent (orchestrator +         LLMClient.chat()  ──▶  groq       → ChatGroq (langchain_groq)
  buyer + supplier nodes)            ──▶                    ──▶  openai     → ChatOpenAI (langchain_openai)
exception_resolver agent            ──▶                    ──▶  anthropic  → ChatAnthropic (langchain_anthropic)
RAG generation (shared/rag/src/llm) ──▶                    ──▶  http       → raw httpx POST /chat/completions
```

Trackers write to **MongoDB** (`MongoTracker`) or a **JSONL file** (`RequestTracker`) as fallback.

---

## 3. Workflow

`LLMClient.chat(messages, caller, temperature?, max_tokens?, **kwargs)`:

```
1. Resolve ordered endpoints from the FallbackChain
     primary (if LLM_PRIMARY_API_KEY set)  →  groq-1  →  groq-2  →  groq-3
2. (opt) Response-cache lookup — return a cached identical request
3. LangGraph loop, per endpoint:
     init_request   → tracker.start_request()
     call_endpoint  → dispatch with retries:
                        • transient error + budget left → sleep(base × 2^attempt), retry
                        • fatal error → break to next model
     handle_outcome → tracker.complete_request(); on failure advance current_idx
4. First success → persist "success"/"fallback" record, (opt) cache, return LLMResponse
5. All endpoints exhausted → raise LLMExhaustedException
```

`LLMResponse` carries `content, model, prompt_tokens, completion_tokens, finish_reason, raw` (+ `total_tokens`). **No attempt is silently dropped** — every attempt reaches the tracker.

---

## 4. Configuration reference (`LLM_` prefix)

| Var | Default | Effect |
|---|---|---|
| `LLM_PRIMARY_MODEL` / `LLM_PRIMARY_API_KEY` / `LLM_PRIMARY_CONNECTOR` | empty / `http` | Optional higher-tier primary (e.g. Claude: `connector=anthropic`, `model=claude-sonnet-4-6`). Empty → chain starts at Groq. |
| `LLM_PRIMARY_BASE_URL` | empty | Only for `connector=http`. |
| `LLM_GROQ_API_KEY` | **hardcoded default in config.py** ⚠️ | Groq fallback chain key (shared across the 3 slots). |
| `LLM_FALLBACK_{1,2,3}_MODEL` | qwen3-32b / qwen3.6-27b / gpt-oss-20b | Groq fallback models. |
| `LLM_MAX_RETRIES_PER_MODEL` / `LLM_RETRY_BASE_DELAY_SECONDS` | 2 / 1.0 | Retry budget + backoff base. |
| `LLM_REQUEST_TIMEOUT_SECONDS` | 60 | Per-request timeout. |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | 0.7 / 2048 | Generation defaults (overridable per call). |
| `LLM_MONGO_TRACKER_ENABLED` + `LLM_MONGO_URI` | true / **hardcoded URI** ⚠️ | MongoDB request tracking; else JSONL file. |
| `LLM_TRACKING_FILE_PATH` | `data/llm_gateway_tracking.txt` | JSONL fallback tracker. |
| `LLM_CACHE_ENABLED` / `LLM_CACHE_TTL_SECONDS` | false / 120 | Opt-in response cache (real, tested). |
| `LLM_AUTH_ENABLED` + `LLM_JWT_SECRET` | false | JWT gate (off by default). |
| `LLM_RATE_LIMIT_ENABLED`, `LLM_CIRCUIT_BREAKER_ENABLED`, `LLM_COST_TRACKING_ENABLED`, `LLM_HEALTH_CHECK_ENABLED`, `LLM_PROMPT_GUARD_ENABLED` | false | **Inert** — see §6; the features are stubs and not wired into the client. |

**To make it work:** set `LLM_PRIMARY_*` (or rely on the Groq default) → `LLMClient().chat(...)` works. Override the two hardcoded secrets below before production.

---

## 5. Implementation status

### Core (fully functional)

| Feature | Status | Notes |
|---|---|---|
| `LLMClient.chat()` resilient path | ✅ | LangGraph fallback + retry + tracking |
| Fallback chain (primary → Groq ×3) | ✅ | `FallbackChain.from_settings()` |
| Per-model retry + exponential backoff | ✅ | `LLM_MAX_RETRIES_PER_MODEL` |
| Fatal-vs-transient error classification | ✅ | `classify_error()` |
| Connectors: groq / openai / anthropic | ✅ | via LangChain |
| Connector: `http` (OpenAI-compatible) | ✅ | raw httpx |
| Embeddings (`GatewayEmbeddings`: OpenAI + local fallback) | ✅ | `embeddings.py`; RAG routes through it; lazy backend, key via gateway. Tested. |
| Request tracking — JSONL file | ✅ | `RequestTracker` |
| Request tracking — MongoDB | ✅ | `MongoTracker` (Motor, lazy connect) |
| Response cache (opt-in) | ✅ | `cache.py`; **tested** (`tests/test_llm_response_cache.py`). NOTE: `__init__` docstring still calls it a "placeholder" — stale. |
| Async/sync tracker transparency | ✅ | `inspect.iscoroutinefunction` dispatch |

### Advanced / governance (❌ stubs — exist but raise `NotImplementedError`, and are **not wired** into `LLMClient`)

| Feature | File | Status |
|---|---|---|
| Rate limiting (per-caller RPM/TPM) | `rate_limiter.py` | ❌ stub, not wired |
| Circuit breaker (per-model) | `circuit_breaker.py` | ❌ stub, not wired |
| Cost estimator / budget caps | `cost_estimator.py` | ❌ stub, not wired |
| Health monitor (background probes) | `health.py` | ❌ stub, not wired |
| Prompt-injection guard | `prompt_guard.py` | ❌ stub, not wired |
| Streaming (`stream_chat` / SSE) | `streaming.py` | ❌ stub, not wired |

All six are **disabled by default** and the client's hooks for them are commented-out TODOs — so **enabling their config flags does nothing** (inert, like a switch with no wire behind it). The core chat path does not need any of them.

### Auth

| Feature | Status | Notes |
|---|---|---|
| JWT validation (`JWTValidator`, `require_valid_token`) | ⚠️ | Present (`auth.py`), **off by default** (`LLM_AUTH_ENABLED=false`); not exercised on the in-process `LLMClient` path. |

---

## 6. Nice-to-have features (💡)

- **Prompt-injection guard** (highest security value) — wire `prompt_guard` (heuristic + optional LLM-judge) before dispatch.
- **Rate limiter** — per-caller RPM/TPM to protect spend and provider quotas.
- **Circuit breaker** — skip a model that's failing repeatedly instead of retrying it every call.
- **Cost estimator + budget caps** — per-call USD accounting; hard-cap per caller.
- **Streaming** — `stream_chat()` async generator + SSE adapter for token-by-token UI.
- **Health monitor** — background endpoint probes to pre-empt fallbacks.
- **Structured output / JSON mode + tool-calling passthrough** — first-class helpers (agents currently parse JSON out of free text).
- **Cost/latency-aware routing** — pick the endpoint per request, not a fixed chain.

---

## 7. Pending tasks

1. **Rotate hardcoded secrets out of source** — `_DEFAULT_GROQ_API_KEY` and the `mongo_uri` with credentials in [config.py:30,93](config.py#L30). Move to env vars; treat the committed key as compromised and rotate it.
2. **Fix `__init__.py` inconsistencies** — (a) the docstring lists `ResponseCache` and `DatabaseTracker` as "NOT YET IMPLEMENTED" though the cache is real and used; (b) `__all__` exports `"DatabaseTracker"` but only `MongoTracker` is imported, so `from shared.llm_gateway import DatabaseTracker` raises `ImportError`.
3. **Implement the six stubs** only where needed (see §6) — each has a defined interface + config flag already, so wiring is incremental (uncomment the hooks in `client.py`).
4. **Live smoke test** — the tests mock/stub providers; run one real `chat()` against a configured key to prove the end-to-end path.

---

## 8. Suggestions / notes

- **You do not need any of the six stubs for basic operation.** The gateway's core job (resilient chat + fallback + tracking) is complete and used in production paths (negotiation, exception-resolver, RAG generation).
- For **security-sensitive deployments**, prioritise `prompt_guard` (injection defense) and `rate_limiter` (abuse/cost control).
- Keep the **cache TTL short** if you enable it — stale answers are bad in a live negotiation.
- Embeddings are **not** here — see the scope note in §1.

---

## 9. Files

| File | Role |
|---|---|
| `client.py` | `LLMClient`, LangGraph fallback loop, connector dispatch, `LLMResponse` |
| `fallback.py` | `FallbackChain`, `ModelEndpoint`, `classify_error` |
| `config.py` | `LLMGatewaySettings` (env `LLM_`); ⚠️ hardcoded Groq key + Mongo URI |
| `tracker.py` / `db_tracker.py` | JSONL `RequestTracker` / `MongoTracker` |
| `mongo_connection.py` | Shared Motor connection pool |
| `cache.py` | `ResponseCache` (real, tested) |
| `auth.py` | JWT scaffolding (off by default) |
| `rate_limiter.py` · `circuit_breaker.py` · `cost_estimator.py` · `health.py` · `prompt_guard.py` · `streaming.py` | ❌ stubs (`NotImplementedError`) |
| `tests/llm_gateway/test_client.py` · `tests/test_llm_response_cache.py` | Test coverage (core + cache) |
