# Identity — Workflow & Implementation Reference

> Module: `services/auth/` (router prefix `/identity`) + `shared/auth/`
> Mounted in the combined monolith only (`app.py`).

---

## 1. Purpose

PortLess authenticates exactly like MakeMeRich: **auth-service**, reached through the
shared **RouteToRich API gateway**, owns sign-up, sign-in, refresh (rotating), sign-out
and password reset. PortLess issues no tokens, holds no signing key and stores no
passwords. It keeps only what auth-service has no notion of:

- **organizations** (multi-tenancy for buyer / supplier users), and
- the **Portless platform-staff** allowlist (`AUTH_PORTLESS_EMAILS`).

| Concern | Owner |
|---|---|
| Register / login / refresh / logout / reset | auth-service, at `<gateway>/auth/*` (the UI calls it there) |
| "Who holds this token?" | auth-service `GET /auth/me`, asked by `shared/auth/identity.py` |
| Organization, staff flag | PortLess `users` / `organizations` collections (`services/auth/`) |
| LLM calls | the same gateway, as PortLess's service account (`shared/llm_gateway/remote.py`) |

---

## 2. How a request is authenticated (`shared/auth/`)

```
browser ──/api/*──▶ gateway /api/portless/* ──▶ PortLess  (Authorization forwarded unchanged)
                                                  │
                AuthMiddleware ─ identity.resolve(headers, bearer)
                                                  │
     AUTH_TRUST_GATEWAY_HEADERS=true and X-User-ID present ─▶ believe X-User-* headers
     otherwise ─▶ GET <AUTH_GATEWAY_URL>/auth/me with the token (cached 60 s by token hash)
                                                  │
                check_account: identity.account_id == PORTLESS_ACCOUNT_ID  (else 403)
                                                  │
                services.auth.service.user_for_identity ─▶ local record (created on first sight)
                                                  │
                request.state.user = AuthUser(user_id, email, name, org_id, is_portless)
```

- **Token check is the default.** It works whether the request came through the gateway
  or straight to the service (local dev, the SSE stream), and a forged `X-User-*` header
  gets nobody anywhere. Revocation (sign-out, refresh rotation) takes effect here within
  `AUTH_INTROSPECTION_CACHE_SECONDS` (60 s).
- **Header trust is opt-in** (`AUTH_TRUST_GATEWAY_HEADERS=true`), MakeMeRich's model: only
  sound while nothing but the gateway can reach the service. A request without the
  headers still falls back to the token check.
- **Account scoping.** A MakeMeRich token is a valid auth-service token too; with
  `PORTLESS_ACCOUNT_ID` set (falls back to `LLM_GATEWAY_ACCOUNT_ID`) it is refused with 403.
  Unset → any application is accepted, a warning is logged once, and the readiness gate
  (`auth_account`) fails in production.
- **Outages are 503, not 401** — an unreachable gateway or unset `AUTH_GATEWAY_URL` (falls
  back to `LLM_GATEWAY_URL`) is reported as such, so the UI doesn't sign the user out.
- **SSE.** `GET /hitl-approval/stream` also accepts `?token=` (EventSource cannot set a
  header) — that path only, checked like any other token.

### Access model (`middleware.py`)

- Portless staff (`is_portless`) reach every endpoint.
- Everyone else reaches only `GET /deals` (their organization's deals — `require_org_scope`
  in the Deals router) and `GET /identity/me`; everything else 403s.
- Public: `/health`, `*/health`, `/docs`, `/redoc`, `/openapi.json`, CORS preflight.

---

## 3. Local records (`service.py`, `repository.py`, `schemas.py`)

`user_for_identity(identity)` returns the `users` record keyed on auth-service's
`user_id` (`USR-<hex>`), creating it on first sight with `email`, `name`, `account_id` and
`org_id = "portless"` for allowlisted staff, `None` otherwise.

- **No adoption by email.** auth-service sign-up does not prove ownership of an address,
  so matching an existing record by email would hand its organization (and its deals) to
  whoever registered that address first. A new identity always gets its own record.
