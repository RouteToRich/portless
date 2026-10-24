"""Shared authentication: JWT, password hashing, FastAPI deps + middleware."""
from .dependencies import AuthUser, get_current_user
from .middleware import AuthMiddleware
from .security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)

__all__ = [
    "AuthUser",
    "get_current_user",
    "AuthMiddleware",
    "create_access_token",
    "decode_token",
    "hash_password",
    "verify_password",
]
