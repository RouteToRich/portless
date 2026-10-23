"""Global JWT validation + Portless-vs-org-user access middleware.

Validates the bearer token on *every* request except a small public allowlist
(auth endpoints, health, API docs, CORS preflight). Also rejects a token whose
``jti`` was revoked via ``POST /auth/logout`` (see blacklist.py) — logout is
otherwise meaningless for a stateless JWT. On success the decoded user is
attached to ``request.state.user`` so downstream dependencies skip re-decoding.

Access model:
  - Portless platform staff (``is_portless``) reach every endpoint — no
    additional restriction here (per-endpoint checks, e.g. ``require_portless``
    on Knowledge Base / org-admin routes, already narrow those further).
  - Everyone else (regular org users) is restricted to a small allowlist —
    today just ``GET /deals`` (their own organization's deals + status,
    filtered by ``require_org_scope`` inside the Deals router) plus
    ``GET /auth/me`` (needed by the UI shell to read its own session).
    Every other endpoint 403s for them, regardless of org membership — this
    mirrors the UI, where non-Portless users only ever see a read-only deals
    list (see ui/src/pages/OrgDeals.tsx), not the full Ops Console.

Mount once on the app::

    from shared.auth.middleware import AuthMiddleware
    app.add_middleware(AuthMiddleware)
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .blacklist import is_token_revoked
from .dependencies import _user_from_claims
from .security import decode_token

# Paths reachable without a token. Prefixes are matched with startswith.
_PUBLIC_PREFIXES = (
    "/auth/login",
    "/auth/register",
    "/docs",
    "/redoc",
    "/openapi.json",
)

# (method, exact path) pairs reachable by an authenticated but non-Portless
# user. Everything else 403s for them — see module docstring.
_NON_PORTLESS_ALLOWED = frozenset({
    ("GET", "/auth/me"),
    ("GET", "/deals"),
    ("POST", "/auth/logout"),
})

# Paths where the browser's native EventSource API is the only client (SSE) —
# it cannot set an Authorization header, so these accept a ?token= query
# param as a narrow, path-scoped exception rather than weakening auth
# elsewhere. Suffix-matched since this runs the same behind /api/hitl-approval
# and directly at /hitl-approval depending on deployment.
_QUERY_TOKEN_PATH_SUFFIXES = ("/hitl-approval/stream",)


def _is_public(path: str) -> bool:
    # "/health" and any "<service>/health" liveness endpoint are public.
    # The deeper reports (/health/downstream, /health/readiness — which names
    # every integration that is or is not configured) need a bearer token.
    if path == "/health" or path.endswith("/health"):
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # CORS preflight carries no auth header.
        if request.method == "OPTIONS" or _is_public(request.url.path):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        elif any(request.url.path.endswith(s) for s in _QUERY_TOKEN_PATH_SUFFIXES):
            token = request.query_params.get("token", "")
        else:
            token = ""

        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            claims = decode_token(token)
        except Exception:  # noqa: BLE001 — any decode/expiry failure → 401
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        if await is_token_revoked(claims.get("jti")):
            return JSONResponse(
                status_code=401,
                content={"detail": "Token has been revoked — please log in again"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        user = _user_from_claims(claims)
        request.state.user = user

        if not user.is_portless and (request.method, request.url.path) not in _NON_PORTLESS_ALLOWED:
            return JSONResponse(
                status_code=403,
                content={"detail": "This feature is restricted to Portless platform staff."},
            )

        return await call_next(request)
