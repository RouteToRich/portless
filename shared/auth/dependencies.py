"""FastAPI auth dependencies + the request-scoped current user.

Any service can scope a route to the authenticated caller::

    from shared.auth.dependencies import get_current_user, AuthUser

    @router.get("/things")
    async def list_things(user: AuthUser = Depends(get_current_user)):
        return await service.list_for_owner(user.user_id)

The AuthMiddleware (shared.auth.middleware) resolves the caller once — the
auth-service identity (shared/auth/identity.py) joined to PortLess's local
record for it (organization, staff flag) — and stashes it on
``request.state.user``; this dependency reads that, and resolves it itself when
the middleware isn't active.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel

from .identity import IdentityError, resolve


class AuthUser(BaseModel):
    user_id: str
    email: str | None = None
    name: str | None = None
    org_id: str | None = None
    is_portless: bool = False
    """True for Portless platform staff (see services/auth/organization.py) —
    such callers bypass per-organization data filtering everywhere."""


def bearer_token(request: Request, *, allow_query: bool = False) -> str:
    """The request's bearer token ("" when none). ``allow_query`` also reads
    ``?token=`` — only for the SSE stream, whose browser client (EventSource)
    cannot set an Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    if allow_query:
        return request.query_params.get("token", "")
    return ""


async def resolve_user(request: Request, *, allow_query_token: bool = False) -> AuthUser:
    """Identity (auth-service) + local record (PortLess) → AuthUser. Raises IdentityError."""
    from services.auth.organization import is_portless_user
    from services.auth.service import user_for_identity

    identity = await resolve(request.headers, bearer_token(request, allow_query=allow_query_token))
    record = await user_for_identity(identity)
    return AuthUser(
        user_id=record.user_id,
        email=record.email,
        name=record.name,
        org_id=record.org_id,
        is_portless=is_portless_user(record.email),
    )


async def get_current_user(request: Request) -> AuthUser:
    """Return the authenticated user or raise 401 / 403."""
    cached = getattr(request.state, "user", None)
    if isinstance(cached, AuthUser):
        return cached
    try:
        user = await resolve_user(request)
    except IdentityError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail=exc.detail,
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
        ) from exc
    request.state.user = user
    return user


def require_org_scope(user: AuthUser) -> str | None:
    """Resolve the org filter a data-owning endpoint should apply.

    Returns ``None`` for Portless platform staff (unrestricted — see every
    organization's data) or the caller's ``org_id`` otherwise. Raises 403 for
    a non-Portless caller with no ``org_id`` — a real, expected state for a
    newly signed-up user who hasn't been assigned to an organization yet by a
    Portless admin (see services/auth/organization.py) — instead of silently
    treating a missing filter as "show everything," which would leak every
    organization's data.

    Any org-scoped service should call this once per request rather than
    reading ``user.org_id`` / ``user.is_portless`` directly.
    """
    if user.is_portless:
        return None
    if not user.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is not yet assigned to an organization. "
                   "Contact a Portless administrator.",
        )
    return user.org_id


def require_portless(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """FastAPI dependency: 403s unless the caller is Portless platform staff.

    Use to gate features reserved for platform staff (organization admin) —
    see services/auth/organization.py::is_portless_user.
    """
    if not user.is_portless:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This feature is restricted to Portless platform staff.",
        )
    return user
