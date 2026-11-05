"""Models for PortLess's local identity records — users (as seen from
auth-service) and organizations."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .organization import is_portless_user


class UserRecord(BaseModel):
    """PortLess's local record of an auth-service user.

    ``user_id`` is auth-service's own id (``USR-<hex>``). The record is created
    the first time that identity reaches PortLess (see
    service.py::user_for_identity) and holds only what auth-service has no
    notion of — the organization. Passwords live in auth-service.

    ``password_hash`` is set only on LEGACY records, from before sign-in moved
    to auth-service. They are kept so an admin can see who belonged to which
    organization, but no one can sign in as them; the person signs up again
    at auth-service and gets a new record that an admin assigns.

    org_id ties the user to their organization. It starts unset (None) at
    signup — a Portless admin assigns it via the Organizations admin UI
    (see organization.py, service.py::assign_user_organization). Every
    org-scoped data query filters on this value, except for Portless
    platform staff (see is_portless_user), who see data across every
    organization regardless of their own org_id.
    """

    user_id: str
    email: str
    name: str
    password_hash: str | None = None
    account_id: str | None = None
    org_id: str | None = None
    created_at: datetime


class UserPublic(BaseModel):
    """User info safe to return to clients."""

    user_id: str
    email: str
    name: str
    org_id: str | None = None
    is_portless: bool = False
    created_at: datetime | None = None
    legacy: bool = False
    """A pre-auth-service record: nobody can sign in as it any more."""

    @classmethod
    def from_record(cls, r: UserRecord) -> "UserPublic":
        return cls(
            user_id=r.user_id,
            email=r.email,
            name=r.name,
            org_id=r.org_id,
            is_portless=is_portless_user(r.email),
            created_at=r.created_at,
            legacy=bool(r.password_hash),
        )


# ---------------------------------------------------------------------------
# Organizations (Portless platform-staff admin only — see require_portless)
# ---------------------------------------------------------------------------

class OrganizationRecord(BaseModel):
    org_id: str
    name: str
    created_by: str
    """user_id of the Portless staff member who created this organization, or
    a "system:*" marker when created automatically (see service.py::
    find_or_create_organization_for_deal)."""
    known_emails: list[str] = Field(default_factory=list)
    """Communication emails associated with this organization (lowercased,
    deduplicated) — populated as deals are signed with a counterparty
    contact email. Lets a Portless admin match a new signup's email to the
    right organization in the assign-user dropdown."""
    created_at: datetime


class CreateOrganizationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AssignUserOrgRequest(BaseModel):
    org_id: str
