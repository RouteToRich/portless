"""Auth business logic — register, authenticate, issue tokens."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from shared.auth.security import create_access_token, hash_password, verify_password

from .repository import get_repository
from .schemas import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserPublic,
    UserRecord,
)

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailTakenError(Exception):
    """Raised when registering an email that already exists (409)."""


class InvalidEmailError(Exception):
    """Raised on malformed email (422)."""


class InvalidCredentialsError(Exception):
    """Raised on bad login (401)."""


def _new_user_id() -> str:
    return f"USR-{uuid4().hex[:12].upper()}"


def _token_for(user: UserRecord) -> TokenResponse:
    token = create_access_token(
        subject=user.user_id,
        extra_claims={"email": user.email, "name": user.name},
    )
    return TokenResponse(access_token=token, user=UserPublic.from_record(user))


async def register(req: RegisterRequest) -> TokenResponse:
    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise InvalidEmailError(f"{req.email!r} is not a valid email address")

    repo = get_repository()
    if await repo.get_by_email(email) is not None:
        raise EmailTakenError(f"An account with {email!r} already exists")

    user = UserRecord(
        user_id=_new_user_id(),
        email=email,
        name=req.name.strip(),
        password_hash=hash_password(req.password),
        created_at=datetime.now(timezone.utc),
    )
    await repo.create(user)
    logger.info("Auth: registered user %s (%s)", user.user_id, email)
    return _token_for(user)


async def login(req: LoginRequest) -> TokenResponse:
    repo = get_repository()
    user = await repo.get_by_email(req.email.strip().lower())
    # Always run verify to keep timing roughly constant whether or not the user exists.
    if user is None or not verify_password(req.password, user.password_hash):
        raise InvalidCredentialsError("Invalid email or password")
    logger.info("Auth: login OK for %s", user.user_id)
    return _token_for(user)


async def get_user(user_id: str) -> UserPublic | None:
    user = await get_repository().get_by_id(user_id)
    return UserPublic.from_record(user) if user else None
