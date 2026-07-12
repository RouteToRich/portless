"""Logout / token revocation.

Access tokens are stateless JWTs, so "logging out" a specific token means
recording its `jti` (see security.py::create_access_token) in a
short-lived blacklist that every subsequent request is checked against.
The entry's TTL is set to the token's own remaining lifetime, so it expires
from the store at the same moment the token itself would have stopped being
valid anyway — the blacklist never grows unbounded.

Reuses the app-wide TTL cache (shared/cache) rather than a bespoke
collection — logout state is exactly the kind of expiring, non-financial,
best-effort key/value data that store already exists for.
"""
from __future__ import annotations

from datetime import datetime, timezone

from shared.cache import cache_get, cache_set

_NAMESPACE = "auth_revoked_tokens"


async def revoke_token(jti: str, exp: int) -> None:
    """Blacklist a token's `jti` until its natural expiry (`exp`, epoch seconds)."""
    ttl_seconds = int(exp - datetime.now(timezone.utc).timestamp())
    if ttl_seconds <= 0:
        return  # already expired — nothing to blacklist
    await cache_set(_NAMESPACE, jti, True, ttl_seconds)


async def is_token_revoked(jti: str | None) -> bool:
    """True if this token's `jti` was revoked (and hasn't naturally expired)."""
    if not jti:
        return False
    return await cache_get(_NAMESPACE, jti) is not None