- **Legacy records** — created before sign-in moved to auth-service — carry a
  `password_hash`; `UserPublic.legacy` is true for them and the Organizations admin shows
  a "Legacy" badge. Nobody can sign in as one. The person signs up at auth-service; an admin
  assigns the new record the organization the legacy one shows.
- The unique `email_unique` index is dropped at startup (legacy and new record share an
  email); `user_id` stays unique.
- The organization is read from the record on **every request**, so an assignment takes
  effect immediately — no re-login.

### Portless staff (`organization.py`)

`is_portless_user(email)` — the email is on `AUTH_PORTLESS_EMAILS`. No domain matching.
Register the staff addresses in the PortLess app account yourself, before anyone else can
(auth-service keeps `(email, account_id)` unique).

### Organizations

Created by staff (`POST /identity/organizations`) or automatically when a deal is signed
(`find_or_create_organization_for_deal`, called from `dealmaker.accept_pi`, which also
records the counterparty's e-mail in `known_emails` so the admin UI can suggest the right
organization for a later sign-up).

---

## 4. API endpoints (`router.py`)

| Method | Path | Who | What |
|---|---|---|---|
| GET | `/identity/health` | public | liveness |
| GET | `/identity/me` | any signed-in user | the caller as PortLess sees them: `user_id, email, name, org_id, is_portless, created_at, legacy` |
| POST | `/identity/organizations` | staff | create an organization |
| GET | `/identity/organizations` | staff | list organizations |
| GET | `/identity/users` | staff | list local user records (incl. legacy) |
| PATCH | `/identity/users/{user_id}/organization` | staff | assign a user to an organization |

There is no `/auth/*` on PortLess.

---

## 5. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_GATEWAY_URL` | `LLM_GATEWAY_URL` | gateway whose `/auth/me` checks tokens |
| `PORTLESS_ACCOUNT_ID` | `LLM_GATEWAY_ACCOUNT_ID` | PortLess's app account; other accounts → 403 |
| `AUTH_TRUST_GATEWAY_HEADERS` | `false` | believe `X-User-*` headers (gateway-only network required) |
| `AUTH_INTROSPECTION_CACHE_SECONDS` | `60` | token-check cache; also the revocation delay |
| `AUTH_PORTLESS_EMAILS` | — | staff allowlist |
| `FRONTEND_URL` | — | deployed console origin, allowed by CORS for the direct SSE stream |
| `AUTH_MONGO_URI` / `AUTH_MONGO_DB_NAME` | `MONGO_URI` / `MONGO_DB_NAME` | local records |

Readiness checks (`shared/readiness.py`): `auth_gateway` (fail when unset), `auth_account`
(warn in dev, fail in production), `auth_header_trust` (warn when header trust is on).

---

## 6. Registering PortLess in account management

1. In the account-management console, create the **PortLess** application account and copy
   its id (UUID). Set its `app_url` to the console's origin (password-reset links).
2. Backend env: `PORTLESS_ACCOUNT_ID=<id>` (and `LLM_GATEWAY_ACCOUNT_ID=<id>`).
3. UI env (Vercel): `VITE_PORTLESS_ACCOUNT_ID=<id>`, then redeploy.
4. Register the LLM service account in that account (`POST <gateway>/auth/register` with
   `account_id`) and set `LLM_GATEWAY_EMAIL` / `LLM_GATEWAY_PASSWORD`.
5. Register the staff addresses from `AUTH_PORTLESS_EMAILS` in that account.
6. In the API gateway's routes, map `/api/portless` → the PortLess backend URL.

---

## 7. Files

`services/auth/`: `config.py` (gateway URL, account id, header trust, cache, allowlist) ·
`service.py` (`user_for_identity`, organization admin) · `repository.py` (users /
organizations, in-memory + Mongo) · `schemas.py` · `organization.py` · `router.py`.
`shared/auth/`: `identity.py` (token check, header trust, account check) ·
`dependencies.py` (`AuthUser`, `get_current_user`, `require_org_scope`, `require_portless`) ·
`middleware.py` (`AuthMiddleware`, access model).
Tests: `tests/test_auth_middleware.py` (fake gateway), `tests/test_readiness.py`.
