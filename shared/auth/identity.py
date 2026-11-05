"""Who is calling? — answered by auth-service, through the shared API gateway.

PortLess holds no signing key and issues no tokens. Users sign up and sign in
at auth-service (the UI reaches it at the gateway's ``/auth/*``, exactly like
MakeMeRich), and every request here carries auth-service's bearer token. This
module turns a request into an ``Identity`` in one of two ways:

**Token check (default).** Ask the gateway's ``GET /auth/me`` with the token —
signature, expiry and revocation are all checked by the one service that owns
them. Answers are cached for ``AUTH_INTROSPECTION_CACHE_SECONDS`` (60 s), keyed
by a hash of the token, so a busy page or a reconnecting SSE stream is one
call a minute, not one per request. This works whether the request came
through the gateway (which forwards the ``Authorization`` header unchanged) or
straight to this service (local dev, the SSE stream) — and a forged header
gets nobody anywhere.

**Gateway headers (opt-in, ``AUTH_TRUST_GATEWAY_HEADERS=true``).** MakeMeRich's
model: the gateway authenticates, strips any identity headers the caller sent
and injects verified ``X-User-ID`` / ``X-User-Email`` / ``X-User-Name`` /
``X-Account-ID``; this service believes them. Only sound while nothing but the
gateway can reach this service. A request without them (the SSE stream, which
the browser opens directly) still falls back to the token check.

Either way the identity must belong to PortLess's app account
(``PORTLESS_ACCOUNT_ID``) when that is configured — a MakeMeRich token is a
valid auth-service token too, and is refused here with 403.

The token is never logged.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    """The auth-service user behind a request."""

    user_id: str
    email: str
    name: str
    account_id: str | None


class IdentityError(Exception):
    """The request carries no usable identity. ``status`` is 401 or 403."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# token-hash -> (expires_at_monotonic, identity). Bounded crudely: cleared when
# it grows past a size no honest deployment reaches.
_CACHE: dict[str, tuple[float, Identity]] = {}
_CACHE_MAX_ENTRIES = 2000

_warned_no_account = False


def _settings():
    from services.auth.config import settings

    return settings


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clear_cache() -> None:
    """Forget every checked token (tests)."""
    _CACHE.clear()


async def _ask_gateway(token: str) -> dict | None:
    """auth-service's ``/auth/me`` body for a valid token, else None.

    Raises IdentityError(503) when the gateway cannot be asked at all — that
    is an outage, not a bad token, and the caller should see the difference.
    """
    s = _settings()
    base = s.gateway_url.strip().rstrip("/")
    if not base:
        raise IdentityError(
            503,
            "Sign-in is not configured on this server: set AUTH_GATEWAY_URL (or "
            "LLM_GATEWAY_URL) to the RouteToRich API gateway.",
        )
    try:
        async with httpx.AsyncClient(timeout=s.introspection_timeout_seconds) as client:
            response = await client.get(
                f"{base}/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        logger.warning("Auth: could not reach the gateway to check a token: %s", exc)
        raise IdentityError(503, "The sign-in service is unreachable — try again shortly.") from exc
    if response.status_code in (401, 403):
        return None
    if response.status_code != 200:
        logger.warning("Auth: gateway /auth/me answered %s", response.status_code)
        raise IdentityError(503, "The sign-in service is unavailable — try again shortly.")
    try:
        return response.json()
    except ValueError:
        logger.warning("Auth: gateway /auth/me answered non-JSON")
        raise IdentityError(503, "The sign-in service answered unexpectedly.") from None


# Swappable for tests: async (token) -> /auth/me body | None.
AskGateway = Callable[[str], Awaitable["dict | None"]]
ask_gateway: AskGateway = _ask_gateway


def _identity_from_me(body: dict) -> Identity | None:
    user_id = str(body.get("user_id") or "").strip()
    if not user_id:
        return None
    accounts = body.get("accounts") or []
    selected = next((a for a in accounts if isinstance(a, dict) and a.get("selected")), None)
    account_id = (selected or {}).get("account_id") or body.get("account_id") or None
    return Identity(
        user_id=user_id,
        email=str(body.get("email") or "").strip().lower(),
        name=str(body.get("name") or "").strip(),
        account_id=account_id,
    )


async def identity_for_token(token: str) -> Identity:
    """Check ``token`` with auth-service (cached). Raises IdentityError(401) if invalid."""
    if not token:
        raise IdentityError(401, "Authentication required")
    s = _settings()
    key = _cache_key(token)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]

    body = await ask_gateway(token)
    identity = _identity_from_me(body) if body else None
    if identity is None:
        raise IdentityError(401, "Invalid or expired token — please sign in again")

    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        _CACHE.clear()
    _CACHE[key] = (now + s.introspection_cache_seconds, identity)
    return identity


def identity_from_headers(headers) -> Identity | None:
    """The identity the gateway asserted, when header trust is on and it sent one."""
    if not _settings().trust_gateway_headers:
        return None
    user_id = (headers.get("x-user-id") or "").strip()
    if not user_id:
        return None
    return Identity(
        user_id=user_id,
        email=(headers.get("x-user-email") or "").strip().lower(),
        name=(headers.get("x-user-name") or "").strip(),
        account_id=(headers.get("x-account-id") or "").strip() or None,
    )


def check_account(identity: Identity) -> None:
    """Refuse an identity that belongs to another application (403)."""
    global _warned_no_account
    expected = _settings().account_id.strip()
    if not expected:
        if not _warned_no_account:
            _warned_no_account = True
            logger.warning(
                "Auth: PORTLESS_ACCOUNT_ID is not set — tokens from ANY RouteToRich "
                "application are accepted. Set it to PortLess's app account id."
            )
        return
    if identity.account_id != expected:
        raise IdentityError(
            403, "This sign-in belongs to another application — sign in to PortLess."
        )


async def resolve(headers, token: str) -> Identity:
    """The caller's identity: trusted gateway headers, else the checked token."""
    identity = identity_from_headers(headers)
    if identity is None:
        identity = await identity_for_token(token)
    if not identity.email:
        # auth-service always returns the email; the gateway always forwards it.
        raise IdentityError(401, "Incomplete identity — please sign in again")
    check_account(identity)
    return identity
