# LLM Gateway client — Workflow & Implementation Reference

> **Module:** `shared/llm_gateway/`
> **Purpose:** the one entry point for every **chat** call in PortLess. Each call goes to the shared RouteToRich **LLM Gateway** over HTTP, the same way makemerich-backend calls it.
> **Last reviewed:** 2026-10-24.
> Legend: ✅ implemented · ⚠️ partial / present-but-off · ❌ stub (raises `NotImplementedError`) · 💡 nice-to-have

---

## 1. Purpose

PortLess holds **no model provider key** and imports **no provider SDK**: no Groq, OpenAI, Anthropic or Gemini. The shared LLM Gateway owns model choice, the fallback chain, retries, provider keys and cost metering. This module decides only:

- **where the gateway is.** `LLM_GATEWAY_URL` + `/api/llm`, which is the API gateway's route to the LLM Gateway.
- **whose call it is.** PortLess's auth-service service account. See §3.
- **what is recorded locally.** One tracker record per call (caller, the model the gateway served, tokens, latency), plus the opt-in response cache.

---

## 2. Where it sits

```
Callers                                   PortLess                         Shared services
───────                                   ────────                         ───────────────
negotiation_agent (orchestrator +         LLMClient.chat()  ─┐
  buyer + supplier nodes)            ──▶                     ├─▶ remote.py ──▶ API gateway /api/llm ──▶ LLM Gateway
exception_resolver agent            ──▶                     │   (vendored        POST /v1/llm            (models, fallback,
                                                       │    remote_sdk.py)  POST /v1/llm             provider keys)
                                                               auth: POST /auth/login (service account) → bearer
```

Trackers write to **MongoDB** (`MongoTracker`, `llm_requests`), or to a **JSONL file** (`RequestTracker`) as the fallback.

---

## 3. Credential

makemerich forwards the requesting user's auth-service token. PortLess can't do that, for two reasons:

- Its LLM calls run inside event-bus continuations, the deal watcher and the exception resolver, so there's no request to take a token from.
- PortLess users hold PortLess's own JWT, and the shared gateway doesn't recognise it.

Instead PortLess signs in as its own service account. `_ServiceAccountAuth` in `remote.py` handles it:

1. Calls `POST <LLM_GATEWAY_URL>/auth/login` with `{email, password, account_id}`.
2. Caches the bearer token for subsequent chat calls.
3. Signs in again before the 1-hour expiry (`LLM_GATEWAY_TOKEN_TTL_SECONDS`), and once more if a call gets a 401.

A pre-minted `LLM_GATEWAY_TOKEN` is sent as-is instead, with no sign-in. The credential is never logged.

---

## 4. Workflow

`LLMClient.chat(messages, caller, temperature?, max_tokens?, category?, **extra)`:

```
1. (opt) Response-cache lookup — return a cached identical request
2. tracker.start_request(caller, model="llm-gateway")
3. POST /v1/llm  {messages, caller: "portless.<caller>", temperature, max_tokens, category, extra}
4. success → tracker records the served model + tokens, (opt) cache, return LLMResponse
   GatewayError / network error → tracker records the error, raise LLMExhaustedException
   no URL / credential → raise LLMConfigurationError
```

There is **no local fallback**: if the gateway fails, the call fails. That way all LLM spend stays inside the gateway's metering. Callers already degrade on failure: negotiation drafts fall back to templates (always L0), and the exception resolver escalates.

`category` pins the gateway's routing category. Leave it unset and the gateway classifies the prompt itself.

---

## 5. Configuration reference

| Var | Default | Effect |
|---|---|---|
| `LLM_GATEWAY_URL` | empty | API gateway base, e.g. `https://api-gateway-two-nu.vercel.app`. Empty → `chat()` raises `LLMConfigurationError`. |
| `LLM_GATEWAY_EMAIL` / `_PASSWORD` / `_ACCOUNT_ID` | empty | PortLess's auth-service service account. |
| `LLM_GATEWAY_TOKEN` | empty | Pre-minted bearer, used instead of email/password. |
| `LLM_GATEWAY_LLM_PATH` / `_AUTH_PATH` | `/api/llm` / `/auth/login` | Routes on the API gateway. |
| `LLM_GATEWAY_TOKEN_TTL_SECONDS` | 3000 | Sign in again after this long (auth-service tokens last 1 h). |
| `LLM_GATEWAY_TIMEOUT_SECONDS` | 120 | Per-call timeout. |
| `LLM_GATEWAY_CALLER_PREFIX` | `portless` | Caller tag `portless.<caller>` in gateway stats. |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | 0.7 / 2048 | Generation defaults (overridable per call). |
| `LLM_MONGO_TRACKER_ENABLED` + `MONGO_URI` | true | MongoDB request tracking; otherwise the JSONL file. |
| `LLM_CACHE_ENABLED` / `LLM_CACHE_TTL_SECONDS` | false / 120 | Opt-in response cache. |
| `LLM_AUTH_ENABLED` + `LLM_JWT_SECRET` | false | Inbound JWT gate (off by default). |

Put the URL and credential in `.env` (gitignored) next to `MONGO_URI`.

---

## 6. Implementation status

| Feature | Status | Notes |
|---|---|---|
| `LLMClient.chat()` → shared gateway | ✅ | `client.py` + `remote.py`; tested with a mocked transport (`tests/llm_gateway/test_remote.py`) |
| Service-account sign-in, token cache, refresh, 401 retry | ✅ | async + sync flows |
| Request tracking — JSONL / MongoDB | ✅ | `RequestTracker` / `MongoTracker` |
| Response cache (opt-in) | ✅ | `cache.py`; `tests/test_llm_response_cache.py` |
| Rate limiter, circuit breaker, cost estimator, health monitor, prompt guard, streaming | ❌ stubs, not wired | The gateway already rate-limits, meters and runs model fallback; these are only worth wiring for PortLess-specific needs (e.g. a prompt guard). |

---

## 7. Files

| File | Role |
|---|---|
| `client.py` | `LLMClient` (tracking, cache, error contract), `LLMResponse` |
| `remote.py` | Gateway client singleton, service-account auth |
| `remote_sdk.py` | Vendored LLM Gateway SDK (`RemoteLLMClient`), same file as makemerich |
| `config.py` | `LLMGatewaySettings` (env `LLM_`) |
| `tracker.py` / `db_tracker.py` / `mongo_connection.py` | Request tracking |
| `cache.py` | `ResponseCache` |
| `auth.py` | Inbound JWT scaffolding (off by default) |
| `rate_limiter.py` · `circuit_breaker.py` · `cost_estimator.py` · `health.py` · `prompt_guard.py` · `streaming.py` | ❌ stubs |
