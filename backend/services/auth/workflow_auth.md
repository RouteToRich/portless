# Auth — Workflow & Implementation Reference

> **Module:** `services/auth/` · **Prefix:** `/auth` (mounted in `app.py`)
> **Role:** Operator **authentication** — register, login, and identity (`/me`)
> using JWT access tokens and hashed passwords.
> **Status:** ✅ Full · **Persistence:** MongoDB (`AUTH_MONGO_URI`, db `portless_auth`) + in-memory

---

## 1. Purpose

- Register operators, verify credentials, and issue JWT access tokens.
- Resolve the current user from a bearer token for protected routes.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| UI / login screen | `POST /auth/register`, `/auth/login` |
| Protected endpoints / gateway | `GET /auth/me` (token → user); intended edge auth check |

| Invokes (downstream) | Why |
|---|---|
| `shared/auth/security` | `hash_password`, `verify_password`, `create_access_token` (PyJWT) |
| `MongoUserRepository` / `InMemoryUserRepository` | User persistence |

## 3. Workflow

```
POST /auth/register {email, password}
  → hash_password() → save user → create_access_token() → TokenResponse

POST /auth/login {email, password}
  → load user → verify_password() → create_access_token() → TokenResponse
  (bad creds → 401)

GET /auth/me   (Authorization: Bearer <jwt>)
  → decode/verify token → UserPublic
```

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/auth/health` | Health |
| `POST` | `/auth/register` | Create user → token (201) |
| `POST` | `/auth/login` | Authenticate → token |
| `GET` | `/auth/me` | Current user from bearer token |

## 5. Completion status

| Feature | Status |
|---|---|
| Register / login / me | ✅ |
| Password hashing + verification | ✅ |
| JWT access-token issuance (PyJWT) | ✅ |
| Mongo (`portless_auth`) + in-memory user store | ✅ |

## 6. Waiting for configuration

| Var | Effect |
|---|---|
| `AUTH_MONGO_URI` (+ `AUTH_MONGO_DB`, default `portless_auth`) | Durable user store; unset → in-memory |
| JWT secret / expiry (in `shared/auth/security`) | **Set a strong secret in production** — do not ship a default |

## 7. Nice-to-have / pending

- Refresh tokens + logout/blacklist.
- Roles/permissions (feeds HITL "who may approve" and gateway edge auth).
- Password reset + email verification (via the Email Gateway).
- Rate-limit login attempts; enforce the JWT check across all protected routes.

## 8. Files

`schemas.py` (TokenResponse, UserPublic, requests) · `service.py` (register /
login / me) · `repository.py` (`InMemoryUserRepository`, `MongoUserRepository`) ·
`router.py` · `config.py` · `main.py`. Shared crypto in `shared/auth/security.py`.
