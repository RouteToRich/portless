"""JWT authentication and validation for LLM Gateway requests."""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

import jwt

from .config import llm_gateway_settings

logger = logging.getLogger(__name__)


@dataclass
class TokenClaims:
    """Decoded and validated JWT claims."""

    sub: str
    iss: str
    aud: str
    exp: int
    iat: int
    extra: dict[str, Any]


class JWTValidator:
    """Validates JWT tokens for LLM gateway access."""

    def __init__(
        self,
        secret: str | None = None,
        algorithm: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
    ) -> None:
        self._secret = secret or llm_gateway_settings.jwt_secret
        self._algorithm = algorithm or llm_gateway_settings.jwt_algorithm
        self._issuer = issuer or llm_gateway_settings.jwt_issuer
        self._audience = audience or llm_gateway_settings.jwt_audience

    def validate(self, token: str) -> TokenClaims:
        """Decode and validate a JWT token.

        Raises:
            InvalidTokenError: If token is expired, malformed, or fails verification.
        """
        if not self._secret:
            raise InvalidTokenError("JWT secret is not configured")

        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["sub", "exp", "iat", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise InvalidTokenError("Token has expired")
        except jwt.InvalidIssuerError:
            raise InvalidTokenError("Invalid token issuer")
        except jwt.InvalidAudienceError:
            raise InvalidTokenError("Invalid token audience")
        except jwt.DecodeError as e:
            raise InvalidTokenError(f"Token decode failed: {e}")
        except jwt.InvalidTokenError as e:
            raise InvalidTokenError(f"Invalid token: {e}")

        return TokenClaims(
            sub=payload["sub"],
            iss=payload["iss"],
            aud=payload["aud"],
            exp=payload["exp"],
            iat=payload["iat"],
            extra={
                k: v
                for k, v in payload.items()
                if k not in ("sub", "iss", "aud", "exp", "iat")
            },
        )

    def create_token(self, subject: str, ttl_seconds: int = 3600, **extra_claims: Any) -> str:
        """Create a signed JWT token (utility for testing and internal service-to-service)."""
        now = int(time.time())
        payload = {
            "sub": subject,
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "exp": now + ttl_seconds,
            **extra_claims,
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)


class InvalidTokenError(Exception):
    """Raised when JWT validation fails."""


def require_valid_token(validator: JWTValidator | None = None) -> Callable:
    """FastAPI dependency factory that extracts and validates the Bearer token.

    Usage:
        from fastapi import Depends
        from shared.llm_gateway import require_valid_token

        @router.post("/chat")
        async def chat(claims: TokenClaims = Depends(require_valid_token())):
            ...
    """
    _validator = validator or JWTValidator()

    async def _dependency(authorization: str = "") -> TokenClaims:
        from fastapi import HTTPException

        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")

        token = authorization.removeprefix("Bearer ").strip()
        try:
            return _validator.validate(token)
        except InvalidTokenError as e:
            logger.warning("JWT validation failed: %s", e)
            raise HTTPException(status_code=401, detail=str(e))

    return _dependency
