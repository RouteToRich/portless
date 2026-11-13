"""Global authentication + Portless-vs-org-user access middleware.

Every request except a small public allowlist (health, API docs, CORS
preflight) must carry an auth-service identity — a bearer token checked with
the shared API gateway, or the gateway's identity headers when
``AUTH_TRUST_GATEWAY_HEADERS`` is on (see shared/auth/identity.py). There is
no /auth/login or /auth/register here any more: those are auth-service's, at
the gateway's ``/auth/*``. On success the caller (identity + PortLess's local
record: organization, staff flag) is attached to ``request.state.user`` so
downstream dependencies don't resolve it again.

Access model:
  - Portless platform staff (``is_portless``) reach every endpoint — no
    additional restriction here (per-endpoint checks, e.g. ``require_portless``
    on org-admin routes, already narrow those further).
  - Everyone else (regular org users) is restricted to a small allowlist —
    today just ``GET /deals`` (their own organization's deals + status,
    filtered by ``require_org_scope`` inside the Deals router) plus
    ``GET /identity/me`` (needed by the UI shell to read its own session).
    Every other endpoint 403s for them, regardless of org membership — this
    mirrors the UI, where non-Portless users only ever see a read-only deals
    list (see portless-ui/src/pages/OrgDeals.tsx), not the full Ops Console.

Mount once on the app::

    from shared.auth.middleware import AuthMiddleware
    app.add_middleware(AuthMiddleware)
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .dependencies import resolve_user
from .identity import IdentityError

# Paths reachable without a token. Prefixes are matched with startswith.
_PUBLIC_PREFIXES = (
    "/docs",
    "/redoc",
    "/openapi.json",
)

# (method, exact path) pairs reachable by an authenticated but non-Portless
# user. Everything else 403s for them — see module docstring.
_NON_PORTLESS_ALLOWED = frozenset({
    ("GET", "/identity/me"),
    ("GET", "/deals"),
    ("GET", "/trade-min/hs-names"),   # product names for HS codes — nothing deal-specific
})

# Paths where the browser's native EventSource API is the only client (SSE) —
# it cannot set an Authorization header, so these accept a ?token= query
# param as a narrow, path-scoped exception rather than weakening auth
# elsewhere. Suffix-matched since this runs the same behind a gateway prefix
# and directly at /hitl-approval depending on deployment.
_QUERY_TOKEN_PATH_SUFFIXES = ("/hitl-approval/stream",)

# Provider callbacks: the sender has no PortLess identity and proves itself
# with a signature instead (shared/webhooks.py). A call without a valid token
# passes marked ``anonymous_webhook``, and the route refuses it unless it is
# correctly signed — so an unset secret never means "open to anyone".
_PROVIDER_WEBHOOK_PATH_SUFFIXES = (
    "/escrow-guard/webhooks/escrow-funded",
    "/escrow-guard/webhooks/payout",
    "/escrow-guard/webhooks/tazapay",
    "/escrow-guard/webhooks/escrow-com",
    "/shield-link/qc/webhook",
    "/shield-link/freight/webhook",
    "/shield-link/carrier/maersk/webhook",
    "/email-processor/inbound/sendgrid",
    "/email-processor/inbound/mailgun",
)


# Signed read-only links for an outside party with no PortLess sign-in: the
# escrow provider downloads a tranche's proof sheet. The route itself refuses
# any call whose ``exp`` / ``sig`` query parameters do not verify
# (shared/orchestration/escrow_proof.py), signed in or not.
_SIGNED_LINK_PATH_MARKERS = ("/escrow-guard/proof/",)


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
        if request.method == "GET" and any(m in request.url.path for m in _SIGNED_LINK_PATH_MARKERS):
            return await call_next(request)

        allow_query = any(request.url.path.endswith(s) for s in _QUERY_TOKEN_PATH_SUFFIXES)
        try:
            user = await resolve_user(request, allow_query_token=allow_query)
        except IdentityError as exc:
            if request.method == "POST" and request.url.path.endswith(_PROVIDER_WEBHOOK_PATH_SUFFIXES):
                request.state.anonymous_webhook = True
                return await call_next(request)
            return JSONResponse(
                status_code=exc.status,
                content={"detail": exc.detail},
                headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
            )
        request.state.user = user

        if not user.is_portless and (request.method, request.url.path) not in _NON_PORTLESS_ALLOWED:
            return JSONResponse(
                status_code=403,
                content={"detail": "This feature is restricted to Portless platform staff."},
            )

        return await call_next(request)
