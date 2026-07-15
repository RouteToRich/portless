# Auth — Workflow & Implementation Reference

> **Module:** `services/auth/` · **Prefix:** `/auth` (mounted in `app.py`)
> **Role:** Operator **authentication** — register, login, and identity (`/me`)
> using JWT access tokens and hashed passwords. Also owns **organizations**
> and the admin actions that assign users to them.
> **Status:** ✅ Full · **Persistence:** MongoDB (shared `MONGO_URI` / `portless` db, `users` + `organizations` collections) + in-memory fallback

---

## 1. Purpose

- Register users, verify credentials, and issue JWT access tokens.
- Resolve the current user from a bearer token for protected routes.
- Revoke a token on logout (see §1c) — every logged-in user, Portless or not.
- Let Portless platform staff create **organizations** and assign users to
  them, so downstream services can scope data to "everyone at this
  organization" rather than one individual account.

## 1a. Portless platform staff (`organization.py`)

Whether a user is Portless platform staff is decided by a single explicit
allowlist — `AUTH_PORTLESS_EMAILS` (comma-separated emails) — checked by
`organization.py::is_portless_user()`. There is **no domain matching or any
other derivation**; an email either is or isn't on the list.

Portless staff:
- get `is_portless=true` in their JWT and `UserPublic`, and see data across
  **every** organization rather than being confined to one — see
  `shared/auth/dependencies.py::require_org_scope()`, which every org-scoped
  service endpoint calls to resolve "filter by my org" vs. "no filter."
- are the only callers who can create organizations, list every user, and
  assign a user to an organization (`require_portless` dependency).
- are the only callers allowed to use the Knowledge Base (`/rag/*` — see
  `shared/rag/src/router.py`). This is also the only user-facing surface
  backed by the LLM Gateway (`shared/llm_gateway/` — a Python library with
  no HTTP endpoints of its own); Portless staff reach it through
  `POST /rag/query`, which calls `LLMClient` in-process.
- reach **every other endpoint** in the app (Approvals, Negotiations, Money,
  Exceptions, Autonomy, System Health, Deals CRUD, ...) — this is the
  Ops Console's full surface.

