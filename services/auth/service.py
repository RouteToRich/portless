"""Identity business logic — local user records for auth-service identities,
and organization admin.

Sign-up, sign-in, refresh, logout and password reset are auth-service's,
reached through the shared API gateway; nothing here checks a password or
issues a token. What stays local is what auth-service has no notion of: which
organization a user belongs to, and whether they are Portless staff.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from shared.auth.identity import Identity

from .organization import PORTLESS_ORG_ID, is_portless_user
from .repository import get_org_repository, get_repository
from .schemas import OrganizationRecord, UserPublic, UserRecord

logger = logging.getLogger(__name__)


class OrganizationNotFoundError(Exception):
    """Raised when assigning a user to an org_id that doesn't exist (404)."""


class UserNotFoundError(Exception):
    """Raised when assigning an org to a user_id that doesn't exist (404)."""


def _new_org_id() -> str:
    return f"ORG-{uuid4().hex[:12].upper()}"


async def user_for_identity(identity: Identity) -> UserRecord:
    """The local record for an auth-service identity, created on first sight.

    A first-seen identity on the AUTH_PORTLESS_EMAILS allowlist gets the
    reserved Portless org; everyone else starts unassigned until a Portless
    admin assigns them (assign_user_organization).

    Deliberately NOT done: adopting an existing record by email. auth-service
    sign-up does not prove ownership of the address, so matching on email
    would hand a legacy record's organization — and its deals — to whoever
    registered that address first. The new identity gets its own record; an
    admin assigns it, and the legacy record (``legacy: true`` in the
    Organizations admin) shows where the person used to belong.
    """
    repo = get_repository()
    user = await repo.get_by_id(identity.user_id)
    if user is not None:
        if user.org_id is None and is_portless_user(user.email):
            # First seen before the address was put on AUTH_PORTLESS_EMAILS: staff
            # belong to the Portless org (their deals need an organization).
            updated = await repo.update_org(user.user_id, PORTLESS_ORG_ID)
            logger.info("Auth: %s (%s) is Portless staff — moved to org %s",
                        user.user_id, user.email, PORTLESS_ORG_ID)
            return updated or user.model_copy(update={"org_id": PORTLESS_ORG_ID})
        return user

    org_id = PORTLESS_ORG_ID if is_portless_user(identity.email) else None
    user = UserRecord(
        user_id=identity.user_id,
        email=identity.email,
        name=identity.name or identity.email,
        account_id=identity.account_id,
        org_id=org_id,
        created_at=datetime.now(timezone.utc),
    )
    try:
        await repo.create(user)
    except Exception:  # noqa: BLE001 — a concurrent first request won the race on user_id
        winner = await repo.get_by_id(identity.user_id)
        if winner is not None:
            return winner
        raise
    logger.info(
        "Auth: first sight of %s (%s) — local record created, org=%s",
        user.user_id, user.email, org_id or "unassigned",
    )
    return user


async def get_user(user_id: str) -> UserPublic | None:
    user = await get_repository().get_by_id(user_id)
    return UserPublic.from_record(user) if user else None


# ---------------------------------------------------------------------------
# Organization admin — callers are gated to Portless staff at the router
# (shared.auth.dependencies.require_portless), not re-checked here.
# ---------------------------------------------------------------------------

async def create_organization(name: str, created_by: str) -> OrganizationRecord:
    org = OrganizationRecord(
        org_id=_new_org_id(),
        name=name.strip(),
        created_by=created_by,
        created_at=datetime.now(timezone.utc),
    )
    await get_org_repository().create(org)
    logger.info("Auth: organization %s (%r) created by %s", org.org_id, org.name, created_by)
    return org


async def list_organizations() -> list[OrganizationRecord]:
    return await get_org_repository().list_all()


async def list_users() -> list[UserPublic]:
    users = await get_repository().list_all()
    return [UserPublic.from_record(u) for u in users]


async def assign_user_organization(user_id: str, org_id: str) -> UserPublic:
    """Assign an existing user to an existing organization."""
    if await get_org_repository().get(org_id) is None:
        raise OrganizationNotFoundError(f"Organization {org_id!r} not found")

    updated = await get_repository().update_org(user_id, org_id)
    if updated is None:
        raise UserNotFoundError(f"User {user_id!r} not found")

    logger.info("Auth: user %s assigned to organization %s", user_id, org_id)
    return UserPublic.from_record(updated)


async def find_or_create_organization_for_deal(name: str, email: str | None = None) -> OrganizationRecord:
    """Best-effort auto-provisioning: find an org by name (case-insensitive)
    or create one, then register ``email`` as a known communication email.

    Called when a deal is signed (see services/dealmaker/service.py::
    accept_pi) so a Portless admin can match a later signup from that email
    to the right organization in the Organizations admin UI's assign
    dropdown, instead of guessing. ``created_by`` is a "system:*" marker
    rather than a real user_id since this isn't a Portless-initiated action.
    """
    org_repo = get_org_repository()
    org = await org_repo.get_by_name(name)
    if org is None:
        org = OrganizationRecord(
            org_id=_new_org_id(),
            name=name.strip(),
            created_by="system:deal-signed",
            created_at=datetime.now(timezone.utc),
        )
        await org_repo.create(org)
        logger.info("Auth: organization %s (%r) auto-created from a signed deal", org.org_id, org.name)

    if email:
        updated = await org_repo.add_known_email(org.org_id, email)
        if updated is not None:
            org = updated

    return org
