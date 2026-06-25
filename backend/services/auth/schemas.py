"""Request/response models for the Auth service."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UserRecord(BaseModel):
    """Stored user document (password is hashed, never returned)."""

    user_id: str
    email: str
    name: str
    password_hash: str
    created_at: datetime


class UserPublic(BaseModel):
    """User info safe to return to clients / embed in tokens."""

    user_id: str
    email: str
    name: str
    created_at: datetime | None = None

    @classmethod
    def from_record(cls, r: UserRecord) -> "UserPublic":
        return cls(user_id=r.user_id, email=r.email, name=r.name, created_at=r.created_at)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=6, max_length=200)


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic
