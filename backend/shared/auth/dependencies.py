"""FastAPI auth dependencies + the request-scoped current user.

Any service can scope a route to the authenticated caller::

    from shared.auth.dependencies import get_current_user, AuthUser

    @router.get("/things")
    async def list_things(user: AuthUser = Depends(get_current_user)):
        return await service.list_for_owner(user.user_id)

The AuthMiddleware (shared.auth.middleware) decodes the token once and stashes
the user on ``request.state.user``; this dependency reads that, falling back to
decoding the Authorization header directly when the middleware isn't active.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status
from pydantic import BaseModel

from .security import decode_token


class AuthUser(BaseModel):
    user_id: str
    email: str | None = None
    name: str | None = None


def _user_from_claims(claims: dict) -> AuthUser:
    return AuthUser(
        user_id=str(claims.get("sub")),
        email=claims.get("email"),
        name=claims.get("name"),
    )


def get_current_user(request: Request) -> AuthUser:
    """Return the authenticated user or raise 401."""
    # Fast path: middleware already validated + stashed the user.
    cached = getattr(request.state, "user", None)
    if isinstance(cached, AuthUser):
        return cached

    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth.split(" ", 1)[1].strip()
    try:
        claims = decode_token(token)
    except Exception as exc:  # noqa: BLE001 — any decode/expiry failure → 401
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = _user_from_claims(claims)
    request.state.user = user
    return user
