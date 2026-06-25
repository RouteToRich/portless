"""HTTP routes for the Auth service."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from shared.auth.dependencies import AuthUser, get_current_user

from . import service
from .schemas import LoginRequest, RegisterRequest, TokenResponse, UserPublic
from .service import EmailTakenError, InvalidCredentialsError, InvalidEmailError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "auth"}


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest) -> TokenResponse:
    try:
        return await service.register(payload)
    except InvalidEmailError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except EmailTakenError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    try:
        return await service.login(payload)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.get("/me", response_model=UserPublic)
async def me(user: AuthUser = Depends(get_current_user)) -> UserPublic:
    """Return the current user's profile (validates the bearer token)."""
    found = await service.get_user(user.user_id)
    if found is None:
        # Token valid but user no longer exists — return token claims as fallback.
        return UserPublic(user_id=user.user_id, email=user.email or "", name=user.name or "")
    return found