**Non-Portless (regular organization) users are restricted to almost
nothing** — see `shared/auth/middleware.py::_NON_PORTLESS_ALLOWED`: exactly
`GET /deals` (their own org's deals, filtered by `require_org_scope`) and
`GET /auth/me`. Every other endpoint in the app 403s for them at the
middleware layer, before it even reaches the target router — this mirrors
the UI, where a non-Portless login only ever renders a read-only deals list
with status (`ui/src/pages/OrgDeals.tsx`), never the Deal Board kanban, deal
detail page, or any of the other Ops Console pages (`ui/src/App.tsx`'s
`RequirePortless` wrapper). Extending the allowlist is the single place to
grant a non-Portless user access to a new endpoint; there is intentionally
no per-endpoint opt-out.

**SSE query-token exception (`_QUERY_TOKEN_PATH_SUFFIXES`).** The browser's
native `EventSource` API — the only client for the HITL SSE stream — cannot
set an `Authorization` header. `AuthMiddleware` therefore accepts a `?token=`
query param as the bearer token for any request path suffix-matched against
`_QUERY_TOKEN_PATH_SUFFIXES` (today: just `/hitl-approval/stream`), instead
of weakening the header check anywhere else. Suffix matching (not exact
path) so this still works whether the app is mounted at `/hitl-approval/stream`
(monolith) or behind `/api/hitl-approval/stream` (gateway-proxied). The token
still goes through the normal `decode_token()` / revocation-blacklist check —
only *where the token travels* differs for this one path.

At signup, a Portless-allowlisted email is auto-assigned the reserved
`PORTLESS_ORG_ID` ("portless") constant — a fixed label, not derived from
their email — purely so any record they personally create has a valid
`org_id`. It plays no role in the allowlist check itself.

## 1b. Organizations (multi-tenancy, admin-provisioned)

Everyone **not** on the Portless allowlist starts with **no organization**
(`UserRecord.org_id = None`) at signup — self-service registration no longer
infers an org from the email domain. A Portless admin must:

1. `POST /auth/organizations {name}` — create the organization (once).
2. `PATCH /auth/users/{user_id}/organization {org_id}` — assign the new user
   to it.

Until assigned, org-scoped endpoints (e.g. `GET /deals`) reject the user with
403 "not yet assigned to an organization" (`require_org_scope`) — this is an
expected, common state for a brand-new signup, not an error condition.

**Auto-provisioned orgs.** An org doesn't have to start with an explicit
`POST /auth/organizations` call — one is also created automatically when a
deal is signed: `services/dealmaker/service.py::accept_pi()` accepts an
optional `buyer_email` and, if supplied, calls
`service.py::find_or_create_organization_for_deal(buyer_name, buyer_email)`,
which finds-or-creates an org by name (case-insensitive) and records the
email on `OrganizationRecord.known_emails`. The Organizations admin UI shows
each org's known emails and pre-selects/suggests a matching org in the
assign-user dropdown when a new signup's email matches one — Portless staff
still make the final call, this only removes the guesswork. This path is
best-effort and never blocks PI acceptance on failure.

`org_id` and `is_portless` are embedded directly in the JWT at issuance
(`_token_for()`), so downstream services never need to look up the user
themselves — they just trust the signed claims. The Deals service
(`services/deals/`) is the reference implementation of org-scoped filtering;
see its `workflow_deals.md`.

## 1c. Logout / token revocation (`shared/auth/blacklist.py`)

Access tokens are stateless JWTs, so "logout" has nothing server-side to act
on unless the token itself is explicitly revoked. Every token now carries a
unique `jti` (`security.py::create_access_token`). `POST /auth/logout`
decodes the caller's own bearer token, extracts `jti` + `exp`, and stores
`{jti: true}` in the app-wide TTL cache (`shared/cache`) with a TTL equal to
the token's own remaining lifetime — the entry expires from the cache at
exactly the moment the token would have stopped being valid anyway, so the
blacklist never grows unbounded. `shared/auth/middleware.py` and
`shared/auth/dependencies.py::get_current_user()` both check
`is_token_revoked(jti)` on every request and 401 if it's blacklisted.

Logging out only revokes the *specific token used to call `/auth/logout`* —
a second token from a separate login (another device/tab) keeps working.
This is deliberately in the non-Portless allowlist (`POST /auth/logout`) —
logging out isn't a Portless-only feature.

The UI (`ui/src/lib/auth.tsx::logout()`) calls this best-effort before
clearing its local token/user state — if the API call fails (offline, token
already expired), local cleanup still proceeds so the user is never stuck
unable to log out client-side.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| UI / login screen | `POST /auth/register`, `/auth/login` |
| UI / Organizations admin page (Portless only) | Create orgs, list users, assign org |
| Protected endpoints / gateway | `GET /auth/me` (token → user); intended edge auth check |

| Invokes (downstream) | Why |
|---|---|
| `shared/auth/security` | `hash_password`, `verify_password`, `create_access_token` (PyJWT) |
| `MongoUserRepository` / `InMemoryUserRepository` | User persistence |
| `MongoOrganizationRepository` / `InMemoryOrganizationRepository` | Organization persistence |

## 3. Workflow

```
POST /auth/register {email, name, password}
  → org_id = PORTLESS_ORG_ID if is_portless_user(email) else None
  → hash_password() → save user → create_access_token() → TokenResponse

POST /auth/login {email, password}
  → load user → verify_password() → create_access_token() → TokenResponse
  (bad creds → 401)

GET /auth/me   (Authorization: Bearer <jwt>)
  → decode/verify token → UserPublic

--- Portless admin only (require_portless) ---

POST /auth/organizations {name}
  → create_organization() → OrganizationRecord

PATCH /auth/users/{user_id}/organization {org_id}
  → validate org exists → update user.org_id → UserPublic
```

## 4. API endpoints

| Method | Path | Purpose | Access |
|---|---|---|---|
| `GET` | `/auth/health` | Health | Public |
| `POST` | `/auth/register` | Create user → token (201) | Public |
| `POST` | `/auth/login` | Authenticate → token | Public |
| `GET` | `/auth/me` | Current user from bearer token | Authenticated (both) |
| `POST` | `/auth/logout` | Revoke the bearer token used for this call (204) | Authenticated (both) |
| `POST` | `/auth/organizations` | Create an organization (201) | Portless only |
| `GET` | `/auth/organizations` | List every organization | Portless only |
| `GET` | `/auth/users` | List every user + their org | Portless only |
| `PATCH` | `/auth/users/{user_id}/organization` | Assign a user to an org | Portless only |

## 5. Completion status

| Feature | Status |
|---|---|
| Register / login / me | ✅ |
| Password hashing + verification | ✅ |
| JWT access-token issuance (PyJWT) | ✅ |
| Mongo (shared `portless` db, `users` + `organizations` collections) + in-memory store | ✅ |
| Organization admin (create org, list users, assign org) — Portless only | ✅ |
| Auto-provision org + known email from a signed deal (DealMaker `accept_pi`) | ✅ |
| Knowledge Base gated to Portless only | ✅ |
| Non-Portless users restricted app-wide to `GET /deals` + `GET /auth/me` (`shared/auth/middleware.py`) | ✅ |
| Logout / per-token revocation (`shared/auth/blacklist.py`) | ✅ |

## 6. Waiting for configuration

| Var | Effect |
|---|---|
| `AUTH_MONGO_URI` / `AUTH_MONGO_DB_NAME` (both optional — fall back to shared `MONGO_URI` / `MONGO_DB_NAME`) | Durable user/org store in the shared `portless` db; unset entirely → in-memory |
| `AUTH_PORTLESS_EMAILS` (comma-separated) | Who counts as Portless platform staff — empty means nobody does, and the Organizations admin UI + Knowledge Base become unreachable for everyone until set |
| JWT secret / expiry (in `shared/auth/security`) | **Set a strong secret in production** — do not ship a default |

## 7. Nice-to-have / pending

- Refresh tokens (logout/revocation is now implemented — see §1c).
- "Log out of all devices" (revoke every `jti` for a user) — today logout
  only revokes the one token used to call it; there's no per-user index of
  outstanding `jti`s to revoke them all at once.
- Roles/permissions *within* an org (feeds HITL "who may approve" and gateway
  edge auth) — today every member of an org has equal read access to its data.
- Password reset + email verification (via the Email Gateway).
- Rate-limit login attempts; enforce the JWT check across all protected routes.
- `require_org_scope()` (org_id filtering within an allowed endpoint) is only
  exercised by the Deals service today, since it's the only data endpoint a
  non-Portless user can reach at all (`GET /deals`) — if the non-Portless
  allowlist ever grows to cover more read endpoints, each one needs the same
  `org_id` field + `require_org_scope()` treatment Deals already has.
- No self-service "join an existing organization" flow — an org's members are
  entirely Portless-admin-provisioned today. A future invite-link/invite-code
  flow could let an existing org member add teammates without going through
  Portless staff for every addition.
- No way to remove a user from an org or deactivate a user from the admin UI.
- Non-Portless access is all-or-nothing per endpoint (see
  `shared/auth/middleware.py::_NON_PORTLESS_ALLOWED`) — there's no per-org or
  per-role carve-out yet (e.g. letting one org's users approve their own
  deals) short of adding that endpoint to the global allowlist for everyone.

## 8. Files

`schemas.py` (TokenResponse, UserPublic, OrganizationRecord, requests) ·
`service.py` (register / login / logout / me / organization admin /
`find_or_create_organization_for_deal`) ·
`organization.py` (`is_portless_user`, `PORTLESS_ORG_ID`) ·
`repository.py` (`InMemoryUserRepository`, `MongoUserRepository`,
`InMemoryOrganizationRepository`, `MongoOrganizationRepository` — the org
repos add `get_by_name()` for case-insensitive dedup and `add_known_email()`)
· `router.py` · `config.py`. Shared crypto + token issuance (`jti`) in
`shared/auth/security.py`; revocation blacklist in `shared/auth/blacklist.py`;
shared `AuthUser` + `require_org_scope()` + `require_portless()` +
non-Portless allowlist (`_NON_PORTLESS_ALLOWED`) in
`shared/auth/middleware.py` / `shared/auth/dependencies.py`.
