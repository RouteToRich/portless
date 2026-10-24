"""Portless platform-staff identification — explicit email allowlist.

There is no domain matching or other auto-derived organization logic: which
emails count as "Portless" (platform staff who can create organizations,
assign users to them, and access cross-organization data) is entirely
controlled by the AUTH_PORTLESS_EMAILS config value (see config.py).

Everyone else starts with no organization at signup (UserRecord.org_id is
None) — a Portless admin assigns them one via the Organizations admin UI
(POST /auth/organizations, PATCH /auth/users/{id}/organization). Until
assigned, org-scoped endpoints reject the caller (see
shared.auth.dependencies.require_org_scope).
"""
from __future__ import annotations

from .config import settings

PORTLESS_ORG_ID = "portless"
"""Reserved org_id auto-assigned to Portless staff at signup — a fixed label,
never derived from their email. It exists only so records they personally
create (e.g. a deal) have a valid org_id; it plays no role in the
allowlist check itself."""


def is_portless_user(email: str) -> bool:
    """True when email is on the AUTH_PORTLESS_EMAILS allowlist."""
    return email.strip().lower() in settings.portless_emails
