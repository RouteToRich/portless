"""Auth primitives — JWT signing/verification and password hashing.

Dependency-light by design:
  • JWT   — PyJWT (already in requirements.txt), HS256.
  • Hash  — stdlib hashlib.pbkdf2_hmac (no passlib/bcrypt needed).

Config via env vars (with safe dev defaults):
  AUTH_JWT_SECRET           — HMAC signing key (CHANGE in production)
  AUTH_JWT_ALGORITHM        — default HS256
  AUTH_ACCESS_TTL_MINUTES   — token lifetime, default 720 (12h)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt  # PyJWT

JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "portless-dev-secret-change-me")
JWT_ALGORITHM = os.getenv("AUTH_JWT_ALGORITHM", "HS256")
ACCESS_TTL_MINUTES = int(os.getenv("AUTH_ACCESS_TTL_MINUTES", "720"))

_PBKDF2_ROUNDS = 240_000
_PBKDF2_ALGO = "sha256"


# ---------------------------------------------------------------------------
# Password hashing (pbkdf2-sha256, stored as "pbkdf2_sha256$rounds$salt$hash")
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode(), salt.encode(), _PBKDF2_ROUNDS)
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds_s, salt, expected = stored.split("$", 3)
        rounds = int(rounds_s)
        algo = scheme.replace("pbkdf2_", "")
        dk = hashlib.pbkdf2_hmac(algo, password.encode(), salt.encode(), rounds)
        # constant-time compare to avoid timing leaks
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(
    subject: str,
    extra_claims: dict[str, Any] | None = None,
    ttl_minutes: int | None = None,
) -> str:
    """Issue a signed HS256 access token. `subject` becomes the `sub` claim.

    Every token gets a unique `jti` — JWTs are otherwise stateless, so
    logout (see shared/auth/blacklist.py) has nothing else to key a
    revocation entry on.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "jti": secrets.token_hex(16),
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes or ACCESS_TTL_MINUTES),
        **(extra_claims or {}),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Verify signature + expiry. Raises jwt.PyJWTError on any failure."""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
