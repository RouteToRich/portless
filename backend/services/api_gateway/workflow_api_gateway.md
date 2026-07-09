# API Gateway — Workflow & Implementation Reference

> **Module:** `services/api_gateway/` · **Port:** 8000 · **Prefix:** `/` (root)
> **Role:** The **edge / facade** — a thin HTTP entry point that fans out to the
> downstream services via their typed clients, applies CORS, and provides a single
> webhook ingress.
> **Status:** ✅ Full · **Persistence:** none (stateless facade)

---

## 1. Purpose

- Present one external surface for the UI / external callers and proxy to the
  right internal service.
- Centralise cross-cutting edge concerns: CORS, a downstream health roll-up, and a
  single signed-webhook ingress (`/webhooks/{provider}`).

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| UI / external clients / providers | All external HTTP traffic + provider webhooks |

| Invokes (downstream — via `clients.py`) | Client |
|---|---|
| Deals | `DealsClient` |
| Trade-Min | `TradeMinClient` |
| Match Engine | `MatchEngineClient` |
| Sourcing | `SourcingClient` |
| Negotiation Agent | `NegotiationAgentClient` |
| DealMaker | `DealMakerClient` |
| Shield-Link | `ShieldLinkClient` |
| DocuGen | `DocuGenClient` |
| Escrow-Guard | `EscrowGuardClient` |
| HITL Approval | `HitlApprovalClient` |
| Exception Resolver | `ExceptionResolverClient` |

*(All base URLs come from `shared/config.py`; each client extends `BaseServiceClient`.)*

## 3. Workflow

```
Request → CORS → route → downstream client.<method>() → response passthrough

GET  /health              → gateway liveness
GET  /health/downstream   → roll-up of every service's /health
POST /webhooks/{provider} → idempotency + signature verification → forward to the
                            owning service (e.g. stripe/sgs/cogoport/…)
```

## 4. API endpoints

| Method | Path | Proxies to |
|---|---|---|
| `GET` | `/health` · `/health/downstream` | self · all services |
| `POST` `GET` | `/deals` · `/deals/{id}` | Deals |
| `PATCH` | `/deals/{id}/status` | Deals |
| `GET` `POST` | `/opportunities` · `/opportunities/scan` | Trade-Min |
| `GET` `POST` | `/approvals` · `/approvals/{id}/resolve` | HITL Approval |
| `GET` | `/exceptions/{id}` | Exception Resolver |
| `POST` | `/webhooks/{provider}` | provider's owning service |

## 5. Completion status

| Feature | Status |
|---|---|
| Client fan-out to all 11 downstream services | ✅ |
| CORS middleware | ✅ |
| Downstream health roll-up | ✅ |
| Single webhook ingress (idempotency + signature) | ✅ |

## 6. Waiting for configuration

| Var | Effect |
|---|---|
| `*_URL` (in `shared/config.py`) | Downstream base URLs (default localhost:8001–8013) |
| `CORS_ALLOW_ORIGINS` | Allowed browser origins (lock down in production) |
| `HTTP_TIMEOUT_SECONDS` | Client timeout |

## 7. Nice-to-have / pending

- AuthN/AuthZ enforcement at the edge (verify the `auth` JWT before proxying).
- Rate limiting + request/response logging + tracing (correlation IDs).
- Retry/circuit-breaker around downstream clients.
- Proxy routes for `email-gateway` / `email-processor` if the UI needs them.

## 8. Files

`clients.py` (downstream client singletons) · `client.py` (`ApiGatewayClient`) ·
`service.py` (proxy + webhook ingress) · `router.py` · `schemas.py` ·
`main.py` (CORS + router mount).
