"""Shared authentication: auth-service identities, FastAPI deps + middleware."""
from .dependencies import AuthUser, get_current_user
from .identity import Identity, IdentityError
from .middleware import AuthMiddleware

__all__ = [
    "AuthUser",
    "get_current_user",
    "AuthMiddleware",
    "Identity",
    "IdentityError",
]
