"""Global JWT validation middleware.

Validates the bearer token on *every* request except a small public allowlist
(auth endpoints, health, API docs, CORS preflight). On success the decoded user
is attached to ``request.state.user`` so downstream dependencies skip re-decoding.

Mount once on the app::

    from shared.auth.middleware import AuthMiddleware
    app.add_middleware(AuthMiddleware)
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .dependencies import _user_from_claims
from .security import decode_token

# Paths reachable without a token. Prefixes are matched with startswith.
_PUBLIC_PREFIXES = (
    "/auth/login",
    "/auth/register",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _is_public(path: str) -> bool:
    # Any "<service>/health" endpoint is public too.
    if path.endswith("/health"):
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # CORS preflight carries no auth header.
        if request.method == "OPTIONS" or _is_public(request.url.path):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth.split(" ", 1)[1].strip()
        try:
            claims = decode_token(token)
        except Exception:  # noqa: BLE001 — any decode/expiry failure → 401
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.user = _user_from_claims(claims)
        return await call_next(request)
