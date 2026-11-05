"""HTTP routes for PortLess identity — the caller's own session, and
organization admin.

Sign-up, sign-in, refresh, logout and password reset are NOT here: they are
auth-service's, at the shared API gateway's ``/auth/*`` (the UI calls them
there, like MakeMeRich). The prefix is ``/identity`` so nothing on this
service looks like it owns ``/auth``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from shared.auth.dependencies import AuthUser, get_current_user, require_portless

from . import service
from .schemas import (
    AssignUserOrgRequest,
    CreateOrganizationRequest,
    OrganizationRecord,
    UserPublic,
)
from .service import OrganizationNotFoundError, UserNotFoundError

router = APIRouter(prefix="/identity", tags=["identity"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "identity"}


@router.get("/me", response_model=UserPublic)
async def me(user: AuthUser = Depends(get_current_user)) -> UserPublic:
    """The caller as PortLess sees them: auth-service identity + organization + staff flag.

    The UI reads this right after signing in at auth-service, and on every
    load, so an organization assigned by an admin shows up without signing in
    again.
    """
    found = await service.get_user(user.user_id)
    if found is None:  # pragma: no cover — the middleware just created it
        return UserPublic(
            user_id=user.user_id,
            email=user.email or "",
            name=user.name or "",
            org_id=user.org_id,
            is_portless=user.is_portless,
        )
    return found


# ---------------------------------------------------------------------------
# Organization admin — Portless platform staff only (see require_portless)
# ---------------------------------------------------------------------------

@router.post(
    "/organizations",
    response_model=OrganizationRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    payload: CreateOrganizationRequest,
    user: AuthUser = Depends(require_portless),
) -> OrganizationRecord:
    """Create a new organization."""
    return await service.create_organization(payload.name, created_by=user.user_id)


@router.get("/organizations", response_model=list[OrganizationRecord])
async def list_organizations(_: AuthUser = Depends(require_portless)) -> list[OrganizationRecord]:
    """List every organization."""
    return await service.list_organizations()


@router.get("/users", response_model=list[UserPublic])
async def list_users(_: AuthUser = Depends(require_portless)) -> list[UserPublic]:
    """List every registered user, including their current org assignment."""
    return await service.list_users()


@router.patch("/users/{user_id}/organization", response_model=UserPublic)
async def assign_user_organization(
    user_id: str,
    payload: AssignUserOrgRequest,
    _: AuthUser = Depends(require_portless),
) -> UserPublic:
    """Assign a user to an organization."""
    try:
        return await service.assign_user_organization(user_id, payload.org_id)
    except OrganizationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UserNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
